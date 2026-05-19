"""Phase B — deterministic date corrector for low-confidence LLM extractions.

The LLM (Llama-Poro 8B Q4_K_M) reliably labels dates with `date_source` after
phase A, but cannot reliably *resolve* weekday-in-anchor cases like:

  Body, sent Fri 2026-04-17:
    "Ensi viikolla on koulussamme lukuviikko. ... Perjantaina on lukupiknik."

The model often returns 2026-04-17 (the send date) instead of 2026-04-24
(the anchored week's friday). This module fixes such cases deterministically.

Strategy
========
For each event with `date_source` in {`weekday_only`, `inferred_weekday_in_anchor`}:

1. Locate `date_evidence` substring in the body.
2. Walk backward (or look at body start for viikkoviesti subjects) for the
   nearest week-anchor phrase:
     - "ensi viikolla" / "ensi viikon" / "next week" → anchor = NEXT week
     - "tällä viikolla" / "this week"               → anchor = SEND week
3. Extract the weekday name from `date_evidence` (e.g. "Perjantaina").
4. Compute the matching weekday inside the anchored week.
5. If we shifted, set `date_source = "corrected_by_dateguard"` and record
   the original LLM date in `_dateguard_original` for auditing.

Viikkoviesti fallback
---------------------
If subject contains "viikkoviesti" / "viikkokirje" / "weekly" AND we found
no explicit anchor in body, default the anchor to NEXT week. These messages
are typically sent Thu/Fri describing the upcoming week.

Hard limits — what this guard does NOT do
-----------------------------------------
- Never touch `explicit_date`, `relative_today`, or `week_event` events.
- Never touch events whose `date_evidence` is NOT found in the body (those
  are already untrustworthy; phase A's verbatim rule should keep this rare).
- Never extend `date_end` (we only correct the start day).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


# Closed enum from the prompt. Match SYSTEM_PROMPT in app/prompt.py.
TRUSTED_SOURCES = frozenset({"explicit_date", "relative_today", "week_event"})
GUESS_SOURCES = frozenset({"weekday_only", "inferred_weekday_in_anchor"})
CORRECTED = "corrected_by_dateguard"


# Finnish weekday names (basic + adessive, lowercase) → ISO weekday (Mon=0..Sun=6).
_WEEKDAYS_FI = {
    "maanantai": 0, "maanantaina": 0,
    "tiistai":   1, "tiistaina":   1,
    "keskiviikko": 2, "keskiviikkona": 2,
    "torstai":   3, "torstaina":   3,
    "perjantai": 4, "perjantaina": 4,
    "lauantai":  5, "lauantaina":  5,
    "sunnuntai": 6, "sunnuntaina": 6,
}
_WEEKDAYS_EN = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_ALL_WEEKDAYS = {**_WEEKDAYS_FI, **_WEEKDAYS_EN}

_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(sorted(_ALL_WEEKDAYS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Anchor phrases. Order matters within a regex alternation only because we
# evaluate matches independently below.
_NEXT_WEEK_RE = re.compile(
    r"\b(ensi\s+viikolla|ensi\s+viikon|next\s+week)\b",
    re.IGNORECASE,
)
_THIS_WEEK_RE = re.compile(
    r"\b(t[äa]ll[äa]\s+viikolla|this\s+week)\b",
    re.IGNORECASE,
)

_VIIKKOVIESTI_SUBJECT_RE = re.compile(
    r"\b(viikkoviesti|viikkokirje|weekly)\b",
    re.IGNORECASE,
)


def _parse_sent_date(sent: str) -> date | None:
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM[:SS]' or ISO with T."""
    if not sent:
        return None
    s = sent.strip().replace("T", " ")
    try:
        return datetime.fromisoformat(s[:19]).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            log.debug("dateguard: cannot parse sent=%r", sent)
            return None


def _monday_of(d: date) -> date:
    """Monday of the ISO week containing d."""
    return d - timedelta(days=d.weekday())


def _find_anchor_kind(body: str, evidence_pos: int) -> str | None:
    """Return 'next' | 'this' | None. Searches body[:evidence_pos] (the prefix
    before the evidence cue) for the nearest anchor; the closest wins."""
    prefix = body[:evidence_pos] if evidence_pos > 0 else ""
    next_m = list(_NEXT_WEEK_RE.finditer(prefix))
    this_m = list(_THIS_WEEK_RE.finditer(prefix))
    last_next = next_m[-1].start() if next_m else -1
    last_this = this_m[-1].start() if this_m else -1
    if last_next < 0 and last_this < 0:
        return None
    return "next" if last_next > last_this else "this"


def _weekday_from_evidence(evidence: str) -> int | None:
    """Return ISO weekday number (Mon=0..Sun=6) or None."""
    if not evidence:
        return None
    m = _WEEKDAY_RE.search(evidence)
    if not m:
        return None
    return _ALL_WEEKDAYS[m.group(1).lower()]


def correct_event(
    ev: dict[str, Any],
    *,
    body: str,
    subject: str,
    sent: str,
) -> dict[str, Any]:
    """Return a possibly-corrected copy of ev. Pure function, never mutates."""
    src = ev.get("date_source")
    if src not in GUESS_SOURCES:
        return ev

    evidence = ev.get("date_evidence") or ""
    if not evidence:
        return ev

    # Verbatim rule: evidence MUST appear in body. Bail otherwise.
    pos = body.find(evidence)
    if pos < 0:
        log.debug("dateguard: evidence not in body, skip: %r", evidence[:60])
        return ev

    weekday = _weekday_from_evidence(evidence)
    if weekday is None:
        # No weekday inside the cue — nothing to snap to.
        return ev

    sent_date = _parse_sent_date(sent)
    if sent_date is None:
        return ev

    anchor_kind = _find_anchor_kind(body, pos)
    if anchor_kind is None and _VIIKKOVIESTI_SUBJECT_RE.search(subject or ""):
        # Fallback: viikkoviesti messages default to NEXT week.
        anchor_kind = "next"

    if anchor_kind is None:
        # Bare weekday with no anchor at all → leave as-is. The LLM's guess
        # (nearest matching weekday) is already the best we can do.
        return ev

    if anchor_kind == "next":
        anchor_monday = _monday_of(sent_date) + timedelta(days=7)
    else:  # 'this'
        anchor_monday = _monday_of(sent_date)
    target = anchor_monday + timedelta(days=weekday)
    target_iso = target.isoformat()

    if target_iso == ev.get("date_start"):
        # Already correct — no change.
        return ev

    corrected = dict(ev)
    corrected["date_source"] = CORRECTED
    corrected["_dateguard_original"] = ev.get("date_start")
    corrected["_dateguard_anchor"] = anchor_kind
    corrected["date_start"] = target_iso
    # If the LLM also set a same-day date_end, keep it consistent.
    if ev.get("date_end") in (None, "", ev.get("date_start")):
        corrected["date_end"] = None
    log.info(
        "dateguard: %r %s -> %s (anchor=%s, evidence=%r)",
        ev.get("title"), ev.get("date_start"), target_iso, anchor_kind, evidence[:40],
    )
    return corrected


def correct_events(
    events: list[dict[str, Any]],
    *,
    body: str,
    subject: str,
    sent: str,
) -> list[dict[str, Any]]:
    """Run correct_event over a list. Returns a new list."""
    return [correct_event(e, body=body, subject=subject, sent=sent) for e in events]
