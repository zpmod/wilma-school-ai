"""
Wilma CLI — interactive command-line client for testing the Wilma integration.

Usage:
    ./wilma-cli children
    ./wilma-cli schedule [--child CHILD_ID]
    ./wilma-cli exams [--child CHILD_ID]
    ./wilma-cli messages [--child CHILD_ID] [--limit N]
    ./wilma-cli homework [--child CHILD_ID]

Credentials are read from environment variables or a .env file:
    WILMA_BASE_URL   — e.g. https://espoo.inschool.fi
    WILMA_USERNAME   — your Wilma username
    WILMA_PASSWORD   — your Wilma password
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Import client.py directly without touching sys.path (to avoid the local
# calendar.py shadowing stdlib calendar, which breaks requests).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

import importlib.util

_client_path = _PROJECT_ROOT / "custom_components" / "wilma_school_ai" / "client.py"
_client_spec = importlib.util.spec_from_file_location("wilma_client", _client_path)
_client_mod = importlib.util.module_from_spec(_client_spec)
_client_spec.loader.exec_module(_client_mod)
WilmaClient = _client_mod.WilmaClient


def _load_env():
    """Load .env file from project root if it exists."""
    env_file = _PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                os.environ.setdefault(key, value)


def _get_client() -> WilmaClient:
    """Create and authenticate a WilmaClient from env vars."""
    _load_env()

    base_url = os.environ.get("WILMA_BASE_URL")
    username = os.environ.get("WILMA_USERNAME")
    password = os.environ.get("WILMA_PASSWORD")

    missing = []
    if not base_url:
        missing.append("WILMA_BASE_URL")
    if not username:
        missing.append("WILMA_USERNAME")
    if not password:
        missing.append("WILMA_PASSWORD")

    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        print("\nSet them in a .env file or export them:", file=sys.stderr)
        print("  export WILMA_BASE_URL=https://espoo.inschool.fi", file=sys.stderr)
        print("  export WILMA_USERNAME=your_username", file=sys.stderr)
        print("  export WILMA_PASSWORD=your_password", file=sys.stderr)
        print("\nOr create a .env file in the project root.", file=sys.stderr)
        sys.exit(1)

    client = WilmaClient(base_url, username, password)
    print("Logging in...", file=sys.stderr)
    client.login()
    print("Login successful.", file=sys.stderr)
    return client


def _resolve_child(client: WilmaClient, child_id: str | None) -> str:
    """Resolve child_id — if not given, auto-select the first (or only) child."""
    children = client.get_children()
    if not children:
        print("Error: No children found on this Wilma account.", file=sys.stderr)
        sys.exit(1)

    if child_id:
        ids = [c["id"] for c in children]
        if child_id not in ids:
            print(f"Error: Child ID '{child_id}' not found. Available: {ids}", file=sys.stderr)
            sys.exit(1)
        return child_id

    if len(children) == 1:
        selected = children[0]
        print(f"Auto-selected child: {selected['name']} (ID: {selected['id']})", file=sys.stderr)
        return selected["id"]

    print("Multiple children found. Please specify --child ID:", file=sys.stderr)
    for c in children:
        print(f"  {c['id']}  {c['name']}", file=sys.stderr)
    sys.exit(1)


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_children(args):
    """List children linked to the Wilma account."""
    client = _get_client()
    children = client.get_children()

    if not children:
        print("No children found.")
        return

    print(f"\n{'ID':<8} {'Name'}")
    print("-" * 40)
    for c in children:
        print(f"{c['id']:<8} {c['name']}")
    print(f"\n{len(children)} child(ren) found.")


def cmd_schedule(args):
    """Fetch weekly schedule for a child."""
    client = _get_client()
    child_id = _resolve_child(client, args.child)

    print(f"Fetching schedule for child {child_id}...", file=sys.stderr)
    overview = client.get_overview(child_id)
    events = WilmaClient.schedule_events(overview)

    # Filter to this week or next N days
    today = date.today()
    end = today + timedelta(days=args.days)
    week_events = [e for e in events if today.isoformat() <= e["date"] <= end.isoformat()]

    if not week_events:
        print(f"No schedule events found in the next {args.days} days.")
        return

    if args.json:
        print(json.dumps(week_events, indent=2, ensure_ascii=False))
        return

    current_date = None
    for ev in week_events:
        if ev["date"] != current_date:
            current_date = ev["date"]
            print(f"\n── {current_date} ──")
        room_str = f" ({ev['room']})" if ev.get("room") else ""
        teacher_str = f" [{ev['teacher']}]" if ev.get("teacher") else ""
        print(f"  {ev['start']}-{ev['end']}  {ev['subject']}{room_str}{teacher_str}")


def cmd_exams(args):
    """List upcoming exams."""
    client = _get_client()
    child_id = _resolve_child(client, args.child)

    print(f"Fetching exams for child {child_id}...", file=sys.stderr)
    exams = client.get_exams(child_id)

    if not exams:
        print("No upcoming exams found.")
        return

    if args.json:
        print(json.dumps(exams, indent=2, ensure_ascii=False))
        return

    print(f"\n{'Date':<14} {'Subject':<20} {'Topic'}")
    print("-" * 60)
    for ex in exams:
        date_str = ex.get("date_iso") or ex.get("date", "?")
        subject = ex.get("subject", "")
        topic = ex.get("topic", "")
        print(f"{date_str:<14} {subject:<20} {topic}")
        if ex.get("details"):
            print(f"{'':14} {'':20} ↳ {ex['details']}")


def cmd_messages(args):
    """List recent messages."""
    client = _get_client()
    child_id = _resolve_child(client, args.child)

    print(f"Fetching messages for child {child_id}...", file=sys.stderr)
    messages = client.get_messages(child_id)

    if args.limit:
        messages = messages[: args.limit]

    if not messages:
        print("No messages found.")
        return

    if args.json:
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        return

    print(f"\n{'ID':<8} {'Date':<18} {'Sender':<25} {'Subject'}")
    print("-" * 80)
    for msg in messages:
        unread = "●" if msg.get("is_unread") else " "
        print(f"{unread}{msg['id']:<7} {msg['sent']:<18} {msg['sender'][:24]:<25} {msg['subject'][:40]}")


def cmd_homework(args):
    """List recent homework entries."""
    client = _get_client()
    child_id = _resolve_child(client, args.child)

    print(f"Fetching homework for child {child_id}...", file=sys.stderr)
    overview = client.get_overview(child_id)
    entries = WilmaClient.homework_entries(overview, lookback_days=args.days)

    if not entries:
        print(f"No homework entries found in the last {args.days} days.")
        return

    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return

    current_date = None
    for hw in entries:
        if hw["date"] != current_date:
            current_date = hw["date"]
            print(f"\n── {current_date} ──")
        print(f"  [{hw['subject']}] {hw['text']}")


def main():
    parser = argparse.ArgumentParser(
        prog="wilma-cli",
        description="Test client for the Wilma School AI integration",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # children
    sub.add_parser("children", help="List children linked to the account")

    # schedule
    p_sched = sub.add_parser("schedule", help="Fetch weekly schedule")
    p_sched.add_argument("--child", "-c", help="Child ID (auto-selects if only one)")
    p_sched.add_argument("--days", "-d", type=int, default=7, help="Days ahead (default: 7)")
    p_sched.add_argument("--json", action="store_true", help="Output raw JSON")

    # exams
    p_exams = sub.add_parser("exams", help="List upcoming exams")
    p_exams.add_argument("--child", "-c", help="Child ID")
    p_exams.add_argument("--json", action="store_true", help="Output raw JSON")

    # messages
    p_msgs = sub.add_parser("messages", help="List recent messages")
    p_msgs.add_argument("--child", "-c", help="Child ID")
    p_msgs.add_argument("--limit", "-n", type=int, default=10, help="Max messages (default: 10)")
    p_msgs.add_argument("--json", action="store_true", help="Output raw JSON")

    # homework
    p_hw = sub.add_parser("homework", help="List recent homework")
    p_hw.add_argument("--child", "-c", help="Child ID")
    p_hw.add_argument("--days", "-d", type=int, default=14, help="Lookback days (default: 14)")
    p_hw.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args()

    commands = {
        "children": cmd_children,
        "schedule": cmd_schedule,
        "exams": cmd_exams,
        "messages": cmd_messages,
        "homework": cmd_homework,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
