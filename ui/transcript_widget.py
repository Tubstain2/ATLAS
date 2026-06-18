"""
ATLAS Transcript Widget

Displays a rolling log of user speech and ATLAS responses at the bottom
of the window.  ATLAS responses are revealed character-by-character with
a blinking cursor.  Older entries fade out progressively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QPainter, QColor, QFont, QPen, QFontDatabase


@dataclass
class _Entry:
    text: str
    is_atlas: bool
    shown: int  = 0      # chars revealed so far
    done: bool  = False  # fully revealed


class TranscriptWidget(QWidget):
    """Rolling transcript with animated reveal for ATLAS responses."""

    _MAX = 7
    _REVEAL_CHARS_PER_TICK = 3    # chars added each 20 ms ≈ 150 cps

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[_Entry] = []

        self._f_user  = QFont("Courier New", 11)
        self._f_atlas = QFont("Courier New", 12)
        self._try_tech_font()

        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setStyleSheet("background: transparent;")

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setInterval(20)
        self._reveal_timer.timeout.connect(self._reveal_tick)

        self._cursor_visible = True
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(530)
        self._cursor_timer.timeout.connect(self._toggle_cursor)
        self._cursor_timer.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def add_user(self, text: str):
        """Add a transcribed user utterance (shown instantly)."""
        if not text.strip():
            return
        e = _Entry(text=text.strip(), is_atlas=False, shown=len(text), done=True)
        self._push(e)

    def add_response(self, text: str):
        """Add an ATLAS response with animated character reveal."""
        if not text.strip():
            return
        e = _Entry(text=text.strip(), is_atlas=True, shown=0, done=False)
        self._push(e)
        self._reveal_timer.start()

    def clear(self):
        self._entries.clear()
        self.update()

    # kept for backward-compat with main_window
    def add_entry(self, text: str, is_atlas: bool = False):
        if is_atlas:
            self.add_response(text)
        else:
            self.add_user(text)

    def show_response(self, text: str):
        self.add_response(text)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _push(self, entry: _Entry):
        self._entries.append(entry)
        if len(self._entries) > self._MAX:
            self._entries = self._entries[-self._MAX:]
        self.update()

    def _try_tech_font(self):
        preferred = ["Share Tech Mono", "JetBrains Mono", "Fira Mono",
                     "Roboto Mono", "Consolas"]
        available = set(QFontDatabase.families())
        for name in preferred:
            if name in available:
                self._f_user  = QFont(name, 11)
                self._f_atlas = QFont(name, 12)
                break

    def _reveal_tick(self):
        pending = False
        for e in self._entries:
            if not e.done and e.is_atlas:
                e.shown = min(e.shown + self._REVEAL_CHARS_PER_TICK, len(e.text))
                if e.shown >= len(e.text):
                    e.done = True
                else:
                    pending = True
        if not pending:
            self._reveal_timer.stop()
        self.update()

    def _toggle_cursor(self):
        self._cursor_visible = not self._cursor_visible
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        if not self._entries:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        w = float(self.width())
        h = float(self.height())
        px = 42.0          # horizontal padding
        lh = 25            # line height px
        gap = 5            # gap between entries

        # Separator
        pen = QPen(QColor(0, 85, 165, 58), 1)
        painter.setPen(pen)
        painter.drawLine(int(w * 0.08), 1, int(w * 0.92), 1)

        y = h - 10.0
        n = len(self._entries)

        for idx in range(n - 1, -1, -1):
            e    = self._entries[idx]
            fade = max(0.22, 1.0 - (n - 1 - idx) * 0.23)

            if e.is_atlas:
                prefix = "  ATLAS › "
                body   = e.text[: e.shown]
                color  = QColor(0, 228, 242, int(225 * fade))
                font   = self._f_atlas
            else:
                prefix = "  YOU   › "
                body   = e.text
                color  = QColor(148, 198, 255, int(150 * fade))
                font   = self._f_user

            painter.setFont(font)
            fm   = painter.fontMetrics()
            full = prefix + body
            lines = self._wrap(full, fm, w - 2 * px)

            block_h = lh * len(lines)
            y -= block_h

            painter.setPen(QPen(color))
            for li, line in enumerate(lines):
                painter.drawText(int(px), int(y + (li + 1) * lh), line)

            # Blinking cursor on the active atlas entry being revealed
            if e.is_atlas and not e.done and self._cursor_visible:
                last = lines[-1] if lines else ""
                cx   = int(px + fm.horizontalAdvance(last) + 3)
                cy_t = int(y + len(lines) * lh)
                painter.setPen(QPen(QColor(0, 228, 242, int(210 * fade)), 2))
                painter.drawLine(cx, cy_t - 14, cx, cy_t - 2)

            y -= gap

        painter.end()

    @staticmethod
    def _wrap(text: str, fm, max_w: float) -> List[str]:
        """Word-wrap text to fit within max_w pixels."""
        words  = text.split(" ")
        lines: List[str] = []
        line   = ""
        for word in words:
            candidate = (line + " " + word).lstrip() if line else word
            if fm.horizontalAdvance(candidate) <= max_w:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines or [""]
