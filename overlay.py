"""
ATLAS Overlay — Cursor Companion

A transparent, frameless, always-on-top window that follows the cursor.
Built with PyQt6 so it integrates naturally with the existing Qt application
without requiring additional native frameworks.

Features:
  - Cyan pulsing dot 35 px right of cursor (idle state)
  - Dot pulses RED while recording voice
  - Response bubble appears next to cursor when ATLAS speaks
  - Typewriter animation for response text
  - Glowing highlight box (visual pointer) when ATLAS references screen elements
  - Auto-fades after 5 s or when user speaks again
  - Never blocks what you are clicking (WA_TransparentForMouseEvents)
  - Three modes: minimal | normal (default) | full

Voice commands handled by main.py meta chain:
  "atlas minimal mode"               → minimal
  "atlas show responses near cursor" → normal
  "atlas hide cursor companion"      → hide
  "atlas show cursor companion"      → show
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import (
    Qt, QTimer, QPoint, QRect, QPropertyAnimation,
    QEasingCurve, pyqtSignal, QObject,
)
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QFontMetrics,
    QCursor, QPainterPath,
)
from PyQt6.QtWidgets import QWidget, QApplication

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_DOT_RADIUS     = 6      # px, idle dot
_DOT_OFFSET_X   = 35     # px right of cursor
_DOT_OFFSET_Y   = 0
_BUBBLE_MAX_W   = 340    # px
_BUBBLE_PADDING = 12     # px
_BUBBLE_FADE_MS = 5000   # auto-fade after 5 s
_PULSE_PERIOD   = 1400   # ms per pulse cycle
_TYPE_INTERVAL  = 22     # ms between typewriter characters
_POINTER_MS     = 3000   # highlight box lifetime ms

_CYAN  = QColor(0, 220, 255, 220)
_RED   = QColor(255, 60,  60,  220)
_DARK  = QColor(8,   12,  28,  210)
_WHITE = QColor(230, 240, 255, 230)


class OverlayWindow(QWidget):
    """
    Transparent overlay window that lives at cursor position.
    Instantiated once in main.py; controlled via public methods.
    """

    MODE_MINIMAL = "minimal"
    MODE_NORMAL  = "normal"
    MODE_FULL    = "full"

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        mode = config.get("overlay_mode", "normal")

        # Window flags: frameless, always on top, tool window (no taskbar entry)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(400, 200)

        self._mode        = mode
        self._dot_color   = _CYAN
        self._pulse_phase = 0.0
        self._recording   = False
        self._hidden      = False

        # Bubble state
        self._bubble_text    = ""
        self._bubble_visible = False
        self._typewriter_pos = 0
        self._bubble_opacity = 1.0

        # Highlight pointer state
        self._pointer_rect:  Optional[QRect]  = None
        self._pointer_label: str              = ""
        self._pointer_alpha: float            = 0.0

        # Timers
        self._move_timer = QTimer(self)
        self._move_timer.setInterval(16)   # ~60 fps
        self._move_timer.timeout.connect(self._update_position)
        self._move_timer.start()

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)  # ~30 fps for pulse
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_timer.start()

        self._type_timer = QTimer(self)
        self._type_timer.setInterval(_TYPE_INTERVAL)
        self._type_timer.timeout.connect(self._tick_typewriter)

        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._start_fade)

        self._pointer_timer = QTimer(self)
        self._pointer_timer.setSingleShot(True)
        self._pointer_timer.timeout.connect(self._clear_pointer)

        self._fade_step_timer = QTimer(self)
        self._fade_step_timer.setInterval(50)
        self._fade_step_timer.timeout.connect(self._step_fade)

        if mode != self.MODE_MINIMAL:
            self.show()

        log.info("Overlay window started (mode=%s).", mode)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_mode(self, mode: str):
        self._mode = mode
        if mode == self.MODE_MINIMAL:
            self._bubble_visible = False
        self.update()

    def set_recording(self, recording: bool):
        """Red dot while recording, cyan when not."""
        self._recording = recording
        self._dot_color = _RED if recording else _CYAN
        self.update()

    def show_response(self, text: str):
        """Start typewriter animation for a new response bubble."""
        if self._mode == self.MODE_MINIMAL:
            return
        self._bubble_text    = text
        self._typewriter_pos = 0
        self._bubble_visible = True
        self._bubble_opacity = 1.0
        self._fade_step_timer.stop()
        self._fade_timer.start(_BUBBLE_FADE_MS)
        self._type_timer.start()
        self.update()

    def clear_bubble(self):
        """Dismiss the response bubble immediately."""
        self._bubble_visible = False
        self._type_timer.stop()
        self._fade_timer.stop()
        self.update()

    def show_pointer(self, label: str, rect: Optional[QRect] = None):
        """
        Draw a glowing highlight box.
        If rect is None, draws a subtle indicator near the bubble.
        Disappears after 3 seconds.
        """
        if self._mode == self.MODE_MINIMAL:
            return
        self._pointer_label = label
        self._pointer_rect  = rect or QRect(20, 20, 120, 36)
        self._pointer_alpha = 1.0
        self._pointer_timer.start(_POINTER_MS)
        self.update()

    def hide_overlay(self):
        self._hidden = True
        self.hide()

    def show_overlay(self):
        self._hidden = False
        self.show()

    # ── Internal timers ───────────────────────────────────────────────────────

    def _update_position(self):
        if self._hidden:
            return
        cursor_pos = QCursor.pos()
        x = cursor_pos.x() + _DOT_OFFSET_X
        y = cursor_pos.y() + _DOT_OFFSET_Y - self.height() // 2
        # Keep on screen
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = min(x, geom.right()  - self.width())
            y = max(y, geom.top())
            y = min(y, geom.bottom() - self.height())
        self.move(x, y)

    def _tick_pulse(self):
        import math
        self._pulse_phase = (self._pulse_phase + 0.045) % (2 * math.pi)
        self.update()

    def _tick_typewriter(self):
        if self._typewriter_pos < len(self._bubble_text):
            self._typewriter_pos += 1
            self.update()
        else:
            self._type_timer.stop()

    def _start_fade(self):
        self._fade_step_timer.start()

    def _step_fade(self):
        self._bubble_opacity = max(0.0, self._bubble_opacity - 0.06)
        self.update()
        if self._bubble_opacity <= 0.0:
            self._fade_step_timer.stop()
            self._bubble_visible = False

    def _clear_pointer(self):
        self._pointer_rect  = None
        self._pointer_label = ""
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        import math
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ── Cursor dot ────────────────────────────────────────────────────────
        pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)
        dot_r = _DOT_RADIUS + int(pulse * 3)

        dot_color = QColor(self._dot_color)
        glow = QColor(dot_color)
        glow.setAlpha(60)

        # Glow ring
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(
            QPoint(dot_r + 4, self.height() // 2),
            dot_r + 5, dot_r + 5
        )
        # Core dot
        painter.setBrush(QBrush(dot_color))
        painter.drawEllipse(
            QPoint(dot_r + 4, self.height() // 2),
            dot_r, dot_r
        )

        if self._mode == self.MODE_MINIMAL:
            painter.end()
            return

        # ── Response bubble ───────────────────────────────────────────────────
        if self._bubble_visible and self._bubble_text:
            visible_text = self._bubble_text[:self._typewriter_pos]
            if visible_text:
                font = QFont("Share Tech Mono", 11)
                painter.setFont(font)
                fm   = QFontMetrics(font)

                max_w  = _BUBBLE_MAX_W
                lines  = self._wrap_text(visible_text, fm, max_w)
                line_h = fm.height() + 3
                bh     = len(lines) * line_h + _BUBBLE_PADDING * 2
                bw     = min(max_w + _BUBBLE_PADDING * 2,
                             max((fm.horizontalAdvance(l) for l in lines), default=40)
                             + _BUBBLE_PADDING * 2)

                bx = dot_r * 2 + 14
                by = self.height() // 2 - bh // 2

                # Bubble background
                alpha = int(self._bubble_opacity * 210)
                bg = QColor(_DARK)
                bg.setAlpha(alpha)
                border = QColor(_CYAN)
                border.setAlpha(int(self._bubble_opacity * 180))

                path = QPainterPath()
                path.addRoundedRect(bx, by, bw, bh, 8, 8)
                painter.fillPath(path, bg)
                pen = QPen(border, 1.2)
                painter.setPen(pen)
                painter.drawPath(path)

                # Text
                text_color = QColor(_WHITE)
                text_color.setAlpha(int(self._bubble_opacity * 230))
                painter.setPen(text_color)
                for i, line in enumerate(lines):
                    painter.drawText(
                        bx + _BUBBLE_PADDING,
                        by + _BUBBLE_PADDING + i * line_h + fm.ascent(),
                        line
                    )

        # ── Visual pointer highlight ───────────────────────────────────────────
        if self._mode == self.MODE_FULL and self._pointer_rect and self._pointer_alpha > 0:
            alpha = int(self._pointer_alpha * 200)
            hl_color = QColor(0, 220, 255, alpha)
            pen = QPen(hl_color, 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self._pointer_rect, 4, 4)

            if self._pointer_label:
                label_color = QColor(0, 220, 255, alpha)
                painter.setPen(label_color)
                painter.setFont(QFont("Share Tech Mono", 9))
                painter.drawText(
                    self._pointer_rect.left(),
                    self._pointer_rect.top() - 4,
                    self._pointer_label
                )

        painter.end()

    @staticmethod
    def _wrap_text(text: str, fm: QFontMetrics, max_w: int) -> list[str]:
        lines  = []
        words  = text.split()
        line   = ""
        for word in words:
            test = (line + " " + word).strip()
            if fm.horizontalAdvance(test) <= max_w:
                line = test
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines or [""]


# ── Voice command handler (wired into main.py meta chain) ─────────────────────

def handle_overlay_command(text: str, overlay: OverlayWindow) -> Optional[str]:
    lower = text.lower().strip()

    if any(p in lower for p in ("atlas minimal mode", "atlas minimise overlay")):
        overlay.set_mode(OverlayWindow.MODE_MINIMAL)
        return "Cursor companion minimised."

    if any(p in lower for p in ("atlas show responses near cursor",
                                 "atlas normal overlay", "atlas cursor companion normal")):
        overlay.set_mode(OverlayWindow.MODE_NORMAL)
        return "Cursor companion normal mode."

    if any(p in lower for p in ("atlas full overlay", "atlas full cursor mode")):
        overlay.set_mode(OverlayWindow.MODE_FULL)
        return "Cursor companion full mode with visual pointers."

    if any(p in lower for p in ("atlas hide cursor companion",
                                 "atlas hide overlay", "atlas hide companion")):
        overlay.hide_overlay()
        return "Cursor companion hidden."

    if any(p in lower for p in ("atlas show cursor companion",
                                 "atlas show overlay", "atlas bring back companion")):
        overlay.show_overlay()
        return "Cursor companion visible."

    return None
