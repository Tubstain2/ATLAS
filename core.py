"""
ATLAS Core Agent Loop (Step 3, updated: Groq-only)

Responsibility: route every utterance to the right system prompt and return
a voice-friendly response string.  Groq (llama3-70b-8192) handles all AI
reasoning.  Web context is fetched via DuckDuckGo and injected into Groq
when the query needs live data — no Gemini required.

Integration
───────────
main.py injects the core into the voice module:
    core = ATLASCore(config)
    voice_module.set_response_callback(core.handle)

Never import Anthropic / Claude here — those are the build tool only.
API keys are read exclusively from environment variables.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)

# ── System prompts ─────────────────────────────────────────────────────────────

_VOICE_PROMPT = """\
You are ATLAS, a voice-activated AI assistant running as a desktop application.

Rules for every response:
- Keep responses SHORT and natural for voice: 1–3 sentences unless the user
  explicitly asks for detail or a list.
- NEVER use markdown, asterisks, hashes, bullet points, or code fences in
  voice responses. Plain prose only.
- Be direct. Omit filler phrases like "Certainly!", "Of course!", "Great question!".
- Natural contractions are fine. Address the user as "you".
- Calm, precise, slightly futuristic tone. You refer to yourself as ATLAS.
- If you cannot answer, say so briefly and suggest what the user could try.\
"""

_RESEARCH_PROMPT = _VOICE_PROMPT + """

For this query you may give a more thorough answer. If code is requested,
provide clean, minimal code with a one-sentence explanation. Still avoid
markdown asterisks and hashes; plain numbered steps are fine for lists.\
"""

_WEB_PROMPT = _VOICE_PROMPT + """

Web search results are included above the user's question. Use them to give
an accurate, up-to-date answer. When citing a specific fact, mention the
source title naturally (e.g. "According to Reuters, ..."). If the results
don't fully cover the question, supplement with your training knowledge and
say so. Keep it concise for voice — 2-4 sentences unless detail is requested.\
"""

_CONTROL_PROMPT = """\
You are an action parser for ATLAS, a desktop AI assistant.
Convert the user's natural language command into a JSON action object.

Output ONLY a valid JSON object — no explanation, no markdown, no code fences.

Available actions:

App control:
  open_app:    {"action":"open_app",    "name":"AppName",      "response":"..."}
  close_app:   {"action":"close_app",   "name":"AppName",      "response":"..."}
  focus_app:   {"action":"focus_app",   "name":"AppName",      "response":"..."}
  minimize_app:{"action":"minimize_app","name":"AppName",      "response":"..."}
  maximize_app:{"action":"maximize_app","name":"AppName",      "response":"..."}
  open_url:    {"action":"open_url",    "url":"https://...",   "response":"..."}
  list_windows:{"action":"list_windows",                       "response":"Listing open windows."}

Keyboard / mouse:
  type_text:   {"action":"type_text",   "text":"...",          "response":"..."}
  press_key:   {"action":"press_key",   "key":"...", "modifiers":[], "response":"..."}
  hotkey:      {"action":"hotkey",      "keys":["command","c"],"response":"..."}
  click:       {"action":"click",       "x":0, "y":0, "button":"left", "double":false, "response":"..."}
  scroll:      {"action":"scroll",      "direction":"down", "amount":3, "response":"..."}
  copy:        {"action":"copy",                               "response":"Copied to clipboard."}
  paste:       {"action":"paste",                              "response":"Pasted from clipboard."}
  select_all:  {"action":"select_all",                         "response":"Selected all."}

Screen:
  screenshot:  {"action":"screenshot",                         "response":"Taking a screenshot."}
  read_screen: {"action":"read_screen",                        "response":"Reading the screen."}

Volume / audio:
  volume_up:   {"action":"volume_up",                          "response":"Turned volume up."}
  volume_down: {"action":"volume_down",                        "response":"Turned volume down."}
  volume_set:  {"action":"volume_set",  "level":50,            "response":"Volume set to 50."}
  volume_get:  {"action":"volume_get",                         "response":"Checking volume."}
  mute:        {"action":"mute",                               "response":"Muted."}
  unmute:      {"action":"unmute",                             "response":"Unmuted."}

Display:
  brightness_up:   {"action":"brightness_up",                  "response":"Increased brightness."}
  brightness_down: {"action":"brightness_down",                "response":"Decreased brightness."}

System info:
  battery:     {"action":"battery",                            "response":"Checking battery."}
  system_stats:{"action":"system_stats",                       "response":"Checking system stats."}

Power:
  lock_screen: {"action":"lock_screen",                        "response":"Locking screen."}
  sleep:       {"action":"sleep",                              "response":"Putting Mac to sleep."}

Files:
  open_folder: {"action":"open_folder", "name":"Downloads",    "response":"Opening Downloads."}
  find_file:   {"action":"find_file",   "name":"document.pdf", "response":"Searching for that file."}
  create_folder:{"action":"create_folder","name":"NewFolder","path":"~/Desktop","response":"Created folder."}
  trash_file:  {"action":"trash_file",  "path":"~/file.txt",   "response":"Moved to trash."}

Browser:
  new_tab:     {"action":"new_tab",                            "response":"Opening new tab."}
  close_tab:   {"action":"close_tab",                          "response":"Closing tab."}
  go_back:     {"action":"go_back",                            "response":"Going back."}
  go_forward:  {"action":"go_forward",                         "response":"Going forward."}
  reload:      {"action":"reload",                             "response":"Reloading page."}
  browser_search:{"action":"browser_search","query":"...",     "response":"Searching for ..."}

Media playback (use run_command with osascript on macOS):
  Play something/start Spotify: {"action":"run_command","command":"open -a Spotify && sleep 1 && osascript -e 'tell application \"Spotify\" to play'","response":"Starting Spotify."}
  Pause Spotify:         {"action":"run_command",  "command":"osascript -e 'tell application \"Spotify\" to pause'",      "response":"Paused."}
  Resume Spotify:        {"action":"run_command",  "command":"osascript -e 'tell application \"Spotify\" to play'",       "response":"Resumed."}
  Toggle play/pause:     {"action":"run_command",  "command":"osascript -e 'tell application \"Spotify\" to playpause'",  "response":"Toggled playback."}
  Spotify next track:    {"action":"run_command",  "command":"osascript -e 'tell application \"Spotify\" to next track'", "response":"Next track."}
  Spotify previous:      {"action":"run_command",  "command":"osascript -e 'tell application \"Spotify\" to previous track'","response":"Previous track."}
  Play YouTube:          {"action":"open_url",     "url":"https://youtube.com", "response":"Opening YouTube."}

Shell / permissions:
  run_command: {"action":"run_command", "command":"...",       "response":"..."}
  check_permissions:{"action":"check_permissions",             "response":"Checking permissions."}

Fallback:
  none:        {"action":"none",                               "response":"I'm not sure what to do."}

Rules:
- "response" is a short, natural voice sentence (no markdown) confirming the action.
- On macOS use "command" as the modifier key (not "ctrl").
- Prefer specific actions (volume_up, mute, brightness_up) over run_command equivalents.
- For "play something on Spotify" or "open Spotify": use open_app with name "Spotify".
- For "play/pause/next/previous" on Spotify: use run_command with osascript.
- open_folder names: Downloads, Desktop, Documents, Music, Movies, Pictures.
- Output ONLY the JSON object.\
"""


def _parse_control_json(text: str) -> dict:
    """Extract a JSON action dict from a raw LLM response string."""
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            pass
    return {"action": "none", "response": text or "Command not understood."}


_parse_json = _parse_control_json


# ── Self-edit prompts ──────────────────────────────────────────────────────────

_EDIT_INTENT_PROMPT = """\
You are a code analyst for ATLAS, a Python desktop AI assistant.
Identify which Python source file the user wants to modify.

ATLAS project files:
{file_list}

Output ONLY a JSON object:
{{"file": "filename.py", "intent": "one-line description of desired change"}}

If no specific file can be determined, output:
{{"file": null, "intent": "description"}}

Output ONLY the JSON object.\
"""

_EDIT_SPEC_PROMPT = """\
You are a precise code editor for ATLAS, a Python desktop AI assistant.
Given the file content and the user's request, produce an exact JSON edit specification.

Supported edit types:
  "replace"       — replace the FIRST occurrence of "old" with "new"
  "insert_after"  — insert "insert" immediately after "after"
  "insert_before" — insert "insert" immediately before "before"
  "full_rewrite"  — replace the entire file (use ONLY for major restructuring)

Output ONLY a valid JSON object in one of these forms:

replace:
{"type":"replace","file":"name.py","old":"exact verbatim string","new":"replacement","description":"..."}

insert_after:
{"type":"insert_after","file":"name.py","after":"exact verbatim string","insert":"new content","description":"..."}

insert_before:
{"type":"insert_before","file":"name.py","before":"exact verbatim string","insert":"new content","description":"..."}

full_rewrite:
{"type":"full_rewrite","file":"name.py","content":"complete new file content","description":"..."}

CRITICAL rules:
- "old", "after", "before" MUST be exact verbatim strings from the file — copy them precisely
- Make the MINIMAL change needed; prefer "replace" over "full_rewrite"
- Preserve existing indentation, spacing, and coding style exactly
- Output ONLY the JSON object — no code fences, no explanation\
"""

_EDIT_TRIGGERS = frozenset({
    "modify your code", "edit your code", "change your code",
    "update your code", "fix your code", "patch your code",
    "rewrite your code", "self-modify", "self modify",
    "make a code change", "apply a code change",
    "change the source code", "edit the source code",
    "modify web.py", "edit web.py", "update web.py", "fix web.py",
    "modify control.py", "edit control.py", "update control.py", "fix control.py",
    "modify voice.py", "edit voice.py", "update voice.py", "fix voice.py",
    "modify core.py", "edit core.py", "update core.py",
    "modify self_editor.py", "edit self_editor.py",
    "modify main.py", "edit main.py",
    "add to web.py", "add to control.py", "add to voice.py",
    "add to core.py", "add to main.py",
    "change the code in", "update the code in",
})


def _is_self_edit(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _EDIT_TRIGGERS)


# ══════════════════════════════════════════════════════════════════════════════
# Conversation history
# ══════════════════════════════════════════════════════════════════════════════

class _History:
    """Sliding-window conversation buffer in OpenAI message format."""

    def __init__(self, max_turns: int = 12):
        self._msgs: list[dict] = []
        self._max = max_turns * 2

    def add(self, role: str, content: str):
        self._msgs.append({"role": role, "content": content})
        if len(self._msgs) > self._max:
            self._msgs = self._msgs[-self._max:]

    def messages(self) -> list[dict]:
        return list(self._msgs)

    def last_user_text(self) -> str:
        for m in reversed(self._msgs):
            if m["role"] == "user":
                return m["content"]
        return ""

    def clear(self):
        self._msgs.clear()

    def __len__(self):
        return len(self._msgs)


# ══════════════════════════════════════════════════════════════════════════════
# Query router — selects system prompt, not provider
# ══════════════════════════════════════════════════════════════════════════════

class _Router:
    """
    Decides whether a query warrants a detailed research-style response
    or a short voice-optimised one.  Everything goes to Groq either way.
    """

    _RESEARCH = frozenset({
        "research", "analyze", "analyse", "explain in detail", "explain why",
        "explain how", "compare", "summarize", "summarise", "pros and cons",
        "advantages and disadvantages", "write a", "write an", "generate",
        "code", "script", "program", "function", "class ", "debug",
        "calculate", "compute", "solve", "translate", "step by step",
        "help me understand", "what is the difference", "difference between",
        "plan", "design", "create a", "build a", "comprehensive",
        "in depth", "detailed explanation", "give me a list",
    })

    _LONG_QUERY_WORDS = 22

    def is_research(self, text: str) -> bool:
        lower = text.lower()
        return (
            any(kw in lower for kw in self._RESEARCH)
            or len(text.split()) >= self._LONG_QUERY_WORDS
        )


# ══════════════════════════════════════════════════════════════════════════════
# Groq client
# ══════════════════════════════════════════════════════════════════════════════

class _GroqClient:

    def __init__(self, model: str, max_tokens: int, temperature: float):
        self._model       = model
        self._max_tokens  = max_tokens
        self._temperature = temperature
        self._client      = None
        self._lock        = Lock()

        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            log.warning("GROQ_API_KEY not set — Groq disabled.")
            return

        try:
            from groq import Groq
            self._client = Groq(api_key=key)
            log.info("Groq ready  (%s)", self._model)
        except ImportError:
            log.error("groq package missing — pip install groq")
        except Exception as exc:
            log.error("Groq init failed: %s", exc)

    @property
    def available(self) -> bool:
        return self._client is not None

    def ask(self, messages: list[dict], system_prompt: str,
            max_tokens: Optional[int] = None) -> str:
        payload = [{"role": "system", "content": system_prompt}] + messages
        limit   = max_tokens or self._max_tokens
        with self._lock:
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=payload,
                    max_tokens=limit,
                    temperature=self._temperature,
                    timeout=25.0,
                )
                return resp.choices[0].message.content.strip()
            except Exception as exc:
                return _api_error(exc, "Groq")


# ══════════════════════════════════════════════════════════════════════════════
# MLX client (Apple Silicon local inference — primary)
# ══════════════════════════════════════════════════════════════════════════════

class _MLXClient:
    """Local inference via mlx-lm on Apple Silicon. No API key needed."""

    def __init__(self, model_id: str, max_tokens: int, temperature: float):
        self._model_id    = model_id
        self._max_tokens  = max_tokens
        self._temperature = temperature
        self._model       = None
        self._tokenizer   = None
        self._lock        = Lock()
        self._load_failed = False

        try:
            import mlx_lm  # noqa: F401 — availability probe only
            self._importable = True
            log.info("MLX available — will load %s on first inference", model_id)
        except ImportError:
            self._importable = False
            log.info("mlx-lm not installed — MLX disabled (using Groq fallback).")

    @property
    def available(self) -> bool:
        return self._importable and not self._load_failed

    def _ensure_loaded(self):
        if self._model is not None:
            return
        log.info("Loading MLX model %s (first run downloads ~2 GB)...", self._model_id)
        from mlx_lm import load
        self._model, self._tokenizer = load(self._model_id)
        log.info("MLX model ready.")

    def ask(self, messages: list[dict], system_prompt: str,
            max_tokens: Optional[int] = None) -> str:
        if not self.available:
            return ""
        limit = max_tokens or self._max_tokens
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        with self._lock:
            try:
                self._ensure_loaded()
                from mlx_lm import generate
                from mlx_lm.sample_utils import make_sampler
                prompt = self._tokenizer.apply_chat_template(
                    full_messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )
                response = generate(
                    self._model,
                    self._tokenizer,
                    prompt=prompt,
                    max_tokens=limit,
                    sampler=make_sampler(temp=self._temperature),
                    verbose=False,
                )
                return response.strip()
            except Exception as exc:
                log.error("MLX inference error (%s): %s — falling back to Groq.", type(exc).__name__, exc)
                self._load_failed = True
                return ""


# ── Shared error handler ──────────────────────────────────────────────────────

def _api_error(exc: Exception, name: str) -> str:
    log.error("%s API error (%s): %s", name, type(exc).__name__, exc)

    # Use SDK exception types first — avoids false positives from string matching
    try:
        from groq import AuthenticationError, RateLimitError, APITimeoutError, APIConnectionError
        if isinstance(exc, AuthenticationError):
            return "There's a problem with my API credentials. Please check the GROQ_API_KEY."
        if isinstance(exc, RateLimitError):
            return "I'm being rate-limited right now. Please try again in a moment."
        if isinstance(exc, APITimeoutError):
            return "That request timed out. Could you try again?"
        if isinstance(exc, APIConnectionError):
            return "I can't reach the server right now. Check your internet connection."
    except ImportError:
        pass

    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "quota" in msg:
        return "I'm being rate-limited right now. Please try again in a moment."
    if "401" in msg or "403" in msg or "unauthorized" in msg:
        return "There's a problem with my API credentials. Please check the GROQ_API_KEY."
    if "timeout" in msg or "timed out" in msg:
        return "That request timed out. Could you try again?"
    if "connect" in msg or "network" in msg or "unreachable" in msg:
        return "I can't reach the server right now. Check your internet connection."
    return "I encountered an error. Could you rephrase or try again?"


# ══════════════════════════════════════════════════════════════════════════════
# ATLASCore — public API
# ══════════════════════════════════════════════════════════════════════════════

class ATLASCore:
    """
    Main orchestrator.  Called from:
      - VoiceModule   via set_response_callback(core.handle)
      - web.py        via core.ask(query)
      - self_editor.py via core.ask(prompt)
    """

    def __init__(self, config: dict):
        cc = config.get("core", {})
        ac = config.get("api",  {})

        groq_model  = ac.get("groq_model",       "llama-3.3-70b-versatile")
        mlx_model   = ac.get("mlx_model",        "mlx-community/Llama-3.2-3B-Instruct-4bit")
        max_turns   = cc.get("max_history_turns", 12)
        max_tokens  = cc.get("groq_max_tokens",   450)
        temp        = cc.get("temperature",        0.7)

        self._router  = _Router()
        self._history = _History(max_turns=max_turns)
        self._mlx     = _MLXClient(mlx_model,  max_tokens, temp)
        self._groq    = _GroqClient(groq_model, max_tokens, temp)

        self._web:     Optional[object] = None
        self._control: Optional[object] = None
        self._editor:  Optional[object] = None

        if self._mlx.available:
            log.info("ATLASCore ready — primary: MLX (%s), fallback: Groq (%s)", mlx_model, groq_model)
        elif self._groq.available:
            log.info("ATLASCore ready — backend: Groq (%s)  [MLX unavailable]", groq_model)
        else:
            log.error("No AI backend available. Install mlx-lm or set GROQ_API_KEY.")

    # ── Module injection ──────────────────────────────────────────────────────

    def set_web_module(self, web) -> None:
        self._web = web
        log.info("Web module attached to ATLASCore.")

    def set_control_module(self, ctrl) -> None:
        self._control = ctrl
        log.info("Control module attached to ATLASCore.")

    def set_self_editor(self, editor) -> None:
        self._editor = editor
        log.info("Self-editor attached to ATLASCore.")

    # ── Primary entry point ───────────────────────────────────────────────────

    def handle(self, text: str) -> str:
        """Route utterance and return a voice-friendly response string."""
        text = text.strip()
        if not text:
            return ""

        # Self-edit routing (checked first — "edit web.py" must not hit control)
        if self._editor is not None and _is_self_edit(text):
            log.info("[EDIT] routing: %r", text[:60])
            response = self._call_edit(text)
            self._history.add("user", text)
            self._history.add("assistant", response)
            return response

        # Control routing — falls through to AI if Groq returns action="none"
        if self._control is not None and self._control.is_control_query(text):
            log.info("[CTRL] routing: %r", text[:60])
            response = self._call_control(text)
            if response is not None:
                self._history.add("user", text)
                self._history.add("assistant", response)
                return response

        # Web augmentation: inject live DuckDuckGo context when needed
        web_context = ""
        if self._web is not None and self._web.needs_web(text):
            log.info("[WEB] augmenting query: %r", text[:60])
            web_context = self._web.build_context(text)

        self._history.add("user", text)

        backend = "MLX" if self._mlx.available else "GROQ"
        log.info("[%s%s] %r", backend, "+WEB" if web_context else "", text[:80])
        response = self._call(text, web_context)

        if response:
            self._history.add("assistant", response)

        return response

    # ── Secondary entry point ─────────────────────────────────────────────────

    def ask(self, text: str) -> str:
        """Direct single-turn query — does not touch conversation history."""
        text = text.strip()
        if not text:
            return ""
        log.info("[AI/ask] %r", text[:80])
        return self._ask([{"role": "user", "content": text}], _RESEARCH_PROMPT)

    # ── History control ───────────────────────────────────────────────────────

    def reset_history(self):
        self._history.clear()
        log.info("Conversation history cleared.")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def mlx_available(self) -> bool:
        return self._mlx.available

    @property
    def groq_available(self) -> bool:
        return self._groq.available

    @property
    def ai_available(self) -> bool:
        return self._mlx.available or self._groq.available

    # ── Self-edit routing ─────────────────────────────────────────────────────

    def _call_edit(self, text: str) -> str:
        """
        Two-pass Groq call:
          Pass 1 → identify which file to edit + one-line intent
          Pass 2 → read that file → generate exact edit spec JSON
          Then: SelfEditor.apply_edit(spec) → EditResult → voice response
        """
        if not self._groq.available:
            return "No AI backend available for code editing."

        # Pass 1: identify file
        file_list  = "\n".join(f"  - {f}" for f in self._list_atlas_files())
        intent_sys = _EDIT_INTENT_PROMPT.format(file_list=file_list)
        raw1 = self._groq.ask(
            [{"role": "user", "content": text}], intent_sys, max_tokens=200
        )

        intent_data = _parse_json(raw1)
        file_name   = intent_data.get("file")

        if not file_name:
            return (
                "I couldn't identify which file to modify. "
                "Try being more specific, like 'update web.py to add the keyword X'."
            )

        # Read target file
        root      = Path(os.environ.get("ATLAS_ROOT", "."))
        file_path = root / file_name
        if not file_path.exists():
            return f"I couldn't find the file: {file_name}."

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"I couldn't read {file_name}: {exc}"

        # Pass 2: generate exact edit spec
        spec_user = (
            f"File: {file_name}\n\n"
            f"Content:\n```python\n{content[:6_000]}\n```\n\n"
            f"User request: {text}\n"
            f"Intent: {intent_data.get('intent', text)}"
        )
        raw2 = self._groq.ask(
            [{"role": "user", "content": spec_user}], _EDIT_SPEC_PROMPT,
            max_tokens=2048,
        )

        edit_spec = _parse_json(raw2)
        if not edit_spec.get("file"):
            edit_spec["file"] = file_name

        log.info(
            "[EDIT] spec: type=%r file=%r desc=%r",
            edit_spec.get("type"), edit_spec.get("file"),
            (edit_spec.get("description") or "")[:50],
        )

        result = self._editor.apply_edit(edit_spec)
        return result.as_voice_response()

    # ── File list helper ──────────────────────────────────────────────────────

    def _list_atlas_files(self) -> list[str]:
        root  = Path(os.environ.get("ATLAS_ROOT", "."))
        files = []
        for f in sorted(root.glob("*.py")):
            if not f.name.startswith("test_") and f.name != "__init__.py":
                files.append(f.name)
        for f in sorted(root.glob("ui/*.py")):
            if not f.name.startswith("test_") and f.name != "__init__.py":
                files.append(f"ui/{f.name}")
        return files

    # ── Control routing ───────────────────────────────────────────────────────

    def _call_control(self, text: str) -> Optional[str]:
        """Parse text into a control action and execute it.
        Returns None if the action is 'none' so the caller can fall through to AI."""
        if not self._groq.available:
            return None
        raw    = self._groq.ask([{"role": "user", "content": text}], _CONTROL_PROMPT)
        action = _parse_control_json(raw)
        kind   = action.get("action", "none")
        log.info("[CTRL] action=%r params=%r", kind, {
            k: v for k, v in action.items() if k not in ("action", "response")
        })
        if kind == "none":
            log.info("[CTRL] Groq returned 'none' — falling through to AI.")
            return None
        return self._control.execute(action)

    # ── Main call ─────────────────────────────────────────────────────────────

    def _ask(self, messages: list[dict], system_prompt: str,
             max_tokens: Optional[int] = None) -> str:
        """Try MLX first; fall back to Groq on failure or unavailability."""
        if self._mlx.available:
            result = self._mlx.ask(messages, system_prompt, max_tokens)
            if result:
                return result
        if self._groq.available:
            return self._groq.ask(messages, system_prompt, max_tokens)
        return self._no_backend()

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%A %B %d %Y, %I:%M %p")

    def _call(self, text: str, web_context: str = "") -> str:
        if not self._mlx.available and not self._groq.available:
            return self._no_backend()

        time_prefix = f"Current date and time: {self._now()}\n\n"

        if web_context:
            augmented = f"{web_context}\n\nUser question: {self._history.last_user_text()}"
            return self._ask(
                [{"role": "user", "content": augmented}], time_prefix + _WEB_PROMPT
            )

        system = _RESEARCH_PROMPT if self._router.is_research(text) else _VOICE_PROMPT
        return self._ask(self._history.messages(), time_prefix + system)

    @staticmethod
    def _no_backend() -> str:
        log.error("No AI backend available.")
        return (
            "I'm not connected to any AI backend. "
            "Install mlx-lm for local inference, or set GROQ_API_KEY for cloud fallback."
        )
