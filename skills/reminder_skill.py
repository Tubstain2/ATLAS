"""Reminder skill — create and read reminders (stored in memory/reminders.json)."""

import json
import threading
from datetime import datetime
from pathlib import Path

_REMINDERS_PATH = Path(__file__).resolve().parent.parent / "memory" / "reminders.json"
_lock = threading.Lock()


def skill_info():
    return {
        "name": "reminder",
        "triggers": ["atlas remind me", "add a reminder", "set a reminder",
                     "atlas remember to", "atlas what are my reminders",
                     "show my reminders", "atlas read my reminders",
                     "atlas reminders", "what do i need to do"],
        "description": "Create and read reminders stored locally",
    }


def execute(query: str, context: dict) -> str:
    lower = query.lower().strip()

    if any(p in lower for p in ("what are my reminders", "show my reminders",
                                 "atlas reminders", "read my reminders",
                                 "what do i need to do")):
        return _list_reminders()

    if any(p in lower for p in ("atlas remind me", "add a reminder",
                                 "set a reminder", "atlas remember to")):
        # Extract the reminder text after the trigger
        for trigger in ("atlas remind me to", "atlas remind me", "add a reminder",
                        "set a reminder", "atlas remember to"):
            if trigger in lower:
                reminder_text = lower.split(trigger, 1)[-1].strip().strip(".")
                if reminder_text:
                    return _add_reminder(reminder_text)
        return "What would you like me to remind you about?"

    return None


def _load() -> list:
    try:
        if _REMINDERS_PATH.exists():
            return json.loads(_REMINDERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save(reminders: list):
    try:
        _REMINDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REMINDERS_PATH.write_text(json.dumps(reminders, indent=2), encoding="utf-8")
    except Exception:
        pass


def _add_reminder(text: str) -> str:
    with _lock:
        reminders = _load()
        reminders.append({
            "text":    text,
            "created": datetime.now().isoformat(timespec="seconds"),
            "done":    False,
        })
        _save(reminders)
    return f"Reminder added: {text}."


def _list_reminders() -> str:
    with _lock:
        reminders = _load()
    active = [r for r in reminders if not r.get("done")]
    if not active:
        return "You have no pending reminders, Boss."
    items = "; ".join(r["text"] for r in active[:5])
    return f"You have {len(active)} reminders: {items}."
