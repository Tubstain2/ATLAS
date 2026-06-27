"""
ATLAS Main Window — WebEngine edition

Renders ui/atlas_ui.html inside a single QWebEngineView.
All Python ↔ UI communication is via page().runJavaScript().

Public API (unchanged from the original PyQt6 widget version):
  set_amplitude(float)          — real-time mic level 0-1
  set_state(str)                — 'idle'|'listening'|'responding'|'thinking'
  add_entry(text, is_atlas)     — append to conversation
  show_response(text)           — ATLAS response with typewriter effect
  set_module_active(name, bool) — engine / skill indicator dot
  set_feed_manager(manager)     — wire FeedManager signals to JS
  toggle_feed() / show_feed() / hide_feed()
  add_feed_item(category, content)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import yaml
from PyQt6.QtCore import (
    Qt, QTimer, QPoint, QEvent, QObject, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QIcon, QKeySequence, QPixmap, QPainter,
    QRadialGradient, QShortcut, QAction,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QSystemTrayIcon, QMenu, QWidget,
)

log = logging.getLogger(__name__)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage
    _WEB_OK = True
except ImportError:
    _WEB_OK = False
    log.error(
        "PyQt6-WebEngine not installed. "
        "Run: pip install PyQt6-WebEngine  then restart ATLAS."
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    path = Path(__file__).resolve().parent.parent / "config.yaml"
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _make_icon(size: int = 64) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QRadialGradient(size / 2, size / 2, size / 2)
    g.setColorAt(0.0, QColor(0, 185, 255, 210))
    g.setColorAt(0.5, QColor(0, 95, 255, 105))
    g.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.setBrush(g)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)
    r = size // 4
    g2 = QRadialGradient(size / 2 - 4, size / 2 - 4, size / 3)
    g2.setColorAt(0.0, QColor(215, 245, 255))
    g2.setColorAt(0.5, QColor(0, 165, 255))
    g2.setColorAt(1.0, QColor(0, 62, 210))
    p.setBrush(g2)
    p.drawEllipse(size // 2 - r, size // 2 - r, r * 2, r * 2)
    p.end()
    return QIcon(px)


# ── Window drag filter for frameless mode ─────────────────────────────────────

class _DragFilter(QObject):
    """Intercepts mouse events on the WebEngineView to enable title-bar drag."""

    DRAG_HEIGHT = 52  # px — must match CSS #title-bar height

    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self._win      = window
        self._dragging = False
        self._drag_pos = QPoint()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        t = event.type()
        if t == QEvent.Type.MouseButtonPress:
            if (
                event.button() == Qt.MouseButton.LeftButton
                and event.position().y() < self.DRAG_HEIGHT
            ):
                self._dragging = True
                self._drag_pos = (
                    event.globalPosition().toPoint()
                    - self._win.frameGeometry().topLeft()
                )
                return True
        elif t == QEvent.Type.MouseMove:
            if self._dragging:
                self._win.move(
                    event.globalPosition().toPoint() - self._drag_pos
                )
                return True
        elif t == QEvent.Type.MouseButtonRelease:
            self._dragging = False
        return False


# ── Main window ───────────────────────────────────────────────────────────────

class ATLASMainWindow(QMainWindow):
    """Top-level application window — single QWebEngineView rendering atlas_ui.html."""

    amplitude_changed = pyqtSignal(float)
    state_changed     = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._cfg         = _load_config()
        self._tray        = None
        self._js_ready    = False
        self._pending_js: list[str] = []

        self._init_window()
        self._init_webview()
        self._init_tray()
        self._init_shortcuts()

    # ── Window setup ──────────────────────────────────────────────────────────

    def _init_window(self):
        wc = self._cfg.get("app", {}).get("window", {})
        w  = wc.get("width",  1200)
        h  = wc.get("height",  750)

        self.setWindowTitle("ATLAS")
        self.setMinimumSize(900, 600)
        self.resize(w, h)
        self.setWindowIcon(_make_icon())
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor("#0A0A0F"))
        self.setPalette(pal)

        scr = self.screen()
        if scr:
            geo = scr.availableGeometry()
            self.move(
                geo.center().x() - w // 2,
                geo.center().y() - h // 2,
            )

    # ── WebEngine view ────────────────────────────────────────────────────────

    def _init_webview(self):
        if not _WEB_OK:
            from PyQt6.QtWidgets import QLabel
            lbl = QLabel(
                "PyQt6-WebEngine not installed.\n\nRun:\n  pip install PyQt6-WebEngine\n\nThen restart ATLAS."
            )
            lbl.setStyleSheet(
                "color:#4FC3F7; background:#0A0A0F; font-size:16px; padding:40px;"
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setCentralWidget(lbl)
            return

        self._view = QWebEngineView()

        # Settings
        s = self._view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled,             True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled,          True)

        # Background
        self._view.setStyleSheet("background: #0A0A0F;")

        # Load HTML
        html_path = Path(__file__).parent / "atlas_ui.html"
        from PyQt6.QtCore import QUrl
        self._view.load(QUrl.fromLocalFile(str(html_path.resolve())))
        self._view.loadFinished.connect(self._on_load_finished)

        # Install drag filter
        self._drag_filter = _DragFilter(self)
        self._view.installEventFilter(self._drag_filter)

        # Expose Python callbacks into JS via title-change channel
        self._view.titleChanged.connect(self._on_title_changed)

        self.setCentralWidget(self._view)

    def _on_load_finished(self, ok: bool):
        if ok:
            self._js_ready = True
            for js in self._pending_js:
                self._view.page().runJavaScript(js)
            self._pending_js.clear()
            log.info("ATLAS UI loaded successfully.")
        else:
            log.error("ATLAS UI failed to load.")

    def _on_title_changed(self, title: str):
        """JS signals Python by setting document.title = 'atlas:cmd:payload'."""
        if not title.startswith("atlas:"):
            return
        parts   = title.split(":", 2)
        cmd     = parts[1] if len(parts) > 1 else ""
        payload = parts[2] if len(parts) > 2 else ""
        log.debug("JS→Python: %s %s", cmd, payload[:80])

        if cmd == "query" and payload:
            self._dispatch_text_query(payload)
        elif cmd == "camera":
            self._dispatch_camera(payload)
        elif cmd == "traffic":
            if payload == "close":
                self._quit()
            elif payload == "min":
                self.showMinimized()
            elif payload == "full":
                self._toggle_fs()

    def _dispatch_camera(self, action: str):
        """Handle camera on/off from the UI button."""
        cam = getattr(self, "_camera_module", None)
        if cam is None:
            log.debug("Camera module not attached — ignoring camera:%s", action)
            return
        if action == "on":
            cam.start()
        elif action == "off":
            cam.stop()

    def _dispatch_text_query(self, text: str):
        """Route a typed query through the same response pipeline as voice."""
        import threading

        vm = getattr(self, "_voice_module", None)
        cb = None
        if vm and vm._worker:
            cb = vm._worker._response_cb

        if not cb:
            log.debug("Text query arrived before callback ready — retrying in 600 ms.")
            QTimer.singleShot(600, lambda: self._dispatch_text_query(text))
            return

        def _run():
            try:
                cb(text)   # callback handles show_response / set_state internally
            except Exception as exc:
                log.error("Text query error: %s", exc)
                QTimer.singleShot(0, lambda: self.set_state("idle"))

        threading.Thread(target=_run, daemon=True, name="text-query").start()

    def _js(self, code: str):
        if not _WEB_OK:
            return
        if self._js_ready and hasattr(self, '_view'):
            self._view.page().runJavaScript(code)
        else:
            self._pending_js.append(code)

    @staticmethod
    def _q(s: str) -> str:
        """JSON-encode a string for safe injection into JS."""
        return json.dumps(str(s))

    # ── System tray ───────────────────────────────────────────────────────────

    def _init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_make_icon(22))
        self._tray.setToolTip("ATLAS — AI Assistant")

        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background: #111118;
                color: #4FC3F7;
                border: 1px solid rgba(79,195,247,0.2);
                font-size: 13px;
            }
            QMenu::item { padding: 6px 18px; }
            QMenu::item:selected { background: rgba(79,195,247,0.15); }
        """)

        show_act = QAction("Show ATLAS", self)
        show_act.triggered.connect(self._show)
        menu.addAction(show_act)
        menu.addSeparator()

        self._mute_act = QAction("Mute Microphone", self)
        self._mute_act.setCheckable(True)
        menu.addAction(self._mute_act)
        menu.addSeparator()

        quit_act = QAction("Quit ATLAS", self)
        quit_act.triggered.connect(self._quit)
        menu.addAction(quit_act)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    # ── Shortcuts ─────────────────────────────────────────────────────────────

    def _init_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(self.hide)
        QShortcut(QKeySequence(Qt.Key.Key_F11),    self).activated.connect(self._toggle_fs)

    # ── Slot helpers ──────────────────────────────────────────────────────────

    def _toggle_fs(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        QApplication.quit()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show() if not self.isVisible() else self.hide()

    def closeEvent(self, ev):
        ev.ignore()
        self.hide()
        if self._tray:
            self._tray.showMessage(
                "ATLAS",
                "Running in the background. Double-click the icon to restore.",
                QSystemTrayIcon.MessageIcon.Information,
                2200,
            )

    # ── Public API (identical signatures to old widget-based window) ──────────

    def set_amplitude(self, value: float):
        self._js(f"setAmplitude({value:.4f})")
        self.amplitude_changed.emit(value)

    def set_state(self, state: str):
        self._js(f"setState({self._q(state)})")
        self.state_changed.emit(state)

    def add_entry(self, text: str, is_atlas: bool = False):
        self._js(f"addEntry({self._q(text)}, {json.dumps(is_atlas)})")

    def show_response(self, text: str):
        self._js(f"showResponse({self._q(text)})")

    def set_module_active(self, module: str, active: bool):
        self._js(f"setModuleActive({self._q(module)}, {json.dumps(active)})")

    # ── Feed panel public API ─────────────────────────────────────────────────

    def toggle_feed(self):
        self._js("toggleFeed()")

    def show_feed(self):
        self._js("showFeed()")

    def hide_feed(self):
        self._js("hideFeed()")

    def add_feed_item(self, category: str, content: str):
        item = {"category": category, "content": content}
        self._js(f"addFeedItem({json.dumps(item)})")

    # ── FeedManager wiring ────────────────────────────────────────────────────

    def set_feed_manager(self, manager) -> None:
        """Connect FeedManager Qt signals to JS bridge calls."""
        if manager is None:
            return
        manager.item_added.connect(
            lambda item: self._js(f"addFeedItem({json.dumps(item)})")
        )
        manager.weather_updated.connect(
            lambda d: self._js(f"updateWeather({json.dumps(d)})")
        )
        manager.stats_updated.connect(
            lambda d: self._js(f"updateStats({json.dumps(d)})")
        )
        manager.spotify_updated.connect(
            lambda d: self._js(f"updateSpotify({json.dumps(d)})")
        )
        manager.context_updated.connect(
            lambda d: self._js(f"updateContext({json.dumps(d)})")
        )
        manager.news_updated.connect(
            lambda arr: self._js(f"updateNews({json.dumps(arr)})")
        )
        manager.obsidian_updated.connect(
            lambda d: self._js(f"updateObsidian({json.dumps(d)})")
        )
        # Push initial reminders after JS loads
        QTimer.singleShot(
            2000,
            lambda: self._js(
                f"updateReminders({json.dumps(manager.get_active_reminders())})"
            ) if hasattr(manager, "get_active_reminders") else None,
        )

    # ── Legacy compat stubs (used by older code paths) ────────────────────────

    def push_atlas_response(self, text: str) -> None:
        self.show_response(text)

    def set_feed_mode(self, mode: str) -> None:
        self._js(f"setFeedMode({self._q(mode)})")

    def clear_feed(self) -> None:
        self._js("clearFeed()")

    def hide_widget(self, name: str) -> None:
        self._js(f"hideWidget({self._q(name)})")

    def show_widget(self, name: str) -> None:
        self._js(f"showWidget({self._q(name)})")

    def pin_last_item(self) -> None:
        self._js("pinLastItem()")

    def show_toast(self, message: str, kind: str = "info") -> None:
        self._js(f"showToast({self._q(message)}, {self._q(kind)})")

    # ── Hologram public API ───────────────────────────────────────────────────

    def show_hologram(self, viz_type: str, data: dict | None = None) -> None:
        payload = json.dumps(data or {})
        self._js(f"showHologram({self._q(viz_type)}, {payload})")

    def hide_hologram(self) -> None:
        self._js("hideHologram()")


# ── feed_panel shim ────────────────────────────────────────────────────────────
# main.py calls window.feed_panel.* — this adapter proxies those calls to the
# window's JS bridge so main.py doesn't need any changes.

class _FeedPanelShim:
    """Mimics the old FeedPanel widget API; all calls delegate to ATLASMainWindow._js()."""

    def __init__(self, window: "ATLASMainWindow"):
        self._w = window

    # Properties main.py inspects
    @property
    def is_panel_visible(self) -> bool:
        return False  # state is in JS; Python doesn't need to gate on it

    def set_feed_manager(self, manager) -> None:
        self._w.set_feed_manager(manager)

    def add_feed_item(self, category: str, content: str) -> None:
        self._w.add_feed_item(category, content)

    def push_atlas_response(self, text: str) -> None:
        self._w.show_response(text)

    def toggle(self) -> None:
        self._w.toggle_feed()

    def slide_in(self) -> None:
        self._w.show_feed()

    def slide_out(self) -> None:
        self._w.hide_feed()

    def set_feed_mode(self, mode: str) -> None:
        self._w.set_feed_mode(mode)

    def clear_feed(self) -> None:
        self._w.clear_feed()

    def hide_widget(self, name: str) -> None:
        self._w.hide_widget(name)

    def show_widget(self, name: str) -> None:
        self._w.show_widget(name)

    def pin_last_item(self) -> None:
        self._w.pin_last_item()


class _ImagePanelShim:
    """Stub so main.py's window.image_panel.* calls don't crash."""
    def show_image(self, path=None, prompt=None, *args, **kwargs):
        pass
    def set_save_callback(self, cb):
        pass
    def set_preview_callback(self, cb):
        pass


class _HUDShim:
    """Stub so main.py's window.hud.* calls don't crash."""
    def set_context(self, app='', file=''):
        pass
    def set_state(self, state):
        pass
    def set_muted(self, muted):
        pass
    def set_module_active(self, name, active):
        pass


# Monkey-patch shims onto ATLASMainWindow so window.feed_panel and
# window.image_panel are always available.
_orig_init = ATLASMainWindow.__init__

def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    self.feed_panel  = _FeedPanelShim(self)
    self.image_panel = _ImagePanelShim()
    self.hud         = _HUDShim()

ATLASMainWindow.__init__ = _patched_init
