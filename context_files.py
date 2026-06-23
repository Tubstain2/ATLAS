"""
ATLAS Context Files — project-specific context injection.

Scans the current working directory (walking up to git root) for
ATLAS.md or AGENTS.md and injects the content as the context tier
of every system prompt.  Pattern borrowed from Hermes agent.

Behaviours:
  • On startup and every 60s, scans CWD for context files
  • If CWD changes (detected via VS Code workspace), rescans
  • Context injected between SOUL block and volatile memory block
  • Files are read fresh each scan — edits take effect within 60s

Voice commands:
  "ATLAS create a context file for this project" → writes ATLAS.md in CWD
  "ATLAS show project context" → reads and speaks active context file
  "ATLAS refresh project context" → forces rescan now
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CONTEXT_FILENAMES = ("ATLAS.md", "AGENTS.md", ".atlas.md", ".agents.md")
_SCAN_INTERVAL     = 60   # seconds between automatic rescans


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk up from start until we find a .git directory or hit /."""
    current = start
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _find_context_file(cwd: Path) -> Optional[Path]:
    """
    Walk from cwd up to git root looking for ATLAS.md or AGENTS.md.
    Returns the first match (closest to cwd wins).
    """
    git_root = _find_git_root(cwd) or cwd
    current  = cwd
    while True:
        for fname in _CONTEXT_FILENAMES:
            candidate = current / fname
            if candidate.exists():
                return candidate
        if current == git_root or current == current.parent:
            break
        current = current.parent
    return None


_ATLAS_MD_TEMPLATE = """\
# Project Context

## Overview
[Brief description of this project]

## Architecture
[Key architectural decisions and components]

## Current State
[What's done, what's in progress]

## ATLAS Instructions
[How ATLAS should behave in this project]
- Language/framework preferences
- Testing requirements
- File structure notes

## Key Files
[Important files and their purpose]
"""


class ContextFilesModule:
    """
    Manages project-specific context file injection.

    Usage:
        ctx = ContextFilesModule(brain, speak_cb)
        ctx.start()
        ctx.inject(brain)   # wraps _build_enriched_system
    """

    def __init__(self, brain=None, speak_cb=None):
        self._brain    = brain
        self._speak    = speak_cb or (lambda s: None)
        self._lock     = threading.Lock()
        self._cwd      = Path.cwd()
        self._context_path: Optional[Path] = None
        self._context_content: str = ""
        self._stop     = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._scan_now()
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="atlas-context-scan"
        )
        self._thread.start()
        log.info("ContextFilesModule: scanner started (CWD=%s).", self._cwd)

    def stop(self) -> None:
        self._stop.set()

    # ── Scanning ──────────────────────────────────────────────────────────────

    def _scan_now(self) -> None:
        cwd  = Path.cwd()
        path = _find_context_file(cwd)
        with self._lock:
            self._cwd = cwd
            if path != self._context_path:
                if path:
                    try:
                        content = path.read_text(encoding="utf-8")[:8000]
                        self._context_content = content
                        self._context_path    = path
                        log.info("ContextFiles: loaded %s (%d chars).", path.name, len(content))
                    except Exception as exc:
                        log.warning("ContextFiles: failed to read %s: %s", path, exc)
                else:
                    if self._context_path:
                        log.info("ContextFiles: context file removed — cleared.")
                    self._context_content = ""
                    self._context_path    = None
            elif path and self._context_path:
                # Re-read in case it changed
                try:
                    new_content = path.read_text(encoding="utf-8")[:8000]
                    if new_content != self._context_content:
                        self._context_content = new_content
                        log.info("ContextFiles: %s updated — %d chars.", path.name, len(new_content))
                except Exception:
                    pass

    def _scan_loop(self) -> None:
        while not self._stop.wait(_SCAN_INTERVAL):
            try:
                self._scan_now()
            except Exception as exc:
                log.debug("ContextFiles: scan error: %s", exc)

    # ── Content access ─────────────────────────────────────────────────────────

    def get_context_block(self) -> str:
        """Return formatted context block for injection into system prompt."""
        with self._lock:
            if not self._context_content:
                return ""
            path_str = str(self._context_path.name) if self._context_path else "context"
            return (
                f"[PROJECT CONTEXT — {path_str}]\n"
                f"{self._context_content[:4000]}"
            )

    def update_cwd(self, new_cwd: str) -> None:
        """Called when VS Code / context manager detects CWD change."""
        new_path = Path(new_cwd)
        if new_path != self._cwd:
            log.info("ContextFiles: CWD changed to %s — rescanning.", new_path)
            self._cwd = new_path
            # Switch OS CWD so _find_context_file works correctly
            try:
                os.chdir(new_path)
            except Exception:
                pass
            self._scan_now()

    # ── Brain injection ────────────────────────────────────────────────────────

    def inject(self, brain) -> None:
        """Wrap brain._build_enriched_system to inject project context."""
        _orig = brain._build_enriched_system
        ctx_ref = self

        def _enriched_with_ctx(query: str = "") -> str:
            base    = _orig(query)
            ctx_blk = ctx_ref.get_context_block()
            if ctx_blk:
                return base + "\n\n" + ctx_blk
            return base

        brain._build_enriched_system = _enriched_with_ctx
        log.info("ContextFiles: injected into brain._build_enriched_system.")

    # ── Context file creation ──────────────────────────────────────────────────

    def create_context_file(self, project_name: str = "") -> Optional[Path]:
        """Write ATLAS.md in CWD with a template."""
        target = self._cwd / "ATLAS.md"
        if target.exists():
            return target   # don't overwrite
        try:
            header = f"# {project_name or self._cwd.name} — ATLAS Context\n"
            target.write_text(header + _ATLAS_MD_TEMPLATE, encoding="utf-8")
            log.info("ContextFiles: created %s", target)
            self._scan_now()
            return target
        except Exception as exc:
            log.warning("ContextFiles: create failed: %s", exc)
            return None

    def create_context_file_with_llm(self) -> Optional[Path]:
        """Ask LLM to generate a context file based on current directory."""
        if self._brain is None:
            return self.create_context_file()
        try:
            # Gather directory listing
            files = [f.name for f in sorted(self._cwd.iterdir())[:20] if not f.name.startswith(".")]
            prompt = (
                f"Generate an ATLAS.md context file for a project in '{self._cwd.name}'.\n"
                f"Files found: {', '.join(files)}\n\n"
                "Write a helpful ATLAS.md with sections: Overview, Architecture, "
                "Current State, ATLAS Instructions, Key Files. Plain markdown only."
            )
            content = self._brain.ask(prompt)
            if not content:
                return self.create_context_file()
            target = self._cwd / "ATLAS.md"
            target.write_text(content, encoding="utf-8")
            self._scan_now()
            log.info("ContextFiles: LLM-generated ATLAS.md written to %s", target)
            return target
        except Exception as exc:
            log.warning("ContextFiles: LLM create failed: %s — using template.", exc)
            return self.create_context_file()

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas create a context file for this project",
                                     "atlas create context file",
                                     "atlas add context file here")):
            path = self.create_context_file_with_llm()
            if path:
                return f"Context file created at {path.name} in {self._cwd.name}, Boss. Edit it in any editor."
            return "Couldn't create context file — check permissions."

        if any(p in lower for p in ("atlas show project context",
                                     "atlas what is the project context",
                                     "atlas project context")):
            with self._lock:
                if not self._context_content:
                    return f"No context file found in {self._cwd.name} or its parents, Boss."
                path_name = self._context_path.name if self._context_path else "context file"
                preview = self._context_content[:400].replace("\n", " | ")
            return f"Active context from {path_name}: {preview}"

        if any(p in lower for p in ("atlas refresh project context",
                                     "atlas reload context",
                                     "atlas rescan context")):
            self._scan_now()
            with self._lock:
                if self._context_path:
                    return f"Context refreshed from {self._context_path.name}, Boss."
            return "No context file found after rescan."

        return None
