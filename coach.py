"""
ATLAS Personal Coach — goal setting, 30-day plans, daily check-ins, progress tracking.

All data stored in Obsidian vault under ATLAS/Coaching/{goal-slug}/

Voice commands:
  "ATLAS coach me on X"           → start new coaching goal
  "ATLAS check in on X"           → manual check-in
  "ATLAS how am I doing with X"   → progress report
  "ATLAS show my coaching progress" → Smart Card
  "ATLAS I completed today's goal" → log completion
  "ATLAS I missed today"           → log miss
  "ATLAS what are my active goals" → list all goals
  "ATLAS pause coaching on X"      → suspend goal
  "ATLAS I am done with X"         → archive goal
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

_ONBOARDING_QUESTIONS = [
    "What specifically do you want to achieve with {topic}?",
    "Why is this important to you?",
    "What does success look like in 30 days?",
    "What has stopped you before?",
    "How much time can you commit daily?",
]

_PLAN_PROMPT = """\
You are a personal coach. Based on these onboarding answers, create a structured 30-day plan \
for the goal: {topic}

Answers:
{answers}

Write a 30-day plan in exactly this format:
Week 1 — Foundation:
- Task 1
- Task 2
- Task 3

Week 2 — Building momentum:
- Task 1
- Task 2
- Task 3

Week 3 — Deepening:
- Task 1
- Task 2
- Task 3

Week 4 — Consolidation:
- Task 1
- Task 2
- Task 3

Be specific and actionable. Tailor tasks to the answers provided. Max 250 words."""

_PROGRESS_PROMPT = """\
You are a personal coach reviewing progress. Goal: {topic}
Days completed: {completed}/{total}. Current streak: {streak}. Longest streak: {longest}.
Recent blockers: {blockers}

Give an encouraging 2-sentence progress update for voice output. Be specific and honest.
Reference the streak and percentage. If blockers exist, acknowledge one directly."""


@dataclass
class CoachingGoal:
    name: str
    topic: str
    start_date: str        # ISO date string
    plan_days: int = 30
    vault_folder: str = ""
    active: bool = True
    paused: bool = False


@dataclass
class DailyCheckin:
    date: str              # ISO date
    completed: str         # yes | no | partial
    blocker: str = ""
    notes: str = ""


class ATLASCoach:
    """Personal coaching system — goal-setting, check-ins, progress tracking."""

    def __init__(self, config: dict, speak_cb: Callable, brain, vault_brain):
        self._config      = config
        self._speak       = speak_cb
        self._brain       = brain
        self._vault_brain = vault_brain
        self._max_goals   = int(config.get("coaching_max_active_goals", 5))
        self._plan_days   = int(config.get("coaching_plan_duration_days", 30))

        # Conversational state machines
        self._onboarding_state: Optional[Dict] = None   # tracks onboarding flow
        self._checkin_state: Optional[Dict]    = None   # tracks check-in flow

        # Active goals cache
        self._goals: List[CoachingGoal] = []
        self._load_goals()

        log.info("ATLASCoach: ready (%d active goals).", len(self._goals))

    # ── Voice router ──────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()
        lower_clean = re.sub(r"^atlas\s+", "", lower)

        # ── Active onboarding flow — absorb next voice input as answer ─────────
        if self._onboarding_state is not None:
            return self._on_onboarding_answer(text)

        # ── Active check-in flow ───────────────────────────────────────────────
        if self._checkin_state is not None:
            return self._on_checkin_answer(text)

        # ── Start new coaching goal ────────────────────────────────────────────
        m = re.search(r"coach me on (.+?)$", lower_clean)
        if m:
            return self._start_onboarding(m.group(1).strip())

        # ── Manual check-in ───────────────────────────────────────────────────
        m = re.search(r"check in on (.+?)$", lower_clean)
        if m:
            return self._start_checkin(m.group(1).strip())

        # ── Progress report ───────────────────────────────────────────────────
        m = re.search(r"how am i doing with (.+?)$", lower_clean)
        if m:
            return self._progress_report(m.group(1).strip())

        # ── Log completion ─────────────────────────────────────────────────────
        if any(p in lower_clean for p in ("i completed today", "completed today's goal",
                                           "i finished today", "done today")):
            return self._log_today("yes", "")

        # ── Log miss ──────────────────────────────────────────────────────────
        if any(p in lower_clean for p in ("i missed today", "didn't do it today",
                                           "failed today", "skipped today")):
            return self._log_today("no", "")

        # ── List goals ────────────────────────────────────────────────────────
        if any(p in lower_clean for p in ("what are my active goals",
                                           "list my goals", "show my goals")):
            return self._list_goals()

        # ── Show coaching progress card ────────────────────────────────────────
        if any(p in lower_clean for p in ("show my coaching progress",
                                           "coaching progress", "show coaching")):
            return self._progress_card()

        # ── Pause goal ────────────────────────────────────────────────────────
        m = re.search(r"pause coaching on (.+?)$", lower_clean)
        if m:
            return self._pause_goal(m.group(1).strip())

        # ── Complete and archive ───────────────────────────────────────────────
        m = re.search(r"i am done with (.+?)$", lower_clean)
        if m:
            return self._archive_goal(m.group(1).strip())

        m = re.search(r"adjust my plan for (.+?)$", lower_clean)
        if m:
            return self._adjust_plan(m.group(1).strip())

        return None

    # ── Onboarding ────────────────────────────────────────────────────────────

    def _start_onboarding(self, topic: str) -> str:
        if len([g for g in self._goals if g.active and not g.paused]) >= self._max_goals:
            return (f"You already have {self._max_goals} active goals, Boss. "
                    f"Complete or pause one before adding another.")
        self._onboarding_state = {
            "topic": topic,
            "q_index": 0,
            "answers": [],
        }
        first_q = _ONBOARDING_QUESTIONS[0].format(topic=topic)
        return f"Excellent, Boss. Let's set up coaching for {topic}. {first_q}"

    def _on_onboarding_answer(self, answer: str) -> str:
        state = self._onboarding_state
        state["answers"].append(answer)
        state["q_index"] += 1

        if state["q_index"] < len(_ONBOARDING_QUESTIONS):
            q = _ONBOARDING_QUESTIONS[state["q_index"]].format(topic=state["topic"])
            return q

        # All 5 answers collected — generate plan
        self._onboarding_state = None
        import threading
        t = threading.Thread(
            target=self._generate_plan_async,
            args=(state["topic"], state["answers"]),
            daemon=True, name="atlas-coach-plan")
        t.start()
        return (f"Perfect, Boss. I have everything I need. "
                f"Generating your 30-day plan for {state['topic']} now.")

    def _generate_plan_async(self, topic: str, answers: List[str]) -> None:
        answers_text = "\n".join(
            f"Q{i+1}: {_ONBOARDING_QUESTIONS[i].format(topic=topic)}\nA: {a}"
            for i, a in enumerate(answers))
        prompt = _PLAN_PROMPT.format(topic=topic, answers=answers_text)
        plan_text = ""
        try:
            plan_text = self._brain.ask(prompt)
        except Exception as exc:
            log.error("Coach: plan generation failed: %s", exc)
            plan_text = "Week 1 — Foundation:\n- Start daily practice\n- Set baseline\n- Track progress"

        # Create goal
        slug = re.sub(r"[^\w\s-]", "", topic.lower()).replace(" ", "-")[:30]
        goal = CoachingGoal(
            name=slug, topic=topic,
            start_date=date.today().isoformat(),
            plan_days=self._plan_days,
            vault_folder=f"Coaching/{slug}",
        )
        self._goals.append(goal)
        self._save_goal_to_vault(goal, answers_text, plan_text)
        self._speak(
            f"Your 30-day coaching plan for {topic} is ready, Boss. "
            f"I have saved it to your Obsidian vault under Coaching. "
            f"Check in tomorrow morning to begin."
        )

    def _save_goal_to_vault(self, goal: CoachingGoal,
                             answers: str, plan: str) -> None:
        if not self._vault_brain:
            return
        try:
            folder = self._vault_brain.atlas / goal.vault_folder
            folder.mkdir(parents=True, exist_ok=True)

            (folder / "goal.md").write_text(
                f"---\ntags: [coaching, goal]\ndate: {goal.start_date}\n---\n\n"
                f"# Goal: {goal.topic}\n\n"
                f"**Started:** {goal.start_date}  \n"
                f"**Duration:** {goal.plan_days} days\n\n"
                f"## Onboarding\n{answers}\n",
                encoding="utf-8")

            (folder / "plan.md").write_text(
                f"---\ntags: [coaching, plan]\ndate: {goal.start_date}\n---\n\n"
                f"# 30-Day Plan: {goal.topic}\n\n{plan}\n",
                encoding="utf-8")

            (folder / "progress.md").write_text(
                f"---\ntags: [coaching, progress]\n---\n\n"
                f"# Progress: {goal.topic}\n\n"
                f"| Date | Status | Blocker |\n"
                f"|------|--------|--------|\n",
                encoding="utf-8")
        except Exception as exc:
            log.error("Coach: vault save failed: %s", exc)

    # ── Daily check-in ────────────────────────────────────────────────────────

    def _start_checkin(self, topic_hint: str = "") -> str:
        goal = self._find_goal(topic_hint)
        if not goal:
            if topic_hint:
                return f"No active coaching goal matching '{topic_hint}', Boss."
            if not self._goals:
                return "No active coaching goals, Boss. Say 'ATLAS coach me on X' to start."
            goal = next((g for g in self._goals if g.active and not g.paused), None)
            if not goal:
                return "No active coaching goals right now, Boss."

        self._checkin_state = {
            "goal": goal,
            "step": "completed",
        }
        yesterday = "yesterday's task"
        return (f"Check-in for {goal.topic}, Boss. "
                f"Did you complete {yesterday}? Say yes, no, or partial.")

    def _on_checkin_answer(self, answer: str) -> str:
        state = self._checkin_state
        goal = state["goal"]
        lower = answer.lower()

        if state["step"] == "completed":
            if any(w in lower for w in ("yes", "did it", "done", "completed",
                                         "finished", "yep", "yeah")):
                completed = "yes"
            elif any(w in lower for w in ("partial", "sort of", "almost",
                                           "half", "partly", "kind of")):
                completed = "partial"
            else:
                completed = "no"
                state["step"] = "blocker"
                self._checkin_state["completed"] = completed
                return "What got in the way yesterday?"

            self._record_checkin(goal, completed, "")
            self._checkin_state = None
            return self._checkin_response(goal, completed)

        elif state["step"] == "blocker":
            blocker = answer.strip()
            completed = state.get("completed", "no")
            self._record_checkin(goal, completed, blocker)
            self._checkin_state = None
            return (f"Noted, Boss. I have logged that. "
                    f"Tomorrow is a fresh start for {goal.topic}.")

        self._checkin_state = None
        return "Check-in recorded, Boss."

    def _checkin_response(self, goal: CoachingGoal, completed: str) -> str:
        streak = self._get_streak(goal)
        if completed == "yes":
            if streak >= 5:
                return (f"Excellent work, Boss. That is {streak} days in a row on {goal.topic}. "
                        f"You are building serious momentum.")
            return (f"Well done, Boss. Keep it up on {goal.topic}. "
                    f"Current streak: {streak} days.")
        elif completed == "partial":
            return (f"Partial counts, Boss. Progress on {goal.topic} is still progress. "
                    f"Aim for completion today.")
        return (f"No problem, Boss. Setbacks are data, not failures. "
                f"What will you do differently today on {goal.topic}?")

    def _record_checkin(self, goal: CoachingGoal,
                         completed: str, blocker: str) -> None:
        if not self._vault_brain:
            return
        try:
            folder = self._vault_brain.atlas / goal.vault_folder
            prog = folder / "progress.md"
            today = date.today().isoformat()
            line = f"| {today} | {completed} | {blocker} |\n"
            if prog.exists():
                content = prog.read_text(encoding="utf-8")
                prog.write_text(content + line, encoding="utf-8")
        except Exception as exc:
            log.error("Coach: checkin save failed: %s", exc)

    def do_morning_checkin(self) -> None:
        """Called by scheduler at coaching_checkin_time."""
        active = [g for g in self._goals if g.active and not g.paused]
        if not active:
            return
        for goal in active:
            resp = self._start_checkin(goal.topic)
            if resp:
                self._speak(resp)
                break   # handle one at a time; coach will chain via state machine

    # ── Progress ──────────────────────────────────────────────────────────────

    def _progress_report(self, topic: str) -> str:
        goal = self._find_goal(topic)
        if not goal:
            return f"No active coaching goal matching '{topic}', Boss."

        stats = self._load_stats(goal)
        prompt = _PROGRESS_PROMPT.format(
            topic=goal.topic,
            completed=stats["completed"],
            total=self._plan_days,
            streak=stats["streak"],
            longest=stats["longest"],
            blockers=", ".join(stats["blockers"][:3]) or "none",
        )
        try:
            return self._brain.ask(prompt)
        except Exception:
            pct = int(stats["completed"] / max(self._plan_days, 1) * 100)
            return (f"You are {pct}% through your {goal.topic} goal, Boss. "
                    f"Streak: {stats['streak']} days.")

    def _progress_card(self) -> str:
        if not self._goals:
            return "No active coaching goals, Boss."
        lines = []
        for g in self._goals:
            if not g.active:
                continue
            s = self._load_stats(g)
            pct = int(s["completed"] / max(self._plan_days, 1) * 100)
            status = "⏸ paused" if g.paused else f"🔥 streak {s['streak']}"
            lines.append(f"{g.topic}: {pct}% complete | {status}")
        return "Here are your active goals, Boss. " + ". ".join(lines) + "."

    def _list_goals(self) -> str:
        active = [g for g in self._goals if g.active and not g.paused]
        paused = [g for g in self._goals if g.active and g.paused]
        if not active and not paused:
            return "No active coaching goals, Boss. Say 'ATLAS coach me on X' to start one."
        parts = []
        if active:
            parts.append("Active: " + ", ".join(g.topic for g in active))
        if paused:
            parts.append("Paused: " + ", ".join(g.topic for g in paused))
        return ". ".join(parts) + "."

    def _log_today(self, completed: str, blocker: str) -> str:
        active = [g for g in self._goals if g.active and not g.paused]
        if not active:
            return "No active coaching goals, Boss."
        for goal in active:
            self._record_checkin(goal, completed, blocker)
        return self._checkin_response(active[0], completed)

    def _pause_goal(self, topic: str) -> str:
        goal = self._find_goal(topic)
        if not goal:
            return f"No active goal matching '{topic}', Boss."
        goal.paused = True
        return f"Coaching on {goal.topic} paused, Boss. Say 'ATLAS check in on {goal.topic}' to resume."

    def _archive_goal(self, topic: str) -> str:
        goal = self._find_goal(topic)
        if not goal:
            return f"No active goal matching '{topic}', Boss."
        goal.active = False
        return (f"Congratulations on completing {goal.topic}, Boss! "
                f"That goal has been archived in your Obsidian vault.")

    def _adjust_plan(self, topic: str) -> str:
        goal = self._find_goal(topic)
        if not goal:
            return f"No active goal matching '{topic}', Boss."
        import threading
        threading.Thread(target=self._regen_plan_async, args=(goal,),
                         daemon=True).start()
        return f"Analysing your progress and adjusting the plan for {goal.topic}, Boss."

    def _regen_plan_async(self, goal: CoachingGoal) -> None:
        stats = self._load_stats(goal)
        prompt = (
            f"A user is coaching for: {goal.topic}. "
            f"Days completed: {stats['completed']}/{self._plan_days}. "
            f"Streak: {stats['streak']}. Blockers: {stats['blockers']}. "
            f"Write an adjusted 4-week plan that accounts for these patterns. "
            f"Be specific and actionable. Max 200 words."
        )
        try:
            new_plan = self._brain.ask(prompt)
            if self._vault_brain:
                folder = self._vault_brain.atlas / goal.vault_folder
                plan_file = folder / "plan.md"
                if plan_file.exists():
                    old = plan_file.read_text(encoding="utf-8")
                    plan_file.write_text(
                        old + f"\n\n## Adjusted Plan ({date.today()})\n{new_plan}\n",
                        encoding="utf-8")
            self._speak(f"Your plan for {goal.topic} has been adjusted, Boss. "
                        f"Check your Obsidian vault for the updated schedule.")
        except Exception as exc:
            log.error("Coach: plan adjustment failed: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_goal(self, topic: str) -> Optional[CoachingGoal]:
        if not topic:
            return next((g for g in self._goals if g.active and not g.paused), None)
        topic_lower = topic.lower()
        for g in self._goals:
            if g.active and (topic_lower in g.topic.lower()
                             or g.name in topic_lower):
                return g
        return None

    def _load_goals(self) -> None:
        if not self._vault_brain:
            return
        try:
            coaching_root = self._vault_brain.atlas / "Coaching"
            if not coaching_root.exists():
                return
            for subfolder in coaching_root.iterdir():
                goal_file = subfolder / "goal.md"
                if goal_file.exists():
                    topic = subfolder.name.replace("-", " ")
                    try:
                        content = goal_file.read_text(encoding="utf-8")
                        m = re.search(r"# Goal: (.+)", content)
                        if m:
                            topic = m.group(1).strip()
                    except Exception:
                        pass
                    self._goals.append(CoachingGoal(
                        name=subfolder.name,
                        topic=topic,
                        start_date=date.today().isoformat(),
                        vault_folder=f"Coaching/{subfolder.name}",
                    ))
        except Exception as exc:
            log.debug("Coach: load goals: %s", exc)

    def _load_stats(self, goal: CoachingGoal) -> dict:
        completed = 0
        streak = 0
        longest = 0
        current_streak = 0
        blockers = []
        if self._vault_brain:
            try:
                folder = self._vault_brain.atlas / goal.vault_folder
                prog = folder / "progress.md"
                if prog.exists():
                    for line in prog.read_text(encoding="utf-8").splitlines():
                        if line.startswith("|") and "Date" not in line and "---" not in line:
                            parts = [p.strip() for p in line.split("|") if p.strip()]
                            if len(parts) >= 2:
                                status = parts[1]
                                if status in ("yes", "partial"):
                                    completed += 1
                                    current_streak += 1
                                    longest = max(longest, current_streak)
                                else:
                                    current_streak = 0
                                if len(parts) >= 3 and parts[2]:
                                    blockers.append(parts[2])
                    streak = current_streak
            except Exception:
                pass
        return {"completed": completed, "streak": streak,
                "longest": longest, "blockers": blockers}

    def _get_streak(self, goal: CoachingGoal) -> int:
        return self._load_stats(goal)["streak"]
