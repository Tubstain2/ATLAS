"""ATLAS Autonomous Task Queue — background tasks that persist across restarts."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class TaskQueue:
    def __init__(self, config: dict, atlas_root: str = "."):
        self._path   = Path(atlas_root) / "ATLAS" / "Tasks" / "task_queue.json"
        self._lock   = threading.Lock()
        self._paused = False
        self._tasks  = self._load()
        log.info("TaskQueue: %d task(s) loaded.", len(self._tasks))

    # ── Mutation ──────────────────────────────────────────────────────────────

    def enqueue(self, task: dict) -> str:
        task = dict(task)
        task.setdefault("id",      str(uuid.uuid4())[:8])
        task.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%S"))
        task.setdefault("status",  "pending")
        task.setdefault("result",  None)
        with self._lock:
            self._tasks.append(task)
            self._save()
        log.info("TaskQueue: enqueued %s (p=%s).", task["id"], task.get("priority", 3))
        return task["id"]

    def dequeue(self) -> Optional[dict]:
        if self._paused:
            return None
        with self._lock:
            pending = [t for t in self._tasks if t.get("status") == "pending"]
            if not pending:
                return None
            task = min(pending, key=lambda t: t.get("priority", 3))
            task["status"] = "running"
            self._save()
        return task

    def requeue(self, task: dict) -> None:
        """Put a dequeued task back as pending (orchestrator full)."""
        with self._lock:
            task["status"] = "pending"
            if not any(t.get("id") == task.get("id") for t in self._tasks):
                self._tasks.append(task)
            self._save()

    def complete(self, task_id: str, result: str) -> None:
        self._update(task_id, status="complete", result=result)

    def fail(self, task_id: str, error: str) -> None:
        self._update(task_id, status="failed", result=error)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            self._tasks = [t for t in self._tasks if t.get("id") != task_id]
            self._save()

    def _update(self, task_id: str, **kwargs) -> None:
        with self._lock:
            for t in self._tasks:
                if t.get("id") == task_id:
                    t.update(kwargs)
                    break
            self._save()

    def pending(self) -> list[dict]:
        with self._lock:
            return sorted(
                [t for t in self._tasks if t.get("status") == "pending"],
                key=lambda t: t.get("priority", 3),
            )

    def completed(self, n: int = 5) -> list[dict]:
        with self._lock:
            done = [t for t in self._tasks if t.get("status") == "complete"]
            return done[-n:]

    def pause(self)  -> None: self._paused = True;  log.info("TaskQueue: paused.")
    def resume(self) -> None: self._paused = False;  log.info("TaskQueue: resumed.")

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.debug("TaskQueue: load failed: %s", exc)
        return []

    def _save(self) -> None:
        # ponytail: called only while _lock held by caller
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._tasks, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as exc:
            log.debug("TaskQueue: save failed: %s", exc)

    # ── Voice ─────────────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lc = text.lower().strip()
        if any(p in lc for p in ("atlas what is in your queue", "atlas show my task queue",
                                  "atlas what's in your queue")):
            p = self.pending()
            if not p:
                return "Your task queue is empty, Boss."
            items = "; ".join(f"{t['description'][:40]} (P{t.get('priority', 3)})" for t in p[:5])
            return f"Queue has {len(p)} task(s): {items}."
        if any(p in lc for p in ("atlas pause all tasks", "atlas pause tasks")):
            self.pause()
            return "All background tasks paused, Boss."
        if any(p in lc for p in ("atlas resume tasks", "atlas resume all tasks")):
            self.resume()
            return "Background tasks resumed, Boss."
        if "atlas clear completed tasks" in lc:
            with self._lock:
                self._tasks = [t for t in self._tasks
                               if t.get("status") not in ("complete", "failed")]
                self._save()
            return "Completed tasks cleared, Boss."
        return None


if __name__ == "__main__":
    import tempfile
    tq  = TaskQueue({}, atlas_root=tempfile.mkdtemp())
    tid = tq.enqueue({
        "type": "research", "description": "test", "priority": 3,
        "confidence": 0.9, "requires_confirmation": False, "due": None, "agent": "research",
    })
    got = tq.dequeue()
    assert got is not None and got["id"] == tid, f"Expected {tid}, got {got}"
    print("task_queue: ok")
