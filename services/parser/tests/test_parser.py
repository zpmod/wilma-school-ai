"""Smoke tests for the parser pipeline (filter, dateguard, timehint).

Uses synthetic Finnish school messages — no real PII.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.filter import filter_events, is_subject_line
from app.dateguard import correct_events
from app.timehint import enrich_events_with_time_hints
from app.correlation import normalize_title, classify_change


FIXTURES = Path(__file__).parent / "fixtures"


def load_messages() -> list[dict]:
    return json.loads((FIXTURES / "synthetic_messages.json").read_text())


# ── Filter tests ─────────────────────────────────────────────────────────────


def test_filter_drops_subject_lines():
    """Subject-line patterns from viikkoviesti should be filtered out."""
    events = [
        {"title": "Math: Ch 35-37", "date_start": "2026-05-08", "notes": None},
        {"title": "Unicef-kävely", "date_start": "2026-05-15", "notes": "klo 8.30-12.15"},
        {"title": "tietotekstin kirjoittaminen", "date_start": "2026-05-09", "notes": "Finnish class"},
    ]
    kept, dropped = filter_events(events)
    assert len(kept) == 1
    assert kept[0]["title"] == "Unicef-kävely"
    assert len(dropped) == 2


def test_filter_deduplicates():
    """Duplicate events within same response are dropped."""
    events = [
        {"title": "Lukupiknik", "date_start": "2026-04-24", "notes": None},
        {"title": "Lukupiknik", "date_start": "2026-04-24", "notes": "duplicate"},
    ]
    kept, dropped = filter_events(events)
    assert len(kept) == 1
    assert any(d["_drop_reason"] == "duplicate" for d in dropped)


def test_is_subject_line():
    assert is_subject_line("Math: Ch 31-34", None) is True
    assert is_subject_line("PE: indoor sports", None) is True
    assert is_subject_line("Unicef-kävely", None) is False
    assert is_subject_line("Vanhempainilta", None) is False


# ── Dateguard tests ──────────────────────────────────────────────────────────


def test_dateguard_corrects_weekday_in_anchor():
    """Weekday after 'ensi viikolla' should snap to next week."""
    body = "Ensi viikolla on koulussamme lukuviikko. Perjantaina on lukupiknik."
    events = [
        {
            "title": "Lukupiknik",
            "date_start": "2026-04-17",  # Wrong: send date (Friday)
            "date_end": None,
            "date_source": "inferred_weekday_in_anchor",
            "date_evidence": "Perjantaina on lukupiknik",
        }
    ]
    corrected = correct_events(events, body=body, subject="Viikkoviesti vko 17", sent="2026-04-17")
    assert corrected[0]["date_start"] == "2026-04-24"  # Correct: next week Friday
    assert corrected[0]["date_source"] == "corrected_by_dateguard"


def test_dateguard_leaves_explicit_dates_alone():
    """Explicit dates should not be touched."""
    body = "Unicef-kävely on 15.5."
    events = [
        {
            "title": "Unicef-kävely",
            "date_start": "2026-05-15",
            "date_end": None,
            "date_source": "explicit_date",
            "date_evidence": "15.5. Unicef-kävely",
        }
    ]
    corrected = correct_events(events, body=body, subject="Unicef-kävely", sent="2026-04-21")
    assert corrected[0]["date_start"] == "2026-05-15"
    assert corrected[0]["date_source"] == "explicit_date"


# ── Timehint tests ───────────────────────────────────────────────────────────


def test_timehint_extracts_klo_range():
    """Time hints near evidence should be added to notes."""
    body = "Unicef-kävely klo 8.30-12.15 koulun pihalla. Paikalla 15.5."
    events = [
        {
            "title": "Unicef-kävely",
            "date_start": "2026-05-15",
            "notes": None,
            "date_evidence": "Paikalla 15.5.",
        }
    ]
    enriched = enrich_events_with_time_hints(events, body=body)
    assert "8.30-12.15" in (enriched[0].get("notes") or "")


def test_timehint_no_duplicate():
    """If time hint already in notes, don't add again."""
    body = "Vanhempainilta klo 17-19 juhlasalissa."
    events = [
        {
            "title": "Vanhempainilta",
            "date_start": "2026-05-14",
            "notes": "Aika: klo 17-19",
            "date_evidence": "Vanhempainilta klo 17-19",
        }
    ]
    enriched = enrich_events_with_time_hints(events, body=body)
    assert enriched[0]["notes"].count("17-19") == 1


# ── Correlation tests ────────────────────────────────────────────────────────


def test_normalize_title():
    assert normalize_title("Unicef-kävely") == normalize_title("Unicefkävely")
    assert normalize_title("Pihajuhla") == normalize_title("pihajuhla")
    assert normalize_title("Lukupiknik") != normalize_title("Lukuviikko")


def test_classify_change_correction():
    old = {"date_start": "2026-05-15", "date_end": None, "notes": ""}
    new = {"date_start": "2026-05-16", "date_end": None, "notes": ""}
    assert classify_change(old, new) == "correction"


def test_classify_change_enrichment():
    old = {"date_start": "2026-05-15", "date_end": None, "notes": ""}
    new = {"date_start": "2026-05-15", "date_end": None, "notes": "Juhlasalissa klo 17-19"}
    assert classify_change(old, new) == "enrichment"
