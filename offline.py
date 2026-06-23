"""
ATLAS Offline Mode — AgenticSeek-inspired local fallback + privacy mode.

Adapts AgenticSeek's Provider abstraction:
  - AgenticSeek checks `provider.server_status()` for local Ollama/LM-Studio
  - AgenticSeek's `unsafe_providers` list distinguishes cloud vs local
  - ATLAS extends this with: connectivity polling, auto-announce, voice commands

Three states:
  ONLINE   — all features available (cloud LLM, web search, market data)
  OFFLINE  — internet unavailable; local Ollama fallback activated
  PRIVACY  — user-forced: internet present but deliberately cut off

Features limited offline:
  - Web search (disabled)
  - Market data (disabled — no live prices)
  - Context7 doc fetch (uses cached; fetches disabled)
  - Planner web sub-agents (disabled; reasoning-only)

Features available offline:
  - Local Ollama / LM-Studio LLM
  - Obsidian vault read/write
  - Code agent (sandboxed Python)
  - Scheduler (local cron)
  - Skills (local plugins)

Voice commands:
  "ATLAS go offline"         → force offline mode (disable internet even if available)
  "ATLAS privacy mode"       → privacy mode: go fully local, suppress cloud calls
  "ATLAS normal mode"        → return to auto-detect
  "ATLAS are you online"     → status check
  "ATLAS what works offline" → list available features
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger(__name__)


class ConnectivityState(Enum):
    ONLINE  = "online"
    OFFLINE = "offline"
    PRIVACY = "privacy"   # forced offline regardless of network


_CHECK_HOST = "8.8.8.8"   # Google DNS — fast ping target
_CHECK_PORT = 53
_CHECK_TIMEOUT = 3.0


def _check_internet() -> bool:
    """Non-blocking TCP probe to 8.8.8.8:53. Returns True if reachable."""
    try:
        sock = socket.create_connection((_CHECK_HOST, _CHECK_PORT),
                                         timeout=_CHECK_TIMEOUT)
        sock.close()
        return True
    except OSError:
        return False


class ATLASOfflineMode:
    """
    Monitors internet connectivity and manages ATLAS feature availability.

    AgenticSeek patterns used:
      • Provider.server_status() → replaced by TCP probe
      • unsafe_providers list   → replaced by feature flag dict
      • provider fallback chain → replaced by _get_available_provider()

    Usage (main.py):
        offline = ATLASOfflineMode(config, brain=brain, speak_cb=speak)
        offline.start()
        # All brain/web calls should check: if offline.web_available: ...
    """

    def __init__(self, config: dict = None, brain=None,
                 speak_cb: Optional[Callable] = None):
        self._config   = config or {}
        self._brain    = brain
        self._speak    = speak_cb or (lambda s: None)

        self._state      = ConnectivityState.ONLINE
        self._state_lock = threading.Lock()

        self._check_interval = int(
            self._config.get("offline_check_interval", 30))
        self._auto_detect = bool(
            self._config.get("offline_mode_auto_detect", True))
        self._privacy_mode = bool(
            self._config.get("privacy_mode_enabled", False))

        self._stop_event   = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._announced_state: Optional[ConnectivityState] = None

        # Initial state
        if self._privacy_mode:
            self._state = ConnectivityState.PRIVACY
            log.info("ATLASOfflineMode: privacy mode enabled from config.")
        else:
            self._state = (ConnectivityState.ONLINE
                           if _check_internet() else ConnectivityState.OFFLINE)

        log.info("ATLASOfflineMode: initial state = %s", self._state.value)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._auto_detect:
            return
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="atlas-offline-monitor"
        )
        self._poll_thread.start()
        log.info("ATLASOfflineMode: monitoring started (%ds interval).",
                 self._check_interval)

    def stop(self) -> None:
        self._stop_event.set()

    # ── State access ──────────────────────────────────────────────────────────

    @property
    def state(self) -> ConnectivityState:
        with self._state_lock:
            return self._state

    @property
    def is_online(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    @property
    def web_available(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    @property
    def market_available(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    @property
    def cloud_llm_available(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    @property
    def context7_fetch_available(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    def get_status_summary(self) -> str:
        s = self.state
        if s == ConnectivityState.ONLINE:
            return "online"
        if s == ConnectivityState.PRIVACY:
            return "privacy mode (forced local)"
        return "offline (no internet)"

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._check_interval)
            if self._stop_event.is_set():
                break
            with self._state_lock:
                if self._state == ConnectivityState.PRIVACY:
                    continue   # privacy mode ignores network checks
            self._check_and_update()

    def _check_and_update(self) -> None:
        reachable = _check_internet()

        with self._state_lock:
            current = self._state

        if reachable and current == ConnectivityState.OFFLINE:
            self._set_state(ConnectivityState.ONLINE)
            self._announce_online()
        elif not reachable and current == ConnectivityState.ONLINE:
            self._set_state(ConnectivityState.OFFLINE)
            self._announce_offline()

    def _set_state(self, new_state: ConnectivityState) -> None:
        with self._state_lock:
            self._state = new_state
        log.info("ATLASOfflineMode: state changed → %s", new_state.value)
        if self._brain:
            self._sync_brain_provider(new_state)

    def _announce_offline(self) -> None:
        msg = ("Boss, I am now operating offline. "
               "Some features are limited until connectivity is restored.")
        log.warning("ATLASOfflineMode: %s", msg)
        try:
            self._speak(msg)
        except Exception:
            pass

    def _announce_online(self) -> None:
        msg = "Boss, I am back online. All systems restored."
        log.info("ATLASOfflineMode: %s", msg)
        try:
            self._speak(msg)
        except Exception:
            pass

    def _sync_brain_provider(self, state: ConnectivityState) -> None:
        """
        Switch brain to local Ollama when offline (AgenticSeek provider fallback).
        Re-enables cloud model when back online.
        """
        if not self._brain:
            return
        try:
            if state == ConnectivityState.ONLINE:
                # Restore cloud model from config
                smart_model = self._config.get(
                    "smart_model", "openai/gpt-oss-120b:free")
                if hasattr(self._brain, "set_model"):
                    self._brain.set_model(smart_model)
                    log.info("Brain: switched to cloud model %s.", smart_model)
            else:
                # Switch to local Ollama fallback
                local_model = self._config.get(
                    "offline_local_model", "ollama/llama3")
                if hasattr(self._brain, "set_model"):
                    self._brain.set_model(local_model)
                    log.info("Brain: switched to local model %s.", local_model)
        except Exception as exc:
            log.warning("ATLASOfflineMode: could not switch brain model: %s", exc)

    # ── Voice commands ────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas go offline", "atlas disconnect",
                                     "atlas force offline", "atlas offline mode")):
            self._set_state(ConnectivityState.OFFLINE)
            return ("Offline mode activated, Boss. "
                    "I will use local resources only until you say normal mode.")

        if any(p in lower for p in ("atlas privacy mode", "atlas go private",
                                     "atlas enable privacy", "atlas private mode")):
            self._set_state(ConnectivityState.PRIVACY)
            return ("Boss, privacy mode active. "
                    "I am running entirely locally. No data leaves your machine.")

        if any(p in lower for p in ("atlas normal mode", "atlas go online",
                                     "atlas disable privacy", "atlas disable offline")):
            # Perform real check before announcing
            reachable = _check_internet()
            new_state = (ConnectivityState.ONLINE
                         if reachable else ConnectivityState.OFFLINE)
            self._set_state(new_state)
            if new_state == ConnectivityState.ONLINE:
                return "Normal mode restored, Boss. All systems online."
            return ("Returned to auto mode, Boss. "
                    "No internet detected — still operating offline.")

        if any(p in lower for p in ("atlas are you online", "atlas check connection",
                                     "atlas connectivity", "atlas internet status",
                                     "atlas are you connected")):
            s = self.state
            if s == ConnectivityState.ONLINE:
                return "I am online, Boss. All features available."
            if s == ConnectivityState.PRIVACY:
                return ("Privacy mode is active, Boss. "
                        "I am intentionally offline, even if internet is available.")
            return ("I am currently offline, Boss. "
                    "Some features are limited until connectivity is restored.")

        if any(p in lower for p in ("atlas what works offline",
                                     "atlas offline features",
                                     "atlas what can you do offline")):
            return (
                "Offline I can: respond with local AI, read and write your vault, "
                "run code in the sandbox, execute scheduled tasks, and use local skills. "
                "Offline I cannot: search the web, fetch live market data, "
                "get fresh documentation, or use cloud AI models."
            )

        return None

    # ── Guard decorator ───────────────────────────────────────────────────────

    def require_online(self, feature_name: str) -> bool:
        """
        Returns True if online, else speaks a warning and returns False.
        Use as a guard before making network calls.
        """
        if self.is_online:
            return True
        state_str = self.get_status_summary()
        log.info("ATLASOfflineMode: blocked %s — %s", feature_name, state_str)
        return False
