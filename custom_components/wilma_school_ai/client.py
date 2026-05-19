"""
client.py — Wilma HTTP client (pure Python, no HA dependencies)
===============================================================
PURPOSE
    Handles all communication with the Wilma school portal: login,
    discovering children linked to the account, and fetching upcoming
    exams for a given child. This file has no knowledge of Home Assistant
    — it is a plain Python module that coordinator.py calls from a
    background thread.

HOW IT WORKS
    WilmaClient wraps a requests.Session that is re-used across calls so
    the login cookie stays alive. Callers must call login() before
    get_children() or get_exams(). The coordinator does this on every
    poll so the session is always fresh (Wilma sessions expire).

    Session cookie bug: After the first successful login, Wilma redirects
    GET /login to the home page because the session already has valid
    cookies. This causes 'NoneType' object is not subscriptable when we
    try to read the SESSIONID. Fixed by resetting self.session at the
    start of every login() call.

    get_children() returns a list of {name, id} dicts discovered by
    parsing the Wilma home page after login. Child links follow the
    pattern /!{child_id}/ in the page HTML.

    get_exams() returns a list of plain dicts. One extra field compared
    to the raw Wilma data is date_iso (ISO-8601 date string, e.g.
    "2026-04-14") — this makes date comparisons in automations easy
    without parsing Finnish weekday names.
"""

import logging
import re
import requests
from bs4 import BeautifulSoup

# Message bodies are fed into the LLM parser for date extraction. At 1000 chars
# typical viikkoviesti mails lose actionable content. 16 KB covers all known messages.
_BODY_PREVIEW_LEN = 16000

_LOGGER = logging.getLogger(__name__)


def _parse_date_iso(date_str: str) -> str | None:
    """Parse Finnish exam date like 'Ti 14.4.2026' to '2026-04-14'."""
    match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', date_str)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


class WilmaClient:
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    }

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(self._HEADERS)

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self) -> None:
        # Reset session before each login so stale cookies from a previous
        # poll don't cause Wilma to redirect /login → home page.
        self.session = requests.Session()
        self.session.headers.update(self._HEADERS)
        r = self.session.get(f"{self.base_url}/login")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        session_input = soup.find("input", {"name": "SESSIONID"})
        if session_input is None:
            _LOGGER.error(
                "SESSIONID not found on Wilma login page. "
                "Status: %s, URL: %s, Body snippet: %.500s",
                r.status_code, r.url, r.text,
            )
            raise RuntimeError(
                f"SESSIONID input not found on login page (status {r.status_code}, url {r.url})"
            )
        session_id = session_input["value"]

        r = self.session.post(
            f"{self.base_url}/login",
            data={
                "Login":      self.username,
                "Password":   self.password,
                "SESSIONID":  session_id,
                "returnpath": "",
                "submit":     "Kirjaudu sisään",
            },
            allow_redirects=True,
        )
        r.raise_for_status()

        if "Kirjaudu sisään" in r.text and 'name="Login"' in r.text:
            raise RuntimeError("Login failed – check your username/password")

    # ── Children ──────────────────────────────────────────────────────────────

    def get_children(self) -> list[dict]:
        """
        Discover children linked to the logged-in account by parsing the
        Wilma home page. Returns a list of dicts with 'name' and 'id' keys.

        Child links follow the pattern /!{child_id}/ in the page HTML.
        Must be called after login().
        """
        r = self.session.get(f"{self.base_url}/")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        seen: dict[str, str] = {}
        for a in soup.find_all("a", href=re.compile(r"^/!\d+")):
            m = re.match(r"^/!(\d+)/", a["href"])
            if m:
                child_id = m.group(1)
                # Use only the first text node — the link may contain nested
                # elements with school/class info that must not be included.
                name = next(a.strings, "").strip()
                if child_id not in seen and name:
                    seen[child_id] = name

        return [{"name": name, "id": cid} for cid, name in seen.items()]

    # ── Messages ─────────────────────────────────────────────────────────────

    def get_messages(self, child_id: str) -> list[dict]:
        """
        Fetch metadata for all inbox messages for a child via the JSON list API.
        Sorted newest-first by TimeStamp. Does NOT fetch bodies.

        Each returned dict has: id, subject, sender, sender_id, sender_type,
        sent (YYYY-MM-DD HH:MM), url, is_unread.
        """
        r = self.session.get(f"{self.base_url}/!{child_id}/messages/list")
        r.raise_for_status()

        data = r.json()
        if data.get("Status") != 200:
            raise RuntimeError(
                f"messages/list returned status {data.get('Status')} for child {child_id}"
            )

        messages = []
        for item in sorted(data.get("Messages", []), key=lambda x: x.get("TimeStamp", ""), reverse=True):
            message_id = str(item["Id"])
            sender = item.get("Sender", "")
            sender_id_match = re.search(r"\(([^)]+)\)$", sender)
            messages.append({
                "id":          message_id,
                "subject":     item.get("Subject", ""),
                "sender":      sender,
                "sender_id":   sender_id_match.group(1) if sender_id_match else "",
                "sender_type": item.get("SenderType"),
                "sent":        item.get("TimeStamp", ""),
                "url":         f"{self.base_url}/!{child_id}/messages/{message_id}",
                "is_unread":   item.get("Status") == 1,
            })

        return messages

    def fetch_message_body(self, child_id: str, message_id: str) -> str:
        """
        Fetch the plain-text body of a single message, truncated to
        _BODY_PREVIEW_LEN characters. Body lives in <div class="ckeditor">.
        """
        r = self.session.get(f"{self.base_url}/!{child_id}/messages/{message_id}")
        if r.status_code == 403:
            _LOGGER.debug(
                "Message %s returned 403 (expired/inaccessible), skipping body",
                message_id,
            )
            return ""
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        body_div = soup.find("div", class_="ckeditor")
        if not body_div:
            return ""
        text = body_div.get_text("\n", strip=True)
        text = text.replace("\\n", "\n")
        text = "\n".join(line for line in text.splitlines() if line.strip())
        if len(text) > _BODY_PREVIEW_LEN:
            text = text[:_BODY_PREVIEW_LEN] + "…"
        return text

    # ── Schedule (overview) ──────────────────────────────────────────────────

    def get_overview(self, child_id: str) -> dict:
        """Fetch /!{child_id}/overview — JSON with the child's full schedule.

        Returns the raw JSON dict. The interesting key is ``Schedule`` — a
        list of recurring lesson slots. Each slot has ``Day`` (1=Mon…5=Fri),
        ``Start``/``End`` (HH:MM), ``Class`` and a ``DateArray`` listing the
        actual ISO dates the slot occurs on. ``Groups[0]`` carries
        ``Caption``/``FullCaption``, ``Teachers[]``, ``Rooms[]``.
        """
        r = self.session.get(f"{self.base_url}/!{child_id}/overview")
        r.raise_for_status()
        return r.json()

    @staticmethod
    def schedule_events(overview: dict) -> list[dict]:
        """Flatten an /overview payload into a list of concrete lesson events.

        Each event dict: ``date`` (ISO), ``start``/``end`` (HH:MM), ``subject``
        (FullCaption or Caption), ``short`` (Caption), ``teacher`` (LongCaption
        joined), ``room`` (Caption joined), ``class_name``, ``schedule_id``.
        """
        events: list[dict] = []
        for slot in overview.get("Schedule", []):
            groups = slot.get("Groups") or []
            primary = groups[0] if groups else {}
            subject = primary.get("FullCaption") or primary.get("Caption") or slot.get("Class", "")
            short = primary.get("Caption") or subject
            teacher = ", ".join(
                t.get("LongCaption") or t.get("Caption", "")
                for t in primary.get("Teachers", [])
                if t.get("LongCaption") or t.get("Caption")
            )
            room = ", ".join(
                r.get("Caption", "")
                for r in primary.get("Rooms", [])
                if r.get("Caption")
            )
            for date_iso in slot.get("DateArray") or []:
                events.append({
                    "date": date_iso,
                    "start": slot.get("Start", ""),
                    "end": slot.get("End", ""),
                    "subject": subject,
                    "short": short,
                    "teacher": teacher,
                    "room": room,
                    "class_name": slot.get("Class", ""),
                    "schedule_id": slot.get("ScheduleID"),
                })
        events.sort(key=lambda e: (e["date"], e["start"]))
        return events

    @staticmethod
    def homework_entries(overview: dict, lookback_days: int) -> list[dict]:
        """Flatten the Homework array embedded inside each Group of /overview.

        Each entry: ``date`` (ISO), ``text``, ``subject`` (CourseName),
        ``course_code`` (CourseCode). Sorted newest-first and filtered to the
        last ``lookback_days`` so HA attributes stay small.
        """
        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
        out: list[dict] = []
        for group in overview.get("Groups") or []:
            subject = group.get("CourseName") or group.get("Caption") or ""
            code = group.get("CourseCode") or ""
            for hw in group.get("Homework") or []:
                day = hw.get("Date") or ""
                if day < cutoff:
                    continue
                text = (hw.get("Homework") or "").strip()
                if not text:
                    continue
                out.append({
                    "date": day,
                    "subject": subject,
                    "course_code": code,
                    "text": text,
                })
        out.sort(key=lambda h: (h["date"], h["subject"]), reverse=True)
        return out

    # ── Exams ─────────────────────────────────────────────────────────────────

    def get_exams(self, child_id: str) -> list[dict]:
        """
        Fetch upcoming exams for a child from /!{child_id}/exams/calendar.

        Returns a list of dicts with keys:
          date, date_iso, topic, subject, group, group_url, teacher, details
        """
        r = self.session.get(f"{self.base_url}/!{child_id}/exams/calendar")
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        exams = []

        for table in soup.select("div.table-responsive table.table-grey"):
            rows = table.find_all("tr")
            if not rows:
                continue

            exam = {}

            for i, row in enumerate(rows):
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                label = cells[0].get_text(strip=True)
                value = cells[1].get_text(" ", strip=True)

                if i == 0:
                    exam["date"] = label
                    exam["date_iso"] = _parse_date_iso(label)

                    parts = [p.strip() for p in value.split(":")]
                    exam["topic"]   = parts[0] if len(parts) > 0 else value
                    exam["subject"] = parts[1] if len(parts) > 1 else ""
                    exam["group"]   = parts[2] if len(parts) > 2 else ""

                    link = cells[1].find("a")
                    exam["group_url"] = (self.base_url + link["href"]) if link else None

                else:
                    if label == "Opettaja":
                        exam["teacher"] = value
                    elif label == "Kokeen lisätiedot":
                        exam["details"] = value
                    else:
                        exam[label] = value

            exams.append(exam)

        return exams
