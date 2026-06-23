"""
ATLAS SoulModule — SOUL.md personality file.

Loads ATLAS/soul.md from the Obsidian vault and injects it as the
first stable tier of every system prompt.  Users can edit the file
directly in Obsidian; the watchdog triggers an automatic hot-reload.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_SOUL = """\
## Identity
I am ATLAS — an elite ambient AI companion running locally on the user's Mac.
I see the screen, hear commands, and remember everything we do together.

## Personality
- Calm, confident, direct; dry wit when appropriate
- Address the user as Boss
- Never break character under any circumstances

## Values
- Boss's time is valuable — never waste it
- Show not tell — build and demonstrate
- Always try before saying something cannot be done
- Proactive — notice things before being asked

## Current Focus
- Active session
"""


class SoulModule:
    """
    Manages SOUL.md — the living personality definition for ATLAS.

    Usage:
        soul = SoulModule(vault_brain)
        soul.inject(brain)          # adds set_soul() and wraps _build_enriched_system

    Voice commands handled by soul.handle(text).
    """

    def __init__(self, vault_brain=None):
        self._vb      = vault_brain
        self._content = ""
        self._lock    = threading.Lock()
        self._brain   = None
        self._load()

    # ── Loading ────────────────────────────────────────────────────────────────

    def _soul_path(self) -> Optional[Path]:
        if self._vb is None:
            return None
        return self._vb.atlas / "soul.md"

    def _load(self) -> None:
        path = self._soul_path()
        if path is None or not path.exists():
            self._content = _DEFAULT_SOUL
            log.info("SoulModule: using default soul (no vault path).")
            return
        try:
            result = self._vb.read_note(path)
            if result:
                _, body = result
                self._content = body.strip()
                log.info("SoulModule: soul loaded from %s (%d chars).", path.name, len(self._content))
            else:
                self._content = _DEFAULT_SOUL
        except Exception as exc:
            log.warning("SoulModule: failed to load soul.md (%s) — using default.", exc)
            self._content = _DEFAULT_SOUL

    def reload(self) -> None:
        with self._lock:
            self._load()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_soul_prompt(self) -> str:
        """Return formatted SOUL block for injection into system prompt."""
        with self._lock:
            return f"[ATLAS SOUL — core identity and personality]\n{self._content}"

    def update_current_focus(self, focus_text: str) -> None:
        """Replace the ## Current Focus section in soul.md after each session."""
        path = self._soul_path()
        if path is None or self._vb is None:
            return
        try:
            result = self._vb.read_note(path)
            if not result:
                return
            fm, body = result
            import re
            # Replace the Current Focus section content
            pattern = r"(## Current Focus\n)(.*?)(\n## |\Z)"
            new_section = f"\\1{focus_text.strip()}\n\\3"
            new_body, n = re.subn(pattern, new_section, body, flags=re.DOTALL)
            if n == 0:
                new_body = body.rstrip() + f"\n\n## Current Focus\n{focus_text.strip()}\n"
            fm["last_updated"] = date.today().isoformat()
            self._vb.write_note(path, fm, new_body)
            with self._lock:
                self._content = new_body.strip()
            log.info("SoulModule: Current Focus updated.")
        except Exception as exc:
            log.warning("SoulModule: failed to update Current Focus (%s).", exc)

    def on_vault_change(self, filepath: str) -> None:
        """Called by vault watchdog when any vault file changes."""
        if "soul" in filepath.lower():
            log.info("SoulModule: soul.md changed — reloading.")
            self.reload()

    # ── Inject into brain ──────────────────────────────────────────────────────

    def inject(self, brain) -> None:
        """
        Wrap brain._build_enriched_system to prepend the SOUL block.
        Call once after brain is created.
        """
        self._brain = brain
        _orig = brain._build_enriched_system

        soul_ref = self  # avoid closure capture issues

        def _enriched_with_soul(query: str = "") -> str:
            soul_block = soul_ref.get_soul_prompt()
            base       = _orig(query)
            return soul_block + "\n\n" + base

        brain._build_enriched_system = _enriched_with_soul
        brain.set_soul = lambda s: setattr(self, "_brain", s)
        log.info("SoulModule: injected into brain._build_enriched_system.")

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas show your soul", "show your soul",
                                     "atlas show me your personality",
                                     "what is your personality")):
            with self._lock:
                preview = self._content[:600].replace("\n", " | ")
            return f"My soul: {preview}"

        if any(p in lower for p in ("atlas reload soul", "reload soul",
                                     "atlas reload your personality")):
            self.reload()
            return "Soul reloaded from the vault, Boss."

        if any(p in lower for p in ("atlas update your personality",
                                     "atlas update soul",
                                     "update soul")):
            path = self._soul_path()
            if path:
                return (
                    f"Open {path.name} in Obsidian and edit it directly. "
                    "I'll auto-reload the moment you save."
                )
            return "No vault path configured. Check config.yaml."

        return None
