"""SQLite cache + event store. Idempotent on re-parse."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .correlation import normalize_title, classify_change, build_change_summary

DB_PATH = Path(os.environ.get("WILMA_PARSER_DB", "data/parser.db"))

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


_conn_singleton: sqlite3.Connection | None = None
_conn_path: Path | None = None


def get_conn() -> sqlite3.Connection:
    global _conn_singleton, _conn_path
    current = Path(os.environ.get("WILMA_PARSER_DB", "data/parser.db"))
    if _conn_singleton is None or _conn_path != current:
        if _conn_singleton is not None:
            try:
                _conn_singleton.close()
            except Exception:
                pass
        # Re-resolve module-level DB_PATH so tests see the new path.
        global DB_PATH
        DB_PATH = current
        _conn_singleton = _conn()
        _conn_path = current
        _init(_conn_singleton)
    return _conn_singleton


def _init(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS message_cache (
            body_sha256   TEXT PRIMARY KEY,
            message_id    TEXT NOT NULL,
            parsed_at     TEXT NOT NULL,
            attempts      INTEGER NOT NULL,
            raw_response  TEXT NOT NULL,
            event_count   INTEGER NOT NULL,
            dropped_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      TEXT NOT NULL,
            body_sha256     TEXT NOT NULL,
            title           TEXT NOT NULL,
            date_start      TEXT NOT NULL,
            date_end        TEXT,
            all_day         INTEGER NOT NULL DEFAULT 1,
            is_week_event   INTEGER NOT NULL DEFAULT 0,
            action_required INTEGER NOT NULL DEFAULT 0,
            notes           TEXT,
            source_sent_at  TEXT,
            created_at      TEXT NOT NULL,
            synced_to_cal   INTEGER NOT NULL DEFAULT 0,
            UNIQUE(message_id, title, date_start)
        );

        CREATE INDEX IF NOT EXISTS events_date_start ON events(date_start);
        CREATE INDEX IF NOT EXISTS events_message_id ON events(message_id);

        -- Denylist: events the user flagged as wrong. Keyed on normalized
        -- (message_id, title_lower, date_start). Any re-parse that matches
        -- these keys is filtered out before insert.
        CREATE TABLE IF NOT EXISTS denylist (
            message_id TEXT NOT NULL,
            title_key  TEXT NOT NULL,
            date_start TEXT NOT NULL,
            added_at   TEXT NOT NULL,
            reason     TEXT,
            PRIMARY KEY (message_id, title_key, date_start)
        );

        -- Revision history: each time a cross-message correlation updates an
        -- existing event (correction or enrichment), the previous values are
        -- snapshot here so the UI can show the full edit trail.
        CREATE TABLE IF NOT EXISTS event_revisions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id          INTEGER NOT NULL REFERENCES events(id),
            revised_at        TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            change_type       TEXT NOT NULL,
            prev_date_start   TEXT,
            prev_date_end     TEXT,
            prev_notes        TEXT,
            change_summary    TEXT
        );

        CREATE INDEX IF NOT EXISTS revisions_event_id ON event_revisions(event_id);
        """
    )
    # Idempotent additive migrations for pre-existing DBs.
    try:
        c.execute("ALTER TABLE events ADD COLUMN synced_to_cal INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    # P4.7.6 phase A — date provenance columns.
    try:
        c.execute("ALTER TABLE events ADD COLUMN date_source TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE events ADD COLUMN date_evidence TEXT")
    except sqlite3.OperationalError:
        pass
    # Correlation / revision tracking columns.
    for col_def in (
        "title_key TEXT",
        "revision_count INTEGER NOT NULL DEFAULT 0",
        "last_updated_by TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE events ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    # Back-fill title_key for any existing rows that pre-date this migration.
    c.execute(
        "UPDATE events SET title_key = LOWER(REPLACE(REPLACE(title, '-', ''), ' ', ''))"
        " WHERE title_key IS NULL"
    )


@contextmanager
def tx():
    c = get_conn()
    with _lock:
        try:
            c.execute("BEGIN")
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_cached(body_sha256: str) -> dict[str, Any] | None:
    c = get_conn()
    row = c.execute(
        "SELECT * FROM message_cache WHERE body_sha256 = ?", (body_sha256,)
    ).fetchone()
    return dict(row) if row else None


def store_parse(
    *,
    body_sha256: str,
    message_id: str,
    raw_response: str,
    attempts: int,
    events: Iterable[dict[str, Any]],
    dropped_count: int,
    source_sent_at: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persist parse result. Returns (kept_events, newly_inserted_events).

    kept_events excludes denylisted entries. newly_inserted_events are kept
    events that did not previously exist in the DB — consumers (e.g. HA
    automation) use this list to drive calendar.create_event without
    producing duplicates on re-parse.
    """
    events = list(events)
    kept: list[dict[str, Any]] = []
    newly_inserted: list[dict[str, Any]] = []
    with tx() as c:
        c.execute(
            """
            INSERT INTO message_cache
              (body_sha256, message_id, parsed_at, attempts, raw_response,
               event_count, dropped_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(body_sha256) DO UPDATE SET
              parsed_at = excluded.parsed_at,
              attempts = excluded.attempts,
              raw_response = excluded.raw_response,
              event_count = excluded.event_count,
              dropped_count = excluded.dropped_count
            """,
            (
                body_sha256,
                message_id,
                now_iso(),
                attempts,
                raw_response,
                len(events),
                dropped_count,
            ),
        )
        for ev in events:
            title = (ev.get("title") or "").strip()
            date_start = ev.get("date_start")
            if not title or not date_start:
                continue
            # Denylist filter.
            denied = c.execute(
                "SELECT 1 FROM denylist WHERE message_id = ? AND title_key = ? AND date_start = ?",
                (message_id, title.lower(), date_start),
            ).fetchone()
            if denied:
                continue
            existing = c.execute(
                "SELECT 1 FROM events WHERE message_id = ? AND title = ? AND date_start = ?",
                (message_id, title, date_start),
            ).fetchone()
            tkey = normalize_title(title)
            c.execute(
                """
                INSERT INTO events
                  (message_id, body_sha256, title, title_key, date_start, date_end,
                   all_day, is_week_event, action_required, notes,
                   source_sent_at, created_at, date_source, date_evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, title, date_start) DO UPDATE SET
                  date_end = excluded.date_end,
                  all_day = excluded.all_day,
                  is_week_event = excluded.is_week_event,
                  action_required = excluded.action_required,
                  notes = excluded.notes,
                  body_sha256 = excluded.body_sha256,
                  date_source = excluded.date_source,
                  date_evidence = excluded.date_evidence,
                  title_key = excluded.title_key
                """,
                (
                    message_id,
                    body_sha256,
                    title,
                    tkey,
                    date_start,
                    ev.get("date_end"),
                    1 if ev.get("all_day", True) else 0,
                    1 if ev.get("is_week_event") else 0,
                    1 if ev.get("action_required") else 0,
                    ev.get("notes"),
                    source_sent_at,
                    now_iso(),
                    ev.get("date_source"),
                    ev.get("date_evidence"),
                ),
            )
            stored_row = c.execute(
                "SELECT * FROM events WHERE message_id = ? AND title = ? AND date_start = ?",
                (message_id, title, date_start),
            ).fetchone()
            stored = dict(stored_row) if stored_row else dict(ev)
            kept.append(stored)
            if not existing:
                newly_inserted.append(stored)
    return kept, newly_inserted


def find_correlated(title_key: str, exclude_message_id: str) -> dict[str, Any] | None:
    """Return the most-recent canonical event whose title_key matches,
    from a *different* source message.  Returns None if no match."""
    c = get_conn()
    row = c.execute(
        """
        SELECT * FROM events
        WHERE title_key = ? AND message_id != ?
        ORDER BY revision_count DESC, id DESC
        LIMIT 1
        """,
        (title_key, exclude_message_id),
    ).fetchone()
    return dict(row) if row else None


def _merge_notes(old_notes: str, new_notes: str) -> str:
    def _split_time_block(text: str) -> tuple[list[str], str | None]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        plain: list[str] = []
        latest_time: str | None = None
        for line in lines:
            if line.lower().startswith("aika:"):
                latest_time = line
            else:
                plain.append(line)
        return plain, latest_time

    old_notes = old_notes.strip()
    new_notes = new_notes.strip()
    if not old_notes:
        return new_notes
    if not new_notes:
        return old_notes
    if old_notes.lower() == new_notes.lower():
        return old_notes

    old_plain, old_time = _split_time_block(old_notes)
    new_plain, new_time = _split_time_block(new_notes)

    merged_plain: list[str] = []
    for line in old_plain + new_plain:
        if not any(line.lower() == seen.lower() for seen in merged_plain):
            merged_plain.append(line)

    merged_parts = ["\n\n".join(merged_plain)] if merged_plain else []
    if new_time or old_time:
        merged_parts.append(new_time or old_time or "")
    return "\n\n".join(part for part in merged_parts if part)


def update_event_correlation(
    event_id: int,
    new_data: dict[str, Any],
    source_message_id: str,
) -> dict[str, Any]:
    """Update a canonical event with data from a correlating message.

    Records the previous state in event_revisions so the UI can show history.
    Returns the updated event row.
    """
    with tx() as c:
        old_row = c.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if old_row is None:
            raise ValueError(f"event id {event_id} not found")
        old = dict(old_row)

        change_type = classify_change(old, new_data)
        summary = build_change_summary(old, new_data)

        c.execute(
            """
            INSERT INTO event_revisions
              (event_id, revised_at, source_message_id, change_type,
               prev_date_start, prev_date_end, prev_notes, change_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                now_iso(),
                source_message_id,
                change_type,
                old.get("date_start"),
                old.get("date_end"),
                old.get("notes"),
                summary,
            ),
        )

        # Pick the better value for each field:
        # - For dates: trust the new data when it differs (it's more specific)
        # - For notes: preserve both when they carry distinct details.
        new_notes = (new_data.get("notes") or "").strip()
        old_notes = (old.get("notes") or "").strip()
        merged_notes = _merge_notes(old_notes, new_notes)

        c.execute(
            """
            UPDATE events SET
              date_start      = ?,
              date_end        = ?,
              all_day         = ?,
              notes           = ?,
              date_source     = ?,
              date_evidence   = ?,
              revision_count  = revision_count + 1,
              last_updated_by = ?
            WHERE id = ?
            """,
            (
                new_data.get("date_start") or old["date_start"],
                new_data.get("date_end", old.get("date_end")),
                1 if new_data.get("all_day", bool(old.get("all_day", 1))) else 0,
                merged_notes or None,
                new_data.get("date_source") or old.get("date_source"),
                new_data.get("date_evidence") or old.get("date_evidence"),
                source_message_id,
                event_id,
            ),
        )

        updated = c.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return dict(updated)


def delete_event_by_id(event_id: int) -> None:
    with tx() as c:
        c.execute("DELETE FROM events WHERE id = ?", (event_id,))


def list_event_revisions(event_id: int) -> list[dict[str, Any]]:
    """Return the revision history for one event, newest-first."""
    c = get_conn()
    rows = c.execute(
        "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY revised_at DESC",
        (event_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_events_for_message(message_id: str) -> list[dict[str, Any]]:
    c = get_conn()
    rows = c.execute(
        "SELECT * FROM events WHERE message_id = ? ORDER BY date_start ASC, id ASC",
        (message_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_denylist(message_id: str, title: str, date_start: str, reason: str | None = None) -> None:
    """Add to denylist AND purge any existing event with the same key."""
    key = title.lower().strip()
    with tx() as c:
        c.execute(
            """
            INSERT INTO denylist (message_id, title_key, date_start, added_at, reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id, title_key, date_start) DO UPDATE SET
              added_at = excluded.added_at, reason = excluded.reason
            """,
            (message_id, key, date_start, now_iso(), reason),
        )
        c.execute(
            "DELETE FROM events WHERE message_id = ? AND LOWER(title) = ? AND date_start = ?",
            (message_id, key, date_start),
        )


def list_denylist() -> list[dict[str, Any]]:
    c = get_conn()
    rows = c.execute("SELECT * FROM denylist ORDER BY added_at DESC").fetchall()
    return [dict(r) for r in rows]


def list_events(since: str | None = None) -> list[dict[str, Any]]:
    c = get_conn()
    if since:
        rows = c.execute(
            "SELECT * FROM events WHERE date_start >= ? OR (date_end IS NOT NULL AND date_end >= ?) "
            "ORDER BY date_start ASC, id ASC",
            (since, since),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM events ORDER BY date_start ASC, id ASC"
        ).fetchall()
    result: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        # Attach revision history inline when this event has been updated.
        if row.get("revision_count", 0):
            rev_rows = c.execute(
                "SELECT revised_at, source_message_id, change_type,"
                " prev_date_start, prev_date_end, prev_notes, change_summary"
                " FROM event_revisions WHERE event_id = ? ORDER BY revised_at ASC",
                (row["id"],),
            ).fetchall()
            row["revisions"] = [dict(rv) for rv in rev_rows]
        else:
            row["revisions"] = []
        result.append(row)
    return result


def stats() -> dict[str, Any]:
    c = get_conn()
    cached = c.execute("SELECT COUNT(*) FROM message_cache").fetchone()[0]
    ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    dl = c.execute("SELECT COUNT(*) FROM denylist").fetchone()[0]
    return {"cached_messages": cached, "events": ev, "denylisted": dl}


def list_unsynced_events() -> list[dict[str, Any]]:
    """Return events where synced_to_cal = 0 (not yet pushed to HA calendar)."""
    c = get_conn()
    rows = c.execute(
        "SELECT * FROM events WHERE synced_to_cal = 0 ORDER BY date_start ASC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_events_synced(event_ids: list[int]) -> None:
    """Mark events as synced after HA creates calendar entries."""
    if not event_ids:
        return
    c = get_conn()
    placeholders = ",".join("?" * len(event_ids))
    c.execute(
        f"UPDATE events SET synced_to_cal = 1 WHERE id IN ({placeholders})",
        event_ids,
    )
