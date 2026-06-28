"""ATLAS Command Centre — aggregates status for the UI command panel."""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)


class CommandCentre:
    def __init__(self, config: dict, speak_cb: Callable,
                 task_queue, orchestrator, resources, safety,
                 agent_loop=None, window=None):
        self._speak        = speak_cb
        self._task_queue   = task_queue
        self._orchestrator = orchestrator
        self._resources    = resources
        self._safety       = safety
        self._window       = window

    def get_status(self) -> dict:
        return {
            "agents":         self._orchestrator.get_status(),
            "queue":          self._task_queue.pending()[:10],
            "resources":      self._resources.get_status(),
            "recent_actions": self._read_recent_audit(5),
            "safety":         self._safety.get_safety_status() if self._safety else {},
        }

    def _read_recent_audit(self, n: int) -> list[str]:
        try:
            path = self._safety._log_path
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()
            data  = [l for l in lines
                     if l.startswith("|") and "---" not in l and "Timestamp" not in l]
            return data[-n:]
        except Exception:
            return []

    def push_status(self, window=None) -> None:
        w = window or self._window
        if not w:
            return
        try:
            w._js(f"commandCentreUpdate({json.dumps(self.get_status())})")
        except Exception as exc:
            log.debug("CommandCentre.push_status: %s", exc)

    def handle(self, text: str) -> Optional[str]:
        lc = text.lower().strip()
        if any(p in lc for p in ("atlas open command centre", "atlas command centre",
                                  "atlas show command centre")):
            if self._window:
                self._window._js("showCommandCentre()")
            return "Opening command centre, Boss."
        if any(p in lc for p in ("atlas close command centre", "atlas hide command centre")):
            if self._window:
                self._window._js("hideCommandCentre()")
            return "Command centre closed, Boss."
        if "atlas show recent actions" in lc:
            lines = self._read_recent_audit(5)
            if not lines:
                return "No recent actions logged, Boss."
            return "Recent actions: " + "; ".join(lines)
        return None
