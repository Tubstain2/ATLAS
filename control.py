"""
ATLAS Laptop Control Module (Step 5)

Components
──────────
  ConfirmationDialog   Thread-safe Qt dialog for destructive-command approval
  MouseController      pyautogui mouse: move, click, scroll, drag
  KeyboardController   pyautogui keyboard: type text, press keys, hotkeys
  WindowManager        Platform-adaptive: open/close/focus/list applications
  ScreenReader         Screenshot + OCR text extraction (pytesseract)
  ShellExecutor        Safe shell execution with destructive-command guard
  ControlModule        Public API — orchestrates all components

Safety rules (enforced here, configured in config.yaml → safety:)
─────────────────────────────────────────────────────────────────
  • Commands matching restricted_commands or _DANGER_PATTERNS → BLOCKED
    unless user types 'confirm' in a Qt dialog (confirm_cb)
  • No confirm_cb set → dangerous command is always blocked
  • Shell commands run in home directory, never in the ATLAS project root
  • pyautogui FAILSAFE enabled: move mouse to top-left corner to abort

Platform support
────────────────
  macOS   → open/osascript for app management; pyautogui for input
  Windows → pygetwindow + subprocess for app management; pyautogui for input
  Linux   → subprocess + wmctrl (best-effort)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Optional

log = logging.getLogger(__name__)

_SYSTEM = platform.system()   # 'Darwin', 'Windows', 'Linux'
_IS_MAC = _SYSTEM == "Darwin"
_IS_WIN = _SYSTEM == "Windows"

# ── Shell safety ──────────────────────────────────────────────────────────────

_DANGER_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\s+-[a-z]*r[a-z]*f",     re.I),  # rm -rf / rm -fr
    re.compile(r"rm\s+-[a-z]*f[a-z]*r",     re.I),
    re.compile(r">\s*/dev/sd[a-z]"),                  # overwrite block device
    re.compile(r">\s*/dev/nvme"),
    re.compile(r"\bmkfs\b",                  re.I),
    re.compile(r"\bdd\s+if=",               re.I),
    re.compile(r":\s*\(\s*\)\s*\{.*\|.*:.*&", re.I), # fork bomb
    re.compile(r"format\s+[a-z]:",          re.I),   # Windows format drive
    re.compile(r"\bdel\s+/[fqs]",           re.I),
    re.compile(r"\breg\s+delete\b",         re.I),
    re.compile(r"\bcipher\s+/w\b",          re.I),
    re.compile(r"\bshred\s+-[un]",          re.I),
    re.compile(r"\bwipefs\b",               re.I),
    re.compile(r"\bsudo\s+rm\b",            re.I),
    re.compile(r"\bsudo\s+shutdown\b",      re.I),
    re.compile(r"\bsudo\s+reboot\b",        re.I),
    re.compile(r"\bhalt\b",                 re.I),
    re.compile(r"\bpoweroff\b",             re.I),
    re.compile(r"\binit\s+0\b",             re.I),
    re.compile(r"\bshutdown\s+/[srh]\b",    re.I),  # Windows shutdown
]

_MAX_OUTPUT_CHARS = 3_000
_SHELL_TIMEOUT    = 30        # seconds


# ══════════════════════════════════════════════════════════════════════════════
# Confirmation dialog (thread-safe, Qt main-thread)
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmationDialog:
    """
    Shows a modal Qt input dialog on the main thread from any background thread.
    The caller blocks until the dialog is dismissed (up to 60 s).

    Usage in main.py:
        dlg = ConfirmationDialog()
        ctrl.set_confirm_cb(dlg.ask)
    """

    def __init__(self):
        self._event  = Event()
        self._result = False
        self._lock   = Lock()   # serialise concurrent requests

    def ask(self, cmd: str) -> bool:
        """Block calling thread; return True only if user typed 'confirm'."""
        with self._lock:
            self._result = False
            self._event.clear()
            try:
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, lambda: self._show(cmd))
            except Exception:
                log.warning("Cannot show confirmation dialog — command blocked.")
                return False
            self._event.wait(timeout=60)
            return self._result

    def _show(self, cmd: str) -> None:
        """Must be called on Qt main thread."""
        try:
            from PyQt6.QtWidgets import QInputDialog, QApplication
            parent = QApplication.activeWindow()
            text, ok = QInputDialog.getText(
                parent,
                "Safety Confirmation Required",
                f"Potentially destructive command:\n\n    {cmd}\n\n"
                "Type  confirm  to execute (or cancel to abort):",
            )
            self._result = ok and text.strip().lower() == "confirm"
        except Exception as exc:
            log.error("Confirmation dialog error: %s", exc)
            self._result = False
        finally:
            self._event.set()


# ══════════════════════════════════════════════════════════════════════════════
# Mouse controller
# ══════════════════════════════════════════════════════════════════════════════

class MouseController:

    def __init__(self):
        self._pyag     = None
        self._available = False
        try:
            import pyautogui
            pyautogui.FAILSAFE = True   # move to top-left corner to abort
            pyautogui.PAUSE    = 0.05
            self._pyag      = pyautogui
            self._available = True
            log.info("MouseController ready.")
        except ImportError:
            log.warning("pyautogui not installed — mouse control disabled.")
        except Exception as exc:
            log.warning("Mouse init failed: %s", exc)

    @property
    def available(self) -> bool:
        return self._available

    def move(self, x: int, y: int, duration: float = 0.25) -> str:
        try:
            self._pyag.moveTo(x, y, duration=duration)
            return f"Moved mouse to ({x}, {y})."
        except Exception as exc:
            return self._err(exc)

    def click(self, x: int | None = None, y: int | None = None,
              button: str = "left", double: bool = False) -> str:
        try:
            if x is not None and y is not None:
                self._pyag.moveTo(x, y, duration=0.15)
            fn = self._pyag.doubleClick if double else self._pyag.click
            fn(button=button)
            label = "Double-clicked" if double else "Clicked"
            pos   = f" at ({x},{y})" if x is not None else ""
            return f"{label}{pos}."
        except Exception as exc:
            return self._err(exc)

    def scroll(self, direction: str = "down", amount: int = 3) -> str:
        try:
            clicks = amount if direction == "up" else -amount
            self._pyag.scroll(clicks)
            return f"Scrolled {direction}."
        except Exception as exc:
            return self._err(exc)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> str:
        try:
            self._pyag.moveTo(x1, y1, duration=0.15)
            self._pyag.dragTo(x2, y2, duration=duration, button="left")
            return f"Dragged from ({x1},{y1}) to ({x2},{y2})."
        except Exception as exc:
            return self._err(exc)

    def _err(self, exc: Exception) -> str:
        msg = str(exc)
        if "permission" in msg.lower() or "accessibility" in msg.lower():
            return (
                "Mouse control requires Accessibility permission. "
                "Go to System Settings → Privacy & Security → Accessibility "
                "and add this application."
            )
        if "failsafe" in msg.lower():
            return "Mouse control aborted (fail-safe triggered)."
        return f"Mouse error: {msg}"


# ══════════════════════════════════════════════════════════════════════════════
# Keyboard controller
# ══════════════════════════════════════════════════════════════════════════════

class KeyboardController:

    _ALIASES: dict[str, str] = {
        "enter": "enter", "return": "enter",
        "escape": "escape", "esc": "escape",
        "backspace": "backspace", "delete": "delete",
        "tab": "tab", "space": "space",
        "up": "up", "down": "down", "left": "left", "right": "right",
        "home": "home", "end": "end",
        "pageup": "pageup", "page up": "pageup",
        "pagedown": "pagedown", "page down": "pagedown",
        "cmd": "command", "command": "command", "super": "command",
        "ctrl": "ctrl", "control": "ctrl",
        "alt": "alt", "option": "alt",
        "shift": "shift",
        **{f"f{i}": f"f{i}" for i in range(1, 13)},
    }

    def __init__(self):
        self._pyag      = None
        self._available = False
        try:
            import pyautogui
            self._pyag      = pyautogui
            self._available = True
            log.info("KeyboardController ready.")
        except ImportError:
            log.warning("pyautogui not installed — keyboard control disabled.")
        except Exception as exc:
            log.warning("Keyboard init failed: %s", exc)

    @property
    def available(self) -> bool:
        return self._available

    def type_text(self, text: str, interval: float = 0.02) -> str:
        try:
            self._pyag.typewrite(text, interval=interval)
            preview = (text[:40] + "…") if len(text) > 40 else text
            return f"Typed: {preview!r}"
        except Exception as exc:
            return self._err(exc)

    def press(self, key: str) -> str:
        try:
            k = self._ALIASES.get(key.lower(), key.lower())
            self._pyag.press(k)
            return f"Pressed {key}."
        except Exception as exc:
            return self._err(exc)

    def hotkey(self, *keys: str) -> str:
        try:
            mapped = [self._ALIASES.get(k.lower(), k.lower()) for k in keys]
            self._pyag.hotkey(*mapped)
            return "Pressed " + "+".join(keys) + "."
        except Exception as exc:
            return self._err(exc)

    def _err(self, exc: Exception) -> str:
        msg = str(exc)
        if "permission" in msg.lower() or "accessibility" in msg.lower():
            return (
                "Keyboard control requires Accessibility permission. "
                "Go to System Settings → Privacy & Security → Accessibility."
            )
        return f"Keyboard error: {msg}"


# ══════════════════════════════════════════════════════════════════════════════
# Window / application manager
# ══════════════════════════════════════════════════════════════════════════════

class WindowManager:
    """
    macOS  → open -a / osascript (AppleScript)
    Windows → os.startfile / pygetwindow / taskkill
    Linux  → subprocess + wmctrl (best-effort)
    """

    def __init__(self):
        log.info("WindowManager ready (%s).", _SYSTEM)

    # ── Open ──────────────────────────────────────────────────────────────────

    def open_app(self, name: str) -> str:
        try:
            if name.startswith(("http://", "https://")):
                return self.open_url(name)
            if _IS_MAC:
                r = subprocess.run(
                    ["open", "-a", name],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode != 0:
                    # Might be a file path or bundle name without .app
                    subprocess.Popen(["open", name])
            elif _IS_WIN:
                os.startfile(name)   # type: ignore[attr-defined]
            else:
                subprocess.Popen([name])
            return f"Opening {name}."
        except FileNotFoundError:
            return f"Application not found: {name}."
        except Exception as exc:
            return f"Failed to open {name}: {exc}"

    def open_url(self, url: str) -> str:
        try:
            import webbrowser
            webbrowser.open(url)
            return f"Opened {url} in your browser."
        except Exception as exc:
            return f"Failed to open URL: {exc}"

    # ── Close ─────────────────────────────────────────────────────────────────

    def close_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                script = f'tell application "{name}" to quit'
                r = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=8,
                )
                if r.returncode != 0 and r.stderr.strip():
                    return f"Could not close {name}: {r.stderr.strip()}"
            elif _IS_WIN:
                subprocess.run(
                    ["taskkill", "/F", "/IM", f"{name}.exe"],
                    capture_output=True, timeout=8,
                )
            else:
                subprocess.run(["pkill", "-f", name], capture_output=True, timeout=8)
            return f"Closed {name}."
        except Exception as exc:
            return f"Failed to close {name}: {exc}"

    # ── Focus ─────────────────────────────────────────────────────────────────

    def focus_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                script = f'tell application "{name}" to activate'
                r = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=8,
                )
                if r.returncode != 0:
                    return f"Could not focus {name}: {r.stderr.strip()}"
            elif _IS_WIN:
                try:
                    import pygetwindow as gw
                    wins = gw.getWindowsWithTitle(name)
                    if wins:
                        wins[0].activate()
                    else:
                        return f"No window found with title: {name}"
                except ImportError:
                    subprocess.Popen(["start", "", name], shell=True)
            else:
                subprocess.run(["wmctrl", "-a", name], timeout=5)
            return f"Switched to {name}."
        except Exception as exc:
            return f"Failed to focus {name}: {exc}"

    # ── Minimize / maximize ───────────────────────────────────────────────────

    def minimize_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                script = (
                    f'tell application "System Events" to set miniaturized of '
                    f'windows of process "{name}" to true'
                )
                subprocess.run(["osascript", "-e", script], timeout=8)
            elif _IS_WIN:
                import pygetwindow as gw
                for w in gw.getWindowsWithTitle(name):
                    w.minimize()
            return f"Minimized {name}."
        except Exception as exc:
            return f"Failed to minimize {name}: {exc}"

    def maximize_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                # Un-miniaturize then activate
                script1 = (
                    f'tell application "System Events" to set miniaturized of '
                    f'windows of process "{name}" to false'
                )
                script2 = f'tell application "{name}" to activate'
                subprocess.run(["osascript", "-e", script1], timeout=8)
                subprocess.run(["osascript", "-e", script2], timeout=8)
            elif _IS_WIN:
                import pygetwindow as gw
                for w in gw.getWindowsWithTitle(name):
                    w.maximize()
            return f"Maximized {name}."
        except Exception as exc:
            return f"Failed to maximize {name}: {exc}"

    # ── List ──────────────────────────────────────────────────────────────────

    def list_windows(self) -> list[str]:
        try:
            if _IS_MAC:
                script = (
                    'tell application "System Events" to get name of '
                    'every process whose visible is true'
                )
                r = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=8,
                )
                if r.returncode == 0 and r.stdout.strip():
                    return [a.strip() for a in r.stdout.strip().split(",") if a.strip()]
            elif _IS_WIN:
                import pygetwindow as gw
                return [w.title for w in gw.getAllWindows() if w.title.strip()]
            else:
                r = subprocess.run(
                    ["wmctrl", "-l"], capture_output=True, text=True, timeout=5
                )
                return [
                    ln.split(None, 3)[-1]
                    for ln in r.stdout.strip().splitlines()
                    if ln
                ]
        except Exception as exc:
            log.warning("list_windows failed: %s", exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Screen reader (screenshot + OCR)
# ══════════════════════════════════════════════════════════════════════════════

class ScreenReader:
    """Takes screenshots and extracts text via Tesseract OCR."""

    _TESSERACT_INSTALL = (
        "Install Tesseract: brew install tesseract (macOS) or "
        "https://github.com/UB-Mannheim/tesseract/wiki (Windows)"
    )

    def __init__(self):
        self._pyag      = None
        self._tesseract = None

        try:
            import pyautogui
            self._pyag = pyautogui
        except ImportError:
            log.warning("pyautogui missing — screenshots unavailable.")

        try:
            import pytesseract
            pytesseract.get_tesseract_version()   # raises if binary missing
            self._tesseract = pytesseract
            log.info("ScreenReader ready (OCR available).")
        except ImportError:
            log.warning("pytesseract not installed.")
        except EnvironmentError:
            log.warning("Tesseract binary not found. %s", self._TESSERACT_INSTALL)
        except Exception as exc:
            log.warning("Tesseract check: %s", exc)

    @property
    def screenshot_available(self) -> bool:
        return self._pyag is not None

    @property
    def ocr_available(self) -> bool:
        return self._tesseract is not None

    def screenshot(self, save_path: str | None = None) -> tuple[str, object]:
        """Return (status_message, PIL_Image_or_None)."""
        if not self._pyag:
            return "Screenshot unavailable — pyautogui not installed.", None
        try:
            img = self._pyag.screenshot()
            if save_path:
                img.save(save_path)
                return f"Screenshot saved to {save_path}.", img
            return "Screenshot captured.", img
        except Exception as exc:
            msg = str(exc).lower()
            if "permission" in msg or "screen recording" in msg:
                return (
                    "Screenshot requires Screen Recording permission. "
                    "Go to System Settings → Privacy & Security → Screen Recording.",
                    None,
                )
            return f"Screenshot failed: {exc}", None

    def read_screen(self, region: tuple | None = None) -> str:
        """Screenshot the screen and return all visible text via OCR."""
        if not self._pyag:
            return "Screenshot unavailable — pyautogui not installed."
        if not self._tesseract:
            return f"OCR unavailable. {self._TESSERACT_INSTALL}"
        try:
            img = self._pyag.screenshot(region=region) if region else self._pyag.screenshot()
            text = self._tesseract.image_to_string(img).strip()
            if not text:
                return "No text detected on the screen."
            if len(text) > 2_000:
                text = text[:2_000] + "\n[…text truncated…]"
            return text
        except Exception as exc:
            if "permission" in str(exc).lower():
                return (
                    "Screen reading requires Screen Recording permission. "
                    "Go to System Settings → Privacy & Security → Screen Recording."
                )
            return f"Screen read failed: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# Shell executor
# ══════════════════════════════════════════════════════════════════════════════

class ShellExecutor:
    """
    Executes shell commands with a destructive-command safety guard.

    Dangerous commands require confirm_cb() to return True before execution.
    If no confirm_cb is set, dangerous commands are always blocked.
    """

    def __init__(self, config: dict, confirm_cb: Callable[[str], bool] | None = None):
        safety                   = config.get("safety", {})
        self._require_confirm    = safety.get("confirm_destructive_commands", True)
        self._restricted         = [
            s.lower() for s in safety.get("restricted_commands", [])
        ]
        self._confirm_cb         = confirm_cb

    def set_confirm_cb(self, cb: Callable[[str], bool]) -> None:
        self._confirm_cb = cb

    def is_dangerous(self, cmd: str) -> bool:
        lower = cmd.lower()
        if any(phrase in lower for phrase in self._restricted):
            return True
        return any(p.search(cmd) for p in _DANGER_PATTERNS)

    def run(self, cmd: str) -> str:
        if not cmd.strip():
            return "No command provided."

        if self._require_confirm and self.is_dangerous(cmd):
            allowed = self._confirm_cb(cmd) if self._confirm_cb else False
            if not allowed:
                return (
                    f"Blocked by safety guard: {cmd!r}\n"
                    "This command is potentially destructive. "
                    "Confirm in the dialog to execute."
                )

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_SHELL_TIMEOUT,
                cwd=os.path.expanduser("~"),
            )
            output = (result.stdout + result.stderr).strip()
            if not output:
                output = f"Done (exit code {result.returncode})."
            elif len(output) > _MAX_OUTPUT_CHARS:
                output = output[:_MAX_OUTPUT_CHARS] + "\n[…output truncated…]"
            log.info("Shell: %r → exit %d", cmd[:60], result.returncode)
            return output
        except subprocess.TimeoutExpired:
            return f"Command timed out after {_SHELL_TIMEOUT} seconds."
        except Exception as exc:
            return f"Command error: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# Control module — public API
# ══════════════════════════════════════════════════════════════════════════════

class ControlModule:
    """
    Public interface for all laptop-control capabilities.

    main.py wires:
        ctrl    = ControlModule(config)
        confirm = ConfirmationDialog()
        ctrl.set_confirm_cb(confirm.ask)
        core.set_control_module(ctrl)

    ATLASCore calls:
        if ControlModule.is_control_query(text):
            response = ctrl.execute(action_dict)
    """

    # Phrases that signal control intent — checked by ATLASCore before routing
    _TRIGGERS = frozenset({
        "open ", "launch ", "start ", "close ", "quit ",
        "switch to ", "focus ", "bring up ", "minimize ", "minimise ",
        "maximize ", "maximise ", "hide ",
        "click ", "double click ", "right click ",
        "scroll up", "scroll down",
        "drag ", "move mouse", "move the mouse",
        "type ", "press ", "hold down", "hold ",
        "screenshot", "take a screenshot", "capture the screen",
        "read the screen", "what's on the screen", "read what's on",
        "what does the screen say", "what is on the screen",
        "what can you see on the screen",
        "run command", "execute command", "run in terminal",
        "open terminal", "open a terminal",
        "paste ", "copy that", "select all",
        "list open apps", "what apps are open", "what windows are open",
    })

    # Phrases that look like control but should stay in regular chat/web flow
    _EXCLUDES = frozenset({
        "open this link", "open the article", "open the url",
        "search for", "look up online", "browse to",
        "play music", "play a song", "play a video",
        "start a new", "close enough", "quit smoking",
    })

    def __init__(self, config: dict):
        self._mouse    = MouseController()
        self._keyboard = KeyboardController()
        self._windows  = WindowManager()
        self._screen   = ScreenReader()
        self._shell    = ShellExecutor(config)
        log.info("ControlModule ready.")

    def set_confirm_cb(self, cb: Callable[[str], bool]) -> None:
        self._shell.set_confirm_cb(cb)

    # ── Control query detection ───────────────────────────────────────────────

    @classmethod
    def is_control_query(cls, text: str) -> bool:
        """Return True if text looks like a laptop control command."""
        lower = text.lower()
        if any(excl in lower for excl in cls._EXCLUDES):
            return False
        return any(kw in lower for kw in cls._TRIGGERS)

    # ── Action executor ───────────────────────────────────────────────────────

    def execute(self, action: dict) -> str:
        """
        Execute a parsed action dict and return a voice-friendly response.

        action must have:
            "action"   : str   — action type key
            "response" : str   — LLM-generated voice confirmation (preferred)
            + action-specific fields (name, text, command, x/y, etc.)
        """
        kind         = action.get("action", "none")
        llm_response = action.get("response", "").strip()

        try:
            actual = self._dispatch(kind, action)
        except Exception as exc:
            log.error("Control dispatch error: %s", exc)
            actual = f"Control error: {exc}"

        # For output-producing actions, include the actual output
        if kind in ("run_command", "read_screen", "list_windows"):
            if llm_response:
                return f"{llm_response}\n{actual}".strip()
            return actual

        # For everything else, the LLM's pre-generated voice response is better
        return llm_response or actual

    # ── Dispatch table ────────────────────────────────────────────────────────

    def _dispatch(self, kind: str, a: dict) -> str:   # noqa: C901
        if not kind or kind == "none":
            return a.get("response", "I didn't understand that control command.")

        # Mouse
        if kind == "move_mouse":
            return self._mouse.move(a.get("x", 0), a.get("y", 0), a.get("duration", 0.25))
        if kind == "click":
            return self._mouse.click(a.get("x"), a.get("y"),
                                     a.get("button", "left"), a.get("double", False))
        if kind == "scroll":
            return self._mouse.scroll(a.get("direction", "down"), a.get("amount", 3))
        if kind == "drag":
            return self._mouse.drag(a.get("x1", 0), a.get("y1", 0),
                                    a.get("x2", 0), a.get("y2", 0))

        # Keyboard
        if kind == "type_text":
            return self._keyboard.type_text(a.get("text", ""))
        if kind == "press_key":
            key  = a.get("key", "")
            mods = a.get("modifiers", [])
            return self._keyboard.hotkey(*mods, key) if mods else self._keyboard.press(key)
        if kind == "hotkey":
            keys = a.get("keys", [])
            return self._keyboard.hotkey(*keys) if keys else "No keys specified."

        # Clipboard shortcuts
        mod = "command" if _IS_MAC else "ctrl"
        if kind == "copy":       return self._keyboard.hotkey(mod, "c")
        if kind == "paste":      return self._keyboard.hotkey(mod, "v")
        if kind == "select_all": return self._keyboard.hotkey(mod, "a")
        if kind == "undo":       return self._keyboard.hotkey(mod, "z")

        # Window / app management
        if kind == "open_app":
            name = a.get("name", "")
            return (self._windows.open_url(name)
                    if name.startswith(("http://", "https://"))
                    else self._windows.open_app(name))
        if kind == "open_url":     return self._windows.open_url(a.get("url", ""))
        if kind == "close_app":    return self._windows.close_app(a.get("name", ""))
        if kind == "focus_app":    return self._windows.focus_app(a.get("name", ""))
        if kind == "minimize_app": return self._windows.minimize_app(a.get("name", ""))
        if kind == "maximize_app": return self._windows.maximize_app(a.get("name", ""))
        if kind == "list_windows":
            apps = self._windows.list_windows()
            return ("Open apps: " + ", ".join(apps[:20]) + ".") if apps else "No windows found."

        # Screen
        if kind == "screenshot":
            msg, _ = self._screen.screenshot(save_path=a.get("path"))
            return msg
        if kind == "read_screen":
            return self._screen.read_screen()

        # Shell
        if kind == "run_command":
            return self._shell.run(a.get("command", ""))

        return f"Unknown action: {kind!r}"

    # ── Convenience methods (callable directly from tests / REPL) ─────────────

    def screenshot_text(self) -> str:
        """Extract all visible text from the screen via OCR."""
        return self._screen.read_screen()

    def open(self, name: str) -> str:
        """Open an application by name."""
        return self._windows.open_app(name)

    def run_command(self, cmd: str, confirmed: bool = False) -> str:
        """Run a shell command. Pass confirmed=True to bypass the safety dialog."""
        if confirmed:
            original = self._shell._confirm_cb
            self._shell._confirm_cb = lambda _: True
            try:
                return self._shell.run(cmd)
            finally:
                self._shell._confirm_cb = original
        return self._shell.run(cmd)
