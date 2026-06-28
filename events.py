"""ATLAS Event Monitor — watches filesystem and system events, fires callbacks."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import psutil

log = logging.getLogger(__name__)

_BATTERY_WARN_COOLDOWN = 1800   # 30 min between battery warnings


class EventMonitor:
    def __init__(self, config: dict, speak_cb: Callable, brain=None,
                 task_queue=None, safety=None):
        self._speak        = speak_cb
        self._brain        = brain
        self._task_queue   = task_queue
        self._safety       = safety
        self._enabled      = config.get("events_enabled", True)
        self._bat_threshold = int(config.get("battery_low_threshold", 20))
        self._last_bat_warn = 0.0
        self._last_stock    = 0.0
        self._last_email    = 0.0
        self._observer: Optional[object] = None

    def start(self) -> None:
        if not self._enabled:
            log.info("EventMonitor: disabled.")
            return
        self._start_fs_watcher()
        threading.Thread(target=self._poll_loop, daemon=True, name="atlas-events").start()
        log.info("EventMonitor: ready (fs watcher + 30s poll).")

    def _start_fs_watcher(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            monitor = self

            class _DownloadHandler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        monitor._on_new_file(event.src_path)

            observer = Observer()
            downloads = Path.home() / "Downloads"
            if downloads.exists():
                observer.schedule(_DownloadHandler(), str(downloads), recursive=False)
            observer.start()
            self._observer = observer
        except Exception as exc:
            log.warning("EventMonitor: watchdog start failed: %s", exc)

    def _poll_loop(self) -> None:
        from safety import HALT_FLAG, PRIVACY_MODE
        while True:
            time.sleep(30)
            if HALT_FLAG.is_set() or PRIVACY_MODE.is_set():
                continue
            try:
                self._check_battery()
                now = time.time()
                if now - self._last_stock >= 300:
                    self._last_stock = now
                    self._check_stocks()
                if now - self._last_email >= 300:
                    self._last_email = now
                    self._check_email()
            except Exception as exc:
                log.debug("EventMonitor poll: %s", exc)

    def _check_battery(self) -> None:
        bat = psutil.sensors_battery()
        if bat is None or bat.power_plugged:
            return
        pct = int(bat.percent)
        now = time.time()
        if pct <= 10 and now - self._last_bat_warn > _BATTERY_WARN_COOLDOWN / 3:
            self._last_bat_warn = now
            self._speak(f"Boss battery critical at {pct} percent.")
        elif pct <= self._bat_threshold and now - self._last_bat_warn > _BATTERY_WARN_COOLDOWN:
            self._last_bat_warn = now
            self._speak(f"Boss battery at {pct} percent. Plugging in is recommended.")

    def _check_stocks(self) -> None:
        pass  # ponytail: market.py already monitors watchlist; don't duplicate

    def _check_email(self) -> None:
        pass  # ponytail: email polling requires OAuth; wire when Gmail MCP is ready

    def _on_new_file(self, path: str) -> None:
        name = os.path.basename(path)
        self._speak(f"Boss a new file arrived in Downloads: {name}. Want me to organise it?")
        if self._task_queue:
            self._task_queue.enqueue({
                "type": "file", "description": f"Organise {name} from Downloads",
                "priority": 4, "confidence": 0.8,
                "requires_confirmation": True, "due": None, "agent": "file",
            })

    def handle(self, text: str) -> Optional[str]:
        return None

    def stop(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join()
            except Exception:
                pass


if __name__ == "__main__":
    em = EventMonitor({}, speak_cb=print, brain=None)
    em.start()
    print("events: ok (watchdog running)")
    em.stop()
