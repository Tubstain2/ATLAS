"""ATLAS Multi-Agent Orchestrator — coordinates background specialist agents."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: dict, speak_cb: Callable, brain,
                 task_queue, safety, decisions, resources,
                 research=None, market=None, code_agent=None):
        self._speak        = speak_cb
        self._brain        = brain
        self._task_queue   = task_queue
        self._safety       = safety
        self._decisions    = decisions
        self._resources    = resources
        self._research     = research
        self._market       = market
        self._code_agent   = code_agent
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="atlas-orchestrator"
        )
        self._thread.start()
        log.info("Orchestrator: started (asyncio loop).")

    def submit(self, task: dict) -> bool:
        if self._loop is None:
            return False
        if len(self._running_tasks) >= self._resources.max_parallel_agents:
            return False
        asyncio.run_coroutine_threadsafe(self._run_task(task), self._loop)
        return True

    async def _run_task(self, task: dict) -> None:
        task_id = task["id"]
        self._running_tasks[task_id] = asyncio.current_task()
        try:
            ok, reason = self._safety.check(task.get("agent", "generic"))
            if not ok:
                self._task_queue.fail(task_id, f"safety: {reason}")
                self._safety.log_action(
                    "orchestrator", task.get("description", ""), task.get("agent", ""),
                    0.0, False, "blocked",
                )
                return

            score = self._decisions.score(
                clarity="clear" if task.get("confidence", 0) > 0.7 else "mostly_clear",
                reversible="full", precedent="novel", risk="minimal",
            )
            decision = self._decisions.decision(score)
            if decision == "ask":
                self._speak(f"Boss, I want to {task.get('description', '')[:60]}. Should I proceed?")
                self._task_queue.fail(task_id, "awaiting_confirmation")
                return

            result = await asyncio.wait_for(self._dispatch(task), timeout=300)
            self._task_queue.complete(task_id, result or "done")
            self._safety.log_action(
                "orchestrator", task.get("description", ""), task.get("agent", ""),
                score, False, "complete",
            )
            if decision == "act_report":
                self._speak(f"Boss, completed: {task.get('description', '')[:60]}.")
        except asyncio.TimeoutError:
            self._task_queue.fail(task_id, "timeout")
            log.warning("Orchestrator: task %s timed out.", task_id)
        except Exception as exc:
            self._task_queue.fail(task_id, str(exc))
            log.warning("Orchestrator: task %s failed: %s", task_id, exc)
        finally:
            self._running_tasks.pop(task_id, None)

    async def _dispatch(self, task: dict) -> str:
        loop       = asyncio.get_running_loop()
        agent_type = task.get("agent", task.get("type", ""))
        if agent_type == "research" and self._research:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._research.handle, task["description"]),
                timeout=300,
            )
        if agent_type == "market" and self._market:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._market.handle, task["description"]),
                timeout=60,
            )
        if agent_type == "code" and self._code_agent:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._code_agent.handle, task["description"]),
                timeout=300,
            )
        # ponytail: generic fallback — brain handles anything
        return await asyncio.wait_for(
            loop.run_in_executor(None, self._brain.handle, task["description"]),
            timeout=300,
        )

    def get_status(self) -> dict:
        return {"running": len(self._running_tasks), "tasks": list(self._running_tasks.keys())}

    def handle(self, text: str) -> Optional[str]:
        lc = text.lower().strip()
        if "atlas agent status" in lc:
            s = self.get_status()
            if s["running"] == 0:
                return "No background agents running, Boss. Queue is idle."
            return f"Boss, {s['running']} agent(s) running: {', '.join(s['tasks'])}."
        if any(p in lc for p in ("atlas recall your agents", "atlas stop all agents")):
            for t in list(self._running_tasks.values()):
                if t:
                    t.cancel()
            return "All agents recalled, Boss."
        if any(p in lc for p in ("atlas what did your agents find", "atlas agent results")):
            done = self._task_queue.completed(5)
            if not done:
                return "No completed background tasks yet, Boss."
            return "Recent completions: " + "; ".join(t.get("description", "")[:40] for t in done) + "."
        return None
