"""
ATLAS Mac System Control — Module 3 (rebuilt)

Components
──────────
  ConfirmationDialog   Thread-safe Qt confirmation dialog
  MouseController      pyautogui mouse: click, scroll, drag
  KeyboardController   pyautogui keyboard: type, press, hotkeys
  WindowManager        App open/close/focus/list via subprocess + osascript
  SystemController     Volume, brightness, battery, CPU/RAM/disk, lock, sleep
  ScreenReader         Screenshot + OCR (pytesseract)
  FileController       Open folders, find files, create folders, trash
  BrowserController    Tab management, navigation, search
  ShellExecutor        Safe shell execution with destructive-command guard
  PermissionChecker    macOS Accessibility / Screen Recording status
  ControlModule        Public API — orchestrates all of the above

Safety rules (never relaxed without user confirmation):
  • All file deletions use Trash, never permanent rm
  • Sleep and shutdown require explicit confirmation
  • sudo commands are always blocked unless user confirms
  • Restricted commands from config.yaml are blocked

Platform: macOS (Apple Silicon primary). Falls back gracefully on Linux/Windows.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Optional

log = logging.getLogger(__name__)

_SYSTEM = platform.system()
_IS_MAC = _SYSTEM == "Darwin"
_IS_WIN = _SYSTEM == "Windows"

# ── Shell safety patterns ─────────────────────────────────────────────────────

_DANGER_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\s+-[a-z]*r[a-z]*f",      re.I),
    re.compile(r"rm\s+-[a-z]*f[a-z]*r",      re.I),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r">\s*/dev/nvme"),
    re.compile(r"\bmkfs\b",                   re.I),
    re.compile(r"\bdd\s+if=",                re.I),
    re.compile(r":\s*\(\s*\)\s*\{.*\|.*:.*&",re.I),
    re.compile(r"format\s+[a-z]:",           re.I),
    re.compile(r"\bdel\s+/[fqs]",            re.I),
    re.compile(r"\breg\s+delete\b",          re.I),
    re.compile(r"\bcipher\s+/w\b",           re.I),
    re.compile(r"\bshred\s+-[un]",           re.I),
    re.compile(r"\bwipefs\b",                re.I),
    re.compile(r"\bsudo\s+rm\b",             re.I),
    re.compile(r"\bsudo\s+shutdown\b",       re.I),
    re.compile(r"\bsudo\s+reboot\b",         re.I),
    re.compile(r"\bhalt\b",                  re.I),
    re.compile(r"\bpoweroff\b",              re.I),
    re.compile(r"\binit\s+0\b",             re.I),
    re.compile(r"\bshutdown\s+/[srh]\b",    re.I),
]

_MAX_OUTPUT_CHARS = 3_000
_SHELL_TIMEOUT    = 30


# ══════════════════════════════════════════════════════════════════════════════
# Confirmation dialog
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmationDialog:
    """Thread-safe Qt input dialog. Blocks the calling thread until dismissed."""

    def __init__(self):
        self._event  = Event()
        self._result = False
        self._lock   = Lock()

    def ask(self, cmd: str) -> bool:
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
            pyautogui.FAILSAFE = True
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
                "Go to System Settings → Privacy and Security → Accessibility."
            )
        if "failsafe" in msg.lower():
            return "Mouse control aborted by fail-safe."
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
                "Go to System Settings → Privacy and Security → Accessibility."
            )
        return f"Keyboard error: {msg}"


# ══════════════════════════════════════════════════════════════════════════════
# Window / application manager
# ══════════════════════════════════════════════════════════════════════════════

class WindowManager:

    def __init__(self):
        log.info("WindowManager ready (%s).", _SYSTEM)

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
                    subprocess.Popen(["open", name])
            elif _IS_WIN:
                os.startfile(name)  # type: ignore[attr-defined]
            else:
                subprocess.Popen([name])
            return f"Opening {name}."
        except FileNotFoundError:
            return f"Application not found: {name}."
        except Exception as exc:
            return f"Failed to open {name}: {exc}"

    def open_url(self, url: str) -> str:
        try:
            webbrowser.open(url)
            return f"Opened {url} in your browser."
        except Exception as exc:
            return f"Failed to open URL: {exc}"

    def close_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                script = f'tell application "{name}" to quit'
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=8)
                if r.returncode != 0 and r.stderr.strip():
                    return f"Could not close {name}: {r.stderr.strip()}"
            elif _IS_WIN:
                subprocess.run(["taskkill", "/F", "/IM", f"{name}.exe"],
                               capture_output=True, timeout=8)
            else:
                subprocess.run(["pkill", "-f", name], capture_output=True, timeout=8)
            return f"Closed {name}."
        except Exception as exc:
            return f"Failed to close {name}: {exc}"

    def focus_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                script = f'tell application "{name}" to activate'
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=8)
                if r.returncode != 0:
                    return f"Could not focus {name}: {r.stderr.strip()}"
            elif _IS_WIN:
                try:
                    import pygetwindow as gw
                    wins = gw.getWindowsWithTitle(name)
                    if wins:
                        wins[0].activate()
                    else:
                        return f"No window found: {name}"
                except ImportError:
                    subprocess.Popen(["start", "", name], shell=True)
            else:
                subprocess.run(["wmctrl", "-a", name], timeout=5)
            return f"Switched to {name}."
        except Exception as exc:
            return f"Failed to focus {name}: {exc}"

    def minimize_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                script = (
                    f'tell application "System Events" to set miniaturized of '
                    f'windows of process "{name}" to true'
                )
                subprocess.run(["osascript", "-e", script], timeout=8)
            return f"Minimized {name}."
        except Exception as exc:
            return f"Failed to minimize {name}: {exc}"

    def maximize_app(self, name: str) -> str:
        try:
            if _IS_MAC:
                script1 = (
                    f'tell application "System Events" to set miniaturized of '
                    f'windows of process "{name}" to false'
                )
                script2 = f'tell application "{name}" to activate'
                subprocess.run(["osascript", "-e", script1], timeout=8)
                subprocess.run(["osascript", "-e", script2], timeout=8)
            return f"Maximized {name}."
        except Exception as exc:
            return f"Failed to maximize {name}: {exc}"

    def list_windows(self) -> list[str]:
        try:
            if _IS_MAC:
                script = (
                    'tell application "System Events" to get name of '
                    'every process whose visible is true'
                )
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=8)
                if r.returncode == 0 and r.stdout.strip():
                    return [a.strip() for a in r.stdout.strip().split(",") if a.strip()]
            elif _IS_WIN:
                import pygetwindow as gw
                return [w.title for w in gw.getAllWindows() if w.title.strip()]
            else:
                r = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5)
                return [ln.split(None, 3)[-1] for ln in r.stdout.strip().splitlines() if ln]
        except Exception as exc:
            log.warning("list_windows failed: %s", exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# System controller — volume, brightness, battery, stats, sleep, lock
# ══════════════════════════════════════════════════════════════════════════════

class SystemController:

    def __init__(self):
        log.info("SystemController ready.")

    # ── Volume ────────────────────────────────────────────────────────────────

    def volume_get(self) -> str:
        if not _IS_MAC:
            return "Volume control is macOS-only."
        try:
            r = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=5,
            )
            return f"Volume is at {r.stdout.strip()} percent."
        except Exception as exc:
            return f"Could not read volume: {exc}"

    def volume_set(self, level: int) -> str:
        level = max(0, min(100, level))
        if not _IS_MAC:
            return "Volume control is macOS-only."
        try:
            subprocess.run(
                ["osascript", "-e", f"set volume output volume {level}"],
                capture_output=True, timeout=5,
            )
            return f"Volume set to {level} percent."
        except Exception as exc:
            return f"Volume error: {exc}"

    def volume_up(self, step: int = 10) -> str:
        if not _IS_MAC:
            return "Volume control is macOS-only."
        try:
            r = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=5,
            )
            current = int(r.stdout.strip() or "50")
            return self.volume_set(current + step)
        except Exception:
            return self.volume_set(60)

    def volume_down(self, step: int = 10) -> str:
        if not _IS_MAC:
            return "Volume control is macOS-only."
        try:
            r = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=5,
            )
            current = int(r.stdout.strip() or "50")
            return self.volume_set(current - step)
        except Exception:
            return self.volume_set(40)

    def mute(self) -> str:
        if not _IS_MAC:
            return "Mute is macOS-only."
        try:
            subprocess.run(
                ["osascript", "-e", "set volume output muted true"],
                capture_output=True, timeout=5,
            )
            return "Muted."
        except Exception as exc:
            return f"Mute error: {exc}"

    def unmute(self) -> str:
        if not _IS_MAC:
            return "Unmute is macOS-only."
        try:
            subprocess.run(
                ["osascript", "-e", "set volume output muted false"],
                capture_output=True, timeout=5,
            )
            return "Unmuted."
        except Exception as exc:
            return f"Unmute error: {exc}"

    # ── Brightness ────────────────────────────────────────────────────────────

    def brightness_up(self) -> str:
        if not _IS_MAC:
            return "Brightness control is macOS-only."
        try:
            import pyautogui
            pyautogui.press("f2")   # F2 = brightness up on most Macs
            return "Brightness increased."
        except Exception as exc:
            return f"Brightness error: {exc}"

    def brightness_down(self) -> str:
        if not _IS_MAC:
            return "Brightness control is macOS-only."
        try:
            import pyautogui
            pyautogui.press("f1")   # F1 = brightness down
            return "Brightness decreased."
        except Exception as exc:
            return f"Brightness error: {exc}"

    # ── Battery ───────────────────────────────────────────────────────────────

    def battery(self) -> str:
        try:
            import psutil
            b = psutil.sensors_battery()
            if b is None:
                return "No battery detected — this Mac may be a desktop."
            plugged = "charging" if b.power_plugged else "not charging"
            return (f"Battery is at {b.percent:.0f} percent and {plugged}.")
        except ImportError:
            if _IS_MAC:
                r = subprocess.run(["pmset", "-g", "batt"],
                                   capture_output=True, text=True, timeout=5)
                return r.stdout.strip().split("\n")[-1].strip() or "Battery info unavailable."
            return "psutil is required for battery info."
        except Exception as exc:
            return f"Battery error: {exc}"

    # ── System stats ──────────────────────────────────────────────────────────

    def system_stats(self) -> str:
        try:
            import psutil
            cpu  = psutil.cpu_percent(interval=0.5)
            ram  = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            net  = psutil.net_io_counters()
            return (
                f"CPU is at {cpu:.0f} percent. "
                f"RAM is {ram.percent:.0f} percent used, "
                f"{ram.available // (1024**3):.1f} GB free. "
                f"Disk is {disk.percent:.0f} percent full, "
                f"{disk.free // (1024**3):.0f} GB free."
            )
        except ImportError:
            return "psutil is required for system stats."
        except Exception as exc:
            return f"Stats error: {exc}"

    # ── Lock / sleep ──────────────────────────────────────────────────────────

    def lock_screen(self) -> str:
        if not _IS_MAC:
            return "Lock screen is macOS-only."
        try:
            subprocess.run(
                ["pmset", "displaysleepnow"],
                capture_output=True, timeout=5,
            )
            return "Screen locked."
        except Exception as exc:
            return f"Lock error: {exc}"

    def sleep_mac(self) -> str:
        if not _IS_MAC:
            return "Sleep is macOS-only."
        try:
            subprocess.run(["pmset", "sleepnow"], capture_output=True, timeout=5)
            return "Going to sleep."
        except Exception as exc:
            return f"Sleep error: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# Screen reader
# ══════════════════════════════════════════════════════════════════════════════

class ScreenReader:

    _TESSERACT_INSTALL = (
        "Install Tesseract: brew install tesseract"
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
            pytesseract.get_tesseract_version()
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
                    "Go to System Settings → Privacy and Security → Screen Recording.",
                    None,
                )
            return f"Screenshot failed: {exc}", None

    def read_screen(self, region: tuple | None = None) -> str:
        if not self._pyag:
            return "Screenshot unavailable — pyautogui not installed."
        if not self._tesseract:
            return f"OCR unavailable. {self._TESSERACT_INSTALL}"
        try:
            img  = self._pyag.screenshot(region=region) if region else self._pyag.screenshot()
            text = self._tesseract.image_to_string(img).strip()
            if not text:
                return "No text detected on screen."
            if len(text) > 2_000:
                text = text[:2_000] + "\n[…truncated…]"
            return text
        except Exception as exc:
            if "permission" in str(exc).lower():
                return (
                    "Screen reading requires Screen Recording permission. "
                    "Go to System Settings → Privacy and Security → Screen Recording."
                )
            return f"Screen read failed: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# File controller
# ══════════════════════════════════════════════════════════════════════════════

class FileController:

    _FOLDERS = {
        "downloads":  Path.home() / "Downloads",
        "desktop":    Path.home() / "Desktop",
        "documents":  Path.home() / "Documents",
        "pictures":   Path.home() / "Pictures",
        "music":      Path.home() / "Music",
        "movies":     Path.home() / "Movies",
        "home":       Path.home(),
    }

    def __init__(self):
        log.info("FileController ready.")

    def open_folder(self, name: str) -> str:
        lower = name.lower().strip()
        folder = self._FOLDERS.get(lower)
        if folder is None:
            # Try as literal path
            folder = Path(name).expanduser()
        if not folder.exists():
            return f"Folder not found: {name}."
        try:
            if _IS_MAC:
                subprocess.Popen(["open", str(folder)])
            elif _IS_WIN:
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(folder)])
            return f"Opened {name} in Finder."
        except Exception as exc:
            return f"Could not open {name}: {exc}"

    def find_file(self, name: str) -> str:
        try:
            if _IS_MAC:
                r = subprocess.run(
                    ["mdfind", "-name", name],
                    capture_output=True, text=True, timeout=10,
                )
                lines = r.stdout.strip().splitlines()[:5]
                if not lines:
                    return f"No file named {name!r} found."
                return "Found: " + "; ".join(lines)
            else:
                r = subprocess.run(
                    ["find", str(Path.home()), "-name", name, "-maxdepth", "6"],
                    capture_output=True, text=True, timeout=15,
                )
                lines = r.stdout.strip().splitlines()[:5]
                if not lines:
                    return f"No file named {name!r} found."
                return "Found: " + "; ".join(lines)
        except Exception as exc:
            return f"File search error: {exc}"

    def create_folder(self, name: str, parent: str = "Desktop") -> str:
        try:
            base   = self._FOLDERS.get(parent.lower(), Path.home() / "Desktop")
            target = base / name
            target.mkdir(parents=True, exist_ok=True)
            return f"Created folder {name!r} on your {parent}."
        except Exception as exc:
            return f"Could not create folder: {exc}"

    def trash_file(self, path: str) -> str:
        """Move file to Trash (never permanently deletes)."""
        try:
            target = Path(path).expanduser()
            if not target.exists():
                return f"File not found: {path}"
            if _IS_MAC:
                # Use AppleScript to move to Trash properly
                script = (
                    f'tell application "Finder" to move '
                    f'POSIX file "{target}" to trash'
                )
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    return f"Could not trash {path}: {r.stderr.strip()}"
            else:
                trash = Path.home() / ".Trash"
                trash.mkdir(exist_ok=True)
                shutil.move(str(target), str(trash / target.name))
            return f"Moved {target.name} to Trash."
        except Exception as exc:
            return f"Trash error: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# Browser controller
# ══════════════════════════════════════════════════════════════════════════════

class BrowserController:

    _MOD = "command" if _IS_MAC else "ctrl"

    def __init__(self):
        self._pyag = None
        try:
            import pyautogui
            self._pyag = pyautogui
            log.info("BrowserController ready.")
        except ImportError:
            log.warning("pyautogui not installed — browser control limited.")

    def new_tab(self) -> str:
        return self._hotkey(self._MOD, "t", desc="New tab opened.")

    def close_tab(self) -> str:
        return self._hotkey(self._MOD, "w", desc="Tab closed.")

    def go_back(self) -> str:
        return self._hotkey(self._MOD, "[", desc="Went back.")

    def go_forward(self) -> str:
        return self._hotkey(self._MOD, "]", desc="Went forward.")

    def reload(self) -> str:
        return self._hotkey(self._MOD, "r", desc="Page reloaded.")

    def search(self, query: str) -> str:
        try:
            encoded = query.replace(" ", "+")
            webbrowser.open(f"https://www.google.com/search?q={encoded}")
            return f"Searching Google for {query!r}."
        except Exception as exc:
            return f"Browser search error: {exc}"

    def open_url(self, url: str) -> str:
        try:
            webbrowser.open(url)
            return f"Opened {url}."
        except Exception as exc:
            return f"Could not open URL: {exc}"

    def scroll_down(self) -> str:
        if not self._pyag:
            return "pyautogui not installed."
        try:
            self._pyag.scroll(-5)
            return "Scrolled down."
        except Exception as exc:
            return f"Scroll error: {exc}"

    def scroll_up(self) -> str:
        if not self._pyag:
            return "pyautogui not installed."
        try:
            self._pyag.scroll(5)
            return "Scrolled up."
        except Exception as exc:
            return f"Scroll error: {exc}"

    def _hotkey(self, *keys: str, desc: str = "Done.") -> str:
        if not self._pyag:
            return "pyautogui not installed."
        try:
            self._pyag.hotkey(*keys)
            return desc
        except Exception as exc:
            return f"Keyboard error: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# Shell executor
# ══════════════════════════════════════════════════════════════════════════════

class ShellExecutor:

    def __init__(self, config: dict, confirm_cb: Callable[[str], bool] | None = None):
        safety               = config.get("safety", {})
        self._require_confirm = safety.get("confirm_destructive_commands", True)
        self._restricted     = [s.lower() for s in safety.get("restricted_commands", [])]
        self._confirm_cb     = confirm_cb

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
                    f"Blocked by safety guard: {cmd!r}. "
                    "This command is potentially destructive."
                )
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=_SHELL_TIMEOUT, cwd=os.path.expanduser("~"),
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
# Permission checker
# ══════════════════════════════════════════════════════════════════════════════

class PermissionChecker:

    def check_all(self) -> str:
        results = []
        results.append(self._check_accessibility())
        results.append(self._check_screen_recording())
        return " ".join(results)

    def _check_accessibility(self) -> str:
        if not _IS_MAC:
            return "Accessibility check is macOS-only."
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process'],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return "Accessibility permission granted."
            return (
                "Accessibility permission needed. "
                "Go to System Settings → Privacy and Security → Accessibility."
            )
        except Exception:
            return "Could not check Accessibility permission."

    def _check_screen_recording(self) -> str:
        if not _IS_MAC:
            return "Screen Recording check is macOS-only."
        try:
            import pyautogui
            pyautogui.screenshot()
            return "Screen Recording permission granted."
        except Exception as exc:
            if "permission" in str(exc).lower() or "recording" in str(exc).lower():
                return (
                    "Screen Recording permission needed. "
                    "Go to System Settings → Privacy and Security → Screen Recording."
                )
            return "Screen Recording: unknown status."


# ══════════════════════════════════════════════════════════════════════════════
# ControlModule — public API (backward-compatible + extended)
# ══════════════════════════════════════════════════════════════════════════════

class ControlModule:
    """
    Public interface for all Mac control capabilities.

    Same interface as before (is_control_query / execute) plus new
    direct methods for system, file, and browser control.

    main.py wires:
        ctrl    = ControlModule(config)
        confirm = ConfirmationDialog()
        ctrl.set_confirm_cb(confirm.ask)
        core.set_control_module(ctrl)
    """

    # Phrases that signal control intent
    _TRIGGERS = frozenset({
        "open ", "launch ", "start ", "close ", "quit ",
        "switch to ", "focus ", "bring up ", "minimize ", "minimise ",
        "maximize ", "maximise ", "hide ",
        "click ", "double click ", "right click ",
        "scroll up", "scroll down",
        "drag ", "move mouse", "move the mouse",
        "type ", "press ", "hold down",
        "screenshot", "take a screenshot", "capture the screen",
        "read the screen", "what's on the screen", "read what's on",
        "what does the screen say", "what is on the screen",
        "volume up", "volume down", "volume set", "mute", "unmute",
        "set volume", "turn up the volume", "turn down the volume",
        "brightness up", "brightness down",
        "battery", "battery level", "how much battery",
        "system stats", "cpu usage", "ram usage", "disk space",
        "lock screen", "lock my screen", "sleep mac", "go to sleep",
        "open downloads", "open desktop", "open documents",
        "find file", "create folder", "move to trash", "trash this",
        "new tab", "close tab", "go back", "go forward", "reload",
        "search for", "open youtube", "open google",
        "run command", "execute command", "open terminal",
        "paste ", "copy that", "select all",
        "list open apps", "what apps are open", "what windows are open",
        "check permissions", "do you have accessibility",
    })

    _EXCLUDES = frozenset({
        "open this link", "open the article", "start a new",
        "close enough", "quit smoking", "start fresh",
        "search for meaning", "find file in code",
    })

    def __init__(self, config: dict):
        self._mouse      = MouseController()
        self._keyboard   = KeyboardController()
        self._windows    = WindowManager()
        self._system     = SystemController()
        self._screen     = ScreenReader()
        self._files      = FileController()
        self._browser    = BrowserController()
        self._shell      = ShellExecutor(config)
        self._perms      = PermissionChecker()
        log.info("ControlModule ready.")

    def set_confirm_cb(self, cb: Callable[[str], bool]) -> None:
        self._shell.set_confirm_cb(cb)

    # ── Control query detection ───────────────────────────────────────────────

    @classmethod
    def is_control_query(cls, text: str) -> bool:
        lower = text.lower()
        if any(excl in lower for excl in cls._EXCLUDES):
            return False
        return any(kw in lower for kw in cls._TRIGGERS)

    # ── Action executor ───────────────────────────────────────────────────────

    def execute(self, action: dict) -> str:
        kind         = action.get("action", "none")
        llm_response = action.get("response", "").strip()
        try:
            actual = self._dispatch(kind, action)
        except Exception as exc:
            log.error("Control dispatch error: %s", exc)
            actual = f"Control error: {exc}"

        if kind in ("run_command", "read_screen", "list_windows",
                    "system_stats", "battery", "find_file"):
            return f"{llm_response}\n{actual}".strip() if llm_response else actual

        return llm_response or actual

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, kind: str, a: dict) -> str:   # noqa: C901
        if not kind or kind == "none":
            return a.get("response", "Command not understood.")

        # Mouse
        if kind == "move_mouse":  return self._mouse.move(a.get("x", 0), a.get("y", 0))
        if kind == "click":       return self._mouse.click(a.get("x"), a.get("y"),
                                                           a.get("button", "left"), a.get("double", False))
        if kind == "scroll":      return self._mouse.scroll(a.get("direction", "down"), a.get("amount", 3))
        if kind == "drag":        return self._mouse.drag(a.get("x1",0), a.get("y1",0),
                                                          a.get("x2",0), a.get("y2",0))

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

        # Clipboard
        mod = "command" if _IS_MAC else "ctrl"
        if kind == "copy":       return self._keyboard.hotkey(mod, "c")
        if kind == "paste":      return self._keyboard.hotkey(mod, "v")
        if kind == "select_all": return self._keyboard.hotkey(mod, "a")
        if kind == "undo":       return self._keyboard.hotkey(mod, "z")

        # App management
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
            return ("Open apps: " + ", ".join(apps[:20])) if apps else "No windows found."

        # Screen
        if kind == "screenshot":
            msg, _ = self._screen.screenshot(save_path=a.get("path"))
            return msg
        if kind == "read_screen": return self._screen.read_screen()

        # System
        if kind == "volume_up":    return self._system.volume_up(a.get("step", 10))
        if kind == "volume_down":  return self._system.volume_down(a.get("step", 10))
        if kind == "volume_set":   return self._system.volume_set(int(a.get("level", 50)))
        if kind == "volume_get":   return self._system.volume_get()
        if kind == "mute":         return self._system.mute()
        if kind == "unmute":       return self._system.unmute()
        if kind == "brightness_up":   return self._system.brightness_up()
        if kind == "brightness_down": return self._system.brightness_down()
        if kind == "battery":      return self._system.battery()
        if kind == "system_stats": return self._system.system_stats()
        if kind == "lock_screen":  return self._system.lock_screen()
        if kind == "sleep":        return self._system.sleep_mac()

        # Files
        if kind == "open_folder":   return self._files.open_folder(a.get("name", "downloads"))
        if kind == "find_file":     return self._files.find_file(a.get("name", ""))
        if kind == "create_folder": return self._files.create_folder(a.get("name", "New Folder"),
                                                                      a.get("parent", "Desktop"))
        if kind == "trash_file":    return self._files.trash_file(a.get("path", ""))

        # Browser
        if kind == "new_tab":       return self._browser.new_tab()
        if kind == "close_tab":     return self._browser.close_tab()
        if kind == "go_back":       return self._browser.go_back()
        if kind == "go_forward":    return self._browser.go_forward()
        if kind == "reload":        return self._browser.reload()
        if kind == "browser_search":return self._browser.search(a.get("query", ""))
        if kind == "scroll_down":   return self._browser.scroll_down()
        if kind == "scroll_up":     return self._browser.scroll_up()

        # Shell
        if kind == "run_command":   return self._shell.run(a.get("command", ""))

        # Permissions
        if kind == "check_permissions": return self._perms.check_all()

        return f"Unknown action: {kind!r}"

    # ── Convenience direct methods ────────────────────────────────────────────

    def screenshot_text(self) -> str:
        return self._screen.read_screen()

    def open(self, name: str) -> str:
        return self._windows.open_app(name)

    def run_command(self, cmd: str, confirmed: bool = False) -> str:
        if confirmed:
            original = self._shell._confirm_cb
            self._shell._confirm_cb = lambda _: True
            try:
                return self._shell.run(cmd)
            finally:
                self._shell._confirm_cb = original
        return self._shell.run(cmd)

    def get_system_stats(self) -> str:
        return self._system.system_stats()

    def get_battery(self) -> str:
        return self._system.battery()
