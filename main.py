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

    # Voice pipeline
    vm = VoiceModule(config, window)
    vm.set_response_callback(brain.handle)  # brain routes: Claude / MLX / Groq

    # Mute toggle from tray
    if hasattr(window, "_mute_act"):
        window._mute_act.triggered.connect(vm.set_muted)

    # Dashboard widget panel
    dash = DashboardWindow(config)
    # Wire dashboard voice commands through brain's meta handler
    _orig_meta = brain._handle_meta

    def _meta_with_dash(text: str):
        resp = dash.handle(text)
        if resp is not None:
            return resp
        return _orig_meta(text)

    brain._handle_meta = _meta_with_dash

    if config.get("dashboard", {}).get("visible_on_startup", True):
        QTimer.singleShot(1200, dash.show)

    # Give the window 800 ms to finish painting before mic capture starts
    QTimer.singleShot(800, vm.start)

    # Keep references so they aren't GC'd
    window._core           = core
    window._web_module     = web
    window._control_module = ctrl
    window._confirm_dialog = confirm
    window._editor         = editor
    window._voice_module   = vm
    window._brain          = brain
    window._self_improve   = engine
    window._dashboard      = dash

    # Graceful shutdown
    app = QApplication.instance()
    if app:
        app.aboutToQuit.connect(vm.stop)


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
