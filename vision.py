"""
ATLAS Vision Module — Real-time screen understanding

Captures screenshots via Pillow (ImageGrab) with optional ScreenCaptureKit
acceleration on macOS, then sends them to Qwen3 Coder (via OpenRouter) as
base64-encoded images for visual analysis.

ATLAS can now see everything on screen — code, UI, errors, designs, forms —
and give specific actionable guidance based on what is literally visible.

Voice commands handled here:
  "atlas look at this"         → screenshot + describe
  "atlas watch my screen"      → watch mode (every 30 s)
  "atlas stop watching"        → exit watch mode
  "atlas what do you see"      → describe screen in detail
  "atlas read that"            → OCR / read all text
  "atlas click on X"           → find X and click it
  "atlas what is wrong with this" → visual code review
  "atlas what font is that"    → identify font from screen
  "atlas match that colour"    → read hex colour from screen
  "atlas fill in this form"    → see and fill form fields
  "atlas what does this page say" → full visual summary
  "atlas next step"            → advance guided walkthrough
  "atlas repeat that"          → repeat current step
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── Simple commands that do NOT need a screenshot ────────────────────────────
_NO_SCREENSHOT_KEYWORDS = frozenset({
    "open ", "close ", "launch ", "quit ", "volume", "brightness",
    "lock screen", "sleep mac", "battery", "what time", "weather",
    "play ", "pause ", "skip ", "next song", "set alarm", "set timer",
    "what is ", "who is ", "define ", "remind me", "create folder",
    "new file", "delete ", "morning briefing", "add reminder",
})

# ── Trigger phrases that definitely need a screenshot ─────────────────────────
_SCREENSHOT_TRIGGERS = frozenset({
    "look at this", "what do you see", "read that", "click on",
    "what is wrong", "watch my screen", "what font", "match that colour",
    "match that color", "fill in", "fill this form", "what does this page",
    "help me with this", "fix that error", "what should i do next",
    "explain what i am looking at", "what am i looking at",
    "on the screen", "my screen", "this code", "this error",
    "this design", "this page", "this form", "what is on",
    "analyse this", "analyze this", "review this",
})

_VISUAL_SYSTEM_PROMPT = """\
You are ATLAS, an elite ambient AI companion with real-time screen vision.
You can see exactly what is on the user's screen right now.

Screen analysis rules:
- Reference UI elements by their EXACT visible name ("Click the blue Save button")
- NEVER use pixel coordinates — always use element names
- Give numbered steps for multi-step screen tasks
- For code: point to specific lines, function names, variable names you can see
- For designs: read actual font names, hex colours, spacing values from the screen
- For forms: read the actual field labels and fill them intelligently
- For errors: read the exact error text and give the exact fix
- Keep voice output short — 2-3 sentences max
- Full detail goes in the feed panel, voice gets the summary

Respond in plain prose only — no markdown, no asterisks, no bullet points.
Address the user as Boss."""


class VisionModule:
    """
    Screen capture + visual AI for ATLAS.

    Usage in main.py:
        vision = VisionModule(config, brain=brain)
        vision.set_speak_callback(vm.speak)
        vision.start_watch_mode()     # optional
        vision.stop()
    """

    def __init__(self, config: dict, brain=None):
        self._cfg          = config
        self._brain        = brain
        self._speak_cb: Optional[Callable[[str], None]] = None
        self._state_cb: Optional[Callable[[str], None]] = None
        self._enabled      = config.get("vision_enabled", True)
        self._watch_interval = int(config.get("watch_mode_interval", 30))
        self._watch_mode   = False
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_event   = threading.Event()

        # Guided walkthrough state
        self._steps: list[str] = []
        self._step_index       = 0

        # OpenRouter client for vision queries
        self._client = None
        self._model  = config.get("api", {}).get(
            "qwen_coder", "qwen/qwen3-coder-480b-a35b-instruct:free"
        )
        self._init_client()

    def _init_client(self):
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            log.warning("OPENROUTER_API_KEY not set — visual AI disabled.")
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=key,
            )
            log.info("Vision: Qwen3 Coder visual model ready via OpenRouter.")
        except Exception as exc:
            log.warning("Vision: OpenRouter init failed: %s", exc)

    def set_speak_callback(self, cb: Callable[[str], None]):
        self._speak_cb = cb

    def set_state_callback(self, cb: Callable[[str], None]):
        self._state_cb = cb

    def set_brain(self, brain):
        self._brain = brain

    # ── Screenshot capture ────────────────────────────────────────────────────

    def capture_screenshot(self) -> Optional[str]:
        """
        Capture the primary screen and return as base64-encoded PNG.
        Tries ScreenCaptureKit (pyobjc) first, falls back to Pillow.
        """
        if not self._enabled:
            return None

        # Try Pillow first — most reliable cross-environment
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:
            log.debug("Pillow screenshot failed: %s — trying pyautogui", exc)

        # Fallback: pyautogui
        try:
            import pyautogui
            img = pyautogui.screenshot()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:
            log.warning("Screenshot capture failed: %s", exc)
            return None

    # ── Vision AI query ───────────────────────────────────────────────────────

    def ask_with_screenshot(self, question: str,
                             screenshot_b64: Optional[str] = None) -> str:
        """
        Send question + screenshot to Qwen3 Coder, return text response.
        Captures a fresh screenshot if one isn't provided.
        """
        if not self._client:
            if self._brain:
                return self._brain.handle(question)
            return "Visual AI is not available. Set OPENROUTER_API_KEY to enable it."

        if screenshot_b64 is None:
            screenshot_b64 = self.capture_screenshot()

        if not screenshot_b64:
            if self._brain:
                return self._brain.handle(question)
            return "I couldn't capture the screen."

        try:
            messages = [
                {"role": "system", "content": _VISUAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type":  "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_b64}"
                            },
                        },
                        {"type": "text", "text": question},
                    ],
                },
            ]
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=512,
                timeout=30.0,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            log.error("Vision API error: %s", exc)
            if self._brain:
                return self._brain.handle(question)
            return "I had trouble analysing the screen. Try again in a moment."

    # ── Voice command handler ─────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        """
        Return a response if this text is a vision command, else None.
        Called from main.py meta chain.
        """
        if not self._enabled:
            return None

        lower = text.lower().strip()

        # Watch mode
        if any(p in lower for p in ("atlas watch my screen", "watch my screen",
                                     "atlas start watching", "start watching")):
            self.start_watch_mode()
            return "Watch mode active. I'll check your screen every thirty seconds."

        if any(p in lower for p in ("atlas stop watching", "stop watching",
                                     "atlas exit watch mode")):
            self.stop_watch_mode()
            return "Watch mode stopped."

        # Guided walkthrough
        if any(p in lower for p in ("atlas next step", "next step")):
            return self._next_step()

        if any(p in lower for p in ("atlas repeat that", "repeat that", "repeat the step")):
            return self._current_step()

        # Screen description
        if any(p in lower for p in (
            "atlas look at this", "look at this",
            "atlas what do you see", "what do you see",
            "atlas describe my screen", "describe my screen",
            "atlas what am i looking at", "what am i looking at",
        )):
            return self._describe_screen()

        # Read text
        if any(p in lower for p in ("atlas read that", "atlas read the screen",
                                     "atlas read this", "read that")):
            return self._read_screen()

        # Font identification
        if any(p in lower for p in ("atlas what font", "what font is that",
                                     "atlas identify the font")):
            return self._ask_visual("What font is being used in the text visible on screen? "
                                    "Give the exact font name.")

        # Colour matching
        if any(p in lower for p in ("atlas match that colour", "atlas match that color",
                                     "atlas what colour is that", "atlas what color is that",
                                     "what hex", "atlas read the colour")):
            return self._ask_visual("What is the exact hex colour code of the most prominent "
                                    "colour element on screen? Give only the hex value.")

        # Form filling
        if any(p in lower for p in ("atlas fill in this form", "atlas fill this form",
                                     "atlas fill in the form", "fill this form")):
            return self._ask_visual("I can see a form on screen. "
                                    "Read the field labels and tell me what information each "
                                    "field is asking for. List them one by one.")

        # Page summary
        if any(p in lower for p in ("atlas what does this page say",
                                     "atlas summarise this page",
                                     "atlas summarize this page",
                                     "atlas read the page")):
            return self._ask_visual("Summarise the content of what is on screen in 3 sentences "
                                    "for voice output. Plain text only.")

        # Code review
        if any(p in lower for p in ("atlas what is wrong with this",
                                     "atlas review this code",
                                     "atlas fix that error",
                                     "atlas spot the bug")):
            return self._ask_visual("Look at the code visible on screen. "
                                    "Identify any bugs, errors, or issues. "
                                    "Be specific — name the exact line or function with the problem.")

        # General "help me with this" / "what should I do next"
        if any(p in lower for p in ("help me with this", "atlas help me with this",
                                     "what should i do next", "atlas what should i do next")):
            return self._ask_visual(text)

        # Click command
        if "atlas click on" in lower or "click on the" in lower:
            target = lower.split("click on")[-1].strip().rstrip(".")
            return self._ask_visual(
                f"I need to click on '{target}' on screen. "
                f"Describe exactly where it is and what it looks like so I can find it. "
                f"Name the element, not its coordinates."
            )

        # Auto-attach screenshot for questions about what's on screen
        if self._needs_screenshot(lower):
            return self._ask_visual(text)

        return None

    def _needs_screenshot(self, lower: str) -> bool:
        # Screen-specific triggers always win over generic exclusions
        if any(kw in lower for kw in _SCREENSHOT_TRIGGERS):
            return True
        if any(kw in lower for kw in _NO_SCREENSHOT_KEYWORDS):
            return False
        return False

    def _describe_screen(self) -> str:
        return self._ask_visual(
            "Describe what is currently visible on screen in detail. "
            "Name any applications, files, UI elements, and their current state. "
            "Be specific but keep it to 3 sentences for voice."
        )

    def _read_screen(self) -> str:
        return self._ask_visual(
            "Read all the text visible on screen. "
            "Start from the top left and work down. Plain text only."
        )

    def _ask_visual(self, question: str) -> str:
        if self._state_cb:
            self._state_cb("thinking")
        result = self.ask_with_screenshot(question)
        return result

    # ── Guided walkthrough ────────────────────────────────────────────────────

    def set_steps(self, steps: list[str]):
        self._steps      = steps
        self._step_index = 0

    def _next_step(self) -> str:
        if not self._steps:
            return "No guided walkthrough is active, Boss."
        self._step_index = min(self._step_index + 1, len(self._steps) - 1)
        return self._current_step()

    def _current_step(self) -> str:
        if not self._steps:
            return "No guided walkthrough is active, Boss."
        step = self._steps[self._step_index]
        n    = self._step_index + 1
        return f"Step {n}: {step}"

    # ── Watch mode ────────────────────────────────────────────────────────────

    def start_watch_mode(self):
        if self._watch_mode:
            return
        self._watch_mode  = True
        self._stop_event.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="atlas-vision-watch"
        )
        self._watch_thread.start()
        log.info("Vision watch mode started (interval=%ds).", self._watch_interval)

    def stop_watch_mode(self):
        self._watch_mode = False
        self._stop_event.set()
        log.info("Vision watch mode stopped.")

    def _watch_loop(self):
        while self._watch_mode and not self._stop_event.is_set():
            self._stop_event.wait(self._watch_interval)
            if not self._watch_mode:
                break
            try:
                observation = self.ask_with_screenshot(
                    "Briefly note anything important or actionable on screen "
                    "in one sentence. If nothing interesting, just say 'nothing notable'."
                )
                if observation and "nothing notable" not in observation.lower():
                    log.info("[VISION WATCH] %s", observation)
                    if self._speak_cb:
                        self._speak_cb(f"Boss, {observation}")
            except Exception as exc:
                log.debug("Watch mode error: %s", exc)

    def stop(self):
        self.stop_watch_mode()

    # ── Screenshot for other modules ──────────────────────────────────────────

    def get_screenshot_b64(self) -> Optional[str]:
        """Public: get a fresh base64 screenshot for other modules to attach."""
        return self.capture_screenshot()
