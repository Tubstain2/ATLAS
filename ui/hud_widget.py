"""
ATLAS HUD Overlay Widget

Transparent widget that floats over the entire window and draws:
  - Corner brackets (sci-fi frame)
  - ATLAS branding + version (top-left)
  - Live clock + date (top-right)
  - State indicator with blink dot (bottom-left)
  - Module status badges (bottom-right)
  - Top-center scanning marker

Receives no audio data; updated by ATLASMainWindow via set_state() / set_module_active().
"""

import platform
from datetime import datetime

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import QTimer, Qt, QRectF
from PyQt6.QtGui import (
    QPainter, QColor, QFont, QPen, QBrush, QFontDatabase,
)

_STATE_LABEL = {
    "idle":       "STANDBY",
    "listening":  "LISTENING",
    "responding": "RESPONDING",
    "thinking":   "PROCESSING",
    "detecting":  "DETECTING",
}

_STATE_COLOR = {
    "idle":       (0,  105, 185),
    "listening":  (0,  165, 255),
    "responding": (0,  232, 242),
    "thinking":   (148, 82,  255),
    "detecting":  (210, 160,   0),
}

_MODULES = ["VOICE", "WEB", "CTRL", "EDIT"]


def _qc(rgb: tuple, alpha: int = 255) -> QColor:
    return QColor(rgb[0], rgb[1], rgb[2], alpha)


class HUDWidget(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet("background: transparent;")

        self._state    = "idle"
        self._muted    = False
        self._modules  = {m: False for m in _MODULES}
        self._blink    = True
        self._os       = platform.system()
        self._ctx_app  = ""
        self._ctx_file = ""

        self._f_sm = QFont("Courier New", 9)
        self._f_md = QFont("Courier New", 11)
        self._f_lg = QFont("Courier New", 14)
        self._f_lg.setBold(True)
        self._try_tech_font()

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self.update)
        self._clock_timer.start()

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(780)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_state(self, state: str):
        self._state = state
        self.update()

    def set_muted(self, muted: bool):
        self._muted = muted
        self.update()

    def set_module_active(self, module: str, active: bool):
        if module in self._modules:
            self._modules[module] = active
            self.update()

    def set_context(self, app: str, file: str = "") -> None:
        self._ctx_app  = app[:28] if app else ""
        self._ctx_file = file[:30] if file else ""
        self.update()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _try_tech_font(self):
        preferred = ["Share Tech Mono", "JetBrains Mono", "Fira Mono",
                     "Roboto Mono", "Consolas"]
        available = set(QFontDatabase.families())
        for name in preferred:
            if name in available:
                self._f_sm = QFont(name, 9)
                self._f_md = QFont(name, 11)
                self._f_lg = QFont(name, 14)
                self._f_lg.setBold(True)
                break

    def _toggle_blink(self):
        self._blink = not self._blink
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        w = float(self.width())
        h = float(self.height())
        m = 22.0   # margin

        self._draw_brackets(painter, w, h, m)
        self._draw_branding(painter, m)
        self._draw_clock(painter, w, m)
        self._draw_status(painter, h, m)
        self._draw_modules(painter, w, h, m)
        self._draw_top_marker(painter, w, m)

        if self._muted:
            self._draw_mute_banner(painter, w, h)

        painter.end()

    def _draw_brackets(self, painter, w, h, m):
        sz  = 22.0
        pen = QPen(QColor(0, 105, 185, 55), 1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for (x, y, dx, dy) in [
            (m, m, 1, 1), (w - m, m, -1, 1),
            (m, h - m, 1, -1), (w - m, h - m, -1, -1),
        ]:
            painter.drawLine(int(x), int(y), int(x + dx * sz), int(y))
            painter.drawLine(int(x), int(y), int(x), int(y + dy * sz))

    def _draw_branding(self, painter, m):
        x = int(m + 6)

        painter.setFont(self._f_lg)
        painter.setPen(QPen(QColor(0, 148, 255, 205)))
        painter.drawText(x, int(m + 26), "ATLAS")

        painter.setFont(self._f_sm)
        painter.setPen(QPen(QColor(0, 105, 185, 125)))
        painter.drawText(x, int(m + 43), "v0.1.0  INITIALIZED")
        painter.drawText(x, int(m + 59), f"OS: {self._os.upper()}")

    def _draw_clock(self, painter, w, m):
        now      = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")

        painter.setFont(self._f_lg)
        painter.setPen(QPen(QColor(0, 162, 255, 205)))
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(time_str)
        painter.drawText(int(w - m - 6 - tw), int(m + 26), time_str)

        painter.setFont(self._f_sm)
        painter.setPen(QPen(QColor(0, 105, 185, 125)))
        fm = painter.fontMetrics()
        dw = fm.horizontalAdvance(date_str)
        painter.drawText(int(w - m - 6 - dw), int(m + 43), date_str)

    def _draw_status(self, painter, h, m):
        label = _STATE_LABEL.get(self._state, self._state.upper())
        rgb   = _STATE_COLOR.get(self._state, (0, 120, 200))

        dot_y = int(h - m - 38)

        # Blinking dot
        if self._blink or self._state == "idle":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(_qc(rgb, 205)))
            painter.drawEllipse(int(m + 6), dot_y, 8, 8)

        painter.setFont(self._f_md)
        painter.setPen(QPen(_qc(rgb, 220)))
        painter.drawText(int(m + 20), int(h - m - 30), label)

        painter.setFont(self._f_sm)
        painter.setPen(QPen(QColor(0, 80, 145, 110)))
        status = "MUTED" if self._muted else "ACTIVE"
        painter.drawText(int(m + 6), int(h - m - 13), f"SYSTEM: {status}")

        # Active context line — shown just above the state dot
        if self._ctx_app:
            ctx_line = self._ctx_app
            if self._ctx_file:
                ctx_line += f"  {self._ctx_file}"
            painter.setFont(self._f_sm)
            painter.setPen(QPen(QColor(0, 162, 255, 90)))
            painter.drawText(int(m + 6), int(h - m - 52), ctx_line)

    def _draw_modules(self, painter, w, h, m):
        bw = 50
        bh = 19
        gap = 7
        total_w = len(_MODULES) * (bw + gap) - gap
        sx = w - m - 6 - total_w
        y  = h - m - 38

        for i, name in enumerate(_MODULES):
            x = sx + i * (bw + gap)
            active = self._modules[name]

            if active:
                bg     = QColor(0,  80, 165, 82)
                border = QColor(0, 165, 255, 165)
                tc     = QColor(0, 225, 255, 225)
            else:
                bg     = QColor(0,  22,  45, 62)
                border = QColor(0,  62, 105, 82)
                tc     = QColor(0,  82, 125, 125)

            rect = QRectF(x, y, bw, bh)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(QBrush(bg))
            painter.drawRoundedRect(rect, 3, 3)

            painter.setFont(self._f_sm)
            painter.setPen(QPen(tc))
            fm  = painter.fontMetrics()
            ntw = fm.horizontalAdvance(name)
            painter.drawText(int(x + (bw - ntw) / 2), int(y + bh - 4), name)

        painter.setFont(self._f_sm)
        painter.setPen(QPen(QColor(0, 80, 145, 110)))
        fm     = painter.fontMetrics()
        lbl    = "MODULES"
        lbl_w  = fm.horizontalAdvance(lbl)
        painter.drawText(int(w - m - 6 - lbl_w), int(h - m - 13), lbl)

    def _draw_top_marker(self, painter, w, m):
        cx  = w / 2.0
        lw  = 115.0

        pen = QPen(QColor(0, 105, 205, 52), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(int(cx - lw / 2), int(m + 11), int(cx + lw / 2), int(m + 11))

        painter.setPen(QPen(QColor(0, 162, 255, 105), 1))
        painter.drawLine(int(cx), int(m + 5), int(cx), int(m + 17))

    def _draw_mute_banner(self, painter, w, h):
        painter.setFont(self._f_lg)
        text = "[ MUTED ]"
        fm   = painter.fontMetrics()
        tw   = fm.horizontalAdvance(text)
        painter.setPen(QPen(QColor(255, 72, 72, 185)))
        painter.drawText(int((w - tw) / 2), int(h / 2 - 18), text)
