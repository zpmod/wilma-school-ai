"""FastAPI app exposing /parse, /events, /healthz."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import store
from .correlation import normalize_title
from .dateguard import correct_events
from .filter import filter_events
from .llm import LLMError, extract_events, healthcheck
from .timehint import enrich_events_with_time_hints

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("wilma-parser")

app = FastAPI(title="wilma-parser", version="0.1.0")


class ParseRequest(BaseModel):
    message_id: str
    sent: str  # ISO date or 'YYYY-MM-DD HH:MM' from wilma sensor
    sender: str
    subject: str
    body: str
    child_id: str = Field(
        "default", description="Child identifier for multi-child support."
    )
    today: str | None = Field(
        None, description="Reference date (YYYY-MM-DD). Defaults to sent's date."
    )
    force: bool = False
    wait: bool = Field(
        True, description="If false, queue LLM in background and return immediately."
    )


class Event(BaseModel):
    title: str
    date_start: str
    date_end: str | None = None
    all_day: bool = True
    is_week_event: bool = False
    action_required: bool = False
    notes: str | None = None
    # P4.7.6 phase A — date provenance.
    # date_source: which kind of cue produced date_start.
    #   explicit_date              — body contains the date literally (trusted)
    #   relative_today             — "huomenna" / "tänään" / "ylihuomenna" (trusted)
    #   week_event                 — whole-week range from "ensi viikolla" etc. (trusted as range)
    #   weekday_only               — bare weekday with no anchor in scope (guess)
    #   inferred_weekday_in_anchor — weekday inside an "ensi viikolla" anchor (guess)
    # Defaults are tolerant for old cached events that lack the fields.
    date_source: str | None = None
    # date_evidence: verbatim substring from the body that justifies date_start.
    # Used by the dashboard to show the user what the LLM looked at, and by a
    # future post-processor (phase B) to re-locate the cue and resolve anchors.
    date_evidence: str | None = None


class ParseResponse(BaseModel):
    message_id: str
    cached: bool
    attempts: int
    events: list[Event]
    new_events: list[Event] = []
    updated_events: list[Event] = []
    dropped: list[dict[str, Any]] = []
    queued: bool = False


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _today_for(req: ParseRequest) -> str:
    if req.today:
        return req.today
    # 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'
    return req.sent[:10]


async def _process_parse_background(req: ParseRequest, body_sha: str) -> None:
    """Background task: run LLM extraction, post-process, store results."""
    today = _today_for(req)
    try:
        raw_events, debug = await extract_events(
            sent=req.sent, sender=req.sender, subject=req.subject, body=req.body, today=today
        )
    except LLMError as e:
        log.error("LLM extraction failed for %s: %s", req.message_id, e)
        return

    kept_pre, dropped = filter_events(raw_events)
    kept_pre = correct_events(kept_pre, body=req.body, subject=req.subject, sent=req.sent)
    kept_pre = enrich_events_with_time_hints(kept_pre, body=req.body)
    kept, newly_inserted = store.store_parse(
        body_sha256=body_sha,
        message_id=req.message_id,
        raw_response=debug["raw"],
        attempts=debug["attempts"],
        events=kept_pre,
        dropped_count=len(dropped),
        source_sent_at=req.sent,
        child_id=req.child_id,
    )

    # Cross-message correlation.
    for ev in newly_inserted:
        title_key = normalize_title(ev.get("title", ""))
        match = store.find_correlated(title_key, exclude_message_id=req.message_id, child_id=req.child_id)
        if match:
            updated = store.update_event_correlation(
                event_id=match["id"],
                new_data=ev,
                source_message_id=req.message_id,
            )
            store.delete_event_by_id(ev["id"])
            log.info(
                "correlation message_id=%s title=%r → event_id=%d (rev %s)",
                req.message_id, ev.get("title"), match["id"],
                updated.get("revision_count"),
            )

    log.info(
        "background parse done message_id=%s attempts=%d kept=%d new=%d dropped=%d",
        req.message_id, debug["attempts"], len(kept), len(newly_inserted), len(dropped),
    )


async def _process_parse_sync(req: ParseRequest, body_sha: str) -> ParseResponse:
    """Synchronous parse: wait for LLM and return full results."""
    today = _today_for(req)
    try:
        raw_events, debug = await extract_events(
            sent=req.sent, sender=req.sender, subject=req.subject, body=req.body, today=today
        )
    except LLMError as e:
        log.error("LLM extraction failed for %s: %s", req.message_id, e)
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    kept_pre, dropped = filter_events(raw_events)
    kept_pre = correct_events(kept_pre, body=req.body, subject=req.subject, sent=req.sent)
    kept_pre = enrich_events_with_time_hints(kept_pre, body=req.body)
    kept, newly_inserted = store.store_parse(
        body_sha256=body_sha,
        message_id=req.message_id,
        raw_response=debug["raw"],
        attempts=debug["attempts"],
        events=kept_pre,
        dropped_count=len(dropped),
        source_sent_at=req.sent,
        child_id=req.child_id,
    )

    # Cross-message correlation.
    correlated_updates: list[dict[str, Any]] = []
    still_new: list[dict[str, Any]] = []
    for ev in newly_inserted:
        title_key = normalize_title(ev.get("title", ""))
        match = store.find_correlated(title_key, exclude_message_id=req.message_id, child_id=req.child_id)
        if match:
            updated = store.update_event_correlation(
                event_id=match["id"],
                new_data=ev,
                source_message_id=req.message_id,
            )
            store.delete_event_by_id(ev["id"])
            correlated_updates.append(updated)
            log.info(
                "correlation message_id=%s title=%r → event_id=%d (rev %s)",
                req.message_id, ev.get("title"), match["id"],
                updated.get("revision_count"),
            )
        else:
            still_new.append(ev)

    log.info(
        "parsed message_id=%s attempts=%d kept=%d new=%d updated=%d dropped=%d",
        req.message_id, debug["attempts"], len(kept), len(still_new),
        len(correlated_updates), len(dropped),
    )
    return ParseResponse(
        message_id=req.message_id,
        cached=False,
        attempts=debug["attempts"],
        events=[Event(**e) for e in kept],
        new_events=[Event(**e) for e in still_new],
        updated_events=[Event(**e) for e in correlated_updates],
        dropped=dropped,
    )


@app.post("/parse", response_model=ParseResponse)
async def parse(req: ParseRequest, background_tasks: BackgroundTasks) -> ParseResponse:
    body_sha = _hash(req.body)
    cached = None if req.force else store.get_cached(body_sha, child_id=req.child_id)
    if cached:
        evs = [Event(**e) for e in store.list_events_for_message(req.message_id, child_id=req.child_id)]
        return ParseResponse(
            message_id=req.message_id,
            cached=True,
            attempts=cached["attempts"],
            events=evs,
            new_events=[],
        )

    if not req.wait:
        # Fire-and-forget: queue LLM processing in background so the HTTP
        # response returns immediately.  HA picks up results via the
        # GET /events/unsynced polling endpoint.
        background_tasks.add_task(_process_parse_background, req, body_sha)
        return ParseResponse(
            message_id=req.message_id,
            cached=False,
            attempts=0,
            events=[],
            new_events=[],
            queued=True,
        )

    # Synchronous mode (default): wait for LLM and return results inline.
    return await _process_parse_sync(req, body_sha)


class EventRevision(BaseModel):
    revised_at: str
    source_message_id: str
    change_type: str
    prev_date_start: str | None = None
    prev_date_end: str | None = None
    prev_notes: str | None = None
    change_summary: str | None = None


@app.get("/events")
def events(
    since: str | None = Query(None, description="YYYY-MM-DD lower bound"),
    child_id: str | None = Query(None, description="Filter by child"),
) -> dict[str, Any]:
    rows = store.list_events(since=since, child_id=child_id)
    # SQLite stores booleans as ints; coerce for JSON consumers.
    for r in rows:
        for k in ("all_day", "is_week_event", "action_required"):
            r[k] = bool(r[k])
    return {"events": rows, "stats": store.stats()}


@app.get("/events/unsynced")
def events_unsynced(
    child_id: str | None = Query(None, description="Filter by child"),
) -> dict[str, Any]:
    """Return events not yet synced to HA calendar. Used by polling automation."""
    rows = store.list_unsynced_events(child_id=child_id)
    for r in rows:
        for k in ("all_day", "is_week_event", "action_required"):
            r[k] = bool(r[k])
    return {"events": rows}


class MarkSyncedRequest(BaseModel):
    event_ids: list[int]


@app.post("/events/mark-synced")
def events_mark_synced(req: MarkSyncedRequest) -> dict[str, Any]:
    """Mark events as synced to calendar after HA creates entries."""
    store.mark_events_synced(req.event_ids)
    return {"marked": len(req.event_ids)}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    h = await healthcheck()
    return {**h, "store": store.stats()}


class DenylistRequest(BaseModel):
    message_id: str
    title: str
    date_start: str
    reason: str | None = None
    child_id: str = "default"


@app.post("/denylist")
def denylist_add(req: DenylistRequest) -> dict[str, Any]:
    """Mark an extraction as wrong. Purges the event and prevents re-extraction."""
    store.add_denylist(req.message_id, req.title, req.date_start, req.reason, child_id=req.child_id)
    log.info("denylist add message_id=%s child=%s title=%r date=%s", req.message_id, req.child_id, req.title, req.date_start)
    return {"ok": True, "stats": store.stats()}


@app.get("/denylist")
def denylist_list() -> dict[str, Any]:
    return {"entries": store.list_denylist()}
