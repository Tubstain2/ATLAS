"""
ATLAS Main Window

Hosts:
  - OrbWidget  (animated core, center region)
  - HUDWidget  (transparent overlay drawn on top)
  - TranscriptWidget (bottom strip for live transcription)
  - QSystemTrayIcon (background persist + mute toggle)

Public API used by voice / core modules:
  set_amplitude(float)          — real-time mic level 0-1
  set_state(str)                — 'idle'|'listening'|'responding'|'thinking'
  add_entry(text, is_atlas)     — append to transcript
  show_response(text)           — ATLAS response with reveal animation
  set_module_active(name, bool) — update HUD badge
"""

import yaml
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QSystemTrayIcon, QMenu, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QPalette, QIcon, QPixmap, QPainter,
    QRadialGradient, QKeySequence, QShortcut, QAction,
)

from .orb_widget import OrbWidget
from .hud_widget import HUDWidget
from .transcript_widget import TranscriptWidget


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
    p  = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    g = QRadialGradient(size / 2, size / 2, size / 2)
    g.setColorAt(0.0, QColor(0, 185, 255, 210))
    g.setColorAt(0.5, QColor(0,  95, 255, 105))
    g.setColorAt(1.0, QColor(0,   0,   0,   0))
    p.setBrush(g); p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)

    r  = size // 4
    g2 = QRadialGradient(size / 2 - 4, size / 2 - 4, size / 3)
    g2.setColorAt(0.0, QColor(215, 245, 255))
    g2.setColorAt(0.5, QColor(0,  165, 255))
    g2.setColorAt(1.0, QColor(0,   62, 210))
    p.setBrush(g2)
    p.drawEllipse(size // 2 - r, size // 2 - r, r * 2, r * 2)

    p.end()
    return QIcon(px)


class ATLASMainWindow(QMainWindow):
    """Top-level application window."""

    # Emitted so external modules can connect without importing Qt
    amplitude_changed = pyqtSignal(float)
    state_changed     = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._cfg    = _load_config()
        self._tray   = None
        self._muting = False

        self._init_window()
        self._init_widgets()
        self._init_tray()
        self._init_shortcuts()

    # ── Window setup ──────────────────────────────────────────────────────────

    def _init_window(self):
        wc  = self._cfg.get("app", {}).get("window", {})
        w   = wc.get("width",  1280)
        h   = wc.get("height",  860)

        self.setWindowTitle("ATLAS")
        self.setMinimumSize(820, 580)
        self.resize(w, h)
        self.setWindowIcon(_make_icon())

        # Deep-space background
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#050510"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        scr = self.screen()
        if scr:
            geo = scr.availableGeometry()
            self.move(geo.center().x() - w // 2, geo.center().y() - h // 2)

    # ── Widget layout ─────────────────────────────────────────────────────────

    def _init_widgets(self):
        root = QWidget()
        root.setObjectName("root")
        root.setStyleSheet("QWidget#root { background: #050510; }")

        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        orb_r = self._cfg.get("ui", {}).get("orb_radius", 170)
        self.orb = OrbWidget(orb_radius=orb_r)
        self.orb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.transcript = TranscriptWidget()
        self.transcript.setFixedHeight(195)

        layout.addWidget(self.orb,        stretch=7)
        layout.addWidget(self.transcript, stretch=0)

        self.setCentralWidget(root)

        # HUD floats on top (transparent, mouse-pass-through)
        self.hud = HUDWidget(parent=root)
        self.hud.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        QTimer.singleShot(80, self._reposition_hud)

    def _reposition_hud(self):
        base = self.centralWidget()
        if base and self.hud:
            self.hud.setGeometry(base.rect())
            self.hud.raise_()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._reposition_hud)

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
                background: #080818;
                color: #80C0FF;
                border: 1px solid #1A3A6A;
                font-family: 'Courier New';
                font-size: 12px;
            }
            QMenu::item { padding: 6px 18px; }
            QMenu::item:selected { background: #1A3A6A; }
        """)

        show_act = QAction("Show ATLAS", self)
        show_act.triggered.connect(self._show)
        menu.addAction(show_act)
        menu.addSeparator()

        self._mute_act = QAction("Mute Microphone", self)
        self._mute_act.setCheckable(True)
        self._mute_act.triggered.connect(self._toggle_mute)
        menu.addAction(self._mute_act)
        menu.addSeparator()

        quit_act = QAction("Quit ATLAS", self)
        quit_act.triggered.connect(self._quit)
        menu.addAction(quit_act)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    # ── Keyboard shortcuts ────────────────────────────────────────────────────

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
        self.show(); self.raise_(); self.activateWindow()

    def _toggle_mute(self, checked: bool):
        self._muting = checked
        self.hud.set_muted(checked)

    def _quit(self):
        from PyQt6.QtWidgets import QApplication
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

    # ── Public API ────────────────────────────────────────────────────────────

    def set_amplitude(self, value: float):
        """Called by voice module with real-time microphone level."""
        self.orb.set_amplitude(value)

    def set_state(self, state: str):
        """'idle' | 'listening' | 'responding' | 'thinking'"""
        self.orb.set_state(state)
        self.hud.set_state(state)
        self.state_changed.emit(state)

    def add_entry(self, text: str, is_atlas: bool = False):
        self.transcript.add_entry(text, is_atlas)

    def show_response(self, text: str):
        self.transcript.show_response(text)

    def set_module_active(self, module: str, active: bool):
        self.hud.set_module_active(module, active)
