"""The parse fail-safe: repeated failures must eventually stop being retried."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAYLOAD = {
    "message_id": "msg-fail",
    "sent": "2026-04-17 14:15",
    "sender": "Heidi",
    "subject": "always fails",
    "body": "This body can never be parsed.",
    "wait": True,
}


@pytest.fixture
def failing_client(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("WILMA_PARSER_DB", str(Path(tmp.name) / "parser.db"))

    for mod in list(sys.modules):
        if mod.startswith("app."):
            del sys.modules[mod]

    from fastapi.testclient import TestClient
    from app import main

    async def always_times_out(*, sent, sender, subject, body, today):
        # A timeout is not LLMError, which is exactly the case that used to
        # escape the handler and leave no record.
        raise TimeoutError("read timeout")

    monkeypatch.setattr(main, "extract_events", always_times_out)

    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c, main
    tmp.cleanup()


def test_failures_are_recorded_and_capped(failing_client):
    client, main = failing_client
    assert main.MAX_PARSE_ATTEMPTS == 3

    for expected in (1, 2, 3):
        r = client.post("/parse", json=PAYLOAD)
        assert r.status_code == 502, "a failed parse should surface as an error"
        listed = client.get("/failures").json()["failures"]
        assert listed[0]["attempts"] == expected

    # Fourth call must not reach the LLM at all.
    r = client.post("/parse", json=PAYLOAD)
    assert r.status_code == 200
    data = r.json()
    assert data["skipped"] is True
    assert data["attempts"] == 3
    assert "failed 3 times" in data["skip_reason"]


def test_skipped_message_is_reported_and_resettable(failing_client):
    client, _ = failing_client
    for _ in range(3):
        client.post("/parse", json=PAYLOAD)

    body = client.get("/failures").json()
    assert body["max_attempts"] == 3
    assert body["failures"][0]["skipped"] is True
    assert client.get("/healthz").json()["store"]["failing_messages"] == 1

    assert client.delete("/failures/msg-fail").json()["cleared"] == 1
    assert client.get("/failures").json()["failures"] == []

    # Cleared, so it is eligible again and fails afresh rather than being skipped.
    assert client.post("/parse", json=PAYLOAD).status_code == 502


def test_force_bypasses_the_skip(failing_client):
    client, _ = failing_client
    for _ in range(3):
        client.post("/parse", json=PAYLOAD)

    r = client.post("/parse", json={**PAYLOAD, "force": True})
    assert r.status_code == 502, "force must still attempt a skipped message"


def test_success_clears_previous_failures(failing_client, monkeypatch):
    client, main = failing_client
    client.post("/parse", json=PAYLOAD)
    assert client.get("/failures").json()["failures"][0]["attempts"] == 1

    async def succeeds(*, sent, sender, subject, body, today):
        return [], {"attempts": 1, "raw": "{}"}

    monkeypatch.setattr(main, "extract_events", succeeds)
    assert client.post("/parse", json=PAYLOAD).status_code == 200
    assert client.get("/failures").json()["failures"] == []
