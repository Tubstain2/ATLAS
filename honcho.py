"""
ATLAS Honcho — user modeling and adaptive personality.

Every N sessions (default 5), reads the last N session summaries and
runs an LLM analysis to extract:
  • Productive times of day
  • Preferred task types (coding, research, market, etc.)
  • Communication style (terse, verbose, formal, casual)
  • Recurring frustrations or friction points
  • Long-term goals and projects
  • Learning patterns

All extracted data is written to atlas-knows-you.md in the vault,
so ATLAS progressively builds a richer model of the user over time.

Voice commands:
  "ATLAS what have you learned about how I work"
  "ATLAS update your model of me"
  "ATLAS show your user model"
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

_DEFAULT_UPDATE_EVERY = 5   # sessions between Honcho updates


class HonchoModule:
    """
    User modeling engine — runs after every N sessions.

    Usage:
        honcho = HonchoModule(vault_brain, brain, config)
        honcho.on_session_end()    # call after each session; updates if N reached
    """

    def __init__(self, vault_brain=None, brain=None, config: dict = None):
        self._vb          = vault_brain
        self._brain       = brain
        self._config      = config or {}
        self._update_every = int(self._config.get("honcho_update_every_sessions",
                                                    _DEFAULT_UPDATE_EVERY))
        self._lock        = threading.Lock()

    # ── Session tracking ───────────────────────────────────────────────────────

    def on_session_end(self) -> None:
        """
        Called at session shutdown. Checks session count; triggers update
        if N sessions have elapsed since last Honcho analysis.
        """
        if self._vb is None:
            return
        session_count = self._get_session_count()
        last_honcho   = self._get_last_honcho_session()
        sessions_since = session_count - last_honcho

        if sessions_since >= self._update_every:
            log.info("Honcho: %d sessions since last update — triggering user model refresh.",
                     sessions_since)
            threading.Thread(
                target=self._run_update,
                daemon=True, name="atlas-honcho-update",
            ).start()

    def _get_session_count(self) -> int:
        if not self._vb:
            return 0
        try:
            result = self._vb.read_note(self._vb.semantic_path)
            if result:
                fm, _ = result
                return int(fm.get("total_sessions", 0))
        except Exception:
            pass
        return 0

    def _get_last_honcho_session(self) -> int:
        if not self._vb:
            return 0
        try:
            result = self._vb.read_note(self._vb.semantic_path)
            if result:
                fm, _ = result
                return int(fm.get("last_honcho_session", 0))
        except Exception:
            pass
        return 0

    def _set_last_honcho_session(self, session_count: int) -> None:
        if not self._vb:
            return
        try:
            self._vb.update_frontmatter(
                self._vb.semantic_path,
                {"last_honcho_session": session_count}
            )
        except Exception as exc:
            log.debug("Honcho: failed to update last_honcho_session: %s", exc)

    # ── Core analysis ──────────────────────────────────────────────────────────

    def _run_update(self) -> None:
        """Read last N session summaries, run LLM analysis, update vault."""
        with self._lock:
            try:
                episodes = self._load_recent_episodes()
                if not episodes:
                    log.info("Honcho: no episodes to analyse.")
                    return

                model = self._extract_user_model(episodes)
                if model:
                    self._write_model_to_vault(model)
                    self._set_last_honcho_session(self._get_session_count())
                    log.info("Honcho: user model updated.")
            except Exception as exc:
                log.warning("Honcho: update failed: %s", exc)

    def _load_recent_episodes(self) -> List[dict]:
        """Load the last N episodic session files."""
        if not self._vb:
            return []
        episodes = []
        for p in sorted(self._vb.episodic_dir.glob("*.md"), reverse=True):
            result = self._vb.read_note(p)
            if result:
                fm, body = result
                episodes.append({"date": fm.get("date", ""), "body": body,
                                  "mood": fm.get("mood", ""), "tags": fm.get("tags", [])})
                if len(episodes) >= self._update_every:
                    break
        return episodes

    def _extract_user_model(self, episodes: List[dict]) -> Optional[dict]:
        """Use LLM to extract structured user model from session summaries."""
        if self._brain is None or not self._brain.smart_available:
            return self._heuristic_model(episodes)

        summaries = "\n\n".join(
            f"--- Session {ep['date']} (mood: {ep['mood']}) ---\n{ep['body'][:500]}"
            for ep in episodes
        )
        prompt = (
            "Analyse these ATLAS session summaries and extract a user model. "
            "Return JSON only:\n"
            '{"productive_times":["<time patterns>"],'
            '"preferred_tasks":["<task types>"],'
            '"communication_style":"<brief description>",'
            '"frustrations":["<recurring issues>"],'
            '"long_term_goals":["<goals mentioned>"],'
            '"learning_patterns":"<how they prefer to learn>",'
            '"work_style":"<brief characterisation>"}\n\n'
            f"Sessions:\n{summaries[:3000]}"
        )
        try:
            raw   = self._brain.ask(prompt)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(clean)
        except Exception as exc:
            log.debug("Honcho: LLM extraction failed (%s) — using heuristic.", exc)
            return self._heuristic_model(episodes)

    def _heuristic_model(self, episodes: List[dict]) -> dict:
        """Fallback: extract model without LLM using keyword frequency."""
        task_types: dict[str, int] = {}
        moods: list[str] = []
        goals: list[str] = []

        for ep in episodes:
            body  = ep.get("body", "").lower()
            mood  = ep.get("mood", "")
            tags  = ep.get("tags", [])
            if mood:
                moods.append(mood)
            for tag in tags:
                if tag not in ("atlas", "episodic"):
                    task_types[tag] = task_types.get(tag, 0) + 1
            if "build" in body or "create" in body:
                goals.append("Building software tools")
            if "market" in body or "stock" in body:
                goals.append("Following financial markets")

        top_tasks = sorted(task_types, key=task_types.get, reverse=True)[:3]
        mood_mode = max(set(moods), key=moods.count) if moods else "productive"

        return {
            "preferred_tasks":      top_tasks,
            "communication_style":  "direct",
            "frustrations":         [],
            "long_term_goals":      list(set(goals))[:3],
            "learning_patterns":    "hands-on, example-first",
            "work_style":           mood_mode,
            "productive_times":     [],
        }

    def _write_model_to_vault(self, model: dict) -> None:
        """Write or update the user model sections in atlas-knows-you.md."""
        if not self._vb:
            return

        today = date.today().isoformat()

        # Write individual facts to atlas-knows-you.md
        for goal in model.get("long_term_goals", [])[:3]:
            if goal:
                self._vb.add_fact(f"Long-term goal: {goal}", section="Goals")

        for frustration in model.get("frustrations", [])[:2]:
            if frustration:
                self._vb.add_fact(f"Recurring friction: {frustration}", section="Notes")

        style = model.get("communication_style", "")
        if style:
            self._vb.add_preference(f"Communication style: {style}", strength="strong", confidence=0.85)

        work_style = model.get("work_style", "")
        if work_style:
            self._vb.add_fact(f"Work style: {work_style}", section="Identity")

        # Write a Honcho summary note
        honcho_path = self._vb.atlas / "Memory" / "honcho-model.md"
        fm = {
            "last_analysis":  today,
            "sessions_analysed": len(model),
            "tags":           ["atlas", "user-model", "honcho"],
        }
        preferred = ", ".join(model.get("preferred_tasks", []))
        times     = ", ".join(model.get("productive_times", [])) or "not yet determined"
        goals_str = "\n".join(f"- {g}" for g in model.get("long_term_goals", []))
        frustrations_str = "\n".join(f"- {f}" for f in model.get("frustrations", []))

        body = (
            f"# ATLAS User Model — Updated {today}\n\n"
            f"## Preferred Tasks\n{preferred or 'Not yet determined'}\n\n"
            f"## Productive Times\n{times}\n\n"
            f"## Communication Style\n{model.get('communication_style','unknown')}\n\n"
            f"## Work Style\n{model.get('work_style','unknown')}\n\n"
            f"## Learning Patterns\n{model.get('learning_patterns','unknown')}\n\n"
            f"## Long-Term Goals\n{goals_str or '- Not yet captured'}\n\n"
            f"## Recurring Frustrations\n{frustrations_str or '- None identified'}\n\n"
            f"## Links\n"
            f"- [[ATLAS/Memory/Semantic/atlas-knows-you]]\n"
        )
        self._vb.write_note(honcho_path, fm, body)
        log.info("Honcho: model written to honcho-model.md")

    # ── Force update ───────────────────────────────────────────────────────────

    def force_update(self) -> Optional[str]:
        """Force a Honcho analysis now, regardless of session count."""
        if self._vb is None or self._brain is None:
            return "Vault or brain not connected, Boss."
        episodes = self._load_recent_episodes()
        if not episodes:
            return "I don't have enough session history to build a user model yet, Boss."
        model = self._extract_user_model(episodes)
        if model:
            self._write_model_to_vault(model)
            self._set_last_honcho_session(self._get_session_count())
            tasks = ", ".join(model.get("preferred_tasks", [])[:3]) or "varied tasks"
            return (f"User model updated, Boss. You seem to prefer {tasks}, "
                    f"and your work style reads as {model.get('work_style','productive')}.")
        return "I couldn't build a user model from the available sessions, Boss."

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas what have you learned about how i work",
                                     "atlas what do you know about my work style",
                                     "atlas describe how i work")):
            honcho_path = (self._vb.atlas / "Memory" / "honcho-model.md") if self._vb else None
            if honcho_path and honcho_path.exists():
                result = self._vb.read_note(honcho_path)
                if result:
                    _, body = result
                    summary = " | ".join(
                        line.strip() for line in body.splitlines()
                        if line.startswith("- ") or (line and not line.startswith("#"))
                    )[:400]
                    return f"My model of you: {summary}"
            return "I haven't built a user model yet. I need at least 5 sessions first."

        if any(p in lower for p in ("atlas update your model of me",
                                     "atlas update user model",
                                     "atlas honcho update")):
            return self.force_update()

        if any(p in lower for p in ("atlas show your user model",
                                     "atlas show honcho",
                                     "atlas open user model")):
            honcho_path = (self._vb.atlas / "Memory" / "honcho-model.md") if self._vb else None
            if honcho_path and honcho_path.exists():
                return f"Your user model is at {honcho_path.name} in the vault. Open it in Obsidian to review."
            return "No user model generated yet, Boss. I need more sessions."

        return None
