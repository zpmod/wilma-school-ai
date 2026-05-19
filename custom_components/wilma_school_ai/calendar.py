"""calendar.py — WilmaScheduleCalendar entity (P4.3).

Exposes each child's lukujärjestys (lessons) as a HA `CalendarEntity`. Data
comes from the coordinator's per-child ``schedule`` list, which is built by
``WilmaClient.schedule_events`` from the ``/!{child_id}/overview`` JSON.

One entity per child:
    calendar.wilma_<child_slug>_schedule

Each lesson becomes a `CalendarEvent` with:
    summary     subject (Groups[0].FullCaption)
    location    room   (Groups[0].Rooms[0].Caption)
    description teacher · class
    start/end   timezone-aware datetime in HA's configured TZ
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    tz = ZoneInfo(hass.config.time_zone or "UTC")
    entities = [
        WilmaScheduleCalendar(coordinator, child, entry.entry_id, tz)
        for child in coordinator.children
    ]
    async_add_entities(entities, True)


def _parse_hhmm(value: str) -> time:
    """Parse 'HH:MM' or 'H:MM' to a `time`. Returns 00:00 on failure."""
    try:
        h, m = value.split(":", 1)
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return time(0, 0)


class WilmaScheduleCalendar(CoordinatorEntity, CalendarEntity):
    """Calendar of upcoming lessons for one child."""

    def __init__(self, coordinator, child: dict, entry_id: str, tz: ZoneInfo) -> None:
        super().__init__(coordinator)
        self._child_name = child["name"]
        self._child_id = child["id"]
        self._entry_id = entry_id
        self._tz = tz

    @property
    def name(self) -> str:
        return f"Wilma {self._child_name} Schedule"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._child_id}_schedule"

    @property
    def icon(self) -> str:
        return "mdi:timetable"

    # ── helpers ──────────────────────────────────────────────────────────────

    def _lessons(self) -> list[dict]:
        return self.coordinator.data.get(self._child_name, {}).get("schedule", []) or []

    def _to_event(self, lesson: dict) -> CalendarEvent | None:
        try:
            day = datetime.fromisoformat(lesson["date"]).date()
        except (KeyError, ValueError):
            return None
        start = datetime.combine(day, _parse_hhmm(lesson.get("start", "")), tzinfo=self._tz)
        end = datetime.combine(day, _parse_hhmm(lesson.get("end", "")), tzinfo=self._tz)
        if end <= start:
            return None
        bits = [b for b in (lesson.get("teacher"), lesson.get("class_name")) if b]
        return CalendarEvent(
            summary=lesson.get("subject", lesson.get("short", "Lesson")),
            start=start,
            end=end,
            location=lesson.get("room") or None,
            description=" · ".join(bits) if bits else None,
        )

    # ── CalendarEntity API ───────────────────────────────────────────────────

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next lesson — what HA shows as the entity state."""
        now = datetime.now(self._tz)
        upcoming = []
        for lesson in self._lessons():
            ev = self._to_event(lesson)
            if ev and ev.end > now:
                upcoming.append(ev)
        upcoming.sort(key=lambda e: e.start)
        return upcoming[0] if upcoming else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Events between start_date and end_date (used by Lovelace calendar cards)."""
        out: list[CalendarEvent] = []
        for lesson in self._lessons():
            ev = self._to_event(lesson)
            if ev and ev.end > start_date and ev.start < end_date:
                out.append(ev)
        out.sort(key=lambda e: e.start)
        return out

    @property
    def extra_state_attributes(self) -> dict:
        return {"child": self._child_name, "lessons_known": len(self._lessons())}
