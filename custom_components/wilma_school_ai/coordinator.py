"""
coordinator.py — DataUpdateCoordinator for the Wilma integration
================================================================
PURPOSE
    The coordinator is the single source of truth for exam and message data.
    It owns the polling loop, calls the Wilma HTTP client, detects new exams
    and messages, and makes the data available to all sensor entities. Only
    one login + HTTP round-trip per data type happens per poll cycle regardless
    of how many sensors exist.

HOW IT WORKS (HA concepts)
    DataUpdateCoordinator (HA base class)
        A helper HA provides for the "poll once, share with many" pattern.
        It manages the update_interval timer and calls _async_update_data()
        on schedule. All entities that subscribe to the coordinator are
        automatically refreshed when new data arrives.

    _async_update_data()
        The method HA calls on each poll. It must return the new data dict
        or raise UpdateFailed (which HA turns into a sensor "unavailable"
        state with a log entry). After the executor job returns we fire
        events safely from the async context.

    async_add_executor_job()
        The requests library is blocking (synchronous). HA runs on an
        asyncio event loop, so blocking calls must be run in a thread pool
        via async_add_executor_job. This keeps the event loop free while
        the HTTP calls are in flight.

    hass.bus.async_fire()
        Fires a named event onto the HA event bus. Any automation with a
        matching event trigger will be woken up. We fire "wilma_new_exam"
        and "wilma_new_message" with the full data dict as event data so
        automations can use the details directly in templates.

    New-exam detection
        Each exam is fingerprinted as "date_iso|topic|subject". On the
        first poll _known_exams is empty so no events fire (avoids a
        flood of notifications on startup). From the second poll onward,
        any key not seen previously triggers an event.

    New-message detection
        Message IDs are incremental, so they serve as a reliable cursor.
        _known_message_ids tracks the set of IDs seen in the previous poll
        per child. First poll populates silently; subsequent polls fire an
        event for each new ID.

    Message filtering
        sender_filters is a list of glob patterns (e.g. ['*smith*']).
        All metadata is fetched in one JSON call, filtered client-side,
        and bodies are fetched only for the top message_limit matches.
        An empty sender_filters list passes all senders through.

    Data structure
        coordinator.data[child_name] = {
            "exams":    [...],   # list of exam dicts
            "messages": [...],   # list of message dicts (with body)
        }

    update_interval
        How often the coordinator polls Wilma. Configured via scan_interval
        in the options flow (default: 4 hours). HA manages the timer.
"""

import fnmatch
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import WilmaClient
from .const import DOMAIN, EVENT_NEW_EXAM, EVENT_NEW_MESSAGE, HOMEWORK_LOOKBACK_DAYS

_LOGGER = logging.getLogger(__name__)


def _sender_matches(sender: str, patterns: list[str]) -> bool:
    """Return True if sender matches any glob pattern, or if patterns is empty."""
    if not patterns:
        return True
    sender_lower = sender.lower()
    return any(fnmatch.fnmatch(sender_lower, pat.lower()) for pat in patterns)


class WilmaCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        base_url: str,
        username: str,
        password: str,
        children: list[dict],
        scan_interval: int,
        sender_filters: list[str],
        message_limit: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="wilma_school_ai",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = WilmaClient(base_url, username, password)
        self.children = children
        self.sender_filters = sender_filters
        self.message_limit = message_limit
        self._known_exams: dict[str, set] = {}
        self._known_message_ids: dict[str, set] = {}
        # Persistent storage: survives HA restarts so we can detect backfill gaps.
        # Key: child name → max integer message ID seen in last successful poll.
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry_id}.last_seen")
        self._stored_max_ids: dict[str, int] = {}
        self._first_poll: bool = True

    async def _async_update_data(self) -> dict:
        # Load persisted last-seen state on the very first poll.
        if self._first_poll:
            stored = await self._store.async_load() or {}
            self._stored_max_ids = {
                k: int(v) for k, v in stored.items()
                if isinstance(v, (int, float, str)) and str(v).isdigit()
            }
            _LOGGER.debug("Backfill: loaded stored max IDs: %s", self._stored_max_ids)

        try:
            data, new_exam_events, new_message_events = (
                await self.hass.async_add_executor_job(self._fetch_all, self._first_poll)
            )
        except Exception as err:
            raise UpdateFailed(f"Error fetching Wilma data: {err}") from err

        # Persist updated max IDs after every successful poll.
        await self._store.async_save(self._stored_max_ids)
        self._first_poll = False

        for event_data in sorted(new_exam_events, key=lambda ev: ev.get("date_iso") or ""):
            self.hass.bus.async_fire(EVENT_NEW_EXAM, event_data)
        for event_data in sorted(new_message_events, key=lambda ev: int(ev.get("id") or 0)):
            self.hass.bus.async_fire(EVENT_NEW_MESSAGE, event_data)

        return data

    def _fetch_all(self, is_backfill_poll: bool = False) -> tuple[dict, list[dict], list[dict]]:
        self.client.login()

        result = {}
        new_exam_events = []
        new_message_events = []

        for child in self.children:
            name = child["name"]
            child_id = child["id"]

            # ── Exams ────────────────────────────────────────────────────────
            exams = self.client.get_exams(child_id)

            current_keys = {
                f"{e.get('date_iso')}|{e.get('topic')}|{e.get('subject')}"
                for e in exams
            }
            known_keys = self._known_exams.get(name)
            if known_keys is not None:
                new_keys = current_keys - known_keys
                for exam in exams:
                    key = f"{exam.get('date_iso')}|{exam.get('topic')}|{exam.get('subject')}"
                    if key in new_keys:
                        new_exam_events.append({"child": name, **exam})
            self._known_exams[name] = current_keys

            # ── Messages ─────────────────────────────────────────────────────
            # Fetch all metadata (1 call), take the N newest regardless of
            # sender, then filter that window by sender. Bodies are fetched
            # only for the matched subset — at most message_limit HTTP calls.
            all_messages = self.client.get_messages(child_id)

            # Backfill: on first poll after restart, fetch bodies for ALL
            # messages with ID > stored_max (messages that arrived while HA
            # was down). Fall back to the normal message_limit window if
            # there's no stored state or no gap.
            stored_max = self._stored_max_ids.get(name, 0)
            if is_backfill_poll and stored_max > 0:
                backfill_msgs = [m for m in all_messages if int(m["id"]) > stored_max]
                if backfill_msgs:
                    _LOGGER.info(
                        "Wilma backfill: %d missed messages for %s (since id=%d)",
                        len(backfill_msgs), name, stored_max,
                    )
                # Use backfill window for body fetching; always keep at least
                # the normal window too so the sensor attributes stay populated.
                body_window = backfill_msgs if backfill_msgs else all_messages[:self.message_limit]
            else:
                body_window = all_messages[:self.message_limit]
                backfill_msgs = []

            matched = [
                m for m in body_window
                if _sender_matches(m["sender"], self.sender_filters)
            ]

            for msg in matched:
                msg["body"] = self.client.fetch_message_body(child_id, msg["id"])

            known_ids = self._known_message_ids.get(name)
            if known_ids is not None:
                # Normal polling cycle: fire events for IDs not seen before.
                for msg in matched:
                    if msg["id"] not in known_ids:
                        new_message_events.append({"child": name, **msg})
            elif is_backfill_poll and stored_max > 0:
                # First poll after restart with stored state: fire events for
                # any message that arrived while HA was down.
                for msg in matched:
                    if int(msg["id"]) > stored_max:
                        new_message_events.append({"child": name, **msg})
            # else: truly first-ever poll — silently populate, no flood.

            # Update in-memory cursor and persisted max for next poll.
            self._known_message_ids[name] = {m["id"] for m in matched}
            if all_messages:
                new_max = max(int(m["id"]) for m in all_messages)
                self._stored_max_ids[name] = max(stored_max, new_max)

            # ── Schedule + Homework (single /overview call) ─────────────────────
            try:
                overview = self.client.get_overview(child_id)
                schedule = self.client.schedule_events(overview)
                homework = self.client.homework_entries(overview, HOMEWORK_LOOKBACK_DAYS)
            except Exception:  # pragma: no cover - log and continue
                _LOGGER.exception("Failed to fetch overview for %s", name)
                schedule = []
                homework = []

            result[name] = {
                "exams": exams,
                "messages": matched,
                "schedule": schedule,
                "homework": homework,
            }

        return result, new_exam_events, new_message_events
