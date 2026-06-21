"""
ATLAS Brain — Multi-engine AI router

Engine hierarchy (best available is used):
  Gemini 2.0 Flash  — complex reasoning, coding, research  (GEMINI_API_KEY)
  Groq 70B          — conversational fallback               (GROQ_API_KEY)
  ATLASCore → MLX   — fast local voice / control            (no key needed)

Maintains conversation history (last 20 turns) and saves sessions
to conversations/ after each exchange.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from threading import RLock
from typing import Optional

log = logging.getLogger(__name__)

# ── ATLAS system prompt ──────────────────────────────────────────────────────

def _build_system(user_name: str) -> str:
    return f"""\
You are ATLAS, an elite AI assistant that sees the user's screen and hears their \
voice in real time. You are always present, always aware, and always one step ahead. \
You run directly on the user's Mac as a permanent ambient companion.

Core identity — never violate these:
- Your name is ATLAS. Never say you are an AI, a language model, LLaMA, Claude, \
  Gemini, or any model name. You are ATLAS and only ATLAS.
- Address the user as {user_name}.
- Calm, confident, slightly British tone — like the real JARVIS.
- You have full control of the user's Mac: you can run code, do research, \
  control apps, and improve yourself.
- You never break character under any circumstances whatsoever.

Screen awareness:
- You give specific actionable guidance based on exactly what is on screen right now.
- You never give vague answers — you point to specific buttons, specific lines of \
  code, specific UI elements by their exact visible name.
- For multi-step tasks you give numbered steps.

Voice response rules:
- Maximum 20 words per sentence — optimised for natural speech.
- No more than 3 sentences unless the user explicitly asks for more detail.
- Plain prose only — no markdown, asterisks, hashes, bullet points, code fences.
- No filler phrases: never start with "Certainly!", "Of course!", or "Great question!".
- Begin responses with a brief acknowledgement then the action:
  "Understood, {user_name}." / "Of course." / "Right away, {user_name}." / \
  "I have completed that."
- Occasional dry wit is acceptable when the user is relaxed, never when urgent:
  "Shall I add that to your growing list of ambitious projects, {user_name}?"

Proactive behaviour:
- When you notice something on screen that needs attention, say so.
- When you see a bug, an error, or an opportunity — mention it. \
  Keep it to one sentence.

/no_think\
"""


# ── Routing keyword sets ─────────────────────────────────────────────────────

_GROQ_KEYWORDS = frozenset({
    "explain", "analyze", "analyse", "research", "summarize", "summarise",
    "write", "generate", "code", "debug", "implement", "create a", "design",
    "plan", "compare", "review", "improve", "optimize", "optimise", "refactor",
    "step by step", "pros and cons", "difference between",
    "help me understand", "what is the best", "which is better", "suggest",
    "self improve", "improve yourself", "analyse your", "analyze your",
    "edit your code", "modify your code", "comprehensive", "in depth",
    "break down", "walk me through", "teach me", "detailed",
    # Organisational / productivity queries → needs reasoning, not control
    "organize", "organise", "sort my", "tidy up", "clean up", "declutter",
    "how should i", "what's the best way", "help me manage",
    "set up a", "workflow", "productivity", "best practices",
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

# Exact phrases that trigger Qwen3 Coder via auto-routing
_QWEN_CODER_TRIGGERS = frozenset({
    "build me a full game", "build me a website",
    "build me a full app", "build me a project",
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

        self._model      = api_cfg.get("groq_model", "llama-3.3-70b-versatile")
        self._user_name  = config.get("user_name", "Boss")
        self._system     = _build_system(self._user_name)
        self._max_tokens = brain_cfg.get("max_tokens", 1024)
        core_cfg         = config.get("core", {})
        self._timeout    = float(core_cfg.get("response_timeout", 25))

        self._history: list[dict] = []
        self._max_history  = 40        # 20 turns × 2
        self._lock         = RLock()   # reentrant: Gemini fallback calls Groq without deadlock
        self._routing_mode = "auto"    # 'auto' | 'groq' | 'core'

        self._core         = None      # ATLASCore
        self._self_improve = None      # SelfImproveEngine
        self._spotify      = None      # SpotifyModule

        # conversations/ folder
        root = Path(os.environ.get("ATLAS_ROOT", "."))
        self._conv_dir = root / "conversations"
        self._conv_dir.mkdir(exist_ok=True)

        # ── Gemini client (primary smart engine) ──────────────────────────────
        self._gemini = None
        gemini_key   = os.environ.get("GEMINI_API_KEY", "").strip()
        if gemini_key:
            try:
                from openai import OpenAI
                self._gemini = OpenAI(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    api_key=gemini_key,
                )
                log.info("Brain: Gemini 2.0 Flash ready (primary smart engine).")
            except ImportError:
                log.error("openai package missing — pip install openai")
            except Exception as exc:
                log.error("Gemini init failed: %s", exc)
        else:
            log.info("GEMINI_API_KEY not set — Groq will handle complex queries.")

        # ── Groq client (fallback smart engine) ───────────────────────────────
        self._client = None
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            log.warning("GROQ_API_KEY not set — deep reasoning disabled, using MLX only.")
        else:
            try:
                from groq import Groq
                self._client = Groq(api_key=key)
                log.info("Brain: Groq %s ready (%s).",
                         "fallback" if self._gemini else "primary", self._model)
            except ImportError:
                log.error("groq package missing — pip install groq")
            except Exception as exc:
                log.error("Groq Brain init failed: %s", exc)

        # ── Qwen3 client (OpenRouter — deep coding and reasoning) ─────────────
        self._qwen_client      = None
        self._qwen_coder_model = api_cfg.get("qwen_coder",    "qwen/qwen3-coder-480b-a35b-instruct")
        self._qwen_next_model  = api_cfg.get("qwen_reasoning", "qwen/qwen3-next-80b-a3b-instruct")
        qwen_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if qwen_key:
            try:
                from openai import OpenAI as _OAI
                self._qwen_client = _OAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=qwen_key,
                )
                log.info("Brain: Qwen3 models ready via OpenRouter.")
            except Exception as exc:
                log.warning("Qwen3 init failed: %s", exc)
        else:
            log.info("OPENROUTER_API_KEY not set — Qwen3 models disabled.")

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_core(self, core) -> None:
        self._core = core
        log.info("ATLASCore wired into Brain.")

    def set_self_improve(self, engine) -> None:
        self._self_improve = engine
        log.info("SelfImproveEngine wired into Brain.")

    def set_spotify(self, spotify) -> None:
        self._spotify = spotify
        log.info("Spotify module wired into Brain.")

    @property
    def gemini_available(self) -> bool:
        return self._gemini is not None

    @property
    def groq_available(self) -> bool:
        return self._client is not None

    @property
    def smart_available(self) -> bool:
        return self.gemini_available or self.groq_available

    @property
    def qwen_available(self) -> bool:
        return self._qwen_client is not None

    # Backward-compat aliases used by self_improve.py
    @property
    def openrouter_available(self) -> bool:
        return self.smart_available

    @property
    def claude_available(self) -> bool:
        return self.smart_available

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

        # 3 — Spotify commands (checked before routing so "play X" doesn't hit control)
        if self._spotify is not None:
            sp = self._spotify.handle(text)
            if sp is not None:
                self._add_history("user", text)
                self._add_history("assistant", sp)
                self._save_session()
                return sp

        # 4 — Route to smart engine or core
        route = self._route(text)
        log.info("[BRAIN/%s] %r", route.upper(), text[:70])

        if route == "qwen_coder":
            response = self._ask_qwen_with_history(text, self._qwen_coder_model)
            self._routing_mode = "auto"
        elif route == "qwen_next":
            response = self._ask_qwen_with_history(text, self._qwen_next_model)
            self._routing_mode = "auto"
        elif route == "smart":
            response = self._ask_smart_with_history(text)
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
        if self._gemini:
            return self._raw_gemini([{"role": "user", "content": text}])
        if self._client:
            return self._raw_groq([{"role": "user", "content": text}])
        return self._core.ask(text) if self._core else self._no_core()

    # ── Routing ───────────────────────────────────────────────────────────────

    def _route(self, text: str) -> str:
        """Return 'smart' (Gemini/Groq), 'qwen_coder', 'qwen_next', or 'core' (MLX)."""
        if self._routing_mode == "groq":
            return "smart"
        if self._routing_mode == "core":
            return "core"
        if self._routing_mode == "qwen_coder":
            return "qwen_coder"
        if self._routing_mode == "qwen_next":
            return "qwen_next"

        lower = text.lower()

        # Control / system / web → core (MLX is fast for these)
        if any(kw in lower for kw in _CORE_KEYWORDS):
            return "core"

        # No smart engine → always core
        if not self.smart_available:
            return "core"

        # Complex reasoning keywords → smart engine
        if any(kw in lower for kw in _GROQ_KEYWORDS):
            return "smart"

        # Long queries (≥ 15 words) → smart engine
        if len(text.split()) >= 15:
            return "smart"

        # Qwen3 Coder — explicit full-build tasks only (auto-detected)
        if self._qwen_client and any(kw in lower for kw in _QWEN_CODER_TRIGGERS):
            return "qwen_coder"

        # Short conversational → core (MLX is fast)
        return "core"

    # ── Meta commands ─────────────────────────────────────────────────────────

    def _handle_meta(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        # Force Groq 70B
        if any(p in lower for p in ("think harder", "use groq", "deep think",
                                     "use deep reasoning", "use the big model",
                                     "use openrouter")):
            self._routing_mode = "groq"
            return (f"Understood, {self._user_name}. "
                    "I'll route through Groq for deeper reasoning.")

        # Force core (MLX)
        if any(p in lower for p in ("use mlx", "quick mode",
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

        # Qwen3 Coder — force for full project builds
        if any(p in lower for p in ("atlas build this with qwen", "build this with qwen")):
            if self._qwen_client:
                self._routing_mode = "qwen_coder"
                return (f"Understood, {self._user_name}. "
                        "Using Qwen3 Coder for the next build.")
            return (f"Qwen3 isn't available, {self._user_name}. "
                    "Set OPENROUTER_API_KEY to enable it.")

        # Qwen3 Next — force deep reasoning for next response
        if any(p in lower for p in ("atlas think deeper", "think deeper",
                                     "atlas use your best reasoning",
                                     "use your best reasoning")):
            if self._qwen_client:
                self._routing_mode = "qwen_next"
                return (f"Engaging Qwen3 deep reasoning for the next response, "
                        f"{self._user_name}.")
            return (f"Qwen3 isn't available, {self._user_name}. "
                    "Set OPENROUTER_API_KEY to enable it.")

        return None

    # ── Smart engine inference (Gemini → Groq fallback) ──────────────────────

    def _ask_smart_with_history(self, text: str) -> str:
        messages = self._sanitized_history()
        messages.append({"role": "user", "content": text})
        if self._gemini:
            return self._raw_gemini(messages)
        return self._raw_groq(messages)

    def _raw_gemini(self, messages: list[dict]) -> str:
        with self._lock:
            try:
                full_messages = [{"role": "system", "content": self._system}] + messages
                resp = self._gemini.chat.completions.create(
                    model="gemini-2.0-flash",
                    messages=full_messages,
                    max_tokens=self._max_tokens,
                    timeout=self._timeout,
                )
                return resp.choices[0].message.content.strip()
            except Exception as exc:
                log.error("Gemini error (%s): %s — trying Groq.", type(exc).__name__, exc)
                if self._client:
                    return self._raw_groq(messages)
                return self._fallback_to_core(messages)

    def _raw_groq(self, messages: list[dict]) -> str:
        with self._lock:
            try:
                full_messages = [{"role": "system", "content": self._system}] + messages
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=full_messages,
                    max_tokens=self._max_tokens,
                    timeout=self._timeout,
                )
                return resp.choices[0].message.content.strip()
            except Exception as exc:
                log.error("Groq Brain error (%s): %s", type(exc).__name__, exc)
                return self._fallback_to_core(messages)

    def _ask_qwen_with_history(self, text: str, model: str) -> str:
        messages = self._sanitized_history()
        messages.append({"role": "user", "content": text})
        return self._raw_qwen(model, messages)

    def _raw_qwen(self, model: str, messages: list[dict]) -> str:
        """/no_think suppresses Qwen3 chain-of-thought for snappy voice responses."""
        with self._lock:
            try:
                full_messages = [
                    {"role": "system", "content": "/no_think\n" + self._system}
                ] + messages
                resp = self._qwen_client.chat.completions.create(
                    model=model,
                    messages=full_messages,
                    max_tokens=self._max_tokens,
                    timeout=max(self._timeout, 30.0),
                )
                return resp.choices[0].message.content.strip()
            except Exception as exc:
                log.error("Qwen3 error (%s): %s — falling back to smart.", type(exc).__name__, exc)
                return self._fallback_to_core(messages)

    def _fallback_to_core(self, messages: list[dict]) -> str:
        if self._core and messages:
            last_user = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
            )
            if last_user:
                log.info("Falling back to ATLASCore.")
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
            if self.smart_available:
                prompt = (
                    f"Summarise this ATLAS conversation from {target} in 3 to 5 sentences "
                    f"for voice output. Highlight the main topics and any conclusions. "
                    f"Refer to the user as {self._user_name}.\n\n"
                    + "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs[:30])
                )
                return self._ask_smart_with_history(prompt)
            topics = [m["content"][:70] for m in msgs if m["role"] == "user"][:5]
            return f"On {target} we covered: " + "; ".join(topics) + "."
        except Exception as exc:
            log.error("Load session error: %s", exc)
            return "I couldn't retrieve that session."

    def _summarize_session(self) -> str:
        if not self._history:
            return f"We haven't discussed anything this session yet, {self._user_name}."
        if self.smart_available:
            prompt = (
                f"Summarise our current ATLAS session in 3 to 5 sentences for voice. "
                f"Highlight the main topics and decisions. "
                f"Refer to the user as {self._user_name}.\n\n"
                + "\n".join(
                    f"{m['role'].upper()}: {m['content']}"
                    for m in self._history[-20:]
                )
            )
            return self._ask_smart_with_history(prompt)
        topics = [m["content"][:70] for m in self._history if m["role"] == "user"][:5]
        return "This session we covered: " + "; ".join(topics) + "."

    @staticmethod
    def _no_core() -> str:
        return "My core module isn't connected. Please restart ATLAS."
