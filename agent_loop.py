"""ATLAS Agent Loop — master background orchestrator, 500ms tick."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from safety import HALT_FLAG

log = logging.getLogger(__name__)


class AgentLoop:
    def __init__(self, config: dict, speak_cb: Callable,
                 task_queue, orchestrator, events, resources,
                 proactive, safety, decisions):
        self._speak        = speak_cb
        self._task_queue   = task_queue
        self._orchestrator = orchestrator
        self._resources    = resources
        self._interval     = float(config.get("agent_loop_interval_ms", 500)) / 1000
        self._running      = False
        self._thread: Optional[threading.Thread] = None
        self._last_queue_process = 0.0
        self._queue_interval     = 5.0  # ponytail: dequeue every 5s, not every 500ms tick

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="atlas-agent-loop")
        self._thread.start()
        log.info("AgentLoop: started (%.0fms tick).", self._interval * 1000)

    def _loop(self) -> None:
        while self._running:
            if HALT_FLAG.is_set():
                time.sleep(0.1)
                continue
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as exc:
                log.debug("AgentLoop tick: %s", exc)
            time.sleep(max(0.0, self._interval - (time.monotonic() - t0)))

    def _tick(self) -> None:
        now = time.time()
        if now - self._last_queue_process >= self._queue_interval:
            self._last_queue_process = now
            self._process_queue()

    def _process_queue(self) -> None:
        if self._resources.mode == "CRITICAL":
            return
        task = self._task_queue.dequeue()
        if task is None:
            return
        if not self._orchestrator.submit(task):
            self._task_queue.requeue(task)   # orchestrator full — try next cycle

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("AgentLoop: stopped.")

    def handle(self, text: str) -> Optional[str]:
        lc = text.lower().strip()
        if "atlas agent status" in lc:
            return self._orchestrator.handle(text)
        if any(p in lc for p in ("atlas what is in your queue", "atlas what's in your queue")):
            return self._task_queue.handle(text)
        if any(p in lc for p in ("atlas pause all agents", "atlas pause all tasks")):
            self._task_queue.pause()
            return "All background tasks paused, Boss."
        if any(p in lc for p in ("atlas resume all agents", "atlas resume tasks")):
            self._task_queue.resume()
            return "Background tasks resumed, Boss."
        if "atlas shutdown" in lc:
            self.stop()
            return "Going offline Boss. All state saved."
        return None
