"""
ATLAS Context Manager — Active Window Detection

Polls macOS every 5 seconds via AppleScript to track:
  - Frontmost application name and window title
  - VS Code: active file path and programming language
  - Browser (Chrome / Safari / Arc / Brave): current URL + page title
  - Terminal: current working directory

Emits Qt signals consumed by the HUD and feed panel.
Injects a short one-line context prefix into every AI query
so ATLAS always knows what the user is doing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

log = logging.getLogger(__name__)

# ── Safe file types (will be read for "help me with this") ───────────────────
_SAFE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".r", ".m", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".md",
    ".txt", ".css", ".html", ".htm", ".sql", ".lua", ".zig",
}

# ── Never read these — even if extension looks safe ──────────────────────────
_BLOCKED_PATTERNS = {
    ".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", "credentials",
    "secret", "password", "passwd", "id_rsa", "id_ed25519", "id_dsa", ".gpg",
    "token", "apikey", "api_key",
}

# ── Browser app names that expose URL via AppleScript ────────────────────────
_CHROME_LIKE = {"Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge"}
_SAFARI_LIKE = {"Safari"}
_ARC         = {"Arc"}

# ── Language map ──────────────────────────────────────────────────────────────
_LANG_MAP: dict[str, str] = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript/React", ".jsx": "JavaScript/React",
    ".java": "Java", ".c": "C", ".cpp": "C++", ".h": "C/C++ Header",
    ".cs": "C#", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".php": "PHP", ".swift": "Swift", ".kt": "Kotlin", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Bash", ".zsh": "Zsh",
    ".html": "HTML", ".css": "CSS", ".sql": "SQL", ".md": "Markdown",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".lua": "Lua",
}


def _run_script(script: str, timeout: float = 3.0) -> str:
    """Run an AppleScript and return stdout, empty string on error."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except Exception:
        return ""


class ContextManager(QObject):
    """
    Polls the macOS window manager every 5 s.
    Emits context_updated(dict) which the HUD and feed panel consume.
    """

    context_updated = pyqtSignal(dict)

    _POLL_INTERVAL = 5  # seconds

    def __init__(self, config: dict):
        super().__init__()
        self._config  = config
        self._active  = False
        self._context: dict = {}
        self._web_module = None   # injected from main.py if available

        root = Path(os.environ.get("ATLAS_ROOT", "."))
        self._log_dir = root / "context"
        self._log_dir.mkdir(exist_ok=True)
        self._log_path = self._log_dir / "context_log.json"

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_web_module(self, web) -> None:
        self._web_module = web

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._active = True
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="atlas-context").start()
        log.info("ContextManager: polling started.")

    def stop(self) -> None:
        self._active = False

    # ── Public API ────────────────────────────────────────────────────────────

    def get_context(self) -> dict:
        return dict(self._context)

    def get_context_prefix(self) -> str:
        """One-line prefix injected before every AI query."""
        ctx = self._context
        if not ctx:
            return ""
        parts = []
        app = ctx.get("app", "")
        if app:
            parts.append(f"Active app: {app}")
        if ctx.get("file"):
            lang = ctx.get("lang", "")
            parts.append(f"File: {ctx['file']}" + (f" ({lang})" if lang else ""))
        if ctx.get("url"):
            parts.append(f"URL: {ctx['url'][:80]}")
        if ctx.get("cwd"):
            parts.append(f"Terminal dir: {ctx['cwd']}")
        return "[Context: " + " | ".join(parts) + "]" if parts else ""

    def get_context_description(self) -> str:
        """Voice-readable description of current context."""
        ctx = self._context
        if not ctx:
            return "I don't have any context right now."
        app  = ctx.get("app", "unknown app")
        desc = f"You are currently in {app}."
        if ctx.get("file"):
            lang = ctx.get("lang", "")
            desc += f" You have {ctx['file']}" + (f", a {lang} file," if lang else "") + " open."
        if ctx.get("url"):
            title = ctx.get("page_title", "")
            desc += f" You're browsing" + (f" '{title}'" if title else f" {ctx['url'][:60]}") + "."
        if ctx.get("cwd"):
            desc += f" Your terminal is in {ctx['cwd']}."
        return desc

    def get_file_content(self, max_lines: int = 120) -> Optional[str]:
        """Read the active VS Code file safely. Returns None if unsafe."""
        fp = self._context.get("file_path", "")
        if not fp:
            return None
        path = Path(fp)
        if not path.exists() or not path.is_file():
            return None
        # Safety checks
        if path.suffix.lower() not in _SAFE_EXTS:
            return None
        lower_name = path.name.lower()
        if any(pat in lower_name for pat in _BLOCKED_PATTERNS):
            log.info("Context: blocked reading sensitive file %s", path.name)
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            snippet = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                snippet += f"\n... ({len(lines) - max_lines} more lines)"
            return snippet
        except Exception as exc:
            log.debug("Context file read error: %s", exc)
            return None

    # ── Voice command handler ─────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()
        name  = self._config.get("user_name", "Boss")

        if any(p in lower for p in ("what am i working on", "what are we working on",
                                     "what's my context", "what is my context")):
            return self.get_context_description()

        if any(p in lower for p in ("remember this context", "save this context",
                                     "remember what i'm working on")):
            self._save_context_memory()
            return f"Context saved, {name}. I'll remember what you're working on."

        if any(p in lower for p in ("atlas help me with this", "help me with this",
                                     "help me here", "atlas help me here")):
            return self._build_help_response(name)

        return None

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._active:
            try:
                ctx = self._detect()
                if ctx:
                    self._context = ctx
                    self.context_updated.emit(ctx)
                    self._log_context(ctx)
            except Exception as exc:
                log.debug("Context poll error: %s", exc)
            time.sleep(self._POLL_INTERVAL)

    def _detect(self) -> dict:
        # 1 — Frontmost app name
        app = _run_script(
            'tell application "System Events" to '
            'get name of first process whose frontmost is true'
        )
        title = self._get_window_title(app)

        ctx: dict = {"app": app, "title": title, "ts": datetime.now().isoformat()}

        # 2 — VS Code
        if "visual studio code" in app.lower() or "code" == app.lower():
            self._enrich_vscode(ctx, title)

        # 3 — Browsers
        elif app in _CHROME_LIKE:
            self._enrich_chrome(ctx, app)
        elif app in _SAFARI_LIKE:
            self._enrich_safari(ctx)
        elif app in _ARC:
            self._enrich_arc(ctx)

        # 4 — Terminal
        elif app in {"Terminal", "iTerm2", "iTerm", "Warp", "Alacritty", "Hyper"}:
            self._enrich_terminal(ctx, app)

        return ctx

    # ── App-specific enrichers ────────────────────────────────────────────────

    def _get_window_title(self, app: str) -> str:
        if not app:
            return ""
        script = (
            f'tell application "System Events"\n'
            f'  tell process "{app}"\n'
            f'    if exists (window 1) then\n'
            f'      return name of window 1\n'
            f'    end if\n'
            f'  end tell\n'
            f'end tell\n'
            f'return ""\n'
        )
        return _run_script(script)

    def _enrich_vscode(self, ctx: dict, title: str) -> None:
        # Window title format: "filename — folder — Visual Studio Code"
        # or "● filename — folder — Visual Studio Code" (unsaved)
        title_clean = title.replace("●", "").strip()
        parts = [p.strip() for p in title_clean.split("—")]
        if parts:
            filename = parts[0].strip()
            if filename and filename.lower() not in ("visual studio code", ""):
                ctx["file"] = filename
                ext = Path(filename).suffix.lower()
                ctx["lang"] = _LANG_MAP.get(ext, "")

                # Try to resolve full path via VS Code recent files or workspace
                # Best-effort: search common project roots
                file_path = self._find_file_path(filename)
                if file_path:
                    ctx["file_path"] = str(file_path)

    def _find_file_path(self, filename: str) -> Optional[Path]:
        """Best-effort search for a file by name, limited to 2 directory levels."""
        search_roots = [
            Path.home() / "Desktop",
            Path.home() / "Documents",
            Path.home() / "Projects",
            Path.home() / "dev",
            Path(os.environ.get("ATLAS_ROOT", ".")),
        ]
        for root in search_roots:
            if not root.exists():
                continue
            try:
                # Direct child first (depth 0)
                candidate = root / filename
                if candidate.is_file():
                    return candidate
                # One level deep — avoids scanning thousands of files on a cluttered Desktop
                for subdir in root.iterdir():
                    if subdir.is_dir():
                        candidate = subdir / filename
                        if candidate.is_file():
                            return candidate
            except (PermissionError, OSError):
                continue
        return None

    def _enrich_chrome(self, ctx: dict, app: str) -> None:
        url_script = (
            f'tell application "{app}"\n'
            f'  get URL of active tab of front window\n'
            f'end tell\n'
        )
        title_script = (
            f'tell application "{app}"\n'
            f'  get title of active tab of front window\n'
            f'end tell\n'
        )
        ctx["url"]        = _run_script(url_script)
        ctx["page_title"] = _run_script(title_script)

    def _enrich_safari(self, ctx: dict) -> None:
        ctx["url"] = _run_script(
            'tell application "Safari" to get URL of current tab of front window'
        )
        ctx["page_title"] = _run_script(
            'tell application "Safari" to get name of current tab of front window'
        )

    def _enrich_arc(self, ctx: dict) -> None:
        # Arc exposes URL similarly to Chrome
        ctx["url"] = _run_script(
            'tell application "Arc" to get URL of active tab of front window'
        )

    def _enrich_terminal(self, ctx: dict, app: str) -> None:
        if app == "Terminal":
            cwd = _run_script(
                'tell application "Terminal" to '
                'get custom title of selected tab of front window'
            )
            if not cwd:
                # Fallback: parse from title
                cwd = self._context.get("title", "")
        elif app in {"iTerm2", "iTerm"}:
            cwd = _run_script(
                'tell application "iTerm2" to '
                'get variable named "session.path" of current session of current tab of current window'
            )
        else:
            cwd = ""
        if cwd:
            ctx["cwd"] = cwd

    # ── Context-aware response builders ──────────────────────────────────────

    def _build_help_response(self, name: str) -> Optional[str]:
        ctx = self._context
        app = ctx.get("app", "")

        # VS Code: read file and ask AI
        if ctx.get("file_path") or ctx.get("file"):
            content = self.get_file_content()
            if content:
                file = ctx.get("file", "the current file")
                lang = ctx.get("lang", "")
                return (
                    f"I can see you're working on {file}"
                    + (f" in {lang}" if lang else "")
                    + ". Reading the file now. Ask me your specific question and I'll use the file as context."
                )
            return f"I can see {ctx.get('file', 'a file')} is open in VS Code, {name}, but I couldn't read it. What would you like help with?"

        # Browser: use web module
        url = ctx.get("url", "")
        if url and url.startswith("http"):
            return f"I see you're on {ctx.get('page_title', url[:60])}. Ask me your question and I'll use this page as context."

        # Generic
        return f"I can see you're in {app}, {name}. Ask me your specific question and I'll help."

    # ── Persistence ───────────────────────────────────────────────────────────

    def _log_context(self, ctx: dict) -> None:
        try:
            log_entry = {k: v for k, v in ctx.items() if k != "file_path"}
            entries: list = []
            if self._log_path.exists():
                try:
                    entries = json.loads(self._log_path.read_text(encoding="utf-8"))
                    if not isinstance(entries, list):
                        entries = []
                except Exception:
                    entries = []
            entries.append(log_entry)
            if len(entries) > 500:
                entries = entries[-500:]
            self._log_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except Exception as exc:
            log.debug("Context log error: %s", exc)

    def _save_context_memory(self) -> None:
        """Append current context to a human-readable memory note."""
        ctx = self._context
        note = (
            f"{ctx.get('ts', datetime.now().isoformat()[:19])} — "
            f"{ctx.get('app', '')} | "
            f"file={ctx.get('file', '')} | "
            f"url={ctx.get('url', '')} | "
            f"cwd={ctx.get('cwd', '')}"
        )
        try:
            mem_path = self._log_dir / "saved_contexts.txt"
            with open(mem_path, "a", encoding="utf-8") as f:
                f.write(note + "\n")
        except Exception as exc:
            log.debug("Context memory save error: %s", exc)
