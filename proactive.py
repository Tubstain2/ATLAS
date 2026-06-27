"""ATLAS Proactive Intelligence — break reminders and stuck detection.

Complements ambient.py (which handles battery warnings and PTT).
This module adds: work-session break reminders and same-app stuck detection.
Uses its own interval (default 15 min) so it doesn't fight ambient.py's 10-min loop.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)


class ProactiveIntelligence:
    def __init__(self, config: dict, speak_cb: Callable,
                 brain=None, task_queue=None):
        self._speak        = speak_cb
        self._min_interval = int(config.get("proactive_min_interval_minutes", 15)) * 60
        self._max_per_hour = int(config.get("proactive_max_per_hour", 3))
        self._last_speak   = 0.0
        self._speak_count  = 0
        self._hour_start   = time.time()
        self._work_start   = 0.0
        self._last_app     = ""
        self._last_app_time = 0.0

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="atlas-proactive").start()
        log.info("ProactiveIntelligence: started (interval=%ds, max=%d/hr).",
                 self._min_interval, self._max_per_hour)

    def _loop(self) -> None:
        while True:
            time.sleep(60)
            if not self._can_speak():
                continue
            suggestion = self._check_all()
            if suggestion:
                self._speak(suggestion)
                self._last_speak  = time.time()
                self._speak_count += 1

    def _can_speak(self) -> bool:
        now = time.time()
        if now - self._hour_start > 3600:
            self._speak_count = 0
            self._hour_start  = now
        return (now - self._last_speak > self._min_interval and
                self._speak_count < self._max_per_hour)

    def _check_all(self) -> Optional[str]:
        return self._check_break_time() or self._check_stuck()

    def _check_break_time(self) -> Optional[str]:
        if self._work_start == 0.0:
            self._work_start = time.time()
            return None
        elapsed = (time.time() - self._work_start) / 60
        if elapsed >= 180:
            return ("Boss you have been working for 3 hours straight. "
                    "A break would significantly help your focus.")
        if elapsed >= 90:
            return "Boss you have been working for 90 minutes. Worth taking a short break."
        return None

    def _check_stuck(self) -> Optional[str]:
        app = self._get_frontmost_app()
        if not app:
            return None
        now = time.time()
        if app == self._last_app:
            if self._last_app_time and now - self._last_app_time > 35 * 60:
                return (f"Boss you have been in {app} for a while. "
                        "Want me to take a look at what you are working on?")
        else:
            self._last_app      = app
            self._last_app_time = now
        return None

    def _get_frontmost_app(self) -> str:
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 "tell application \"System Events\" to get name of first process "
                 "whose frontmost is true"],
                capture_output=True, text=True, timeout=3,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    def record_work_start(self) -> None:
        self._work_start = time.time()

    def handle(self, text: str) -> Optional[str]:
        lc = text.lower().strip()
        if "atlas less interruptions" in lc:
            self._min_interval = 30 * 60
            return "Proactive suggestions reduced to every 30 minutes, Boss."
        if "atlas more proactive" in lc:
            self._min_interval = 5 * 60
            return "Proactive mode increased. I will check in every 5 minutes, Boss."
        return None
