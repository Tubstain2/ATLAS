"""Calendar skill — read macOS Calendar events via AppleScript."""

import platform
import subprocess
from datetime import datetime


def skill_info():
    return {
        "name": "calendar",
        "triggers": ["atlas what do i have today", "atlas my schedule",
                     "atlas calendar", "what are my events", "what meetings do i have",
                     "atlas what is on my calendar", "do i have any meetings",
                     "what is my schedule", "atlas events today"],
        "description": "Read today's calendar events from macOS Calendar",
    }


def execute(query: str, context: dict) -> str:
    if platform.system() != "Darwin":
        return "Calendar reading via AppleScript is only available on macOS."

    today   = datetime.now().strftime("%A, %d %B %Y")
    script  = '''
tell application "Calendar"
    set todayEvents to {}
    set theDate to current date
    set startOfDay to theDate - (time of theDate)
    set endOfDay to startOfDay + (23 * hours) + (59 * minutes)
    repeat with cal in calendars
        repeat with ev in (events of cal whose start date >= startOfDay and start date <= endOfDay)
            set evTitle to summary of ev
            set evStart to start date of ev
            set timeStr to (time string of evStart)
            set end of todayEvents to (evTitle & " at " & timeStr)
        end repeat
    end repeat
    return todayEvents
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()

        if not output or output == "{}":
            return f"You have no calendar events today, Boss. Today is {today}."

        events = [e.strip() for e in output.split(",") if e.strip()]
        if not events:
            return f"No events found for today, Boss."

        count = len(events)
        if count == 1:
            return f"You have one event today: {events[0]}."
        summary = "; ".join(events[:4])
        return f"You have {count} events today, Boss. {summary}."

    except subprocess.TimeoutExpired:
        return "Calendar took too long to respond."
    except Exception as exc:
        return f"I couldn't read your calendar: {exc}"
