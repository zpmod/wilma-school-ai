"""Deterministic time-hint enrichment for parsed events.

The LLM schema stores only dates, not clock times. For dashboard rendering we
still want nearby time ranges (for example "klo 17-19" or "8.30-12.15") to
appear in notes when the body clearly contains them near the event's date cue.
"""
from __future__ import annotations

import re
from typing import Iterable

_TIME_RE = re.compile(
    r"(?:klo\s*)?"
    r"(\d{1,2}(?:[.:]\d{2})?)"
    r"\s*[-–]\s*"
    r"(\d{1,2}(?:[.:]\d{2})?)",
    re.IGNORECASE,
)


def _normalise_time_hint(text: str) -> str:
    m = _TIME_RE.search(text)
    if not m:
        return ""
    prefix = "klo " if "klo" in text.lower() else ""
    return f"{prefix}{m.group(1)}-{m.group(2)}"


def _find_nearby_time_hint(body: str, date_evidence: str | None) -> str | None:
    if not body:
        return None
    if date_evidence:
        idx = body.lower().find(date_evidence.lower())
        if idx >= 0:
            window = body[idx: idx + 120]
            hint = _normalise_time_hint(window)
            if hint:
                return hint
    # Fallback: first explicit time range in the body.
    hint = _normalise_time_hint(body)
    return hint or None


def enrich_events_with_time_hints(events: Iterable[dict], body: str) -> list[dict]:
    enriched: list[dict] = []
    for ev in events:
        out = dict(ev)
        hint = _find_nearby_time_hint(body, out.get("date_evidence"))
        notes = (out.get("notes") or "").strip()
        if hint and hint.lower() not in notes.lower():
            out["notes"] = f"{notes}\n\nAika: {hint}".strip() if notes else f"Aika: {hint}"
        enriched.append(out)
    return enriched
