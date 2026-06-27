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

log = logging.getLogger(__name__)


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
logging.getLogger("primp").setLevel(logging.WARNING)          # suppress 200 OK noise
logging.getLogger("ddgs.engines.yahoo_news").setLevel(logging.ERROR)  # suppress IndexError noise

# macOS Sequoia: dispatch_assert_queue_fail raises EXC_BREAKPOINT/SIGTRAP from TSM bg threads.
# Ignore it — log.warning() is unsafe inside a signal handler (reentrant), so just no-op.
import signal as _signal
_signal.signal(_signal.SIGTRAP, _signal.SIG_IGN)
os.environ.setdefault("QT_IM_MODULE", "")              # suppress TSM macOS error

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
from camera import CameraModule
from overlay import OverlayWindow, handle_overlay_command
from ambient import AmbientModule
from skills.loader import SkillsLoader
from skills.hermes import HermesSkillsModule
from digest import DigestModule
from sounds import SoundEngine
from playbook import PlaybookModule
from memory import MemoryModule
from vault_brain import VaultBrain
from soul import SoulModule
from trajectory_compressor import ATLASTrajectoryCompressor
from session_search import SessionSearch
from scheduler import ATLASScheduler
from learning_loop import LearningLoop
from context_files import ContextFilesModule
from honcho import HonchoModule
from pipeline import ATLASVoicePipeline, EchoGate
from smart_card import SmartCardManager
from planner import ATLASPlanner
from code_agent import ATLASCodeAgent
from offline import ATLASOfflineMode
from context7 import ATLASContext7
from recorder import ATLASRecorder
from coach import ATLASCoach
from debate import ATLASDebate
from tutor import ATLASTutor
from research import ATLASResearch
from hologram import ATLASHologram

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

    # ── VaultBrain — Obsidian as single source of truth ───────────────────────
    vault_brain = None
    soul_mod    = None
    try:
        obs_cfg    = config.get("obsidian", {})
        vault_path = obs_cfg.get("vault_path", "")
        if vault_path:
            atlas_folder = config.get("obsidian_atlas_folder", "ATLAS")
            vault_brain  = VaultBrain(Path(vault_path).expanduser(), atlas_folder)
            vault_brain.ensure_daily_note()  # create today's daily note if missing

            # SoulModule — load personality from ATLAS/soul.md
            soul_mod = SoulModule(vault_brain)
            soul_mod.inject(brain)

            # Watchdog: speak when user edits vault files from Obsidian
            if config.get("obsidian_watch_for_changes", True):
                def _on_vault_change(filepath: str):
                    soul_mod.on_vault_change(filepath)
                    # defined after hermes_skills but captured by closure at call time
                    _hs = window.__dict__.get("_hermes_skills")
                    if _hs is not None:
                        _hs.reload_skill(filepath)
                    fname = Path(filepath).name.replace(".md", "").replace("-", " ")
                    msg   = f"Boss, I noticed you updated {fname} in the vault."
                    QTimer.singleShot(0, lambda: vm.speak(msg))

                vault_brain.start_watcher(_on_vault_change)
                log.info("Vault watchdog active.")

            log.info("VaultBrain: ready at %s", vault_path)
    except Exception as _vb_err:
        log.warning("VaultBrain unavailable (%s) — memory using in-memory only.", _vb_err)

    # ── ACE Playbook ──────────────────────────────────────────────────────────
    playbook = PlaybookModule(
        config,
        brain=brain,
        feed_cb=window.feed_panel.add_feed_item,
        vault_brain=vault_brain,
    )
    brain.set_playbook(playbook)

    _orig_meta_playbook = brain._handle_meta

    def _meta_with_playbook(text: str):
        resp = playbook.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_playbook(text)

    brain._handle_meta = _meta_with_playbook
    window._playbook = playbook

    # ── HERMES Memory ─────────────────────────────────────────────────────────
    memory_mod = MemoryModule(config, brain=brain, obsidian=obsidian_mod,
                              vault_brain=vault_brain)
    brain.set_memory(memory_mod)

    _orig_meta_memory = brain._handle_meta

    def _meta_with_memory(text: str):
        resp = memory_mod.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_memory(text)

    brain._handle_meta = _meta_with_memory
    window._memory = memory_mod

    # ── Soul voice commands ───────────────────────────────────────────────────
    _orig_meta_soul = brain._handle_meta

    def _meta_with_soul(text: str):
        if soul_mod is not None:
            resp = soul_mod.handle(text)
            if resp is not None:
                return resp
        return _orig_meta_soul(text)

    brain._handle_meta = _meta_with_soul
    window._soul = soul_mod

    # ── Hermes vault skills ───────────────────────────────────────────────────
    hermes_skills = HermesSkillsModule(vault_brain, brain)

    _orig_meta_hermes = brain._handle_meta

    def _meta_with_hermes(text: str):
        resp = hermes_skills.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_hermes(text)

    brain._handle_meta = _meta_with_hermes
    window._hermes_skills = hermes_skills

    # ── Trajectory compressor ─────────────────────────────────────────────────
    compressor = ATLASTrajectoryCompressor(brain, vault_brain)

    _orig_meta_compress = brain._handle_meta

    def _meta_with_compress(text: str):
        resp = compressor.handle(text)
        if resp is not None:
            return resp
        result = _orig_meta_compress(text)
        # Auto-compress after each turn if threshold hit
        compressor.maybe_compress()
        return result

    brain._handle_meta = _meta_with_compress
    window._compressor = compressor

    # ── FTS5 session search ───────────────────────────────────────────────────
    _search_db = Path(os.environ.get("ATLAS_ROOT", ".")) / "memory" / "atlas_search.db"
    session_search = SessionSearch(_search_db, brain)

    # Bulk-index vault contents on startup (background)
    if vault_brain is not None:
        import threading as _threading
        def _bulk_index():
            session_search.bulk_index_from_vault(vault_brain)
        _threading.Thread(target=_bulk_index, daemon=True, name="atlas-search-index").start()

    # Wire into brain.handle to auto-index every turn
    _orig_handle_search = brain.handle

    def _handle_with_search(text: str) -> str:
        response = _orig_handle_search(text)
        session_search.index_message("user", text)
        if response:
            session_search.index_message("assistant", response)
        return response

    brain.handle = _handle_with_search
    vm.set_response_callback(_handle_with_search)

    _orig_meta_search = brain._handle_meta

    def _meta_with_search(text: str):
        resp = session_search.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_search(text)

    brain._handle_meta = _meta_with_search
    window._session_search = session_search

    # ── Cron scheduler ────────────────────────────────────────────────────────
    atlas_scheduler = ATLASScheduler(
        config,
        brain        = brain,
        vault_brain  = vault_brain,
        speak_cb     = vm.speak,
        memory_module= memory_mod,
    )
    QTimer.singleShot(6000, atlas_scheduler.start)   # start after modules are wired

    _orig_meta_sched = brain._handle_meta

    def _meta_with_scheduler(text: str):
        resp = atlas_scheduler.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_sched(text)

    brain._handle_meta = _meta_with_scheduler
    window._scheduler  = atlas_scheduler

    # ── Learning loop ─────────────────────────────────────────────────────────
    learning_loop = LearningLoop(
        brain         = brain,
        memory_module = memory_mod,
        vault_brain   = vault_brain,
        hermes_skills = hermes_skills,
    )

    # Wrap brain.handle to trigger evaluation after each response
    _orig_handle_ll = brain.handle

    def _handle_with_learning(text: str) -> str:
        response = _orig_handle_ll(text)
        if response:
            learning_loop.evaluate(text, response)
        return response

    brain.handle = _handle_with_learning
    vm.set_response_callback(_handle_with_learning)

    _orig_meta_ll = brain._handle_meta

    def _meta_with_ll(text: str):
        resp = learning_loop.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_ll(text)

    brain._handle_meta = _meta_with_ll
    window._learning_loop = learning_loop

    # ── Context files ─────────────────────────────────────────────────────────
    ctx_files = ContextFilesModule(brain=brain, speak_cb=vm.speak)
    ctx_files.inject(brain)
    QTimer.singleShot(3000, ctx_files.start)

    # Wire CWD updates from context_manager into ctx_files
    def _on_ctx_with_cwd(ctx: dict):
        cwd = ctx.get("file", "")
        if cwd:
            import os as _os
            project_dir = _os.path.dirname(cwd)
            if project_dir:
                ctx_files.update_cwd(project_dir)

    ctx_mgr.context_updated.connect(_on_ctx_with_cwd, Qt.ConnectionType.QueuedConnection)

    _orig_meta_ctxf = brain._handle_meta

    def _meta_with_ctxfiles(text: str):
        resp = ctx_files.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_ctxf(text)

    brain._handle_meta = _meta_with_ctxfiles
    window._ctx_files  = ctx_files

    # ── Honcho user modeling ──────────────────────────────────────────────────
    honcho = HonchoModule(vault_brain=vault_brain, brain=brain, config=config)

    _orig_meta_honcho = brain._handle_meta

    def _meta_with_honcho(text: str):
        resp = honcho.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_honcho(text)

    brain._handle_meta = _meta_with_honcho
    window._honcho     = honcho

    # Wire Honcho into shutdown — fires after memory_mod.on_shutdown() saves the episode
    if app := QApplication.instance():
        def _honcho_on_shutdown():
            honcho.on_session_end()
        app.aboutToQuit.connect(_honcho_on_shutdown)

    # ── Market research ───────────────────────────────────────────────────────
    market = None
    try:
        from market import MarketModule
        market = MarketModule(config, speak_cb=vm.speak, brain=brain, obsidian=obsidian_mod)

        _orig_meta_market_base = brain._handle_meta

        def _meta_with_market(text: str):
            resp = market.handle(text)
            if resp is not None:
                return resp
            return _orig_meta_market_base(text)

        brain._handle_meta = _meta_with_market
        QTimer.singleShot(5000, market.start)
    except Exception as _mkt_err:
        log.warning("MarketModule unavailable: %s", _mkt_err)

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

    # ── Webcam camera module ──────────────────────────────────────────────────
    camera = CameraModule(config, brain=brain)
    camera.set_speak_callback(vm.speak)
    camera.set_state_callback(window.set_state)
    # UI dot update: call JS setCameraState(on)
    camera.set_ui_camera_callback(
        lambda on: QTimer.singleShot(0, lambda: window._js(f"setCameraState({'true' if on else 'false'})"))
    )
    # Attach to window so UI button can toggle via window._camera_module
    window._camera_module = camera

    _orig_meta_camera = brain._handle_meta

    def _meta_with_camera(text: str):
        if camera.handles(text):
            return camera.handle(text)
        return _orig_meta_camera(text)

    brain._handle_meta = _meta_with_camera

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
    QTimer.singleShot(5000, ambient.start)      # must start AFTER voice calibration (~2.3s)
    QTimer.singleShot(3500, _post_calibration_setup)

    # ── Session greeting (after UI is ready) ──────────────────────────────────
    if config.get("session_greeting_enabled", True):
        def _speak_startup_greeting():
            try:
                greeting = memory_mod.generate_greeting()
                if greeting:
                    vm.speak(greeting)
            except Exception:
                pass
        QTimer.singleShot(4500, _speak_startup_greeting)

    # ── Voice pipeline (Pipecat-inspired) ─────────────────────────────────────
    # Created first so vm.speak wrapper is in place before other new modules
    voice_pipeline = ATLASVoicePipeline(config, speak_cb=vm.speak)
    echo_gate      = EchoGate(gate_duration_secs=2.0)

    # Wrap vm.speak: notify pipeline of TTS start/stop for interruption tracking
    _orig_speak = vm.speak

    def _speak_with_pipeline(text: str, **kwargs):
        voice_pipeline.notify_tts_started()
        echo_gate.on_tts_started()
        try:
            _orig_speak(text, **kwargs)
        finally:
            voice_pipeline.notify_tts_done()
            echo_gate.on_tts_done()

    vm.speak = _speak_with_pipeline

    # Wire interrupt callback to stop TTS mid-sentence
    def _interrupt_tts():
        try:
            if hasattr(vm, "_tts") and vm._tts:
                if hasattr(vm._tts, "stop_speaking"):
                    vm._tts.stop_speaking()
        except Exception as exc:
            log.debug("TTS interrupt error: %s", exc)

    voice_pipeline.set_interrupt_callback(_interrupt_tts)

    # Wire VAD speech-start → pipeline (delayed until worker exists at ~3.5s)
    def _post_calibration_setup_pipeline():
        if vm._worker:
            try:
                vm._worker.wake_word_detected.connect(
                    voice_pipeline.notify_user_speech_detected)
            except Exception:
                pass

    QTimer.singleShot(3600, _post_calibration_setup_pipeline)
    voice_pipeline.start()

    _orig_meta_pipeline = brain._handle_meta

    def _meta_with_pipeline(text: str):
        resp = voice_pipeline.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_pipeline(text)

    brain._handle_meta = _meta_with_pipeline
    window._voice_pipeline = voice_pipeline
    window._echo_gate      = echo_gate

    # ── Offline mode monitor (AgenticSeek-inspired) ──────────────────────────
    # vm.speak is now the pipeline-wrapped version — offline announcements are tracked
    offline_mode = ATLASOfflineMode(config, brain=brain, speak_cb=vm.speak)
    offline_mode.start()

    _orig_meta_offline = brain._handle_meta

    def _meta_with_offline(text: str):
        resp = offline_mode.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_offline(text)

    brain._handle_meta = _meta_with_offline

    # ── Context7 live documentation ──────────────────────────────────────────
    context7 = ATLASContext7(config, offline_mode=offline_mode)
    context7.start()

    _orig_meta_ctx7 = brain._handle_meta

    def _meta_with_ctx7(text: str):
        resp = context7.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_ctx7(text)

    brain._handle_meta = _meta_with_ctx7
    window._context7 = context7

    # Context7 doc injector for coding requests
    def _inject_docs_into_code(task: str) -> str:
        libs = context7.detect_libraries(task)
        if libs and offline_mode.context7_fetch_available:
            return context7.inject_into_prompt(task, *libs)
        return task

    # ── Code agent (Smolagents-inspired) ─────────────────────────────────────
    code_agent_enabled = config.get("code_agent_enabled", True)
    code_agent = None
    if code_agent_enabled:
        code_agent = ATLASCodeAgent(
            brain=brain, config=config,
            vault_brain=vault_brain, speak_cb=vm.speak,   # pipeline-wrapped speak
        )

        _orig_meta_code = brain._handle_meta

        def _meta_with_code(text: str):
            resp = code_agent.handle(text)
            if resp is not None:
                return resp
            return _orig_meta_code(text)

        brain._handle_meta = _meta_with_code

        _orig_handle_code = brain.handle

        def _handle_with_code(text: str) -> str:
            if code_agent.is_code_request(text):
                enriched = _inject_docs_into_code(text)
                return code_agent.process_request(enriched)
            return _orig_handle_code(text)

        brain.handle = _handle_with_code
        vm.set_response_callback(_handle_with_code)
        window._code_agent = code_agent

    # ── Lead agent planner (DeerFlow-inspired) ────────────────────────────────
    planner_enabled = config.get("planner_enabled", True)
    planner = None
    if planner_enabled:
        planner = ATLASPlanner(
            brain=brain, config=config,
            vault_brain=vault_brain, speak_cb=vm.speak,   # pipeline-wrapped speak
            web_module=web,
        )
        planner.inject(brain)   # wraps brain.handle to intercept MEDIUM/COMPLEX tasks

        _orig_meta_planner = brain._handle_meta

        def _meta_with_planner(text: str):
            resp = planner.handle(text)
            if resp is not None:
                return resp
            return _orig_meta_planner(text)

        brain._handle_meta = _meta_with_planner
        window._planner = planner

    # ── Smart Card (floating visualizer) ─────────────────────────────────────
    smart_card_mgr = SmartCardManager(
        config, speak_cb=vm.speak, vault_brain=vault_brain, brain=brain,
    )

    # Wire into brain.handle — show card after every response (simultaneous with speech)
    _orig_handle_sc = brain.handle

    def _handle_with_smart_card(text: str) -> str:
        response = _orig_handle_sc(text)
        if response and config.get('smart_card_enabled', True):
            log.info("SmartCard: calling on_response for %d-word reply.", len(response.split()))
            smart_card_mgr.on_response(text, response)   # signal bridge handles thread safety
        else:
            log.info("SmartCard: skipped — response=%r, enabled=%r",
                     bool(response), config.get('smart_card_enabled', True))
        return response

    brain.handle = _handle_with_smart_card
    vm.set_response_callback(_handle_with_smart_card)

    _orig_meta_sc = brain._handle_meta

    def _meta_with_smart_card(text: str):
        resp = smart_card_mgr.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_sc(text)

    brain._handle_meta = _meta_with_smart_card
    window._smart_card_mgr = smart_card_mgr

    # ── Screen Recorder ───────────────────────────────────────────────────────
    recorder = ATLASRecorder(
        config, speak_cb=vm.speak, brain=brain,
        vault_brain=vault_brain, smart_card_mgr=smart_card_mgr,
    )

    _orig_meta_recorder = brain._handle_meta

    def _meta_with_recorder(text: str):
        resp = recorder.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_recorder(text)

    brain._handle_meta = _meta_with_recorder
    window._recorder = recorder

    # ── Personal Coach ────────────────────────────────────────────────────────
    coach = ATLASCoach(
        config, speak_cb=vm.speak, brain=brain, vault_brain=vault_brain,
    )

    _orig_meta_coach = brain._handle_meta

    def _meta_with_coach(text: str):
        resp = coach.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_coach(text)

    brain._handle_meta = _meta_with_coach
    window._coach = coach

    # ── Debate Engine ─────────────────────────────────────────────────────────
    debate = ATLASDebate(
        config, speak_cb=vm.speak, brain=brain,
        vault_brain=vault_brain, smart_card_mgr=smart_card_mgr,
    )

    _orig_meta_debate = brain._handle_meta

    def _meta_with_debate(text: str):
        resp = debate.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_debate(text)

    brain._handle_meta = _meta_with_debate
    window._debate = debate

    # ── Socratic Tutor ────────────────────────────────────────────────────────
    tutor = ATLASTutor(
        config, speak_cb=vm.speak, brain=brain, vault_brain=vault_brain,
    )

    _orig_meta_tutor = brain._handle_meta

    def _meta_with_tutor(text: str):
        resp = tutor.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_tutor(text)

    brain._handle_meta = _meta_with_tutor
    window._tutor = tutor

    # ── Academic Research ─────────────────────────────────────────────────────
    research = ATLASResearch(
        config, speak_cb=vm.speak, brain=brain,
        vault_brain=vault_brain, smart_card_mgr=smart_card_mgr,
    )

    _orig_meta_research = brain._handle_meta

    def _meta_with_research(text: str):
        resp = research.handle(text)
        if resp is not None:
            return resp
        return _orig_meta_research(text)

    brain._handle_meta = _meta_with_research
    window._research = research

    # ── 3D Hologram System ────────────────────────────────────────────────────
    hologram = None
    if config.get("hologram_enabled", True):
        hologram = ATLASHologram(
            config,
            speak_cb=vm.speak,
            brain=brain,
            vault_brain=vault_brain,
            window=window,
            market_mod=market,
        )

        # Wire amplitude → hologram in real-time
        window.amplitude_changed.connect(hologram.push_amplitude)
        window.state_changed.connect(hologram.push_state)

        _orig_meta_hologram = brain._handle_meta

        def _meta_with_hologram(text: str):
            resp = hologram.handle(text)
            if resp is not None:
                return resp
            return _orig_meta_hologram(text)

        brain._handle_meta = _meta_with_hologram
        window._hologram = hologram

    # ── Agentic OS Layer ──────────────────────────────────────────────────────
    if config.get("agentic_os_enabled", True):
        from safety      import SafetyLayer
        from decisions   import ConfidenceEngine
        from task_queue  import TaskQueue
        from events      import EventMonitor
        from resources   import ResourceManager
        from orchestrator import Orchestrator
        from proactive   import ProactiveIntelligence
        from agent_loop  import AgentLoop
        from command     import CommandCentre

        atlas_root    = str(Path(__file__).parent)
        safety_layer  = SafetyLayer(config, atlas_root=atlas_root)
        decisions_eng = ConfidenceEngine(config, safety_layer)
        decisions_eng.load_precedents(atlas_root)
        task_q        = TaskQueue(config, atlas_root=atlas_root)
        resources_mgr = ResourceManager(config, speak_cb=vm.speak)
        orchestrator  = Orchestrator(
            config, speak_cb=vm.speak, brain=brain,
            task_queue=task_q, safety=safety_layer, decisions=decisions_eng,
            resources=resources_mgr,
            research=getattr(window, "_research", None),
            market=market,
            code_agent=getattr(brain, "_code_agent", None),
        )
        event_monitor = EventMonitor(config, speak_cb=vm.speak, brain=brain,
                                     task_queue=task_q, safety=safety_layer)
        proactive_mod = ProactiveIntelligence(config, speak_cb=vm.speak, brain=brain,
                                              task_queue=task_q)
        agent_lp      = AgentLoop(
            config, speak_cb=vm.speak,
            task_queue=task_q, orchestrator=orchestrator,
            events=event_monitor, resources=resources_mgr,
            proactive=proactive_mod, safety=safety_layer, decisions=decisions_eng,
        )
        cmd_centre    = CommandCentre(
            config, speak_cb=vm.speak,
            task_queue=task_q, orchestrator=orchestrator,
            resources=resources_mgr, safety=safety_layer,
            agent_loop=agent_lp, window=window,
        )

        resources_mgr.start()
        orchestrator.start()
        event_monitor.start()
        proactive_mod.start()
        agent_lp.start()

        _orig_meta_aos = brain._handle_meta

        def _meta_with_aos(text: str):
            for handler in (safety_layer, agent_lp, task_q, cmd_centre, proactive_mod):
                resp = handler.handle(text)
                if resp is not None:
                    return resp
            return _orig_meta_aos(text)

        brain._handle_meta  = _meta_with_aos
        window._agent_loop  = agent_lp
        window._cmd_centre  = cmd_centre
        window._task_queue  = task_q
        log.info("Agentic OS: all layers active.")

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
    window._market         = market
    window._chrome         = chrome
    window._vision         = vision
    window._camera         = camera
    window._overlay        = overlay
    window._skills         = skills
    window._digest         = digest
    window._ambient        = ambient
    window._sounds         = sounds
    window._vault_brain    = vault_brain
    window._offline_mode   = offline_mode
    window._context7       = context7
    window._voice_pipeline = voice_pipeline

    # Graceful shutdown
    app = QApplication.instance()
    if app:
        app.aboutToQuit.connect(vm.stop)
        app.aboutToQuit.connect(ctx_mgr.stop)
        app.aboutToQuit.connect(feed_mgr.stop)
        app.aboutToQuit.connect(vision.stop)
        app.aboutToQuit.connect(camera.stop)
        app.aboutToQuit.connect(ambient.stop)
        app.aboutToQuit.connect(sounds.stop)
        app.aboutToQuit.connect(skills.stop)
        app.aboutToQuit.connect(digest.stop)
        if market is not None:
            app.aboutToQuit.connect(market.stop)
        if chrome is not None:
            app.aboutToQuit.connect(lambda: chrome._cmd_q.put(None))
        # Memory and playbook shutdown — save session, extract facts, export
        app.aboutToQuit.connect(memory_mod.on_shutdown)
        if vault_brain is not None:
            app.aboutToQuit.connect(vault_brain.stop_watcher)
        app.aboutToQuit.connect(offline_mode.stop)
        app.aboutToQuit.connect(voice_pipeline.stop)


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
