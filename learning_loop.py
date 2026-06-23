"""
ATLAS Learning Loop — post-task skill extraction and success tracking.

After each ATLAS response, evaluates whether the exchange warrants:
  • Writing or updating a skill in the vault
  • Recording an outcome on an existing skill
  • Scheduling a 7-day review of low-success-rate skills

Complexity scoring (0–10):
  ≥ 6 → write skill
  ≥ 4 → check if matching skill exists and append outcome
  < 4 → no action

Skills written to ATLAS/Skills/ via VaultBrain (single .md format).
Hermes directory-based skills updated via HermesSkillsModule if available.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

_SKILL_COMPLEXITY_THRESHOLD = 6   # score out of 10 to write a new skill
_OUTCOME_THRESHOLD          = 4   # score to record outcome on existing skill
_LOW_SUCCESS_RATE           = 0.5  # skills below this rate get 7-day review
_REVIEW_DAYS                = 7


def _score_complexity(user_msg: str, assistant_response: str) -> int:
    """
    Heuristic complexity score (0–10).
    High score = multi-step, technical, or novel task worth capturing.
    """
    score = 0
    combined = (user_msg + " " + assistant_response).lower()

    # Length signals complexity
    if len(assistant_response) > 800:
        score += 2
    elif len(assistant_response) > 400:
        score += 1

    # Technical content
    if any(w in combined for w in ("def ", "class ", "import ", "pip ", "function", "module")):
        score += 2
    if any(w in combined for w in ("step", "first", "then", "finally", "next",
                                    "1.", "2.", "3.")):
        score += 1

    # Multi-part request
    if user_msg.count("?") >= 2 or len(user_msg.split()) >= 20:
        score += 1

    # Novel keywords (not simple Q&A)
    novel_keywords = ("build", "create", "implement", "write a", "set up",
                      "configure", "design", "integrate", "fix", "debug",
                      "upgrade", "automate", "schedule")
    if any(kw in combined for kw in novel_keywords):
        score += 2

    # Short answer = low complexity
    if len(assistant_response) < 100:
        score = max(0, score - 2)

    return min(score, 10)


def _extract_task_name(user_msg: str) -> str:
    """Extract a concise skill name from a user message."""
    lower = user_msg.lower().strip()
    for prefix in ("how do i ", "how to ", "help me ", "can you ",
                   "please ", "atlas ", "write ", "build ", "create ",
                   "implement ", "set up ", "explain "):
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
    # Trim to first clause
    for sep in ("?", ".", "and then", " so that", " in order"):
        if sep in lower:
            lower = lower.split(sep)[0]
    return lower.strip()[:60] or "task"


class LearningLoop:
    """
    Post-turn learning engine.

    Wire into brain.handle via main.py:
        ll = LearningLoop(brain, memory_mod, vault_brain, hermes_skills)
        ll.evaluate(user_msg, atlas_response)   # called after each turn
    """

    def __init__(
        self,
        brain=None,
        memory_module=None,
        vault_brain=None,
        hermes_skills=None,
    ):
        self._brain   = brain
        self._mem     = memory_module
        self._vb      = vault_brain
        self._hermes  = hermes_skills
        self._lock    = threading.Lock()
        self._last_review_date: Optional[date] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def evaluate(self, user_msg: str, atlas_response: str) -> None:
        """
        Evaluate a user/assistant exchange and trigger learning actions.
        Runs in background to avoid blocking the voice pipeline.
        """
        threading.Thread(
            target=self._evaluate_bg,
            args=(user_msg, atlas_response),
            daemon=True, name="atlas-learning-loop",
        ).start()

    # ── Background evaluation ──────────────────────────────────────────────────

    def _evaluate_bg(self, user_msg: str, atlas_response: str) -> None:
        try:
            score = _score_complexity(user_msg, atlas_response)
            log.debug("LearningLoop: complexity score=%d for '%s...'", score, user_msg[:50])

            if score >= _SKILL_COMPLEXITY_THRESHOLD:
                self._maybe_write_skill(user_msg, atlas_response)
            elif score >= _OUTCOME_THRESHOLD:
                self._maybe_record_outcome(user_msg, atlas_response)

            # Periodic 7-day review
            self._maybe_run_low_success_review()
        except Exception as exc:
            log.debug("LearningLoop: error in bg evaluation: %s", exc)

    def _maybe_write_skill(self, user_msg: str, atlas_response: str) -> None:
        """Write a new skill if the task warrants it and no matching skill exists."""
        if self._vb is None:
            return

        task_name = _extract_task_name(user_msg)

        # Check if skill already exists
        existing = self._vb.get_skill(task_name)
        if existing:
            # Update outcome on existing skill instead
            self._update_existing_skill_outcome(task_name, atlas_response, success=True)
            return

        # Ask LLM to extract skill components
        if self._brain and self._brain.smart_available:
            skill_data = self._extract_skill_via_llm(user_msg, atlas_response)
            if skill_data:
                try:
                    self._vb.write_skill(
                        name         = skill_data.get("name", task_name),
                        task_type    = skill_data.get("type", "general"),
                        trigger      = skill_data.get("trigger", user_msg[:200]),
                        steps        = skill_data.get("steps", atlas_response[:500]),
                        outcome      = skill_data.get("outcome", "Task completed successfully."),
                        pitfalls_text= skill_data.get("pitfalls", "None documented yet."),
                    )
                    log.info("LearningLoop: new skill written — %s", skill_data.get("name"))
                    return
                except Exception as exc:
                    log.warning("LearningLoop: skill write failed: %s", exc)

        # Fallback: write minimal skill from raw exchange
        try:
            steps = "\n".join(
                f"{i+1}. {line.strip()}"
                for i, line in enumerate(atlas_response.split(". ")[:5])
                if line.strip()
            )
            self._vb.write_skill(
                name         = task_name,
                task_type    = "general",
                trigger      = user_msg[:200],
                steps        = steps or atlas_response[:400],
                outcome      = "Completed.",
                pitfalls_text= "None documented yet.",
            )
            log.info("LearningLoop: minimal skill written — %s", task_name)
        except Exception as exc:
            log.warning("LearningLoop: minimal skill write failed: %s", exc)

    def _maybe_record_outcome(self, user_msg: str, atlas_response: str) -> None:
        """Append outcome to an existing matching skill."""
        task_name = _extract_task_name(user_msg)
        self._update_existing_skill_outcome(task_name, atlas_response, success=True)

    def _update_existing_skill_outcome(self, query: str, outcome_text: str,
                                       success: bool = True) -> None:
        """Append outcome line to matching vault skill file."""
        if self._vb is None:
            return
        try:
            # Find skill by keyword match
            for p in self._vb.list_notes(self._vb.skills_dir):
                result = self._vb.read_note(p)
                if not result:
                    continue
                fm, body = result
                task_type = str(fm.get("task_type", p.stem))
                search_in = task_type + " " + body
                score = sum(1 for w in query.lower().split()
                            if len(w) > 3 and w in search_in.lower())
                if score >= 2:
                    today  = date.today().isoformat()
                    flag   = "✓" if success else "✗"
                    line   = f"- [{today}] {flag} {outcome_text[:200]}"
                    if "## Outcome" in body:
                        body = body.rstrip() + "\n" + line + "\n"
                    else:
                        body = body.rstrip() + f"\n\n## Outcome\n{line}\n"
                    times_used = int(fm.get("times_used", 0)) + 1
                    fm["times_used"] = times_used
                    fm["last_used"]  = today
                    self._vb.write_note(p, fm, body)
                    log.debug("LearningLoop: outcome recorded on %s", p.name)
                    return
        except Exception as exc:
            log.debug("LearningLoop: outcome update failed: %s", exc)

    def _extract_skill_via_llm(self, user_msg: str, atlas_response: str) -> Optional[dict]:
        """Ask the LLM to extract structured skill components from an exchange."""
        prompt = (
            "Extract a reusable skill from this Q&A. Return JSON only:\n"
            '{"name":"<short skill name>","type":"<coding|research|system|general>",'
            '"trigger":"<when to use>","steps":"<numbered steps>",'
            '"outcome":"<expected result>","pitfalls":"<what to avoid>"}\n\n'
            f"USER: {user_msg[:300]}\n"
            f"ATLAS: {atlas_response[:600]}"
        )
        try:
            import json
            raw = self._brain.ask(prompt)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(clean)
        except Exception as exc:
            log.debug("LearningLoop: LLM skill extraction failed: %s", exc)
            return None

    # ── 7-day low-success-rate review ─────────────────────────────────────────

    def _maybe_run_low_success_review(self) -> None:
        today = date.today()
        if self._last_review_date == today:
            return
        if self._vb is None:
            return

        # Only run once per day
        self._last_review_date = today

        try:
            low_rate_skills = []
            for p in self._vb.list_notes(self._vb.skills_dir):
                result = self._vb.read_note(p)
                if not result:
                    continue
                fm, body = result
                rate      = float(fm.get("success_rate", 1.0))
                times     = int(fm.get("times_used", 0))
                # Only flag skills used ≥3 times with low success
                if times >= 3 and rate < _LOW_SUCCESS_RATE:
                    low_rate_skills.append((p, fm, body, rate))

            if not low_rate_skills:
                return

            log.info("LearningLoop: %d low-success-rate skills found for review.",
                     len(low_rate_skills))

            for p, fm, body, rate in low_rate_skills[:3]:
                self._improve_low_success_skill(p, fm, body, rate)
        except Exception as exc:
            log.debug("LearningLoop: 7-day review failed: %s", exc)

    def _improve_low_success_skill(self, path: Path, fm: dict, body: str, rate: float) -> None:
        """Ask LLM to suggest improvements to a low-success-rate skill."""
        if self._brain is None or not self._brain.smart_available:
            return
        try:
            name = fm.get("title", path.stem)
            prompt = (
                f"This skill has a {int(rate*100)}% success rate:\n\n{body[:800]}\n\n"
                "Suggest 2-3 improvements to the Steps or What to Avoid sections. "
                "Return plain text, starting with 'Improved Steps:' or 'Additional Pitfalls:'."
            )
            suggestions = self._brain.ask(prompt)
            if suggestions:
                improvement_note = (
                    f"\n\n## Learning Loop Review — {date.today().isoformat()}\n"
                    f"Success rate was {int(rate*100)}%. Suggested improvements:\n{suggestions[:500]}\n"
                )
                body_updated = body.rstrip() + improvement_note
                self._vb.write_note(path, fm, body_updated)
                log.info("LearningLoop: improved skill '%s' (was %d%% success).", name, int(rate*100))
        except Exception as exc:
            log.debug("LearningLoop: skill improvement failed: %s", exc)

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas run skill review", "atlas review low skills",
                                     "atlas improve low success skills")):
            if self._vb is None:
                return "No vault connected, Boss."
            self._last_review_date = None  # force re-run
            self._maybe_run_low_success_review()
            return "Skill review running in the background, Boss."

        return None
