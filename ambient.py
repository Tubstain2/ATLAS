"""
ATLAS Ambient Module — Always-on presence

Provides:
  1. Push-to-talk hotkey (Cmd+Space by default) via pynput
     Hold to speak, release to send — no wake word required
  2. Proactive intelligence — notices things on screen and speaks up
     Minimum 10 minutes between suggestions (configurable)
  3. Context memory between app switches
     "ATLAS I'm back" → summarises where you left off
  4. macOS menu bar presence (requires pyobjc-framework-AppKit)

This module does NOT replace the wake word — both work simultaneously.
Push-to-talk is the low-latency path; wake word handles hands-free use.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_CONTEXT_HISTORY_PATH = Path(__file__).resolve().parent / "memory" / "context_history.json"
_MAX_CONTEXT_HISTORY  = 50   # entries


class AmbientModule:
    """
    Always-on ambient presence for ATLAS.

    Usage in main.py:
        ambient = AmbientModule(config, brain=brain, voice_module=vm)
        ambient.start()
        ambient.stop()       # on shutdown
    """

    def __init__(self, config: dict, brain=None, voice_module=None,
                 vision=None, state_cb: Optional[Callable] = None):
        self._cfg          = config
        self._brain        = brain
        self._voice        = voice_module
        self._vision       = vision
        self._state_cb     = state_cb
        self._stop_event   = threading.Event()

        # Push-to-talk
        self._ptt_hotkey   = config.get("push_to_talk_hotkey", "cmd+space")
        self._ptt_active   = False
        self._ptt_listener = None

        # Proactive suggestions
        self._proactive_enabled  = config.get("proactive_suggestions", True)
        self._proactive_interval = int(
            config.get("proactive_min_interval_minutes", 10)
        ) * 60
        self._last_suggestion_at = 0.0
        self._proactive_thread   = None

        # Context history
        self._context_history: list[dict] = []
        self._current_app  = ""
        self._current_file = ""
        self._load_context_history()

        # Battery / calendar check intervals
        self._last_battery_warn = 0.0
        self._battery_threshold = 15   # % — warn below this

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._start_ptt()
        if self._proactive_enabled:
            self._proactive_thread = threading.Thread(
                target=self._proactive_loop, daemon=True, name="atlas-ambient"
            )
            self._proactive_thread.start()
        log.info("Ambient module started (PTT=%s, proactive=%s).",
                 self._ptt_hotkey, self._proactive_enabled)

    def stop(self):
        self._stop_event.set()
        self._stop_ptt()
        log.info("Ambient module stopped.")

    # ── Push-to-talk ──────────────────────────────────────────────────────────

    def _start_ptt(self):
        try:
            from pynput import keyboard as kb

            # Parse hotkey string: "cmd+space" → {Key.cmd, Key.space}
            key_names = [k.strip().lower() for k in self._ptt_hotkey.split("+")]
            self._ptt_keys_required = self._parse_ptt_keys(key_names, kb)
            self._ptt_keys_held: set = set()

            def _on_press(key):
                key_id = self._key_id(key)
                self._ptt_keys_held.add(key_id)
                if self._ptt_keys_required.issubset(self._ptt_keys_held):
                    if not self._ptt_active:
                        self._ptt_active = True
                        self._on_ptt_start()

            def _on_release(key):
                key_id = self._key_id(key)
                self._ptt_keys_held.discard(key_id)
                if self._ptt_active and not self._ptt_keys_required.issubset(self._ptt_keys_held):
                    self._ptt_active = False
                    self._on_ptt_end()

            from pynput.keyboard import Listener
            self._ptt_listener = Listener(on_press=_on_press, on_release=_on_release)
            self._ptt_listener.daemon = True
            self._ptt_listener.start()
            log.info("Push-to-talk ready: hold %s to speak.", self._ptt_hotkey)

        except ImportError:
            log.warning("pynput not installed — push-to-talk disabled. pip install pynput")
        except Exception as exc:
            log.warning("Push-to-talk init failed: %s", exc)

    def _stop_ptt(self):
        if self._ptt_listener:
            try:
                self._ptt_listener.stop()
            except Exception:
                pass
            self._ptt_listener = None

    def _parse_ptt_keys(self, key_names: list[str], kb) -> set:
        key_map = {
            "cmd":     kb.Key.cmd,
            "command": kb.Key.cmd,
            "ctrl":    kb.Key.ctrl,
            "control": kb.Key.ctrl,
            "alt":     kb.Key.alt,
            "option":  kb.Key.alt,
            "shift":   kb.Key.shift,
            "space":   kb.Key.space,
            "tab":     kb.Key.tab,
            "esc":     kb.Key.esc,
        }
        result = set()
        for name in key_names:
            if name in key_map:
                result.add(key_map[name])
            elif len(name) == 1:
                result.add(name)
        return result

    def _key_id(self, key) -> object:
        try:
            from pynput import keyboard as kb
            if isinstance(key, kb.Key):
                return key
            return key.char
        except Exception:
            return key

    def _on_ptt_start(self):
        log.info("PTT: hold detected — recording")
        if self._state_cb:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._state_cb("listening"))
        if self._voice:
            try:
                self._voice._worker._recording_forced = True
            except Exception:
                pass

    def _on_ptt_end(self):
        log.info("PTT: released — sending command")
        if self._voice:
            try:
                self._voice._worker._recording_forced = False
            except Exception:
                pass

    # ── Proactive intelligence loop ───────────────────────────────────────────

    def _proactive_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(60)   # check every minute
            if self._stop_event.is_set():
                break

            now = time.time()
            if now - self._last_suggestion_at < self._proactive_interval:
                continue

            suggestion = self._generate_proactive_suggestion()
            if suggestion:
                self._last_suggestion_at = now
                log.info("[PROACTIVE] %s", suggestion)
                if self._voice:
                    self._voice.speak(suggestion)

    def _generate_proactive_suggestion(self) -> Optional[str]:
        """
        Check various signals and return a proactive suggestion string,
        or None if nothing worth saying.
        """
        # Battery warning
        battery = self._get_battery_level()
        if battery is not None and battery <= self._battery_threshold:
            if time.time() - self._last_battery_warn > 1800:  # 30 min cooldown
                self._last_battery_warn = time.time()
                return f"Boss, your battery is at {battery} percent."

        # No other checks pass → no suggestion
        return None

    def _get_battery_level(self) -> Optional[int]:
        if platform.system() != "Darwin":
            return None
        try:
            out = subprocess.run(
                ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3
            ).stdout
            import re
            m = re.search(r'(\d+)%', out)
            return int(m.group(1)) if m else None
        except Exception:
            return None

    # ── Context history ───────────────────────────────────────────────────────

    def update_context(self, app: str, file: str = ""):
        """Called by ContextManager when the user switches apps."""
        if app == self._current_app and file == self._current_file:
            return

        if self._current_app:
            self._context_history.append({
                "app":  self._current_app,
                "file": self._current_file,
                "time": datetime.now().isoformat(timespec="seconds"),
            })
            if len(self._context_history) > _MAX_CONTEXT_HISTORY:
                self._context_history = self._context_history[-_MAX_CONTEXT_HISTORY:]
            self._save_context_history()

        self._current_app  = app
        self._current_file = file

    def handle(self, text: str) -> Optional[str]:
        """Return a response if this is an ambient/context command, else None."""
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas i am back", "atlas i'm back",
                                     "atlas what was i doing")):
            return self._recall_context()

        if "atlas what was i doing in" in lower:
            # "atlas what was i doing in VS Code"
            target_app = lower.split("atlas what was i doing in")[-1].strip().rstrip("?.")
            return self._recall_app_context(target_app)

        if any(p in lower for p in ("atlas proactive on", "atlas enable suggestions",
                                     "atlas enable proactive")):
            self._proactive_enabled = True
            if not self._proactive_thread or not self._proactive_thread.is_alive():
                self._proactive_thread = threading.Thread(
                    target=self._proactive_loop, daemon=True, name="atlas-ambient"
                )
                self._proactive_thread.start()
            return "Proactive suggestions enabled."

        if any(p in lower for p in ("atlas proactive off", "atlas disable suggestions",
                                     "atlas disable proactive")):
            self._proactive_enabled = False
            return "Proactive suggestions disabled."

        return None

    def _recall_context(self) -> str:
        if not self._context_history:
            return (f"I don't have any context history yet, Boss. "
                    f"You are currently in {self._current_app or 'an unknown app'}.")
        last = self._context_history[-1]
        app  = last.get("app", "an unknown app")
        file = last.get("file", "")
        t    = last.get("time", "")
        if file:
            return f"Before this, Boss, you were working in {app} on {file}."
        return f"Before this, Boss, you were in {app}."

    def _recall_app_context(self, target_app: str) -> str:
        hits = [
            h for h in reversed(self._context_history)
            if target_app.lower() in h.get("app", "").lower()
        ]
        if not hits:
            return f"I don't have any recorded context for {target_app}, Boss."
        h    = hits[0]
        file = h.get("file", "")
        t    = h.get("time", "")
        if file:
            return f"In {h['app']} you were working on {file}."
        return f"You were in {h['app']} at {t}."

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_context_history(self):
        try:
            if _CONTEXT_HISTORY_PATH.exists():
                self._context_history = json.loads(
                    _CONTEXT_HISTORY_PATH.read_text(encoding="utf-8")
                )
        except Exception as exc:
            log.debug("Context history load failed: %s", exc)

    def _save_context_history(self):
        try:
            _CONTEXT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CONTEXT_HISTORY_PATH.write_text(
                json.dumps(self._context_history, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.debug("Context history save failed: %s", exc)
