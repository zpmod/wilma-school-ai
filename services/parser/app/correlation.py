"""Event correlation helpers.

Detects when a newly-parsed event refers to the same real-world event as
one already in the store, and classifies the relationship:

  correction  — date_start or date_end changed (an earlier message had the
                wrong time; the new message corrects it)
  enrichment  — dates match but notes were added or expanded (the new
                message adds detail about an event that was first mentioned
                elsewhere)

Title matching is done on a *normalised key* that is tolerant of spelling
variants that appear in practice:
  "Unicef-kävely"  → "unicefkävely"
  "Unicefkävely"   → "unicefkävely"   → same key, match!

  "Pihajuhla"      → "pihajuhla"
  "Pihajuhla"      → "pihajuhla"      → match (different times = correction)
"""
from __future__ import annotations

import re
import unicodedata

_TIME_RE = re.compile(
    r"(?:klo\s*)?(\d{1,2}(?:[.:]\d{2})?)\s*[-–]\s*(\d{1,2}(?:[.:]\d{2})?)",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """Return a search key: lowercase, hyphens/spaces stripped, NFC normalised.

    Intentionally keeps Finnish characters (ä, ö) so "kävely" ≠ "kavely".
    The only collapsing done is hyphen/space removal and case folding.
    """
    t = (title or "").lower().strip()
    # Remove hyphens and whitespace (covers "Unicef-kävely" == "Unicefkävely")
    t = t.replace("-", "").replace(" ", "")
    # NFC normalisation keeps pre-composed Finnish characters stable
    return unicodedata.normalize("NFC", t)


def _extract_time_hint(text: str | None) -> str | None:
    if not text:
        return None
    m = _TIME_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def classify_change(existing: dict, new_event: dict) -> str:
    """Return 'correction' if dates or time changed, else 'enrichment'."""
    date_changed = (
        existing.get("date_start") != new_event.get("date_start")
        or existing.get("date_end") != new_event.get("date_end")
    )
    if date_changed:
        return "correction"
    old_time = _extract_time_hint(existing.get("notes"))
    new_time = _extract_time_hint(new_event.get("notes"))
    if old_time and new_time and old_time != new_time:
        return "correction"
    old_notes = (existing.get("notes") or "").strip()
    new_notes = (new_event.get("notes") or "").strip()
    if new_notes and new_notes != old_notes:
        return "enrichment"
    return "enrichment"  # re-extraction with no real diff — treat as enrichment


def build_change_summary(existing: dict, new_event: dict) -> str:
    """One-line human-readable diff summary."""
    parts: list[str] = []
    if existing.get("date_start") != new_event.get("date_start"):
        parts.append(
            f"date_start: {existing.get('date_start')} → {new_event.get('date_start')}"
        )
    if existing.get("date_end") != new_event.get("date_end"):
        parts.append(
            f"date_end: {existing.get('date_end')} → {new_event.get('date_end')}"
        )
    old_time = _extract_time_hint(existing.get("notes"))
    new_time = _extract_time_hint(new_event.get("notes"))
    if old_time and new_time and old_time != new_time:
        parts.append(f"time: {old_time} → {new_time}")
    old_notes = (existing.get("notes") or "").strip()
    new_notes = (new_event.get("notes") or "").strip()
    if new_notes and new_notes != old_notes and not (old_time and new_time and old_time != new_time):
        parts.append("notes updated")
    return "; ".join(parts) if parts else "re-extraction (no change)"
