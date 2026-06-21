#!/usr/bin/env python3
"""
ATLAS — AI Assistant
Entry point for the standalone desktop application.

Launch:
    python main.py            # normal mode (voice active)
    python main.py --demo     # cycle through states without microphone
    python main.py --no-voice # UI only, no audio
"""

import sys
import os
import logging
from pathlib import Path


def _load_zshenv():
    """Load exports from ~/.zshenv so GUI app launches have the same env as the shell."""
    zshenv = Path.home() / ".zshenv"
    if not zshenv.exists():
        return
    try:
        for line in zshenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


_load_zshenv()

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("ATLAS_ROOT", str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

from ui.main_window import ATLASMainWindow, _make_icon
from voice import VoiceModule
from core import ATLASCore
from web import WebModule
from control import ControlModule, ConfirmationDialog
from self_editor import SelfEditor
from brain import Brain
from self_improve import SelfImproveEngine
from widgets import DashboardWindow
from spotify import SpotifyModule
from feed import FeedManager
from context import ContextManager
from shazam import ShazamModule
from imagegen import ImageGenModule
from obsidian import ObsidianModule
from vision import VisionModule
from overlay import OverlayWindow, handle_overlay_command
from ambient import AmbientModule
from skills.loader import SkillsLoader
from digest import DigestModule
from sounds import SoundEngine

import yaml


def _load_config() -> dict:
    path = PROJECT_ROOT / "config.yaml"
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def main() -> None:
    args = set(sys.argv[1:])
    demo     = "--demo"     in args
    no_voice = "--no-voice" in args

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("ATLAS")
    app.setApplicationDisplayName("ATLAS")
    app.setOrganizationName("ATLAS")
    app.setWindowIcon(_make_icon(64))
    app.setQuitOnLastWindowClosed(False)

    config = _load_config()
    window = ATLASMainWindow()
    window.show()

    if demo:
        _run_demo(window)
    elif not no_voice:
        _start_voice(config, window)
    else:
        window.set_state("idle")

    sys.exit(app.exec())


def _start_voice(config: dict, window: ATLASMainWindow) -> None:
    """
    Initialise ATLASCore + VoiceModule and wire them to the window.
    Runs after a short delay so the UI is fully painted first.
    """
    # Core agent (Groq + Gemini)
    core = ATLASCore(config)

    # Web module (DuckDuckGo + BeautifulSoup)
    web = WebModule(config)
    core.set_web_module(web)                # gives core live search context

    # Control module (mouse/keyboard/windows/shell)
    # Shared ConfirmationDialog — used by both control and self-editor
    ctrl    = ControlModule(config)
    confirm = ConfirmationDialog()
    ctrl.set_confirm_cb(confirm.ask)
    core.set_control_module(ctrl)
    window.set_module_active("CTRL", True)

    # Self-editor (backup / patch / test / rollback)
    editor = SelfEditor(config, PROJECT_ROOT, confirm_cb=confirm.ask)
    core.set_self_editor(editor)
    window.set_module_active("EDIT", True)

    # Brain — primary AI reasoning layer via OpenRouter (wraps ATLASCore)
    brain = Brain(config)
    brain.set_core(core)

    # Self-improvement engine — wired into brain for voice commands
    engine = SelfImproveEngine(config, brain, PROJECT_ROOT)
    brain.set_self_improve(engine)

    # Spotify module — search catalog + control playback
    spotify = SpotifyModule(config)
    brain.set_spotify(spotify)

    # Voice pipeline (callback set below, after all wrappers are built)
    vm = VoiceModule(config, window)

    # Mute toggle from tray
    if hasattr(window, "_mute_act"):
        window._mute_act.triggered.connect(vm.set_muted)

    # Dashboard widget panel
    dash = DashboardWindow(config)
    _orig_meta = brain._handle_meta

    def _meta_with_dash(text: str):
        resp = dash.handle(text)
        if resp is not None:
            return resp
        return _orig_meta(text)

    brain._handle_meta = _meta_with_dash

    if config.get("dashboard", {}).get("visible_on_startup", True):
        QTimer.singleShot(1200, dash.show)

    # Shazam module — song identification
    shazam_mod = ShazamModule(config, state_cb=window.set_state, brain=brain)
    shazam_mod.set_speak_callback(vm.speak)

    _orig_meta_base = brain._handle_meta

    def _meta_with_shazam(text: str):
        resp = shazam_mod.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_base(text)

    brain._handle_meta = _meta_with_shazam

    # Context manager — active window detection
    ctx_mgr = ContextManager(config)
    ctx_mgr.set_web_module(web)

    # Connect context updates to HUD and feed panel.
    # Use QueuedConnection so updates are delivered on the main thread
    # (context_updated is emitted from the polling background thread).
    def _on_context(ctx: dict):
        window.hud.set_context(ctx.get("app", ""), ctx.get("file", ""))
        label = (f"{ctx['app']} | {ctx['file']}" if ctx.get("file") else ctx.get("app", ""))
        if label:
            window.feed_panel.add_feed_item("system", label)

    ctx_mgr.context_updated.connect(_on_context, Qt.ConnectionType.QueuedConnection)

    # Wire context voice commands into meta chain
    _orig_meta_ctx_base = brain._handle_meta

    def _meta_with_ctx(text: str):
        resp = ctx_mgr.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_ctx_base(text)

    brain._handle_meta = _meta_with_ctx

    # Feed manager — drives the TARS-style side panel
    feed_mgr = FeedManager(config)
    window.feed_panel.set_feed_manager(feed_mgr)

    # Feed voice commands — chained BEFORE handle wrapper so meta short-circuits correctly
    _orig_meta_dash = brain._handle_meta

    def _meta_with_feed(text: str):
        resp = _handle_feed_command(text, window, feed_mgr)
        if resp is not None:
            return resp
        return _orig_meta_dash(text)

    brain._handle_meta = _meta_with_feed

    # Wrap brain.handle to: (1) inject context prefix, (2) mirror into feed
    # All UI calls use QTimer.singleShot(0) to post to the main thread —
    # this callback runs on the VoiceWorker thread, not the main thread.
    _orig_brain_handle = brain.handle

    def _handle_with_feed(text: str) -> str:
        _preview = f"You: {text[:120]}"
        QTimer.singleShot(0, lambda: window.feed_panel.add_feed_item("voice", _preview))
        response = _orig_brain_handle(text)
        if response:
            _r = response
            QTimer.singleShot(0, lambda: window.feed_panel.push_atlas_response(_r))
        return response

    brain.handle = _handle_with_feed

    # Register the final wrapped callback with the voice module
    vm.set_response_callback(_handle_with_feed)

    # Show feed panel by default (side-by-side layout)
    QTimer.singleShot(1000, window.show_feed)

    # Start background services
    QTimer.singleShot(1500, feed_mgr.start)
    QTimer.singleShot(2000, ctx_mgr.start)

    # Keep references
    window._feed_manager   = feed_mgr
    window._ctx_manager    = ctx_mgr
    window._shazam         = shazam_mod

    # Give the window 800 ms to finish painting before mic capture starts
    QTimer.singleShot(800, vm.start)

    # Image generation module — no download triggered on init
    imagegen_mod = ImageGenModule(
        config,
        state_cb=window.set_state,
        speak_cb=vm.speak,
        show_image_cb=window.image_panel.show_image,
        brain=brain,
    )
    window.image_panel.set_save_callback(imagegen_mod._save_to_desktop)
    window.image_panel.set_preview_callback(imagegen_mod._open_in_preview)

    _orig_meta_imagegen_base = brain._handle_meta

    def _meta_with_imagegen(text: str):
        resp = imagegen_mod.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_imagegen_base(text)

    brain._handle_meta = _meta_with_imagegen
    window._imagegen = imagegen_mod

    # Obsidian module — full vault integration, no extra packages
    obsidian_mod = ObsidianModule(config, speak_cb=vm.speak, brain=brain)
    feed_mgr.set_obsidian_module(obsidian_mod)

    _orig_meta_obs_base = brain._handle_meta

    def _meta_with_obsidian(text: str):
        resp = obsidian_mod.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_obs_base(text)

    brain._handle_meta = _meta_with_obsidian

    # Track last ATLAS response so "save that to Obsidian" / "note this" work
    _orig_handle_with_feed = brain.handle

    def _handle_with_feed_obs(text: str) -> str:
        response = _orig_handle_with_feed(text)
        if response:
            obsidian_mod.set_last_response(response)
        return response

    brain.handle = _handle_with_feed_obs
    vm.set_response_callback(_handle_with_feed_obs)

    window._obsidian = obsidian_mod

    # ── Sound engine ──────────────────────────────────────────────────────────
    sounds = SoundEngine(config)
    sounds.start_ambient()

    # Sound signals wired in _post_calibration_setup (worker doesn't exist yet)

    _orig_meta_sounds = brain._handle_meta

    def _meta_with_sounds(text: str):
        resp = sounds.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_sounds(text)

    brain._handle_meta = _meta_with_sounds

    # ── Chrome control ────────────────────────────────────────────────────────
    chrome = None
    try:
        from chrome_control import ChromeControl as _ChromeCtrl
        chrome = _ChromeCtrl(config, speak_cb=vm.speak, brain=brain)
        core.set_chrome_control(chrome)

        def _bg_connect_chrome():
            import threading
            def _run():
                ok = chrome.connect()
                log.info("ChromeControl: %s", "Playwright connected" if ok else "using AppleScript fallback")
            threading.Thread(target=_run, daemon=True, name="atlas-chrome-init").start()

        QTimer.singleShot(4000, _bg_connect_chrome)
    except Exception as _chrome_err:
        log.warning("ChromeControl unavailable: %s", _chrome_err)

    # ── Screen vision ─────────────────────────────────────────────────────────
    vision = VisionModule(config, brain=brain)
    vision.set_speak_callback(vm.speak)
    vision.set_state_callback(window.set_state)

    _orig_meta_vision = brain._handle_meta

    def _meta_with_vision(text: str):
        resp = vision.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_vision(text)

    brain._handle_meta = _meta_with_vision

    # ── Cursor overlay ────────────────────────────────────────────────────────
    overlay = OverlayWindow(config)

    # Overlay signals wired in _post_calibration_setup (worker doesn't exist yet)

    _orig_brain_handle_overlay = brain.handle

    def _handle_with_overlay(text: str) -> str:
        overlay.clear_bubble()
        response = _orig_brain_handle_overlay(text)
        if response:
            _r = response
            QTimer.singleShot(0, lambda: overlay.show_response(_r))
        return response

    brain.handle = _handle_with_overlay
    vm.set_response_callback(_handle_with_overlay)

    _orig_meta_overlay = brain._handle_meta

    def _meta_with_overlay(text: str):
        resp = handle_overlay_command(text, overlay)
        if resp is not None:
            return resp
        return _orig_meta_overlay(text)

    brain._handle_meta = _meta_with_overlay

    # ── Skills system ─────────────────────────────────────────────────────────
    skills_context = {
        "brain":        brain,
        "config":       config,
        "vision":       vision,
        "speak_cb":     vm.speak,
        "voice_module": vm,
    }
    skills = SkillsLoader(config, context=skills_context)
    skills.start()

    _orig_meta_skills = brain._handle_meta

    def _meta_with_skills(text: str):
        resp = skills.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_skills(text)

    brain._handle_meta = _meta_with_skills

    # ── Morning digest ────────────────────────────────────────────────────────
    digest = DigestModule(config, brain=brain)
    digest.set_speak_callback(vm.speak)
    digest.set_feed_callback(window.feed_panel.add_feed_item)
    QTimer.singleShot(3000, digest.start)

    _orig_meta_digest = brain._handle_meta

    def _meta_with_digest(text: str):
        resp = digest.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_digest(text)

    brain._handle_meta = _meta_with_digest

    # ── Ambient always-on module ──────────────────────────────────────────────
    ambient = AmbientModule(
        config,
        brain=brain,
        voice_module=vm,
        vision=vision,
        state_cb=window.set_state,
    )

    # Wire context manager updates → ambient context memory
    ctx_mgr.context_updated.connect(
        lambda ctx: ambient.update_context(ctx.get("app", ""), ctx.get("file", "")),
        Qt.ConnectionType.QueuedConnection
    )

    _orig_meta_ambient = brain._handle_meta

    def _meta_with_ambient(text: str):
        resp = ambient.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_ambient(text)

    brain._handle_meta = _meta_with_ambient

    # Startup sequence:
    # • vm.start() fires at 800ms → mic calibration runs 800ms–2300ms
    # • Pre-warm TTS at 1000ms (safe — no audio output)
    # • Ambient push-to-talk starts at 2500ms
    # • At 3500ms calibration is done: wire worker signals then play chime
    def _post_calibration_setup():
        if vm._worker:
            vm._worker.wake_word_detected.connect(lambda: sounds.play("WAKE"))
            vm._worker.speaking_started.connect(lambda: sounds.play("RESPONSE_READY"))
            vm._worker.wake_word_detected.connect(lambda: overlay.set_recording(True))
            vm._worker.speaking_done.connect(lambda: overlay.set_recording(False))
        sounds.play("STARTUP")

    QTimer.singleShot(1000, vm.pre_warm)
    QTimer.singleShot(2500, ambient.start)
    QTimer.singleShot(3500, _post_calibration_setup)

    # Keep references so they aren't GC'd
    window._core           = core
    window._web_module     = web
    window._control_module = ctrl
    window._confirm_dialog = confirm
    window._editor         = editor
    window._voice_module   = vm
    window._brain          = brain
    window._self_improve   = engine
    window._spotify        = spotify
    window._dashboard      = dash
    window._chrome         = chrome
    window._vision         = vision
    window._overlay        = overlay
    window._skills         = skills
    window._digest         = digest
    window._ambient        = ambient
    window._sounds         = sounds

    # Graceful shutdown
    app = QApplication.instance()
    if app:
        app.aboutToQuit.connect(vm.stop)
        app.aboutToQuit.connect(ctx_mgr.stop)
        app.aboutToQuit.connect(feed_mgr.stop)
        app.aboutToQuit.connect(vision.stop)
        app.aboutToQuit.connect(ambient.stop)
        app.aboutToQuit.connect(sounds.stop)
        app.aboutToQuit.connect(skills.stop)
        app.aboutToQuit.connect(digest.stop)
        if chrome is not None:
            app.aboutToQuit.connect(lambda: chrome._cmd_q.put(None))


def _handle_feed_command(text: str, window: ATLASMainWindow, feed_mgr: FeedManager):
    """
    Handle feed panel voice commands.
    Returns a response string if handled, None otherwise.
    """
    lower = text.lower().strip()
    fp    = window.feed_panel

    # ── Show / hide ──────────────────────────────────────────────────────────
    if any(p in lower for p in ("atlas show feed", "show the feed", "open feed")):
        window.show_feed()
        return "Feed panel open."

    if any(p in lower for p in ("atlas hide feed", "hide the feed", "close feed")):
        window.hide_feed()
        return "Feed panel hidden."

    if any(p in lower for p in ("atlas toggle feed", "toggle feed")):
        window.toggle_feed()
        return "Feed panel toggled."

    # ── Side ─────────────────────────────────────────────────────────────────
    if any(p in lower for p in ("atlas move feed to left", "feed on the left", "move feed left")):
        feed_mgr.set_feed_side("left")
        return "Feed will appear on the left next launch."

    if any(p in lower for p in ("atlas move feed to right", "feed on the right", "move feed right")):
        feed_mgr.set_feed_side("right")
        return "Feed will appear on the right next launch."

    # ── Modes ────────────────────────────────────────────────────────────────
    if any(p in lower for p in ("atlas focus mode", "feed focus mode")):
        fp.set_feed_mode("focus")
        return "Focus mode — showing coding and system info only."

    if any(p in lower for p in ("atlas news mode", "feed news mode")):
        fp.set_feed_mode("news")
        return "News mode — showing news and weather."

    if any(p in lower for p in ("atlas full feed", "full feed mode", "atlas show everything")):
        fp.set_feed_mode("full")
        return "Full feed — showing everything."

    if any(p in lower for p in ("atlas minimal feed", "minimal feed")):
        fp.set_feed_mode("minimal")
        return "Minimal feed — clock and ATLAS responses only."

    if any(p in lower for p in ("atlas briefing mode", "briefing mode")):
        fp.set_feed_mode("full")
        window.show_feed()
        return "Briefing mode active. Feed panel open."

    # ── Utilities ─────────────────────────────────────────────────────────────
    if any(p in lower for p in ("atlas clear feed", "clear the feed", "clear feed")):
        fp.clear_feed()
        return "Feed cleared."

    if any(p in lower for p in ("atlas pin that", "pin that", "pin this")):
        fp.pin_last_item()
        return "Item pinned."

    # ── Widget visibility ─────────────────────────────────────────────────────
    _WIDGET_NAMES = {
        "weather": "weather", "clock": "clock", "music": "music",
        "spotify": "music",   "system": "system", "stats": "system",
        "context": "context", "reminders": "reminders",
    }
    for word, widget in _WIDGET_NAMES.items():
        if f"remove {word} from feed" in lower or f"hide {word}" in lower:
            fp.hide_widget(widget)
            return f"{word.capitalize()} widget hidden."
        if f"add {word} to feed" in lower or f"show {word}" in lower:
            fp.show_widget(widget)
            return f"{word.capitalize()} widget visible."

    # ── Reminders ─────────────────────────────────────────────────────────────
    if lower.startswith("atlas add reminder ") or lower.startswith("add reminder "):
        reminder_text = lower.split("reminder ", 1)[-1].strip()
        if reminder_text:
            feed_mgr.add_reminder(reminder_text)
            return f"Reminder added: {reminder_text}."

    if any(p in lower for p in ("atlas mark that done", "mark reminder done",
                                 "complete reminder", "done with reminder")):
        feed_mgr.complete_reminder()
        return "Reminder marked complete."

    return None


def _run_demo(window: ATLASMainWindow) -> None:
    """
    Cycle through all four states with sample transcript entries.
    Activated with:  python main.py --demo
    """
    steps = [
        (500,  lambda: window.set_state("idle")),
        (1800, lambda: (
            window.set_state("listening"),
            window.add_entry("Hey ATLAS, what's the weather like today?", is_atlas=False),
        )),
        (2100, lambda: window.set_amplitude(0.50)),
        (2400, lambda: window.set_amplitude(0.82)),
        (2700, lambda: window.set_amplitude(0.35)),
        (3000, lambda: (window.set_amplitude(0.0), window.set_state("thinking"))),
        (3500, lambda: (
            window.set_state("responding"),
            window.show_response(
                "It's currently 72°F and partly cloudy in your area. "
                "Skies will clear by afternoon — great conditions for a walk."
            ),
            window.set_module_active("WEB", True),
        )),
        (8500, lambda: (
            window.set_state("idle"),
            window.set_module_active("WEB", False),
        )),
        (10000, lambda: (
            window.set_state("listening"),
            window.add_entry("Open Spotify and play my morning playlist.", is_atlas=False),
        )),
        (10300, lambda: window.set_amplitude(0.65)),
        (10600, lambda: (window.set_amplitude(0.0), window.set_state("thinking"))),
        (11200, lambda: (
            window.set_state("responding"),
            window.show_response("Opening Spotify and starting your Morning Vibes playlist now."),
            window.set_module_active("CTRL", True),
        )),
        (14000, lambda: (
            window.set_state("idle"),
            window.set_module_active("CTRL", False),
        )),
    ]

    for delay, fn in steps:
        QTimer.singleShot(delay, fn)


if __name__ == "__main__":
    main()
