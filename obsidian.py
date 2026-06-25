"""
ATLAS Obsidian Integration

Reads and writes markdown files directly to the user's Obsidian vault.
No Obsidian API required — the vault is just a folder of .md files.

Vault folder structure created automatically:
  Daily/      → daily notes (YYYY-MM-DD.md)
  Notes/      → voice notes
  Tasks/      → tasks.md
  Research/   → web research saves
  Inbox/      → quick captures (inbox.md)

Two-step voice interactions work by setting _waiting_* flags.
The next voice utterance is intercepted as the content.

Wire-up in main.py:
    obsidian_mod = ObsidianModule(config, speak_cb=vm.speak, brain=brain)
    # Add to meta chain and track last response
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_TASK_TAG    = "#atlas"
_TASK_REGEX  = re.compile(r"^- \[( |x|X)\] (.+)$", re.MULTILINE)
_DUE_REGEX   = re.compile(r"\bdue\s+([0-9]{4}-[0-9]{2}-[0-9]{2}|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE)


def _slugify(title: str) -> str:
    """Convert a title to a safe filename stem."""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:60] or "note"


def _auto_title(text: str) -> str:
    """Generate a title from the first sentence of a note."""
    first = re.split(r"[.!?\n]", text.strip())[0].strip()
    # Title-case first 8 words max
    words = first.split()[:8]
    return " ".join(w.capitalize() for w in words) or "Voice Note"


def _frontmatter(title: str, tags: list[str], extra: dict | None = None) -> str:
    now  = datetime.now()
    lines = [
        "---",
        f'title: "{title}"',
        f'date: {now.strftime("%Y-%m-%d")}',
        f'time: {now.strftime("%H:%M")}',
        f'tags: [{", ".join(tags)}]',
        'source: ATLAS',
    ]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


class ObsidianModule:
    """Voice-driven Obsidian vault controller."""

    def __init__(self, config: dict,
                 speak_cb=None,
                 brain=None):
        self._cfg        = config
        self._speak_cb   = speak_cb
        self._brain      = brain
        self._user_name  = config.get("user_name", "Boss")

        obs = config.get("obsidian", {})
        raw_path = obs.get("vault_path", "")
        self._vault: Optional[Path] = (
            Path(raw_path).expanduser() if raw_path else None
        )
        self._daily_folder    = obs.get("daily_folder",    "Daily")
        self._notes_folder    = obs.get("notes_folder",    "Notes")
        self._tasks_file      = obs.get("tasks_file",      "Tasks/tasks.md")
        self._research_folder = obs.get("research_folder", "Research")
        self._inbox_file      = obs.get("inbox_file",      "Inbox/inbox.md")
        self._save_briefing   = obs.get("save_briefing_to_daily", True)
        self._ask_before_save = obs.get("ask_before_saving_research", True)

        # Two-step interaction state
        self._waiting_for_note         = False
        self._waiting_for_task         = False
        self._waiting_for_daily_append = False
        self._waiting_for_note_title: Optional[str] = None

        # Runtime state
        self._last_response           = ""
        self._last_mentioned_task     = ""
        self._last_note_path: Optional[Path] = None
        self._last_search_results: list[dict] = []

        if self._vault and self._vault.exists():
            self._ensure_folders()
            log.info("Obsidian: vault ready at %s", self._vault)
        elif self._vault:
            log.warning("Obsidian: vault path set but not found: %s", self._vault)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_last_response(self, response: str) -> None:
        """Called by main.py _handle_with_feed after every AI response."""
        if response:
            self._last_response = response

    def set_vault_path(self, path: str) -> None:
        """Programmatic vault path setter (used by config save)."""
        self._vault = Path(path).expanduser()
        self._ensure_folders()

    # ── Main voice handler ─────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        # ── Two-step captures (intercept next utterance) ─────────────────────
        if self._waiting_for_note:
            self._waiting_for_note       = False
            title                        = self._waiting_for_note_title
            self._waiting_for_note_title = None
            return self._take_note(content=text, title=title)

        if self._waiting_for_task:
            self._waiting_for_task = False
            return self._add_task_from_text(text)

        if self._waiting_for_daily_append:
            self._waiting_for_daily_append = False
            return self._append_daily_note(text)

        # ── Vault setup ──────────────────────────────────────────────────────
        if any(p in lower for p in ("set my obsidian vault to", "set obsidian vault to",
                                     "set vault to", "my vault is at", "vault path is")):
            for marker in ("vault to ", "vault is at ", "path is "):
                if marker in lower:
                    raw = text[text.lower().find(marker) + len(marker):].strip()
                    return self._cmd_set_vault(raw)
            return f"Please say the full path after 'vault to', {self._user_name}."

        # ── Vault not set guard ──────────────────────────────────────────────
        _OBS_KEYWORDS = ("note", "task", "daily", "obsidian", "inbox",
                         "vault", "journal", "research my notes", "search my notes")
        if not self._vault_ready():
            if any(kw in lower for kw in _OBS_KEYWORDS):
                return (
                    f"Obsidian vault isn't configured yet, {self._user_name}. "
                    "Say 'ATLAS set my Obsidian vault to' followed by the folder path."
                )
            return None

        # ── Voice notes ──────────────────────────────────────────────────────
        if "take a note called" in lower:
            title = self._after(lower, "take a note called")
            return self._prompt_note_content(title=title)

        if any(p in lower for p in (
            "atlas take a note", "take a note", "create a note",
            "make a note", "write a note", "jot this down", "jot that down",
            "add a note", "save a note", "note this down", "make note",
        )):
            return self._prompt_note_content()

        if any(p in lower for p in ("add to my notes", "add to notes", "append to my notes")):
            return self._prompt_note_content(append_recent=True)

        if any(p in lower for p in ("atlas note this", "note this",
                                     "save that as a note", "save last response as a note")):
            return self._note_last_response()

        # ── Quick capture ────────────────────────────────────────────────────
        for prefix in ("atlas quick note ", "quick note "):
            if lower.startswith(prefix) or (f" {prefix}" in lower):
                content = self._after(text.lower(), prefix, preserve_case=True, text_orig=text)
                if content:
                    return self._quick_note(content)

        if any(p in lower for p in ("atlas inbox", "read my inbox",
                                     "what's in my inbox", "check inbox")):
            return self._read_inbox()

        if any(p in lower for p in ("atlas clear inbox", "clear my inbox", "clear inbox")):
            return self._clear_inbox()

        # ── Daily note ───────────────────────────────────────────────────────
        if any(p in lower for p in ("open my daily note", "open daily note",
                                     "atlas daily note", "today's note", "todays note")):
            return self._open_daily_note()

        if any(p in lower for p in ("add to my daily note", "add to daily note",
                                     "write to daily note")):
            return self._prompt_daily_append()

        if any(p in lower for p in ("what is in my daily note", "read my daily note",
                                     "what's in my daily note", "daily note summary")):
            return self._read_daily_note()

        # ── Tasks ─────────────────────────────────────────────────────────────
        if any(p in lower for p in ("atlas add a task", "add a task", "create a task",
                                     "new task", "add task")):
            inline = self._extract_task_inline(lower)
            if inline:
                return self._add_task_from_text(inline)
            return self._prompt_task()

        if any(p in lower for p in ("what are my tasks", "list my tasks",
                                     "show my tasks", "read my tasks", "what tasks do i have")):
            return self._list_tasks()

        if any(p in lower for p in ("most urgent task", "what is my most urgent",
                                     "what's my most urgent", "top task", "next task")):
            return self._most_urgent_task()

        if any(p in lower for p in ("mark that task done", "mark that done",
                                     "complete that task", "done with that task")):
            return self._mark_task_done()

        if "mark" in lower and ("done" in lower or "complete" in lower or "finished" in lower):
            task_name = self._extract_mark_target(lower)
            if task_name:
                return self._mark_task_done(task_name)

        # ── Search ────────────────────────────────────────────────────────────
        for prefix in ("search my notes for ", "what do i know about ",
                       "search obsidian for ", "find notes about "):
            if prefix in lower:
                query = self._after(lower, prefix)
                if query:
                    return self._search_notes(query)

        if "find my note about" in lower:
            query = self._after(lower, "find my note about")
            if query:
                return self._find_and_open_note(query)

        # ── Research & save ───────────────────────────────────────────────────
        if any(p in lower for p in ("save that to obsidian", "save that to my vault",
                                     "save to obsidian", "add that to obsidian")):
            return self._save_last_response_to_vault()

        if "research" in lower and ("save" in lower or "obsidian" in lower):
            query = self._extract_research_query(lower)
            if query:
                return self._research_and_save(query, original=text)

        # ── Show widget / summary ─────────────────────────────────────────────
        if any(p in lower for p in ("show my notes", "atlas show notes",
                                     "obsidian summary", "vault summary")):
            return self._vault_summary()

        # ── Knowledge graph ───────────────────────────────────────────────────
        if any(p in lower for p in ("open obsidian graph", "show knowledge graph",
                                     "open graph view", "show my knowledge map",
                                     "open knowledge map", "show obsidian graph",
                                     "open my knowledge graph", "knowledge graph",
                                     "map of knowledge", "full map of knowledge")):
            return self._open_graph_view()

        return None

    # ── Voice note commands ────────────────────────────────────────────────────

    def _prompt_note_content(self, title: Optional[str] = None,
                              append_recent: bool = False) -> str:
        if append_recent:
            if self._last_note_path and self._last_note_path.exists():
                self._waiting_for_note       = True
                self._waiting_for_note_title = "__append__"
                return f"Go ahead, I'll add it to your most recent note, {self._user_name}."
            return f"No recent note to append to, {self._user_name}. Say 'take a note' instead."
        self._waiting_for_note       = True
        self._waiting_for_note_title = title
        if title:
            return f"Go ahead, I'm listening for the content of '{title}'."
        return f"Go ahead, what's the note?"

    def _take_note(self, content: str, title: Optional[str] = None) -> str:
        try:
            if title == "__append__":
                return self._append_to_recent_note(content)

            if not title:
                title = _auto_title(content)

            now      = datetime.now()
            slug     = _slugify(title)
            filename = f"{now.strftime('%Y-%m-%d-%H-%M')}-{slug}.md"
            folder   = self._vault / self._notes_folder
            folder.mkdir(parents=True, exist_ok=True)
            path     = folder / filename

            body = _frontmatter(title, ["atlas", "voice-note"])
            body += f"# {title}\n\n{content}\n"
            path.write_text(body, encoding="utf-8")
            self._last_note_path = path

            log.info("Obsidian: note saved → %s", path.name)
            return f"Note saved to Obsidian, {self._user_name}. Title: {title}."
        except Exception as exc:
            log.error("Obsidian take_note error: %s", exc)
            return f"Couldn't save the note. {exc}"

    def _append_to_recent_note(self, content: str) -> str:
        if not self._last_note_path or not self._last_note_path.exists():
            return f"No recent note found, {self._user_name}."
        try:
            existing = self._last_note_path.read_text(encoding="utf-8")
            now = datetime.now().strftime("%H:%M")
            with open(self._last_note_path, "a", encoding="utf-8") as f:
                f.write(f"\n**{now}** — {content}\n")
            return f"Added to your note, {self._user_name}."
        except Exception as exc:
            log.error("Obsidian append error: %s", exc)
            return f"Couldn't append to note: {exc}"

    def _note_last_response(self) -> str:
        if not self._last_response:
            return f"No recent ATLAS response to save, {self._user_name}."
        title = _auto_title(self._last_response)
        return self._take_note(content=self._last_response, title=f"ATLAS - {title}")

    def _quick_note(self, content: str) -> str:
        try:
            inbox = self._vault / self._inbox_file
            inbox.parent.mkdir(parents=True, exist_ok=True)
            if not inbox.exists():
                inbox.write_text("# Inbox\n\n", encoding="utf-8")
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(inbox, "a", encoding="utf-8") as f:
                f.write(f"- **{now}** — {content}\n")
            return f"Quick note saved, {self._user_name}."
        except Exception as exc:
            log.error("Obsidian quick_note error: %s", exc)
            return f"Couldn't save quick note: {exc}"

    def _read_inbox(self) -> str:
        try:
            inbox = self._vault / self._inbox_file
            if not inbox.exists():
                return f"Your inbox is empty, {self._user_name}."
            lines = inbox.read_text(encoding="utf-8").splitlines()
            items = [l.lstrip("- ").strip() for l in lines if l.strip().startswith("-")]
            if not items:
                return f"Your inbox is empty, {self._user_name}."
            count = len(items)
            preview = ". ".join(items[:4])
            suffix = f" and {count - 4} more." if count > 4 else "."
            return f"You have {count} inbox items, {self._user_name}. {preview}{suffix}"
        except Exception as exc:
            log.error("Obsidian read_inbox error: %s", exc)
            return f"Couldn't read inbox: {exc}"

    def _clear_inbox(self) -> str:
        try:
            inbox = self._vault / self._inbox_file
            if not inbox.exists():
                return f"Inbox is already empty, {self._user_name}."
            lines = inbox.read_text(encoding="utf-8").splitlines()
            items = [l for l in lines if l.strip().startswith("-")]
            if not items:
                return f"Nothing to clear, {self._user_name}."
            # Archive items to a dated note in Notes/
            now      = datetime.now()
            slug     = f"inbox-{now.strftime('%Y-%m-%d')}"
            archive  = self._vault / self._notes_folder / f"{slug}.md"
            archive.parent.mkdir(parents=True, exist_ok=True)
            body  = _frontmatter(f"Inbox {now.strftime('%Y-%m-%d')}", ["atlas", "inbox"])
            body += "# Inbox Archive\n\n" + "\n".join(items) + "\n"
            archive.write_text(body, encoding="utf-8")
            # Clear the inbox file
            inbox.write_text("# Inbox\n\n", encoding="utf-8")
            return (
                f"Cleared {len(items)} inbox items, {self._user_name}. "
                f"Archived to Notes/{slug}.md."
            )
        except Exception as exc:
            log.error("Obsidian clear_inbox error: %s", exc)
            return f"Couldn't clear inbox: {exc}"

    # ── Daily note ─────────────────────────────────────────────────────────────

    def _open_daily_note(self) -> str:
        path = self._daily_note_path()
        try:
            created = not path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            if created:
                self._create_daily_note(path)
                msg = f"Created today's daily note, {self._user_name}."
            else:
                msg = f"Daily note for today already exists, {self._user_name}."
            # Try to open in Obsidian app
            self._open_in_obsidian(path)
            return msg
        except Exception as exc:
            log.error("Obsidian open_daily error: %s", exc)
            return f"Couldn't open daily note: {exc}"

    def _create_daily_note(self, path: Path) -> None:
        now  = datetime.now()
        day  = now.strftime("%A")
        date = now.strftime("%d %B %Y")
        body = f"# {day}, {date}\n\n"
        body += "## Morning Briefing\n\n"
        body += "## Tasks\n- [ ] \n\n"
        body += "## Notes\n\n"
        body += "## Journal\n\n"
        path.write_text(body, encoding="utf-8")

    def _prompt_daily_append(self) -> str:
        self._waiting_for_daily_append = True
        return f"Go ahead, what should I add to your daily note?"

    def _append_daily_note(self, content: str) -> str:
        try:
            path = self._daily_note_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                self._create_daily_note(path)
            now = datetime.now().strftime("%H:%M")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n**{now}** — {content}\n")
            return f"Added to your daily note, {self._user_name}."
        except Exception as exc:
            log.error("Obsidian append_daily error: %s", exc)
            return f"Couldn't update daily note: {exc}"

    def append_morning_briefing(self, summary: str) -> None:
        """Called by morning briefing integration. Non-interactive."""
        if not self._vault_ready() or not self._save_briefing:
            return
        try:
            path = self._daily_note_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                self._create_daily_note(path)
            content = path.read_text(encoding="utf-8")
            marker  = "## Morning Briefing\n"
            if marker in content:
                insertion = f"{marker}\n{summary}\n\n"
                content   = content.replace(marker, insertion, 1)
                path.write_text(content, encoding="utf-8")
            else:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"\n## Morning Briefing\n\n{summary}\n")
            log.info("Obsidian: morning briefing appended to daily note.")
        except Exception as exc:
            log.error("Obsidian briefing append error: %s", exc)

    def _read_daily_note(self) -> str:
        try:
            path = self._daily_note_path()
            if not path.exists():
                return f"No daily note for today yet, {self._user_name}. Say 'open my daily note' to create one."
            content = path.read_text(encoding="utf-8")
            # Strip frontmatter and headings, keep plain text
            lines = [l for l in content.splitlines() if not l.startswith("#") and l.strip()]
            if not lines:
                return f"Your daily note is empty so far, {self._user_name}."
            summary = " ".join(lines[:10])
            if len(lines) > 10:
                summary += f" ... and {len(lines) - 10} more lines."
            return summary
        except Exception as exc:
            log.error("Obsidian read_daily error: %s", exc)
            return f"Couldn't read daily note: {exc}"

    # ── Task management ────────────────────────────────────────────────────────

    def _prompt_task(self) -> str:
        self._waiting_for_task = True
        return f"What's the task, {self._user_name}?"

    def _extract_task_inline(self, lower: str) -> Optional[str]:
        for prefix in ("add task ", "add a task ", "create a task ", "new task "):
            if prefix in lower:
                rest = lower.split(prefix, 1)[-1].strip()
                if len(rest) > 3:
                    return rest
        return None

    def _add_task_from_text(self, text: str, original: Optional[str] = None) -> str:
        source = original or text
        # Extract optional due date
        due_match = _DUE_REGEX.search(source)
        due_str   = ""
        task_text = source
        if due_match:
            raw_due   = due_match.group(1).lower()
            task_text = source[:due_match.start()].strip()
            if raw_due == "today":
                raw_due = datetime.now().strftime("%Y-%m-%d")
            elif raw_due == "tomorrow":
                from datetime import timedelta
                raw_due = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            due_str = f" (due: {raw_due})"
        # Clean up
        for prefix in ("add task ", "add a task ", "create a task ", "new task "):
            task_text = re.sub(f"^{prefix}", "", task_text, flags=re.IGNORECASE).strip()
        task_text = task_text.strip().rstrip(".")
        if not task_text:
            return f"I didn't catch a task name, {self._user_name}."
        return self._write_task(task_text, due_str)

    def _write_task(self, task_text: str, due_str: str = "") -> str:
        try:
            tasks_path = self._vault / self._tasks_file
            tasks_path.parent.mkdir(parents=True, exist_ok=True)
            if not tasks_path.exists():
                tasks_path.write_text("# Tasks\n\n", encoding="utf-8")
            line = f"- [ ] {task_text}{due_str} {_TASK_TAG}\n"
            with open(tasks_path, "a", encoding="utf-8") as f:
                f.write(line)
            self._last_mentioned_task = task_text
            return f"Task added, {self._user_name}: {task_text}{due_str}."
        except Exception as exc:
            log.error("Obsidian write_task error: %s", exc)
            return f"Couldn't save task: {exc}"

    def _list_tasks(self, max_count: int = 5) -> str:
        tasks = self._get_open_tasks()
        if not tasks:
            return f"No open tasks, {self._user_name}. You're all caught up!"
        display = tasks[:max_count]
        names   = ", ".join(t["text"] for t in display)
        suffix  = f" and {len(tasks) - max_count} more" if len(tasks) > max_count else ""
        return (
            f"You have {len(tasks)} open task{'s' if len(tasks) != 1 else ''}, "
            f"{self._user_name}: {names}{suffix}."
        )

    def _most_urgent_task(self) -> str:
        tasks = self._get_open_tasks()
        if not tasks:
            return f"No open tasks, {self._user_name}."
        task = tasks[0]
        self._last_mentioned_task = task["text"]
        due = f", due {task['due']}" if task.get("due") else ""
        return f"Your most urgent task is: {task['text']}{due}, {self._user_name}."

    def _mark_task_done(self, task_name: Optional[str] = None) -> str:
        target = task_name or self._last_mentioned_task
        if not target:
            return f"Which task should I mark as done, {self._user_name}?"
        try:
            tasks_path = self._vault / self._tasks_file
            if not tasks_path.exists():
                return f"No task file found, {self._user_name}."
            content = tasks_path.read_text(encoding="utf-8")
            # Find best match
            pattern      = re.compile(r"^(- \[ \] )(.+)$", re.MULTILINE)
            best_line    = None
            best_ratio   = 0.0
            target_lower = target.lower()
            for m in pattern.finditer(content):
                task_line  = m.group(2).lower()
                # Strip due date and tag before scoring
                clean_line = re.sub(r"\(due:[^)]+\)", "", task_line)
                clean_line = clean_line.replace(_TASK_TAG, "").strip()
                # Substring hit → maximum score
                ratio = 1.0 if target_lower in clean_line else self._similarity(target_lower, clean_line)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_line  = m

            if best_line and best_ratio > 0.3:
                new_content = (
                    content[:best_line.start()]
                    + "- [x] "
                    + best_line.group(2)
                    + content[best_line.end():]
                )
                tasks_path.write_text(new_content, encoding="utf-8")
                return f"Marked as done: {best_line.group(2).strip()}, {self._user_name}."
            return f"Couldn't find a task matching '{target}', {self._user_name}."
        except Exception as exc:
            log.error("Obsidian mark_done error: %s", exc)
            return f"Couldn't update task: {exc}"

    def _get_open_tasks(self) -> list[dict]:
        try:
            tasks_path = self._vault / self._tasks_file
            if not tasks_path.exists():
                return []
            content = tasks_path.read_text(encoding="utf-8")
            results = []
            for m in _TASK_REGEX.finditer(content):
                if m.group(1) == " ":
                    text     = m.group(2)
                    due_m    = re.search(r"\(due:\s*([^)]+)\)", text)
                    due      = due_m.group(1) if due_m else None
                    clean    = re.sub(r"\(due:[^)]+\)", "", text).replace(_TASK_TAG, "").strip()
                    results.append({"text": clean, "due": due})
            # Sort: tasks with due dates first, then the rest
            with_due    = sorted([t for t in results if t["due"]], key=lambda t: t["due"])
            without_due = [t for t in results if not t["due"]]
            return with_due + without_due
        except Exception as exc:
            log.error("Obsidian get_tasks error: %s", exc)
            return []

    # ── Search ─────────────────────────────────────────────────────────────────

    def _search_notes(self, query: str) -> str:
        try:
            results = self._run_search(query)
            if not results:
                return f"No notes found about '{query}', {self._user_name}."
            self._last_search_results = results

            if self._brain:
                snippets = "\n\n".join(
                    f"Note: {r['name']}\n{r['excerpt']}" for r in results[:3]
                )
                summary = self._brain.ask(
                    f"Summarise these Obsidian note excerpts about '{query}' in 2-3 sentences "
                    f"for voice. Plain prose only.\n\n{snippets}"
                )
                if summary:
                    return summary

            # Fallback: list note names
            names = ", ".join(r["name"] for r in results[:3])
            return f"Found {len(results)} notes about '{query}': {names}."
        except Exception as exc:
            log.error("Obsidian search error: %s", exc)
            return f"Search failed: {exc}"

    def _find_and_open_note(self, query: str) -> str:
        results = self._run_search(query)
        if not results:
            return f"No notes found about '{query}', {self._user_name}."
        best = results[0]
        path = best["path"]
        self._open_in_obsidian(path)
        return f"Opening the closest match: {best['name']}, {self._user_name}."

    def _run_search(self, query: str) -> list[dict]:
        results = []
        query_lower = query.lower()
        try:
            for md_path in self._vault.rglob("*.md"):
                try:
                    text = md_path.read_text(encoding="utf-8", errors="ignore")
                    if query_lower in text.lower():
                        # Extract best excerpt
                        idx = text.lower().find(query_lower)
                        start = max(0, idx - 80)
                        end   = min(len(text), idx + 160)
                        excerpt = text[start:end].replace("\n", " ").strip()
                        results.append({
                            "name":    md_path.stem,
                            "path":    md_path,
                            "excerpt": excerpt,
                            "score":   text.lower().count(query_lower),
                        })
                except (PermissionError, OSError):
                    continue
        except Exception as exc:
            log.error("Obsidian rglob error: %s", exc)
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:5]

    # ── Research & save ────────────────────────────────────────────────────────

    def _save_last_response_to_vault(self) -> str:
        if not self._last_response:
            return f"No recent response to save, {self._user_name}."
        title = _auto_title(self._last_response)
        return self._save_research_note(
            title   = f"Research - {title}",
            content = self._last_response,
            tags    = ["research", "atlas"],
        )

    def _research_and_save(self, query: str, original: str = "") -> str:
        if not self._brain:
            return f"Brain not connected, {self._user_name}."
        try:
            self._speak(f"Researching {query} and saving to Obsidian, {self._user_name}.")
            summary = self._brain.ask(
                f"Research this topic thoroughly: {query}. "
                f"Provide a well-structured, detailed summary suitable for saving to Obsidian. "
                f"Use plain prose, no markdown formatting."
            )
            if not summary:
                return f"Couldn't get a research summary, {self._user_name}."
            result = self._save_research_note(
                title   = f"Research - {query.title()}",
                content = summary,
                tags    = ["research", "atlas"],
                query   = query,
            )
            return result
        except Exception as exc:
            log.error("Obsidian research_and_save error: %s", exc)
            return f"Research failed: {exc}"

    def _save_research_note(self, title: str, content: str,
                             tags: list[str], query: str = "") -> str:
        try:
            now      = datetime.now()
            slug     = _slugify(title)
            filename = f"{now.strftime('%Y-%m-%d-%H-%M')}-{slug}.md"
            folder   = self._vault / self._research_folder
            folder.mkdir(parents=True, exist_ok=True)
            path     = folder / filename

            extra = {"query": query} if query else None
            body  = _frontmatter(title, tags, extra=extra)
            body += f"# {title}\n\n{content}\n"
            path.write_text(body, encoding="utf-8")
            self._last_note_path = path

            return f"Saved to Obsidian, {self._user_name}. Title: {title}."
        except Exception as exc:
            log.error("Obsidian save_research error: %s", exc)
            return f"Couldn't save: {exc}"

    # ── Dashboard data ─────────────────────────────────────────────────────────

    def get_widget_data(self) -> dict:
        """Returns data dict for the Obsidian feed widget (called every 60s)."""
        if not self._vault_ready():
            return {"ready": False}
        try:
            tasks         = self._get_open_tasks()
            task_count    = len(tasks)
            urgent        = tasks[0]["text"] if tasks else ""
            daily_exists  = self._daily_note_path().exists()
            recent_notes  = self._get_recent_notes(3)
            return {
                "ready":       True,
                "task_count":  task_count,
                "urgent_task": urgent,
                "daily_ready": daily_exists,
                "recent":      recent_notes,
                "vault_name":  self._vault.name if self._vault else "",
            }
        except Exception as exc:
            log.debug("Obsidian widget_data error: %s", exc)
            return {"ready": False}

    def get_tasks_for_briefing(self) -> list[str]:
        """Returns plain task text list for morning briefing."""
        return [t["text"] for t in self._get_open_tasks()[:5]]

    def _get_recent_notes(self, n: int = 3) -> list[dict]:
        notes = []
        for folder_name in (self._notes_folder, self._research_folder, self._daily_folder):
            folder = self._vault / folder_name
            if folder.exists():
                for p in sorted(folder.glob("*.md"),
                                 key=lambda f: f.stat().st_mtime, reverse=True)[:n]:
                    notes.append({"name": p.stem, "path": str(p)})
        notes.sort(key=lambda n: n.get("mtime", 0), reverse=True)
        return notes[:n]

    def _vault_summary(self) -> str:
        data = self.get_widget_data()
        if not data.get("ready"):
            return f"Vault not available, {self._user_name}."
        parts = []
        if data["task_count"]:
            parts.append(f"{data['task_count']} open task{'s' if data['task_count'] != 1 else ''}")
        if data["urgent_task"]:
            parts.append(f"most urgent: {data['urgent_task']}")
        if data["daily_ready"]:
            parts.append("daily note active")
        if data["recent"]:
            names = ", ".join(n["name"] for n in data["recent"])
            parts.append(f"recent notes: {names}")
        return (
            f"Obsidian vault '{data['vault_name']}': "
            + (", ".join(parts) or "vault is empty")
            + f", {self._user_name}."
        )

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _vault_ready(self) -> bool:
        return bool(self._vault and self._vault.exists())

    def _ensure_folders(self) -> None:
        if not self._vault:
            return
        for folder_name in (
            self._daily_folder,
            self._notes_folder,
            self._research_folder,
            (self._vault / self._tasks_file).parent.relative_to(self._vault),
            (self._vault / self._inbox_file).parent.relative_to(self._vault),
        ):
            try:
                (self._vault / folder_name).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                log.debug("Obsidian mkdir error for %s: %s", folder_name, exc)

    def _daily_note_path(self) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self._vault / self._daily_folder / f"{today}.md"

    def _open_in_obsidian(self, path: Path) -> None:
        try:
            subprocess.run(["open", "-a", "Obsidian", str(path)], timeout=4, check=False)
        except Exception:
            try:
                subprocess.run(["open", str(path)], timeout=4, check=False)
            except Exception:
                pass

    def _open_graph_view(self) -> str:
        if not self._vault_ready():
            return f"Obsidian vault isn't configured, {self._user_name}."
        vault_name = urllib.parse.quote(self._vault.name)
        uri = f"obsidian://open?vault={vault_name}"
        try:
            subprocess.Popen(["open", uri])
        except Exception as exc:
            log.error("Obsidian: failed to open vault URI: %s", exc)
            return f"Couldn't launch Obsidian, {self._user_name}."
        # AppleScript: activate Obsidian, wait for it to load, then open graph view (Cmd+Shift+G)
        script = (
            'tell application "Obsidian" to activate\n'
            'delay 1.5\n'
            'tell application "System Events"\n'
            '    tell process "Obsidian"\n'
            '        keystroke "g" using {command down, shift down}\n'
            '    end tell\n'
            'end tell'
        )
        try:
            subprocess.Popen(["osascript", "-e", script])
        except Exception as exc:
            log.debug("Obsidian: AppleScript graph trigger failed: %s", exc)
        return f"Opening your full knowledge graph in Obsidian, {self._user_name}."

    def _cmd_set_vault(self, raw_path: str) -> str:
        raw_path = raw_path.strip().strip('"').strip("'")
        p = Path(raw_path).expanduser()
        if not p.exists():
            return (
                f"That path doesn't exist, {self._user_name}. "
                f"Check the spelling: {raw_path}"
            )
        self._vault = p
        self._ensure_folders()
        log.info("Obsidian: vault set to %s", p)
        return (
            f"Obsidian vault set to {p.name}, {self._user_name}. "
            "You can start taking notes now."
        )

    @staticmethod
    def _after(lower: str, marker: str, preserve_case: bool = False,
                text_orig: Optional[str] = None) -> str:
        idx = lower.find(marker)
        if idx < 0:
            return ""
        result_lower = lower[idx + len(marker):].strip()
        if preserve_case and text_orig:
            start = idx + len(marker)
            result_lower = text_orig[start:].strip()
        return result_lower

    @staticmethod
    def _extract_mark_target(lower: str) -> Optional[str]:
        for pattern in (
            r"mark (.+?) (?:as done|done|complete|finished)",
            r"complete (?:task )?(.+)",
            r"finished? (?:with )?(.+)",
        ):
            m = re.search(pattern, lower)
            if m:
                target = m.group(1).strip()
                if len(target) > 2:
                    return target
        return None

    @staticmethod
    def _extract_research_query(lower: str) -> Optional[str]:
        patterns = [
            r"research (.+?) and save",
            r"research (.+?) (?:to|in|into) obsidian",
            r"research (.+)",
        ]
        for pat in patterns:
            m = re.search(pat, lower)
            if m:
                q = m.group(1).strip().rstrip(".")
                if len(q) > 2:
                    return q
        return None

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Quick word-overlap similarity 0-1."""
        wa = set(a.split())
        wb = set(b.split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / max(len(wa), len(wb))

    # ── Public API used by other modules (MarketModule, etc.) ─────────────────

    def write_note(self, rel_path: str, content: str) -> None:
        """Write (overwrite) a note at a vault-relative path. Creates parent dirs."""
        if not self._vault_ready():
            return
        path = self._vault / rel_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            log.info("Obsidian.write_note: %s", path.name)
        except Exception as exc:
            log.warning("Obsidian.write_note failed (%s): %s", rel_path, exc)

    def append_to_note(self, rel_path: str, content: str) -> None:
        """Append content to a note at a vault-relative path."""
        if not self._vault_ready():
            return
        path = self._vault / rel_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")
            with open(path, "a", encoding="utf-8") as f:
                f.write(content)
            log.info("Obsidian.append_to_note: %s", path.name)
        except Exception as exc:
            log.warning("Obsidian.append_to_note failed (%s): %s", rel_path, exc)

    def search_notes(self, query: str) -> list[dict]:
        """Search vault for notes matching query. Returns list of {name, path, excerpt}."""
        if not self._vault_ready():
            return []
        return self._run_search(query)

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _speak(self, text: str) -> None:
        if self._speak_cb:
            try:
                self._speak_cb(text)
            except Exception:
                pass
