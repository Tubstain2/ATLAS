"""
ATLAS Planner — DeerFlow-inspired lead agent + sub-agent architecture.

Adapts DeerFlow's lead_agent pattern:
  • Lead agent analyses task, breaks into subtasks, dispatches to specialists
  • Specialists run in parallel via asyncio (not processes — lightweight tasks)
  • Results synthesised into a final voice response
  • Plans persisted to ATLAS/Memory/plans/ in Obsidian vault

Complexity classification (before every request):
  SIMPLE  — single-step, direct answer → skip planner, direct brain response
  MEDIUM  — 2–3 steps, needs research or code → sequential plan
  COMPLEX — 4+ steps, multi-domain, takes minutes → parallel sub-agents

DeerFlow source studied:
  backend/packages/harness/deerflow/agents/lead_agent/agent.py
  backend/packages/harness/deerflow/subagents/executor.py
  skills/public/bootstrap/SKILL.md (SKILL.md format)

Voice commands:
  "ATLAS plan this out"         → force planning mode
  "ATLAS quick answer"          → skip planner, direct response
  "ATLAS what is your plan"     → read current plan aloud
  "ATLAS cancel that plan"      → cancel current plan execution
  "ATLAS research X in depth"   → deep research mode
  "ATLAS deep dive on X"        → same as research mode
  "ATLAS how many steps is this"→ read plan step count
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

# ── Complexity levels (mirrors DeerFlow is_plan_mode + subagent_enabled) ─────

class TaskComplexity(Enum):
    SIMPLE  = "simple"    # direct answer, no planner
    MEDIUM  = "medium"    # sequential 2-3 step plan
    COMPLEX = "complex"   # parallel sub-agents


# ── Sub-task (adapted from DeerFlow's SubagentResult) ────────────────────────

@dataclass
class SubTask:
    """A single step in a decomposed plan."""
    id: str
    description: str
    tool: str               # web | code | file | reasoning | market
    depends_on: List[str] = field(default_factory=list)
    result: Optional[str]  = None
    status: str            = "pending"   # pending|running|done|failed
    started_at: float      = 0.0
    finished_at: float     = 0.0
    timeout_secs: int      = 30


@dataclass
class Plan:
    """A decomposed task plan with its sub-tasks."""
    id: str
    original_task: str
    complexity: TaskComplexity
    subtasks: List[SubTask]
    created_at: float = field(default_factory=time.monotonic)
    final_result: Optional[str] = None
    status: str = "pending"   # pending|running|done|cancelled|failed


# ── Complexity keywords (DeerFlow uses LangChain; we use fast keyword check) ──

_SIMPLE_INDICATORS = {
    "what is", "what's", "when is", "who is", "how do you",
    "tell me", "what time", "what day", "spell", "define",
}
_COMPLEX_INDICATORS = {
    "research", "investigate", "analyse", "analyze", "compare", "deep dive",
    "write a report", "build a plan", "create a strategy", "multiple",
    "step by step", "comprehensive", "in depth", "thoroughly",
}
_RESEARCH_TRIGGERS = {
    "research", "deep dive", "investigate", "in depth", "deep research",
    "find out everything", "full report",
}
_CODE_TRIGGERS = {
    "build", "create", "write code", "implement", "develop", "program",
    "script", "function", "class", "module",
}


class ATLASPlanner:
    """
    Lead agent orchestrator for ATLAS.

    Borrows DeerFlow's architecture:
      1. classify_complexity() → decides whether to engage planner
      2. build_plan()          → lead agent decomposes task into subtasks
      3. execute_plan()        → runs subtasks (parallel for COMPLEX, sequential for MEDIUM)
      4. synthesise()          → lead agent combines results into final answer

    Usage:
        planner = ATLASPlanner(brain, config, vault_brain=vb, speak_cb=speak)
        result = await planner.run(user_query)
        # or from sync context:
        result = planner.run_sync(user_query)
    """

    def __init__(self, brain=None, config: dict = None,
                 vault_brain=None, speak_cb=None, web_module=None):
        self._brain         = brain
        self._config        = config or {}
        self._vb            = vault_brain
        self._speak         = speak_cb or (lambda s: None)
        self._web           = web_module

        self._complexity_threshold = int(
            self._config.get("planner_complexity_threshold", 3))
        self._subagent_timeout     = int(
            self._config.get("subagent_timeout_seconds", 30))
        self._max_parallel         = int(
            self._config.get("subagent_max_parallel", 4))

        self._current_plan: Optional[Plan] = None
        self._force_plan   = False
        self._force_direct = False
        self._lock         = threading.Lock()

        log.info("ATLASPlanner: initialised (threshold=%d, timeout=%ds, parallel=%d).",
                 self._complexity_threshold, self._subagent_timeout, self._max_parallel)

    # ── Complexity classification ─────────────────────────────────────────────

    def classify_complexity(self, text: str) -> TaskComplexity:
        """
        Fast keyword-based classification (< 5ms).
        Mirrors DeerFlow's is_plan_mode and subagent_enabled flags.
        """
        lower = text.lower()
        word_count = len(text.split())

        # Force flags from voice commands
        if self._force_plan:
            return TaskComplexity.COMPLEX
        if self._force_direct:
            return TaskComplexity.SIMPLE

        # Simple: short + known simple indicator
        if word_count < 8 and any(s in lower for s in _SIMPLE_INDICATORS):
            return TaskComplexity.SIMPLE

        # Complex: explicit research/analysis or very long
        if any(c in lower for c in _COMPLEX_INDICATORS) or word_count >= 30:
            return TaskComplexity.COMPLEX

        # Medium: code build tasks, multi-part questions (2+ "and"/"then"/"also")
        is_code = any(c in lower for c in _CODE_TRIGGERS)
        is_multi = lower.count(" and ") >= 2 or " then " in lower or " also " in lower
        if is_code or is_multi:
            return TaskComplexity.MEDIUM

        # Default: simple for short queries, medium for longer ones
        return TaskComplexity.SIMPLE if word_count < 15 else TaskComplexity.MEDIUM

    # ── Plan building (lead agent decomposition) ──────────────────────────────

    def build_plan(self, task: str, complexity: TaskComplexity) -> Plan:
        """
        Ask the AI to decompose the task into subtasks (DeerFlow lead_agent pattern).
        Returns a Plan with ordered SubTasks.
        """
        plan_id = str(uuid.uuid4())[:8]

        if complexity == TaskComplexity.SIMPLE:
            # No decomposition needed — single direct-answer step
            return Plan(
                id=plan_id, original_task=task, complexity=complexity,
                subtasks=[SubTask(id="s1", description=task,
                                  tool="reasoning", depends_on=[])]
            )

        # Ask AI to decompose — adapted from DeerFlow lead_agent prompt
        prompt = (
            f"Break this task into {self._complexity_threshold}–5 concrete subtasks. "
            "Reply with ONLY a numbered list, one subtask per line. "
            "Each line: [tool] task description\n"
            "Tools: web (search/browse), code (write/run code), "
            "reasoning (think/analyse), file (read/write files), market (prices/data)\n\n"
            f"Task: {task}\n\n"
            "Example output:\n"
            "1. [web] Search for current Python async best practices\n"
            "2. [reasoning] Analyse findings and design solution\n"
            "3. [code] Implement the async pattern\n"
        )

        subtasks: List[SubTask] = []
        try:
            if self._brain:
                raw = self._brain.ask(prompt)
                subtasks = self._parse_subtasks(raw)
        except Exception as exc:
            log.warning("Planner: decomposition LLM call failed: %s", exc)

        if not subtasks:
            # Fallback: two-step plan
            subtasks = [
                SubTask(id="s1", description=f"Research: {task}",
                        tool="reasoning", depends_on=[]),
                SubTask(id="s2", description=f"Synthesise answer to: {task}",
                        tool="reasoning", depends_on=["s1"]),
            ]

        return Plan(id=plan_id, original_task=task,
                    complexity=complexity, subtasks=subtasks)

    def _parse_subtasks(self, raw: str) -> List[SubTask]:
        subtasks: List[SubTask] = []
        tool_re  = re.compile(r"\[(\w+)\]")
        num_re   = re.compile(r"^\s*\d+[\.\)]\s*")

        for i, line in enumerate(raw.strip().splitlines()):
            line = line.strip()
            if not line or not re.match(r"^\s*\d", line):
                continue
            tool_m = tool_re.search(line)
            tool   = tool_m.group(1).lower() if tool_m else "reasoning"
            if tool not in ("web", "code", "file", "reasoning", "market"):
                tool = "reasoning"
            desc = tool_re.sub("", num_re.sub("", line)).strip()
            if not desc:
                continue
            # Each step depends on the previous (sequential by default)
            depends = [f"s{i}"] if i > 0 else []
            subtasks.append(SubTask(
                id=f"s{i + 1}", description=desc, tool=tool,
                depends_on=depends,
                timeout_secs=self._subagent_timeout,
            ))

        return subtasks[:6]   # cap at 6 steps

    # ── Execution ─────────────────────────────────────────────────────────────

    async def execute_plan(self, plan: Plan) -> Plan:
        """
        Run all subtasks.
        COMPLEX: parallel where no dependencies; MEDIUM: sequential.
        Adapted from DeerFlow's SubagentExecutor (ThreadPoolExecutor + asyncio).
        """
        plan.status = "running"
        with self._lock:
            self._current_plan = plan

        if plan.complexity == TaskComplexity.COMPLEX:
            await self._execute_parallel(plan)
        else:
            await self._execute_sequential(plan)

        plan.status = "done"
        self._save_plan_to_vault(plan)
        return plan

    async def _execute_sequential(self, plan: Plan) -> None:
        for subtask in plan.subtasks:
            if plan.status == "cancelled":
                break
            await self._run_subtask(subtask, plan)

    async def _execute_parallel(self, plan: Plan) -> None:
        """
        Run independent subtasks in parallel (DeerFlow subagent model).
        A subtask is independent if its depends_on are already done.
        """
        done: set[str] = set()
        pending = list(plan.subtasks)

        while pending and plan.status != "cancelled":
            # Find tasks whose dependencies are all satisfied
            ready = [t for t in pending
                     if all(d in done for d in t.depends_on)]
            if not ready:
                # No tasks ready — wait for one to finish (shouldn't happen with good plans)
                await asyncio.sleep(0.1)
                continue

            # Run ready tasks in parallel, up to max_parallel
            batch = ready[:self._max_parallel]
            for t in batch:
                pending.remove(t)

            results = await asyncio.gather(
                *[self._run_subtask(t, plan) for t in batch],
                return_exceptions=True,
            )

            for t, r in zip(batch, results):
                if isinstance(r, Exception):
                    t.status = "failed"
                    t.result = f"[error: {r}]"
                done.add(t.id)

    async def _run_subtask(self, subtask: SubTask, plan: Plan) -> None:
        """Execute a single subtask using the appropriate tool."""
        subtask.status = "running"
        subtask.started_at = time.monotonic()

        try:
            result = await asyncio.wait_for(
                self._dispatch_tool(subtask, plan),
                timeout=subtask.timeout_secs,
            )
            subtask.result  = result
            subtask.status  = "done"
        except asyncio.TimeoutError:
            subtask.status = "failed"
            subtask.result = f"[timeout after {subtask.timeout_secs}s]"
            log.warning("Planner: subtask '%s' timed out.", subtask.id)
        except Exception as exc:
            subtask.status = "failed"
            subtask.result = f"[error: {exc}]"
            log.warning("Planner: subtask '%s' failed: %s", subtask.id, exc)
        finally:
            subtask.finished_at = time.monotonic()

    async def _dispatch_tool(self, subtask: SubTask, plan: Plan) -> str:
        """Route subtask to the right tool (web, code, reasoning, file, market)."""
        desc        = subtask.description
        prior_ctx   = self._get_prior_results(subtask, plan)
        tool        = subtask.tool

        if tool == "web" and self._web:
            # Web search via existing web module
            return await asyncio.get_event_loop().run_in_executor(
                None, self._web_search, desc)

        if tool == "market" and self._brain:
            prompt = f"Provide market data or analysis for: {desc}"
            return await asyncio.get_event_loop().run_in_executor(
                None, self._brain.ask, prompt)

        # Default: reasoning via brain
        if self._brain:
            context = f"\nPrior results:\n{prior_ctx}\n" if prior_ctx else ""
            prompt  = f"{context}Task: {desc}"
            return await asyncio.get_event_loop().run_in_executor(
                None, self._brain.ask, prompt)

        return f"[no brain available for: {desc}]"

    def _web_search(self, query: str) -> str:
        """Sync web search wrapper for executor."""
        try:
            if self._web:
                return self._web.search(query) or f"No results for: {query}"
        except Exception as exc:
            return f"[search failed: {exc}]"
        return f"[web module not available for: {query}]"

    def _get_prior_results(self, subtask: SubTask, plan: Plan) -> str:
        """Collect results from prerequisite subtasks."""
        prior = []
        for dep_id in subtask.depends_on:
            for t in plan.subtasks:
                if t.id == dep_id and t.result:
                    prior.append(f"[{t.id}] {t.result[:500]}")
        return "\n".join(prior)

    # ── Synthesis (DeerFlow lead agent synthesises subagent results) ──────────

    def synthesise(self, plan: Plan) -> str:
        """Synthesise all subtask results into a final voice-ready response."""
        if not self._brain:
            completed = [t for t in plan.subtasks if t.result]
            if completed:
                return completed[-1].result or "Plan complete, Boss."
            return "Plan complete, Boss."

        results_text = "\n".join(
            f"Step {t.id}: {t.result[:800]}"
            for t in plan.subtasks if t.result
        )
        prompt = (
            f"Original task: {plan.original_task}\n\n"
            f"Completed steps:\n{results_text}\n\n"
            "Synthesise these results into a concise, direct response. "
            "3 sentences maximum for voice delivery. Focus on the key answer."
        )
        try:
            return self._brain.ask(prompt)
        except Exception as exc:
            log.warning("Planner: synthesis failed: %s", exc)
            return "Plan complete, Boss. Check the vault for full details."

    # ── Research mode (DeerFlow deep research pattern) ────────────────────────

    async def research_mode(self, topic: str) -> str:
        """
        Deep research on a topic.
        1. Generates research questions
        2. Searches each in parallel
        3. Extracts key facts
        4. Synthesises structured report
        5. Saves to ATLAS/Research/ vault folder
        """
        self._speak(f"Entering research mode on {topic}. This will take a moment, Boss.")

        # Step 1: Generate research questions
        questions_prompt = (
            f"Generate 4 focused research questions about: {topic}\n"
            "Reply with ONLY a numbered list, one question per line."
        )
        questions: List[str] = []
        try:
            if self._brain:
                raw = self._brain.ask(questions_prompt)
                questions = [re.sub(r"^\d+[\.\)]\s*", "", l).strip()
                             for l in raw.strip().splitlines()
                             if re.match(r"^\s*\d", l)][:4]
        except Exception:
            pass

        if not questions:
            questions = [f"What is {topic}?", f"Latest developments in {topic}?",
                         f"Key challenges in {topic}?", f"Future of {topic}?"]

        # Step 2: Research each question in parallel
        research_tasks = [
            SubTask(id=f"r{i+1}", description=q, tool="web", depends_on=[])
            for i, q in enumerate(questions)
        ]
        research_plan = Plan(
            id=f"research-{uuid.uuid4().hex[:6]}",
            original_task=f"Deep research: {topic}",
            complexity=TaskComplexity.COMPLEX,
            subtasks=research_tasks,
        )

        await self._execute_parallel(research_plan)

        # Step 3: Synthesise into structured report
        findings = "\n\n".join(
            f"Q: {t.description}\nA: {t.result or '[no result]'}"
            for t in research_tasks
        )

        report_prompt = (
            f"Create a structured research report on: {topic}\n\n"
            f"Research findings:\n{findings}\n\n"
            "Format:\n"
            "## Summary\n[2-3 sentence overview]\n\n"
            "## Key Findings\n[bullet points]\n\n"
            "## Open Questions\n[what remains unclear]\n\n"
            "## Related Topics\n[3-4 related areas]\n"
        )

        report = ""
        try:
            if self._brain:
                report = self._brain.ask(report_prompt)
        except Exception as exc:
            log.warning("Research synthesis failed: %s", exc)
            report = findings

        # Step 4: Save to Obsidian vault
        if self._vb:
            self._save_research_report(topic, report)

        # Step 5: Voice summary (first 2 sentences)
        lines = [l.strip() for l in report.split("\n")
                 if l.strip() and not l.startswith("#") and not l.startswith("-")]
        voice_summary = " ".join(lines[:2])[:400]
        return (f"Research complete, Boss. {voice_summary} "
                f"Full report saved to Research folder in your vault.")

    def _save_research_report(self, topic: str, report: str) -> None:
        try:
            from datetime import date
            slug    = re.sub(r"[^\w\s-]", "", topic.lower())[:40].replace(" ", "-")
            fname   = f"{date.today().isoformat()}-{slug}.md"
            folder  = self._vb.vault / "Research"
            folder.mkdir(parents=True, exist_ok=True)
            path    = folder / fname
            path.write_text(
                f"---\ntopic: \"{topic}\"\ndate: {date.today().isoformat()}\n"
                f"tags: [research, atlas]\n---\n\n{report}\n",
                encoding="utf-8",
            )
            log.info("Research report saved: %s", path)
        except Exception as exc:
            log.warning("Could not save research report: %s", exc)

    # ── Vault plan persistence ─────────────────────────────────────────────────

    def _save_plan_to_vault(self, plan: Plan) -> None:
        if not self._vb:
            return
        try:
            folder = self._vb.atlas / "Memory" / "plans"
            folder.mkdir(parents=True, exist_ok=True)
            path   = folder / f"{plan.id}.md"
            steps  = "\n".join(
                f"- [{t.status}] {t.id} ({t.tool}): {t.description}"
                for t in plan.subtasks
            )
            path.write_text(
                f"---\ntask: \"{plan.original_task[:80]}\"\n"
                f"complexity: {plan.complexity.value}\n"
                f"status: {plan.status}\ntags: [atlas, plan]\n---\n\n"
                f"# Plan: {plan.original_task[:80]}\n\n## Steps\n{steps}\n\n"
                f"## Result\n{plan.final_result or '[pending]'}\n",
                encoding="utf-8",
            )
        except Exception as exc:
            log.debug("Could not save plan to vault: %s", exc)

    # ── Sync wrapper ─────────────────────────────────────────────────────────

    def run_sync(self, task: str) -> Optional[str]:
        """
        Sync wrapper around async plan+execute — runs in a new event loop
        on a background thread so it never blocks the Qt event loop.
        Returns final synthesised response string.
        """
        complexity = self.classify_complexity(task)

        if complexity == TaskComplexity.SIMPLE and not self._force_plan:
            return None   # caller should use direct brain response

        plan = self.build_plan(task, complexity)
        log.info("Planner: %s plan built (%d steps) for: %.60s…",
                 complexity.value, len(plan.subtasks), task)

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self.execute_plan(plan))
            loop.close()
        except Exception as exc:
            log.warning("Planner: execution error: %s", exc)
            plan.status = "failed"

        result = self.synthesise(plan)
        plan.final_result = result
        return result

    # ── Voice commands ────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas plan this out", "atlas use planner",
                                     "atlas plan mode", "atlas enter plan mode")):
            self._force_plan   = True
            self._force_direct = False
            return "Planning mode activated, Boss. I'll break down the next task step by step."

        if any(p in lower for p in ("atlas quick answer", "atlas skip planner",
                                     "atlas direct mode", "atlas no planning")):
            self._force_direct = True
            self._force_plan   = False
            return "Direct mode activated, Boss. I'll respond immediately without planning."

        if any(p in lower for p in ("atlas what is your plan", "atlas read the plan",
                                     "atlas show the plan")):
            with self._lock:
                plan = self._current_plan
            if not plan:
                return "No active plan right now, Boss."
            steps = "; ".join(
                f"step {t.id}: {t.description[:50]}" for t in plan.subtasks)
            return (f"Current plan has {len(plan.subtasks)} steps: {steps}. "
                    f"Status: {plan.status}.")

        if any(p in lower for p in ("atlas cancel that plan", "atlas stop the plan",
                                     "atlas cancel plan")):
            with self._lock:
                if self._current_plan:
                    self._current_plan.status = "cancelled"
                    self._force_plan          = False
                    return "Plan cancelled, Boss."
            return "No active plan to cancel."

        if any(p in lower for p in ("atlas how many steps", "atlas step count")):
            with self._lock:
                plan = self._current_plan
            if not plan:
                return "No active plan, Boss."
            done  = sum(1 for t in plan.subtasks if t.status == "done")
            total = len(plan.subtasks)
            return f"Plan has {total} steps — {done} complete, {total - done} remaining."

        # Research mode triggers
        research_m = re.search(
            r"atlas (?:research|deep dive|investigate|deep research) (.+?)(?:\s*$)",
            lower)
        if research_m:
            topic = research_m.group(1).strip()
            if topic:
                threading.Thread(
                    target=lambda: asyncio.run(self.research_mode(topic)),
                    daemon=True, name="atlas-research",
                ).start()
                return f"Starting deep research on {topic}. I'll report back when complete, Boss."

        return None

    def inject(self, brain) -> None:
        """Wire planner into brain.handle so complex tasks are intercepted."""
        _orig = brain.handle
        planner_ref = self

        def _handle_with_planner(text: str) -> str:
            complexity = planner_ref.classify_complexity(text)
            if complexity != TaskComplexity.SIMPLE or planner_ref._force_plan:
                result = planner_ref.run_sync(text)
                if result:
                    # Reset force flags after one use
                    planner_ref._force_plan   = False
                    planner_ref._force_direct = False
                    return result
            # For SIMPLE or when planner returns None, use original brain
            return _orig(text)

        brain.handle = _handle_with_planner
        log.info("ATLASPlanner: injected into brain.handle.")
