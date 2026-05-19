"""Post-LLM filter: drop FPs that the prompt couldn't fully suppress.

P4.7.1 bench showed 3/3 false positives were viikkoviesti subject-line leaks
("Tietotekstin kirjoittaminen", "Lukemisen tunti", duplicate "Kirjastovierailu").
Catch them with regex on title + notes.
"""
from __future__ import annotations

import re
from typing import Iterable

# Subject-line prefixes (English + Finnish) commonly used in viikkoviesti.
_SUBJECT_PREFIX = re.compile(
    r"^(math|finnish|science|social\s+studies|english|pe|"
    r"matematiikka|suomi|englanti|liikunta|ympäristöoppi|yhteiskuntaoppi|musiikki)\s*:",
    re.IGNORECASE,
)

# Patterns that strongly indicate "weekly subject plan", not a calendar event.
_SUBJECT_PATTERNS = [
    re.compile(r"\bch\s*\d+", re.IGNORECASE),
    re.compile(r"\bchapter\s*\d+", re.IGNORECASE),
    re.compile(r"\bkappale(?:et)?\s*\d+", re.IGNORECASE),
    re.compile(r"\bs\.\s*\d+", re.IGNORECASE),
    re.compile(r"\breading\s+lesson\b", re.IGNORECASE),
    re.compile(r"\btietotekstin\s+kirjoittaminen\b", re.IGNORECASE),
    re.compile(r"\blähteiden\s+merkitseminen\b", re.IGNORECASE),
    re.compile(r"\bindoor\s+sports?\b", re.IGNORECASE),
    re.compile(r"\bvälkky\b", re.IGNORECASE),  # textbook name
]


def is_subject_line(title: str, notes: str | None) -> bool:
    """Return True if this looks like a weekly subject-plan line, not a real event."""
    if not title:
        return False
    if _SUBJECT_PREFIX.match(title.strip()):
        return True
    haystack = f"{title}  {notes or ''}"
    matches = sum(1 for p in _SUBJECT_PATTERNS if p.search(haystack))
    # Title hit alone is enough; in notes only, require 2+ to be safe.
    if any(p.search(title) for p in _SUBJECT_PATTERNS):
        return True
    return matches >= 2


def filter_events(events: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Split events into (kept, dropped). Caller can log dropped for audit."""
    kept: list[dict] = []
    dropped: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for ev in events:
        title = (ev.get("title") or "").strip()
        notes = ev.get("notes")
        if is_subject_line(title, notes):
            dropped.append({**ev, "_drop_reason": "subject_line"})
            continue
        # Dedup within a single response (model sometimes emits twice).
        key = (title.lower(), str(ev.get("date_start") or ""))
        if key in seen_keys:
            dropped.append({**ev, "_drop_reason": "duplicate"})
            continue
        seen_keys.add(key)
        kept.append(ev)
    return kept, dropped
