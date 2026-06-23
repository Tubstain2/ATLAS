"""
ATLAS Skills Loader

Discovers, loads, and hot-reloads skill plugins from the skills/ folder.
Each skill is a self-contained .py file with skill_info() and execute().

Skill contract:
    def skill_info() -> dict:
        return {
            "name": "skill_name",          # unique slug
            "triggers": ["phrase", ...],   # lowercase trigger phrases
            "description": "What it does"
        }

    def execute(query: str, context: dict) -> str:
        # context keys: brain, config, vision, speak_cb, voice_module
        return "response string"

Hot reload:
    - Polls skills/ every 5 seconds
    - Reloads changed files automatically
    - "ATLAS reload skills" → manual trigger
    - "ATLAS disable X skill" → disables named skill
    - "ATLAS what skills do you have" → list loaded skills
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_POLL_INTERVAL = 5   # seconds between hot-reload checks


class SkillsLoader:
    """
    Discovers and hot-reloads skills from the configured skills folder.

    Usage in main.py:
        skills = SkillsLoader(config, context={...})
        skills.start()
        response = skills.handle("atlas what is the weather")
        skills.stop()
    """

    def __init__(self, config: dict, context: Optional[dict] = None):
        folder = config.get("skills_folder", "./skills")
        self._skills_dir  = Path(folder).resolve()
        self._context     = context or {}
        self._skills: dict[str, dict] = {}   # name → {module, info, disabled, mtime}
        self._disabled: set = set()
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._skipped: dict[Path, float] = {}   # path → mtime of non-skill files
        self._load_all()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._poll_thread = threading.Thread(
            target=self._hot_reload_loop, daemon=True, name="atlas-skills-watch"
        )
        self._poll_thread.start()
        log.info("Skills loader started (watching %s).", self._skills_dir)

    def stop(self):
        self._stop_event.set()

    def set_context(self, key: str, value):
        self._context[key] = value

    # ── Skill discovery ───────────────────────────────────────────────────────

    def _load_all(self):
        if not self._skills_dir.exists():
            log.warning("Skills folder not found: %s", self._skills_dir)
            return

        # Skip __init__.py, loader.py, module files that are not skill plugins
        _SKIP = {"__init__.py", "loader.py", "hermes.py"}
        for path in sorted(self._skills_dir.glob("*.py")):
            if path.name.startswith("_") or path.name in _SKIP:
                continue
            self._load_skill(path)

        log.info("Skills loaded: %s", list(self._skills.keys()))

    def _load_skill(self, path: Path) -> bool:
        name = path.stem
        mtime = path.stat().st_mtime

        try:
            spec   = importlib.util.spec_from_file_location(f"atlas_skill_{name}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if not hasattr(module, "skill_info") or not hasattr(module, "execute"):
                log.warning("Skill %s missing skill_info() or execute() — skipped.", name)
                self._skipped[path] = mtime   # don't retry unless file changes
                return False

            info = module.skill_info()
            with self._lock:
                self._skills[info["name"]] = {
                    "module":  module,
                    "info":    info,
                    "path":    path,
                    "mtime":   mtime,
                }
            log.info("Skill loaded: %s — triggers: %s",
                     info["name"], info.get("triggers", []))
            return True

        except Exception as exc:
            log.error("Failed to load skill %s: %s", path.name, exc)
            return False

    def _hot_reload_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(_POLL_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._check_for_changes()
            except Exception as exc:
                log.debug("Skills hot-reload error: %s", exc)

    def _check_for_changes(self):
        if not self._skills_dir.exists():
            return

        _SKIP = {"__init__.py", "loader.py", "hermes.py"}
        current_files = {
            p: p.stat().st_mtime
            for p in self._skills_dir.glob("*.py")
            if not p.name.startswith("_") and p.name not in _SKIP
        }

        with self._lock:
            loaded_paths = {s["path"]: s["mtime"] for s in self._skills.values()}

        # New or modified files
        for path, mtime in current_files.items():
            # Skip files previously found to not be skill plugins, unless they changed
            if path in self._skipped and self._skipped[path] >= mtime:
                continue
            if path not in loaded_paths or loaded_paths[path] < mtime:
                log.info("Hot-reloading skill: %s", path.name)
                self._load_skill(path)

        # Removed files
        for path in list(loaded_paths.keys()):
            if path not in current_files:
                name = path.stem
                with self._lock:
                    removed = [k for k, v in self._skills.items() if v["path"] == path]
                    for k in removed:
                        del self._skills[k]
                log.info("Skill removed: %s", name)

    # ── Query routing ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        """
        Try each loaded skill against the query.
        Returns a response string if any skill matches, else None.
        """
        lower = text.lower().strip()

        # Meta commands
        if any(p in lower for p in ("atlas what skills do you have",
                                     "atlas list skills", "atlas show skills")):
            return self._list_skills()

        if any(p in lower for p in ("atlas reload skills", "atlas refresh skills")):
            self._load_all()
            return f"Skills reloaded. {len(self._skills)} skills active."

        if "atlas disable" in lower and "skill" in lower:
            skill_name = lower.split("atlas disable")[-1].replace("skill", "").strip()
            return self._disable_skill(skill_name)

        if "atlas enable" in lower and "skill" in lower:
            skill_name = lower.split("atlas enable")[-1].replace("skill", "").strip()
            return self._enable_skill(skill_name)

        # Route to skill by trigger phrase
        with self._lock:
            skills_snapshot = dict(self._skills)

        for name, entry in skills_snapshot.items():
            if name in self._disabled:
                continue
            triggers = entry["info"].get("triggers", [])
            if any(t in lower for t in triggers):
                try:
                    result = entry["module"].execute(text, self._context)
                    if result:
                        return str(result)
                except Exception as exc:
                    log.error("Skill %s execute error: %s", name, exc)
                    return f"The {name} skill encountered an error."

        return None

    def _list_skills(self) -> str:
        with self._lock:
            names = [
                f"{e['info']['name']} ({', '.join(e['info'].get('triggers', [])[:2])})"
                for e in self._skills.values()
                if e["info"]["name"] not in self._disabled
            ]
        if not names:
            return "No skills are currently loaded, Boss."
        return f"I have {len(names)} skills: " + "; ".join(names) + "."

    def _disable_skill(self, name: str) -> str:
        with self._lock:
            matches = [k for k in self._skills if name.lower() in k.lower()]
        if not matches:
            return f"I don't have a skill named {name}."
        self._disabled.add(matches[0])
        return f"{matches[0]} skill disabled."

    def _enable_skill(self, name: str) -> str:
        match = next((k for k in self._disabled if name.lower() in k.lower()), None)
        if not match:
            return f"The {name} skill is already active."
        self._disabled.discard(match)
        return f"{match} skill enabled."
