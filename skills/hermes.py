"""
ATLAS Hermes Skills — agentskills.io-compatible directory-based skills.

Each skill lives in ATLAS/Skills/Hermes/<slug>/ with:
  SKILL.md        ← metadata frontmatter + steps
  references/     ← links, docs, context files
  assets/         ← images, screenshots, examples

Skills self-improve: outcome is appended to SKILL.md after each use.
Skills can be created by voice, via LLM (brain.ask), or manually in Obsidian.
Watchdog hot-reload: editing SKILL.md in Obsidian auto-reloads within 2s.

SKILL.md frontmatter schema:
  name, description, version, platforms, prerequisites,
  lifecycle (active|stale|archived), success_rate, times_used, last_used, tags
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

_SKILL_FRONTMATTER_TEMPLATE = """\
name: "{name}"
description: "{description}"
version: "1.0"
platforms: ["macos"]
prerequisites: []
lifecycle: "active"
success_rate: 1.0
times_used: 0
last_used: null
tags: [atlas, skill]
"""

_SKILL_BODY_TEMPLATE = """\
# Skill: {name}

## When to Use
{trigger}

## Steps
{steps}

## What to Avoid
{pitfalls}

## Outcomes
"""


def _slug(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:50] or "skill"


class HermesSkillsModule:
    """
    Manages directory-based Hermes-format skills in ATLAS/Skills/Hermes/.

    Usage:
        hermes = HermesSkillsModule(vault_brain, brain)
        response = hermes.handle("atlas create a skill for X")
    """

    def __init__(self, vault_brain=None, brain=None, config=None):
        self._vb     = vault_brain
        self._brain  = brain
        self._lock   = threading.Lock()
        self._cache: dict[str, dict] = {}   # slug → {path, meta, body}

        if self._vb is not None:
            self._skills_dir = self._vb.atlas / "Skills" / "Hermes"
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            self._load_all()
        else:
            self._skills_dir = None

    # ── Loading ────────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        if self._skills_dir is None:
            return
        count = 0
        for skill_dir in sorted(self._skills_dir.iterdir()):
            if skill_dir.is_dir():
                self._load_skill_dir(skill_dir)
                count += 1
        log.info("HermesSkills: loaded %d skills from vault.", count)

    def _load_skill_dir(self, skill_dir: Path) -> Optional[dict]:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return None
        try:
            result = self._vb.read_note(skill_md)
            if not result:
                return None
            fm, body = result
            entry = {"path": skill_dir, "skill_md": skill_md, "meta": fm, "body": body}
            slug  = skill_dir.name
            with self._lock:
                self._cache[slug] = entry
            return entry
        except Exception as exc:
            log.warning("HermesSkills: failed to load %s: %s", skill_dir.name, exc)
            return None

    def reload_skill(self, filepath: str) -> None:
        """Called by vault watchdog when SKILL.md changes."""
        path = Path(filepath)
        if "Hermes" in path.parts and path.name == "SKILL.md":
            self._load_skill_dir(path.parent)
            log.info("HermesSkills: hot-reloaded %s", path.parent.name)

    # ── Skill lookup ───────────────────────────────────────────────────────────

    def find_skill(self, query: str) -> Optional[dict]:
        """Return the best-matching skill entry for a query, or None."""
        words = {w for w in query.lower().split() if len(w) > 3}
        if not words:
            return None
        best_score = 0
        best_entry = None
        with self._lock:
            snapshot = dict(self._cache)
        for slug, entry in snapshot.items():
            if entry["meta"].get("lifecycle", "active") == "archived":
                continue
            search_in = (
                str(entry["meta"].get("name", "")) + " "
                + str(entry["meta"].get("description", "")) + " "
                + entry["body"]
            ).lower()
            score = sum(1 for w in words if w in search_in)
            if score > best_score:
                best_score = score
                best_entry = entry
        return best_entry if best_score >= 2 else None

    def get_skill_text(self, query: str) -> Optional[str]:
        """Return formatted skill text for injection into brain context."""
        entry = self.find_skill(query)
        if not entry:
            return None
        name = entry["meta"].get("name", entry["path"].name)
        return f"[HERMES SKILL: {name}]\n{entry['body'][:1000]}"

    # ── Skill creation ─────────────────────────────────────────────────────────

    def create_skill(self, name: str, trigger: str = "", steps: str = "",
                     pitfalls: str = "", description: str = "") -> Path:
        """Write a new directory-based skill to the vault."""
        slug = _slug(name)
        skill_dir = self._skills_dir / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "references").mkdir(exist_ok=True)
        (skill_dir / "assets").mkdir(exist_ok=True)

        skill_md = skill_dir / "SKILL.md"
        fm = {
            "name":         name[:80],
            "description":  description[:200] or f"How to {name}",
            "version":      "1.0",
            "platforms":    ["macos"],
            "prerequisites": [],
            "lifecycle":    "active",
            "success_rate": 1.0,
            "times_used":   0,
            "last_used":    None,
            "tags":         ["atlas", "skill"],
        }
        body = _SKILL_BODY_TEMPLATE.format(
            name     = name,
            trigger  = trigger  or f"When the user asks about {name}.",
            steps    = steps    or "1. Understand the context.\n2. Apply the skill.\n3. Report outcome.",
            pitfalls = pitfalls or "- None documented yet.",
        )
        self._vb.write_note(skill_md, fm, body)
        self._load_skill_dir(skill_dir)
        log.info("HermesSkills: created skill '%s' at %s", name, skill_dir)
        return skill_dir

    def create_skill_from_llm(self, task_description: str) -> Optional[Path]:
        """Ask the brain to generate a skill for a task and write it."""
        if self._brain is None:
            return None
        prompt = (
            f"Generate a concise skill template for this task: {task_description}\n\n"
            "Return exactly these sections (plain text, no markdown fences):\n"
            "NAME: <short skill name>\n"
            "DESCRIPTION: <one sentence>\n"
            "TRIGGER: <when to use this skill>\n"
            "STEPS:\n1. step\n2. step\n3. step\n"
            "PITFALLS:\n- pitfall\n"
        )
        try:
            raw = self._brain.ask(prompt)
            lines = raw.splitlines()

            def _extract(key: str) -> str:
                for line in lines:
                    if line.upper().startswith(key + ":"):
                        return line.split(":", 1)[1].strip()
                return ""

            def _extract_block(key: str) -> str:
                capturing = False
                out = []
                for line in lines:
                    if line.upper().startswith(key + ":"):
                        capturing = True
                        continue
                    if capturing:
                        if re.match(r"^[A-Z]+:", line):
                            break
                        out.append(line)
                return "\n".join(out).strip()

            name     = _extract("NAME") or task_description[:40]
            desc     = _extract("DESCRIPTION") or f"How to {name}"
            trigger  = _extract("TRIGGER") or ""
            steps    = _extract_block("STEPS")
            pitfalls = _extract_block("PITFALLS")

            return self.create_skill(name, trigger, steps, pitfalls, desc)
        except Exception as exc:
            log.warning("HermesSkills: LLM skill generation failed: %s", exc)
            return None

    # ── Outcome recording ──────────────────────────────────────────────────────

    def record_outcome(self, slug: str, outcome: str, success: bool = True) -> None:
        """Append an outcome line to SKILL.md after a skill is used."""
        with self._lock:
            entry = self._cache.get(slug)
        if entry is None:
            return
        try:
            skill_md = entry["skill_md"]
            result   = self._vb.read_note(skill_md)
            if not result:
                return
            fm, body = result
            today    = date.today().isoformat()
            outcome_line = f"- [{today}] {'✓' if success else '✗'} {outcome[:200]}"

            if "## Outcomes" in body:
                body = body.rstrip() + "\n" + outcome_line + "\n"
            else:
                body = body.rstrip() + f"\n\n## Outcomes\n{outcome_line}\n"

            times_used = int(fm.get("times_used", 0)) + 1
            total      = times_used
            successes  = int(round(float(fm.get("success_rate", 1.0)) * (total - 1)))
            if success:
                successes += 1
            fm["times_used"]   = times_used
            fm["last_used"]    = today
            fm["success_rate"] = round(successes / total, 2) if total > 0 else 1.0

            self._vb.write_note(skill_md, fm, body)
            with self._lock:
                self._cache[slug]["meta"] = fm
                self._cache[slug]["body"] = body
        except Exception as exc:
            log.warning("HermesSkills: outcome record failed for %s: %s", slug, exc)

    def list_skills(self) -> List[str]:
        with self._lock:
            return [
                f"{e['meta'].get('name', slug)} ({e['meta'].get('lifecycle','active')})"
                for slug, e in self._cache.items()
            ]

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas list vault skills", "atlas show vault skills",
                                     "atlas what hermes skills")):
            skills = self.list_skills()
            if not skills:
                return "No Hermes-format skills in the vault yet, Boss."
            return f"Vault skills: {'; '.join(skills[:8])}."

        if "atlas create a skill for" in lower:
            task = lower.split("atlas create a skill for", 1)[-1].strip()
            if not task:
                return "What task should I create a skill for, Boss?"
            skill_dir = self.create_skill_from_llm(task)
            if skill_dir:
                return f"Skill created and saved to {skill_dir.name} in the vault, Boss."
            return "I couldn't generate the skill. Try again or create it manually in Obsidian."

        if any(p in lower for p in ("atlas reload vault skills", "atlas reload hermes skills")):
            self._load_all()
            return f"Vault skills reloaded. {len(self._cache)} skills active."

        return None
