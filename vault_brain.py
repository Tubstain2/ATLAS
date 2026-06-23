"""
ATLAS VaultBrain — Obsidian vault as the single source of truth.

All memory, playbook entries, skills, and improvement logs are stored
as readable markdown files inside the Obsidian vault.

Folder layout (relative to vault root):
  ATLAS/
  ├── Memory/
  │   ├── Semantic/atlas-knows-you.md   ← permanent facts about user
  │   ├── Episodic/YYYY-MM-DD-HH-MM.md ← one file per session
  │   └── Working/current-session.md   ← live session context
  ├── Playbook/
  │   ├── Strategies/[slug].md
  │   ├── Pitfalls/[slug].md
  │   └── Preferences/[slug].md
  ├── Skills/[slug].md
  ├── Improvements/YYYY-MM-DD-[slug].md
  └── Daily/YYYY-MM-DD.md
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import frontmatter
import yaml

log = logging.getLogger(__name__)


def _slug(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:50] or "entry"


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _detect_category(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("code", "python", "function", "widget", "script",
                                 "bug", "error", "import", "class", "def ")):
        return "coding"
    if any(w in lower for w in ("stock", "market", "price", "trade", "invest",
                                 "ticker", "earnings")):
        return "market"
    if any(w in lower for w in ("search", "look up", "research", "news", "weather")):
        return "research"
    if any(w in lower for w in ("note", "task", "obsidian", "vault", "reminder", "daily")):
        return "obsidian"
    if any(w in lower for w in ("remember", "memory", "forget", "recall", "last time")):
        return "memory"
    return "general"


class VaultBrain:
    """
    I/O engine for ATLAS's Obsidian-backed intelligence.

    All read/write goes through this class. Atomic writes prevent corruption.
    Thread-safe via per-path locks.
    Watchdog monitors ATLAS/ folder for user edits — calls registered callbacks.
    """

    def __init__(self, vault_path, atlas_folder: str = "ATLAS"):
        self.vault  = Path(vault_path)
        self.atlas  = self.vault / atlas_folder

        # Standard paths
        self.semantic_dir  = self.atlas / "Memory" / "Semantic"
        self.episodic_dir  = self.atlas / "Memory" / "Episodic"
        self.weekly_dir    = self.atlas / "Memory" / "Episodic" / "Weekly"
        self.working_dir   = self.atlas / "Memory" / "Working"
        self.strat_dir     = self.atlas / "Playbook" / "Strategies"
        self.pitfall_dir   = self.atlas / "Playbook" / "Pitfalls"
        self.pref_dir      = self.atlas / "Playbook" / "Preferences"
        self.skills_dir    = self.atlas / "Skills"
        self.improve_dir   = self.atlas / "Improvements"
        self.daily_dir     = self.atlas / "Daily"

        self.semantic_path = self.semantic_dir / "atlas-knows-you.md"

        self._lock   = threading.Lock()
        self._plocks: Dict[str, threading.Lock] = {}

        # Watchdog
        self._observer = None
        self._change_callbacks: List[Callable[[str], None]] = []

        self.ensure_structure()
        self._ensure_semantic_note()

    # ── Directory setup ────────────────────────────────────────────────────────

    def ensure_structure(self) -> None:
        for d in (
            self.semantic_dir, self.episodic_dir, self.weekly_dir, self.working_dir,
            self.strat_dir, self.pitfall_dir, self.pref_dir,
            self.skills_dir, self.improve_dir, self.daily_dir,
            self.atlas / "Research" / "Market",
            self.atlas / "Notes", self.atlas / "Tasks", self.atlas / "Inbox",
        ):
            d.mkdir(parents=True, exist_ok=True)

    def _ensure_semantic_note(self) -> None:
        if not self.semantic_path.exists():
            fm = {
                "last_updated":    _today(),
                "total_sessions":  0,
                "tags":            ["atlas", "memory", "semantic"],
            }
            body = (
                "# What ATLAS Knows About You\n\n"
                "## Identity\n- Name: Boss\n\n"
                "## Preferences\n\n"
                "## Projects\n- [[ATLAS/ATLAS]] — primary ongoing project\n\n"
                "## Skills You Have\n\n"
                "## Goals\n\n"
                "## Notes\n"
            )
            self.write_note(self.semantic_path, fm, body)
            log.info("VaultBrain: created atlas-knows-you.md")

    # ── Atomic read / write ────────────────────────────────────────────────────

    def _plock(self, path: Path) -> threading.Lock:
        key = str(path)
        with self._lock:
            if key not in self._plocks:
                self._plocks[key] = threading.Lock()
            return self._plocks[key]

    def write_note(self, path: Path, fm: dict, body: str) -> None:
        """Atomically write a markdown note with YAML frontmatter."""
        path.parent.mkdir(parents=True, exist_ok=True)
        post    = frontmatter.Post(body, **fm)
        content = frontmatter.dumps(post)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def read_note(self, path: Path) -> Optional[Tuple[dict, str]]:
        """Return (frontmatter_dict, body_text) or None if file missing."""
        if not path.exists():
            return None
        try:
            post = frontmatter.load(str(path))
            return dict(post.metadata), post.content
        except Exception as exc:
            log.warning("VaultBrain: failed to read %s: %s", path.name, exc)
            return None

    def append_note(self, path: Path, content: str) -> None:
        """Append text to an existing note (after the body). Thread-safe."""
        with self._plock(path):
            if not path.exists():
                self.write_note(path, {}, content)
                return
            try:
                existing = path.read_text(encoding="utf-8")
                path.write_text(existing.rstrip() + "\n" + content + "\n",
                                encoding="utf-8")
            except Exception as exc:
                log.warning("VaultBrain: append failed for %s: %s", path.name, exc)

    def update_frontmatter(self, path: Path, updates: dict) -> None:
        """Update specific frontmatter keys without changing body."""
        with self._plock(path):
            result = self.read_note(path)
            if result is None:
                return
            fm, body = result
            fm.update(updates)
            self.write_note(path, fm, body)

    def insert_under_heading(self, path: Path, heading: str, line: str) -> None:
        """Insert a bullet line under a specific ## Heading in a note."""
        with self._plock(path):
            if not path.exists():
                return
            text  = path.read_text(encoding="utf-8")
            marker = f"## {heading}"
            if marker not in text:
                self.append_note(path, f"\n## {heading}\n{line}")
                return
            idx   = text.index(marker) + len(marker)
            # Find next heading or end of file
            rest  = text[idx:]
            next_h = re.search(r"\n## ", rest)
            insert_at = idx + (next_h.start() if next_h else len(rest))
            text = text[:insert_at].rstrip() + f"\n{line}\n" + text[insert_at:]
            path.write_text(text, encoding="utf-8")

    # ── Listing and searching ──────────────────────────────────────────────────

    def list_notes(self, folder: Path, recursive: bool = False) -> List[Path]:
        if not folder.exists():
            return []
        if recursive:
            return sorted(folder.rglob("*.md"))
        return sorted(folder.glob("*.md"))

    def search_notes(self, folder: Path, category: str = "",
                     tags: List[str] = None, recursive: bool = False) -> List[Tuple[dict, str, Path]]:
        """
        Load all .md files in a folder and return those matching
        the given category or tags (from frontmatter).
        Returns list of (frontmatter, body, path) sorted by helpful_count desc.
        """
        results = []
        paths   = self.list_notes(folder, recursive=recursive)
        for p in paths:
            result = self.read_note(p)
            if result is None:
                continue
            fm, body = result
            # Category filter
            if category:
                fm_cat = str(fm.get("category", ""))
                if category not in (fm_cat, "general") and fm_cat != "general":
                    # include "general" entries always
                    if fm_cat and fm_cat not in (category, "general"):
                        continue
            # Tags filter
            if tags:
                fm_tags = fm.get("tags", [])
                if not any(t in fm_tags for t in tags):
                    continue
            results.append((fm, body, p))

        # Sort by helpful_count - harmful_count descending
        results.sort(
            key=lambda r: int(r[0].get("helpful_count", 0)) - int(r[0].get("harmful_count", 0)),
            reverse=True,
        )
        return results

    # ── Context builders (token-efficient) ────────────────────────────────────

    def get_playbook_context(self, query: str, max_chars: int = 2000) -> str:
        """
        Generator: returns compact playbook context for AI injection (≤500 tokens).
        Reads vault files, never loads entire folder into memory.
        """
        cat   = _detect_category(query)
        lines: List[str] = []

        # Strategies — top 3 by net score
        for fm, body, p in self.search_notes(self.strat_dir, category=cat)[:3]:
            title = fm.get("title", p.stem.replace("-", " ").title())
            desc  = body.split("\n## What to Do")[-1][:120].strip().replace("\n", " ")
            lines.append(f"• STRATEGY [{fm.get('category','?')}]: {title} — {desc}")

        # Pitfalls — top 2 by occurrence
        pf = self.list_notes(self.pitfall_dir)
        pf_loaded = []
        for p in pf:
            r = self.read_note(p)
            if r:
                pf_loaded.append((r[0], r[1], p))
        pf_loaded.sort(key=lambda r: int(r[0].get("occurrence_count", 0)), reverse=True)
        for fm, body, p in pf_loaded[:2]:
            title = fm.get("title", p.stem.replace("-", " ").title())
            lines.append(f"• AVOID [{fm.get('category','?')}]: {title}")

        # Preferences — top 3 by confidence
        pr = self.list_notes(self.pref_dir)
        pr_loaded = []
        for p in pr:
            r = self.read_note(p)
            if r:
                pr_loaded.append((r[0], r[1], p))
        pr_loaded.sort(key=lambda r: float(r[0].get("confidence", 0)), reverse=True)
        for fm, body, p in pr_loaded[:3]:
            title = fm.get("title", p.stem.replace("-", " ").title())
            lines.append(f"• USER PREFERENCE: {title}")

        if not lines:
            return ""
        block = "PLAYBOOK (apply these learned strategies):\n" + "\n".join(lines)
        return block[:max_chars]

    def get_semantic_context(self, max_chars: int = 1200) -> str:
        """
        Returns a compact summary of atlas-knows-you.md for AI injection.
        Extracts key sections without loading the full file into every prompt.
        """
        result = self.read_note(self.semantic_path)
        if not result:
            return ""
        fm, body = result
        # Extract bullet points from each section (skip headings, blank lines)
        lines  = []
        section = ""
        for raw in body.splitlines():
            raw = raw.strip()
            if raw.startswith("## "):
                section = raw[3:]
                continue
            if raw.startswith("- ") and section in (
                "Identity", "Preferences", "Projects", "Skills You Have", "Goals", "Notes"
            ):
                lines.append(f"• [{section}] {raw[2:]}")
        if not lines:
            return ""
        block = "ABOUT THE USER (from ATLAS memory):\n" + "\n".join(lines[:15])
        return block[:max_chars]

    def get_skill(self, query: str) -> Optional[str]:
        """Load the most relevant skill file for a query (keyword match)."""
        best_score = 0
        best_text  = None
        for p in self.list_notes(self.skills_dir):
            r = self.read_note(p)
            if not r:
                continue
            fm, body = r
            task_type = str(fm.get("task_type", p.stem))
            search_in = task_type + " " + body
            score = sum(1 for w in query.lower().split()
                        if len(w) > 3 and w in search_in.lower())
            if score > best_score:
                best_score = score
                best_text  = body
        return best_text[:800] if best_score >= 2 and best_text else None

    # ── Semantic memory helpers ────────────────────────────────────────────────

    def add_fact(self, fact: str, section: str = "Notes") -> None:
        """Append a bullet fact under a section heading in atlas-knows-you.md."""
        line = f"- {fact.strip()}"
        self.insert_under_heading(self.semantic_path, section, line)
        self.update_frontmatter(self.semantic_path, {"last_updated": _today()})
        log.debug("VaultBrain: fact added to %s: %s", section, fact[:60])

    def add_preference(self, preference: str, strength: str = "medium",
                       confidence: float = 0.8) -> None:
        """Write a preference note to ATLAS/Playbook/Preferences/."""
        slug  = _slug(preference[:40])
        path  = self.pref_dir / f"{slug}.md"
        if path.exists():
            self.update_frontmatter(path, {"confidence": confidence, "last_updated": _today()})
            return
        fm = {
            "title":       preference[:80],
            "confidence":  confidence,
            "strength":    strength,
            "learned":     _today(),
            "tags":        ["atlas", "preference"],
        }
        body = (
            f"# Preference: {preference[:80]}\n\n"
            f"## Observation\n{preference}\n\n"
            f"## Behaviour Change\nApply this preference in future responses.\n"
        )
        self.write_note(path, fm, body)
        # Also mirror into atlas-knows-you.md
        self.insert_under_heading(self.semantic_path, "Preferences", f"- {preference[:120]}")

    # ── Episodic memory helpers ────────────────────────────────────────────────

    def write_episode(self, summary: str, duration_min: int, mood: str,
                      projects: List[str], tags: List[str], learned: List[str]) -> Path:
        """Write a session summary as a dated episodic note."""
        now      = datetime.now()
        filename = now.strftime("%Y-%m-%d-%H-%M") + ".md"
        path     = self.episodic_dir / filename
        day_name = now.strftime("%A %d %B %Y")

        fm = {
            "date":             _today(),
            "duration_minutes": duration_min,
            "mood":             mood,
            "tags":             ["atlas", "episodic"] + tags,
            "projects":         projects,
        }
        body = (
            f"# Session — {day_name}\n\n"
            f"## What We Did\n{summary}\n\n"
            f"## What ATLAS Learned\n"
            + "".join(f"- {item}\n" for item in learned)
            + f"\n## Links\n- [[ATLAS/Memory/Semantic/atlas-knows-you]]\n"
        )
        self.write_note(path, fm, body)
        # Also append link to daily note
        daily = self.daily_dir / f"{_today()}.md"
        self.append_note(daily,
                         f"\n## Session Summary\n- [[ATLAS/Memory/Episodic/{filename[:-3]}]]\n")
        log.info("VaultBrain: episode written → %s", filename)
        return path

    def get_last_episode(self) -> Optional[Tuple[dict, str]]:
        episodes = sorted(self.episodic_dir.glob("*.md"), reverse=True)
        for p in episodes:
            r = self.read_note(p)
            if r:
                return r
        return None

    def search_episodes(self, query: str, days_back: int = 365,
                        max_results: int = 3) -> List[Tuple[dict, str]]:
        cutoff = date.today().toordinal() - days_back
        results = []
        for p in sorted(self.episodic_dir.glob("*.md"), reverse=True):
            r = self.read_note(p)
            if not r:
                continue
            fm, body = r
            ep_date_str = fm.get("date", "")
            try:
                ep_date = date.fromisoformat(ep_date_str).toordinal()
                if ep_date < cutoff:
                    break
            except Exception:
                pass
            search_text = body + " " + " ".join(fm.get("tags", []))
            words = {w for w in query.lower().split() if len(w) > 3}
            if any(w in search_text.lower() for w in words):
                results.append((fm, body))
                if len(results) >= max_results:
                    break
        return results

    # ── Playbook helpers ───────────────────────────────────────────────────────

    def write_strategy(self, title: str, description: str, why: str,
                       category: str, helpful: int = 0, harmful: int = 0) -> Path:
        slug  = _slug(title)
        path  = self.strat_dir / f"{slug}.md"
        # Don't overwrite existing — just update counts
        if path.exists():
            self.update_frontmatter(path, {
                "helpful_count": helpful,
                "harmful_count": harmful,
                "last_updated":  _today(),
            })
            return path
        fm = {
            "title":         title[:80],
            "category":      category,
            "helpful_count": helpful,
            "harmful_count": harmful,
            "last_updated":  _today(),
            "tags":          ["atlas", "strategy", category],
        }
        body = (
            f"# Strategy: {title}\n\n"
            f"## What to Do\n{description}\n\n"
            f"## Why It Works\n{why}\n\n"
            f"## Evidence\nUsed {helpful} times successfully.\n"
        )
        self.write_note(path, fm, body)
        return path

    def write_pitfall(self, title: str, description: str,
                      category: str, occurrences: int = 1) -> Path:
        slug = _slug(title)
        path = self.pitfall_dir / f"{slug}.md"
        if path.exists():
            r = self.read_note(path)
            if r:
                fm, body = r
                fm["occurrence_count"] = int(fm.get("occurrence_count", 0)) + 1
                fm["last_seen"]        = _today()
                self.write_note(path, fm, body)
            return path
        fm = {
            "title":            title[:80],
            "category":         category,
            "occurrence_count": occurrences,
            "last_seen":        _today(),
            "tags":             ["atlas", "pitfall", category],
        }
        body = (
            f"# Pitfall: {title}\n\n"
            f"## What Happens\n{description}\n\n"
            f"## What to Do Instead\nAvoid this approach in future responses.\n"
        )
        self.write_note(path, fm, body)
        return path

    def increment_strategy_count(self, slug: str, helpful: bool) -> None:
        path = self.strat_dir / f"{slug}.md"
        if not path.exists():
            return
        r = self.read_note(path)
        if not r:
            return
        fm, body = r
        key = "helpful_count" if helpful else "harmful_count"
        fm[key] = int(fm.get(key, 0)) + 1
        fm["last_updated"] = _today()
        self.write_note(path, fm, body)

    # ── Skills helpers ─────────────────────────────────────────────────────────

    def write_skill(self, name: str, task_type: str, trigger: str,
                    steps: str, outcome: str, pitfalls_text: str) -> Path:
        slug = _slug(name)
        path = self.skills_dir / f"{slug}.md"
        times_used = 1
        if path.exists():
            r = self.read_note(path)
            if r:
                times_used = int(r[0].get("times_used", 0)) + 1
        fm = {
            "title":        name[:80],
            "task_type":    task_type,
            "times_used":   times_used,
            "last_used":    _today(),
            "tags":         ["atlas", "skill"],
        }
        body = (
            f"# Skill: {name}\n\n"
            f"## When to Use This\n{trigger}\n\n"
            f"## Steps That Work\n{steps}\n\n"
            f"## What to Avoid\n{pitfalls_text}\n\n"
            f"## Outcome\n{outcome}\n"
        )
        self.write_note(path, fm, body)
        return path

    def list_skills(self) -> List[str]:
        return [p.stem.replace("-", " ").title() for p in self.list_notes(self.skills_dir)]

    # ── Improvement log ────────────────────────────────────────────────────────

    def write_improvement(self, module: str, description: str,
                          why: str, result: str, success: bool) -> Path:
        slug = _slug(description[:30])
        filename = f"{_today()}-{slug}.md"
        path = self.improve_dir / filename
        fm = {
            "module":  module,
            "type":    "performance",
            "success": success,
            "date":    _today(),
            "tags":    ["atlas", "improvement", module],
        }
        body = (
            f"# Improvement: {description}\n\n"
            f"## What Was Changed\n{description}\n\n"
            f"## Why\n{why}\n\n"
            f"## Result\n{result}\n"
        )
        self.write_note(path, fm, body)
        log.info("VaultBrain: improvement logged → %s", filename)
        return path

    # ── Daily note ─────────────────────────────────────────────────────────────

    def ensure_daily_note(self) -> Path:
        path = self.daily_dir / f"{_today()}.md"
        if not path.exists():
            now      = datetime.now()
            day_name = now.strftime("%A %d %B %Y")
            fm = {
                "date":            _today(),
                "atlas_sessions":  0,
                "tags":            ["atlas", "daily"],
            }
            body = (
                f"# {day_name}\n\n"
                f"## Morning Briefing\n\n"
                f"## What ATLAS Learned Today\n\n"
                f"## Tasks\n\n"
                f"## Notes\n\n"
                f"## Session Summaries\n"
            )
            self.write_note(path, fm, body)
        return path

    def append_daily_learned(self, fact: str) -> None:
        daily = self.ensure_daily_note()
        self.insert_under_heading(daily, "What ATLAS Learned Today", f"- {fact}")

    def append_daily_eod_summary(self, sessions: int, minutes: int,
                                  topics: List[str], skills: int, improvements: int) -> None:
        daily = self.ensure_daily_note()
        summary = (
            f"\n## End of Day Summary\n"
            f"- Sessions today: {sessions}\n"
            f"- Total time: {minutes} minutes\n"
            f"- Main topics: {', '.join(topics[:3])}\n"
            f"- New skills learned: {skills}\n"
            f"- Improvements made: {improvements}\n"
        )
        self.append_note(daily, summary)

    # ── Weekly review ──────────────────────────────────────────────────────────

    def write_weekly_review(self, summary: str, episodes: List[Tuple[dict, str]]) -> Optional[Path]:
        week_num = date.today().isocalendar()[1]
        year     = date.today().year
        filename = f"{year}-W{week_num:02d}.md"
        path     = self.weekly_dir / filename
        if path.exists():
            return None  # Already written this week

        topics   = []
        skills   = []
        for fm, body in episodes:
            topics.extend(fm.get("tags", []))
        topics   = list(dict.fromkeys(t for t in topics if t not in ("atlas", "episodic")))[:5]

        fm = {
            "week":  f"{year}-W{week_num:02d}",
            "date":  _today(),
            "sessions": len(episodes),
            "tags":  ["atlas", "weekly-review"],
        }
        ep_lines = "\n".join(
            f"- **{ep[0].get('date','?')}**: {ep[1][:150].split(chr(10))[0]}"
            for ep in episodes[:10]
        )
        body = (
            f"# Weekly Review — Week {week_num}, {year}\n\n"
            f"## Summary\n{summary}\n\n"
            f"## Sessions This Week\n{ep_lines}\n\n"
            f"## Topics Covered\n"
            + "".join(f"- {t}\n" for t in topics)
            + f"\n## Skills Learned\n\n## Improvements Made\n"
        )
        self.write_note(path, fm, body)
        log.info("VaultBrain: weekly review written → %s", filename)
        return path

    # ── Watchdog ───────────────────────────────────────────────────────────────

    def start_watcher(self, on_change: Callable[[str], None]) -> None:
        """
        Monitor ATLAS/ folder for user edits.
        Calls on_change(filepath) within ~1s of any .md file change.
        """
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class _Handler(FileSystemEventHandler):
                def __init__(self, cb):
                    self._cb = cb
                    self._last: Dict[str, float] = {}

                def on_modified(self, event):
                    if event.is_directory or not event.src_path.endswith(".md"):
                        return
                    # Debounce — suppress events within 2s of last fire for this path
                    now = time.time()
                    if now - self._last.get(event.src_path, 0) < 2.0:
                        return
                    self._last[event.src_path] = now
                    try:
                        self._cb(event.src_path)
                    except Exception:
                        pass

            self._observer = Observer()
            self._observer.schedule(_Handler(on_change), str(self.atlas), recursive=True)
            self._observer.start()
            log.info("VaultBrain: watchdog started on %s", self.atlas)
        except Exception as exc:
            log.warning("VaultBrain: watchdog unavailable (%s) — vault monitoring off.", exc)

    def stop_watcher(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None
