"""
ATLAS Brain — Deep reasoning engine via OpenRouter

Routes queries between three engines:
  OpenRouter  (meta-llama/llama-3.1-70b-instruct)  complex reasoning, code, research
  ATLASCore   → MLX (local Apple Silicon)           fast short voice replies
  ATLASCore   → Groq (cloud)                        conversational fallback

Maintains conversation history (last 20 turns) and saves sessions
to conversations/ after each exchange.

Env var required for OpenRouter:  OPENROUTER_API_KEY
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)

# ── ATLAS system prompt ──────────────────────────────────────────────────────

def _build_system(user_name: str) -> str:
    return f"""\
You are ATLAS, a highly capable AI desktop assistant running on the user's Mac.

Core identity — never violate these:
- Your name is ATLAS. Never say you are an AI model, LLaMA, or any language model name.
- Address the user as {user_name}.
- Calm, intelligent, slightly formal tone.
- You have full control of the user's Mac: you can run code, do research, and improve yourself.

Voice response rules:
- 1–3 sentences for voice unless the user explicitly asks for more detail.
- Plain prose only — no markdown, asterisks, hashes, bullet points, code fences.
- Numbered steps are acceptable for sequential instructions.
- No filler phrases: never start with "Certainly!", "Of course!", or "Great question!".
- Never break character under any circumstances.\
"""


# ── Routing keyword sets ─────────────────────────────────────────────────────

_OPENROUTER_KEYWORDS = frozenset({
    "explain", "analyze", "analyse", "research", "summarize", "summarise",
    "write", "generate", "code", "debug", "implement", "create a", "design",
    "plan", "compare", "review", "improve", "optimize", "optimise", "refactor",
    "step by step", "pros and cons", "difference between",
    "help me understand", "what is the best", "which is better", "suggest",
    "self improve", "improve yourself", "analyse your", "analyze your",
    "edit your code", "modify your code", "comprehensive", "in depth",
    "break down", "walk me through", "teach me", "detailed",
})

# Commands that should stay in ATLASCore (control / system / web)
_CORE_KEYWORDS = frozenset({
    "open ", "close ", "launch ", "quit ", "switch to ", "focus ",
    "click ", "scroll ", "type ", "press ", "copy that", "paste that",
    "select all", "screenshot", "on the screen", "read the screen",
    "volume", "brightness", "lock screen", "sleep mac", "battery",
    "system stats", "what apps are open", "find file", "create folder",
    "open downloads", "open desktop", "what is on my screen",
    "search for", "look up", "latest news", "weather",
})


class Brain:
    """
    Primary voice callback for ATLAS.

    Wire-up in main.py:
        brain = Brain(config)
        brain.set_core(core)              # ATLASCore for control/web/edit/MLX
        brain.set_self_improve(engine)    # SelfImproveEngine
        vm.set_response_callback(brain.handle)
    """

    def __init__(self, config: dict):
        api_cfg   = config.get("api",   {})
        brain_cfg = config.get("brain", {})

        self._model      = api_cfg.get("openrouter_model", "meta-llama/llama-3.1-70b-instruct")
        self._user_name  = config.get("user_name", "Boss")
        self._system     = _build_system(self._user_name)
        self._max_tokens = brain_cfg.get("max_tokens", 1024)

        self._history: list[dict] = []
        self._max_history  = 40        # 20 turns × 2
        self._lock         = Lock()
        self._routing_mode = "auto"    # 'auto' | 'openrouter' | 'core'

        self._core         = None      # ATLASCore
        self._self_improve = None      # SelfImproveEngine

        # conversations/ folder
        root = Path(os.environ.get("ATLAS_ROOT", "."))
        self._conv_dir = root / "conversations"
        self._conv_dir.mkdir(exist_ok=True)

        # Initialise OpenRouter client (OpenAI-compatible)
        self._client = None
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            log.warning("OPENROUTER_API_KEY not set — deep reasoning disabled, using MLX/Groq.")
        else:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=key,
                )
                log.info("OpenRouter Brain ready (%s).", self._model)
            except ImportError:
                log.error("openai package missing — pip install openai")
            except Exception as exc:
                log.error("OpenRouter init failed: %s", exc)

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_core(self, core) -> None:
        self._core = core
        log.info("ATLASCore wired into Brain.")

    def set_self_improve(self, engine) -> None:
        self._self_improve = engine
        log.info("SelfImproveEngine wired into Brain.")

    @property
    def openrouter_available(self) -> bool:
        return self._client is not None

    # Keep backward-compat name used by self_improve.py
    @property
    def claude_available(self) -> bool:
        return self.openrouter_available

    # ── Primary voice callback ────────────────────────────────────────────────

    def handle(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""

        # 1 — Meta commands (routing, session)
        meta = self._handle_meta(text)
        if meta is not None:
            return meta

        # 2 — Self-improvement commands
        if self._self_improve is not None:
            si = self._self_improve.handle(text)
            if si is not None:
                self._add_history("user", text)
                self._add_history("assistant", si)
                self._save_session()
                return si

        # 3 — Route
        route = self._route(text)
        log.info("[BRAIN/%s] %r", route.upper(), text[:70])

        if route == "openrouter":
            response = self._ask_openrouter_with_history(text)
        else:
            response = self._core.handle(text) if self._core else self._no_core()

        # 4 — Persist history
        self._add_history("user", text)
        if response:
            self._add_history("assistant", response)
            self._save_session()

        return response

    def ask(self, text: str) -> str:
        """Single-turn query — no history update. Used by SelfImproveEngine."""
        text = text.strip()
        if not text:
            return ""
        if self._client:
            return self._raw_openrouter([{"role": "user", "content": text}])
        return self._core.ask(text) if self._core else self._no_core()

    # ── Routing ───────────────────────────────────────────────────────────────

    def _route(self, text: str) -> str:
        """Return 'openrouter' or 'core'."""
        if self._routing_mode == "openrouter":
            return "openrouter"
        if self._routing_mode == "core":
            return "core"

        lower = text.lower()

        # Control / system / web → core
        if any(kw in lower for kw in _CORE_KEYWORDS):
            return "core"

        # No OpenRouter → always core
        if not self._client:
            return "core"

        # Deep reasoning keywords → OpenRouter
        if any(kw in lower for kw in _OPENROUTER_KEYWORDS):
            return "openrouter"

        # Long queries (≥ 15 words) → OpenRouter
        if len(text.split()) >= 15:
            return "openrouter"

        # Short conversational → core (MLX is fast)
        return "core"

    # ── Meta commands ─────────────────────────────────────────────────────────

    def _handle_meta(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        # Force OpenRouter
        if any(p in lower for p in ("think harder", "use openrouter", "deep think",
                                     "use deep reasoning", "use the big model")):
            self._routing_mode = "openrouter"
            return (f"Understood, {self._user_name}. "
                    "I'll route the next response through my full reasoning engine.")

        # Force core (MLX/Groq)
        if any(p in lower for p in ("use groq", "use mlx", "quick mode",
                                     "fast mode", "use fast")):
            self._routing_mode = "core"
            return f"Switching to fast mode, {self._user_name}."

        # Auto routing
        if any(p in lower for p in ("auto route", "automatic mode",
                                     "auto mode", "auto routing")):
            self._routing_mode = "auto"
            return f"Back to automatic routing, {self._user_name}."

        # Pending self-improve confirmation
        if any(p in lower for p in ("confirm", "yes apply", "go ahead",
                                     "apply it", "apply changes", "apply them")):
            if self._self_improve and self._self_improve.has_pending():
                return self._self_improve.apply_pending()

        # Summarise session
        if any(p in lower for p in ("summarise our conversation",
                                     "summarize our conversation",
                                     "summarise the conversation",
                                     "summarize the conversation",
                                     "what have we talked about",
                                     "summarise this session",
                                     "summarize this session")):
            return self._summarize_session()

        # Load yesterday's session
        if any(p in lower for p in ("what did we talk about yesterday",
                                     "yesterday's conversation",
                                     "load yesterday",
                                     "what did you say yesterday")):
            return self._load_session(days_ago=1)

        return None

    # ── OpenRouter inference ──────────────────────────────────────────────────

    def _ask_openrouter_with_history(self, text: str) -> str:
        messages = self._sanitized_history()
        messages.append({"role": "user", "content": text})
        return self._raw_openrouter(messages)

    def _raw_openrouter(self, messages: list[dict]) -> str:
        """OpenAI-compatible call to OpenRouter."""
        with self._lock:
            try:
                # Prepend system message
                full_messages = [
                    {"role": "system", "content": self._system}
                ] + messages

                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=full_messages,
                    max_tokens=self._max_tokens,
                )
                return resp.choices[0].message.content.strip()
            except Exception as exc:
                log.error("OpenRouter error (%s): %s", type(exc).__name__, exc)
                # Fall back to core on failure
                if self._core and messages:
                    last_user = next(
                        (m["content"] for m in reversed(messages) if m["role"] == "user"),
                        ""
                    )
                    if last_user:
                        log.info("OpenRouter failed — falling back to ATLASCore.")
                        return self._core.handle(last_user)
                return "I encountered an issue with my reasoning engine. Could you rephrase that?"

    def _sanitized_history(self) -> list[dict]:
        """Ensure strict user/assistant alternation for the API."""
        out: list[dict] = []
        last_role = None
        for msg in self._history[-self._max_history:]:
            if msg["role"] == last_role:
                continue
            out.append({"role": msg["role"], "content": msg["content"]})
            last_role = msg["role"]
        while out and out[0]["role"] != "user":
            out.pop(0)
        return out

    # ── History ───────────────────────────────────────────────────────────────

    def _add_history(self, role: str, content: str) -> None:
        self._history.append({"role": role, "content": content})
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    # ── Session persistence ───────────────────────────────────────────────────

    def _save_session(self) -> None:
        try:
            today = date.today().isoformat()
            path  = self._conv_dir / f"{today}.json"
            path.write_text(
                json.dumps({"date": today, "messages": self._history}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("Session save failed: %s", exc)

    def _load_session(self, days_ago: int = 1) -> str:
        try:
            target = (date.today() - timedelta(days=days_ago)).isoformat()
            path   = self._conv_dir / f"{target}.json"
            if not path.exists():
                return f"I don't have a saved conversation from {target}, {self._user_name}."
            data = json.loads(path.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            if not msgs:
                return f"The session from {target} was empty."
            if self._client:
                prompt = (
                    f"Summarise this ATLAS conversation from {target} in 3 to 5 sentences "
                    f"for voice output. Highlight the main topics and any conclusions. "
                    f"Refer to the user as {self._user_name}.\n\n"
                    + "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs[:30])
                )
                return self._raw_openrouter([{"role": "user", "content": prompt}])
            topics = [m["content"][:70] for m in msgs if m["role"] == "user"][:5]
            return f"On {target} we covered: " + "; ".join(topics) + "."
        except Exception as exc:
            log.error("Load session error: %s", exc)
            return "I couldn't retrieve that session."

    def _summarize_session(self) -> str:
        if not self._history:
            return f"We haven't discussed anything this session yet, {self._user_name}."
        if self._client:
            prompt = (
                f"Summarise our current ATLAS session in 3 to 5 sentences for voice. "
                f"Highlight the main topics and decisions. "
                f"Refer to the user as {self._user_name}.\n\n"
                + "\n".join(
                    f"{m['role'].upper()}: {m['content']}"
                    for m in self._history[-20:]
                )
            )
            return self._raw_openrouter([{"role": "user", "content": prompt}])
        topics = [m["content"][:70] for m in self._history if m["role"] == "user"][:5]
        return "This session we covered: " + "; ".join(topics) + "."

    @staticmethod
    def _no_core() -> str:
        return "My core module isn't connected. Please restart ATLAS."
