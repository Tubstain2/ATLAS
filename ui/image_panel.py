"""
ATLAS Image Panel

Floating widget that displays generated images over the orb area.
Appears as a semi-transparent overlay card with image, prompt text,
generation time, and Save / Open buttons.

Triggered by ImageGenModule via the show_image callback:
    show_image(path: str | None, prompt: str | None, elapsed: int | None)
    → pass None to hide the panel
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtGui import (
    QPixmap, QColor, QPalette, QFont, QPainter,
    QBrush, QPen, QFontDatabase,
)


def _tech_font(size: int, bold: bool = False) -> QFont:
    preferred = ["Share Tech Mono", "JetBrains Mono", "Fira Mono",
                 "Roboto Mono", "Consolas", "Courier New"]
    available = set(QFontDatabase.families())
    for name in preferred:
        if name in available:
            f = QFont(name, size)
            f.setBold(bold)
            return f
    f = QFont("Courier New", size)
    f.setBold(bold)
    return f


class ImagePanel(QWidget):
    """
    Floating image display card.
    Parented to the left_area widget so it stays positioned over the orb.
    """

    _PANEL_W = 420
    _PANEL_H = 540

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("image_panel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._visible   = False
        self._img_path: Optional[str]  = None
        self._prompt:   Optional[str]  = None
        self._elapsed:  Optional[int]  = None
        self._save_cb   = None
        self._preview_cb = None

        self._build_ui()
        self.hide()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.setFixedSize(self._PANEL_W, self._PANEL_H)

        # Card background via stylesheet — drawn by paintEvent
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # Header row
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)

        self._title_label = QLabel("ATLAS GENERATION")
        self._title_label.setFont(_tech_font(10, bold=True))
        self._title_label.setStyleSheet("color: #00C8FF; background: transparent;")

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setFont(_tech_font(9))
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #406080;
                border: 1px solid #1A3A5A;
                border-radius: 3px;
            }
            QPushButton:hover { color: #00C8FF; border-color: #00C8FF; }
        """)
        close_btn.clicked.connect(self.hide_panel)
        header_row.addWidget(self._title_label)
        header_row.addStretch()
        header_row.addWidget(close_btn)
        outer.addLayout(header_row)

        # Image display
        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setFixedSize(self._PANEL_W - 24, 320)
        self._img_label.setStyleSheet("""
            QLabel {
                background: #03070F;
                border: 1px solid #0A2040;
                color: #304060;
            }
        """)
        self._img_label.setText("No image generated yet.")
        self._img_label.setFont(_tech_font(9))
        outer.addWidget(self._img_label)

        # Prompt label (scrollable if long)
        self._prompt_label = QLabel("")
        self._prompt_label.setFont(_tech_font(8))
        self._prompt_label.setStyleSheet("color: #5090B0; background: transparent;")
        self._prompt_label.setWordWrap(True)
        self._prompt_label.setMaximumHeight(52)
        self._prompt_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self._prompt_label)

        # Stats row
        self._stats_label = QLabel("")
        self._stats_label.setFont(_tech_font(8))
        self._stats_label.setStyleSheet("color: #3060A0; background: transparent;")
        outer.addWidget(self._stats_label)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_style = """
            QPushButton {
                background: #03070F;
                color: #4090C0;
                border: 1px solid #1A3A5A;
                border-radius: 3px;
                padding: 5px 10px;
                font-family: 'Courier New';
                font-size: 9pt;
            }
            QPushButton:hover {
                color: #00E8FF;
                border-color: #00A8D8;
                background: #050F1A;
            }
            QPushButton:pressed { background: #0A1F30; }
        """

        self._save_btn = QPushButton("SAVE TO DESKTOP")
        self._save_btn.setStyleSheet(btn_style)
        self._save_btn.clicked.connect(self._on_save)
        self._save_btn.setEnabled(False)

        self._preview_btn = QPushButton("OPEN IN PREVIEW")
        self._preview_btn.setStyleSheet(btn_style)
        self._preview_btn.clicked.connect(self._on_preview)
        self._preview_btn.setEnabled(False)

        btn_row.addWidget(self._save_btn)
        btn_row.addWidget(self._preview_btn)
        outer.addLayout(btn_row)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_save_callback(self, cb) -> None:
        self._save_cb = cb

    def set_preview_callback(self, cb) -> None:
        self._preview_cb = cb

    def show_image(self, path: Optional[str], prompt: Optional[str],
                   elapsed: Optional[int]) -> None:
        """Called from ImageGenModule thread-safely via QTimer.singleShot."""
        QTimer.singleShot(0, lambda: self._show_image_main(path, prompt, elapsed))

    def _show_image_main(self, path: Optional[str], prompt: Optional[str],
                         elapsed: Optional[int]) -> None:
        if path is None:
            self.hide_panel()
            return

        self._img_path = path
        self._prompt   = prompt
        self._elapsed  = elapsed

        # Load image
        px = QPixmap(path)
        if not px.isNull():
            scaled = px.scaled(
                self._img_label.width() - 4,
                self._img_label.height() - 4,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_label.setPixmap(scaled)
            self._img_label.setText("")

        # Prompt: truncate to keep UI clean
        display_prompt = (prompt or "")[:160]
        if len(prompt or "") > 160:
            display_prompt += "…"
        self._prompt_label.setText(display_prompt)

        # Stats
        stats_parts = []
        if elapsed is not None:
            stats_parts.append(f"Generated in {elapsed}s")
        if path:
            stats_parts.append(Path(path).name)
        self._stats_label.setText("  ·  ".join(stats_parts))

        self._save_btn.setEnabled(True)
        self._preview_btn.setEnabled(True)

        self._position_panel()
        self.show()
        self.raise_()

    def hide_panel(self) -> None:
        self.hide()
        self._img_label.setPixmap(QPixmap())
        self._img_label.setText("No image generated yet.")
        self._save_btn.setEnabled(False)
        self._preview_btn.setEnabled(False)

    # ── Positioning ────────────────────────────────────────────────────────────

    def _position_panel(self) -> None:
        if not self.parent():
            return
        parent_rect = self.parent().rect()
        x = (parent_rect.width()  - self._PANEL_W) // 2
        y = (parent_rect.height() - self._PANEL_H) // 2
        self.move(max(0, x), max(0, y))

    # ── Custom background ─────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Dark semi-transparent card
        painter.setBrush(QBrush(QColor(3, 7, 18, 235)))
        painter.setPen(QPen(QColor(0, 80, 140, 160), 1.5))
        painter.drawRoundedRect(self.rect(), 6, 6)

        # Left glow edge
        painter.setPen(QPen(QColor(0, 160, 255, 80), 2))
        painter.drawLine(1, 20, 1, self.height() - 20)

        painter.end()

    # ── Button handlers ───────────────────────────────────────────────────────

    def _on_save(self) -> None:
        if self._save_cb:
            self._save_cb()

    def _on_preview(self) -> None:
        if self._preview_cb:
            self._preview_cb()
