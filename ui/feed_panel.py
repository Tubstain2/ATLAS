"""
ATLAS Feed Panel — TARS-style side panel widget

Renders ui/feed.html inside a QWebEngineView and forwards live data
from FeedManager via JavaScript calls.  All Python→JS communication
uses page().runJavaScript(); no QWebChannel dependency required.

Requires: pip install PyQt6-WebEngine
Falls back to a plain text placeholder if not installed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QEasingCurve, QPropertyAnimation,
    QParallelAnimationGroup, Qt, QTimer, pyqtProperty,
)
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

log = logging.getLogger(__name__)

_PANEL_WIDTH = 380
_SLIDE_MS    = 300

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    _WEB_OK = True
except ImportError:
    _WEB_OK = False
    log.warning(
        "PyQt6-WebEngine not installed — feed panel degraded. "
        "Run: pip install PyQt6-WebEngine"
    )


class FeedPanel(QWidget):
    """
    380 px-wide slide-in/out feed panel.
    Starts hidden (width=0); call slide_in() or toggle() to show.
    """

    PANEL_WIDTH = _PANEL_WIDTH

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._feed_manager = None
        self._visible      = False
        self._js_ready     = False
        self._pending_js: list[str] = []
        self._anim_group: Optional[QParallelAnimationGroup] = None

        # Start hidden
        self.setFixedWidth(0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if _WEB_OK:
            self._view = QWebEngineView()
            self._view.settings().setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptEnabled, True
            )
            self._view.settings().setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
            )
            html_path = Path(__file__).parent / "feed.html"
            from PyQt6.QtCore import QUrl
            self._view.load(QUrl.fromLocalFile(str(html_path.resolve())))
            self._view.loadFinished.connect(self._on_load_finished)
            layout.addWidget(self._view)
        else:
            lbl = QLabel(
                "FEED PANEL\n\nRequires:\npip install PyQt6-WebEngine\n\nRestart ATLAS after installing."
            )
            lbl.setStyleSheet(
                "color: #00eeff; background: #030310; font-family: monospace; "
                "font-size: 12px; padding: 20px;"
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl)
            self._view = None

    # ── Custom width property for animation ───────────────────────────────────

    def _get_panel_w(self) -> int:
        return self.width()

    def _set_panel_w(self, w: int) -> None:
        self.setFixedWidth(max(0, w))

    panel_w = pyqtProperty(int, _get_panel_w, _set_panel_w)

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_feed_manager(self, manager) -> None:
        self._feed_manager = manager
        if manager is None:
            return
        manager.item_added.connect(self._on_item)
        manager.weather_updated.connect(self._on_weather)
        manager.stats_updated.connect(self._on_stats)
        manager.spotify_updated.connect(self._on_spotify)
        manager.context_updated.connect(self._on_context)
        manager.news_updated.connect(self._on_news)
        manager.obsidian_updated.connect(self._on_obsidian)

        # Push initial reminders once JS is ready
        QTimer.singleShot(2000, self._push_reminders)

    # ── Public API ────────────────────────────────────────────────────────────

    def toggle(self) -> None:
        if self._visible:
            self.slide_out()
        else:
            self.slide_in()

    def slide_in(self) -> None:
        if self._visible:
            return
        self._visible = True
        self._animate(self.width(), _PANEL_WIDTH)

    def slide_out(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._animate(self.width(), 0)

    @property
    def is_panel_visible(self) -> bool:
        return self._visible

    def add_feed_item(self, category: str, content: str) -> None:
        if self._feed_manager:
            self._feed_manager.add_item(category, content)

    def push_atlas_response(self, text: str) -> None:
        """Show ATLAS response with typewriter animation in feed."""
        self._run_js(f"atlasTypewriter({json.dumps(text)})")

    def set_feed_mode(self, mode: str) -> None:
        self._run_js(f"setFeedMode({json.dumps(mode)})")
        if self._feed_manager:
            self._feed_manager.set_feed_mode(mode)

    def clear_feed(self) -> None:
        self._run_js("clearFeed()")

    def hide_widget(self, name: str) -> None:
        self._run_js(f"hideWidget({json.dumps(name)})")
        if self._feed_manager:
            self._feed_manager.hide_widget(name)

    def show_widget(self, name: str) -> None:
        self._run_js(f"showWidget({json.dumps(name)})")
        if self._feed_manager:
            self._feed_manager.show_widget(name)

    def pin_last_item(self) -> None:
        self._run_js("pinLastItem()")

    # ── JS slot receivers ─────────────────────────────────────────────────────

    def _on_item(self, item: dict) -> None:
        self._run_js(f"addFeedItem({json.dumps(item)})")

    def _on_weather(self, data: dict) -> None:
        self._run_js(f"updateWeather({json.dumps(data)})")

    def _on_stats(self, data: dict) -> None:
        self._run_js(f"updateStats({json.dumps(data)})")

    def _on_spotify(self, data: dict) -> None:
        self._run_js(f"updateSpotify({json.dumps(data)})")

    def _on_context(self, data: dict) -> None:
        self._run_js(f"updateContext({json.dumps(data)})")

    def _on_news(self, headlines: list) -> None:
        self._run_js(f"updateNews({json.dumps(headlines)})")

    def _on_obsidian(self, data: dict) -> None:
        self._run_js(f"updateObsidian({json.dumps(data)})")

    def _push_reminders(self) -> None:
        if self._feed_manager:
            reminders = self._feed_manager.get_active_reminders()
            self._run_js(f"updateReminders({json.dumps(reminders)})")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self._js_ready = True
            for js in self._pending_js:
                self._view.page().runJavaScript(js)
            self._pending_js.clear()
            log.info("Feed panel HTML loaded successfully.")
        else:
            log.error("Feed panel HTML failed to load.")

    def _run_js(self, js: str) -> None:
        if not _WEB_OK or self._view is None:
            return
        if self._js_ready:
            self._view.page().runJavaScript(js)
        else:
            self._pending_js.append(js)

    def _animate(self, from_w: int, to_w: int) -> None:
        if self._anim_group and self._anim_group.state() == QParallelAnimationGroup.State.Running:
            self._anim_group.stop()

        def _make_anim(prop: bytes) -> QPropertyAnimation:
            a = QPropertyAnimation(self, prop)
            a.setStartValue(from_w)
            a.setEndValue(to_w)
            a.setDuration(_SLIDE_MS)
            a.setEasingCurve(QEasingCurve.Type.InOutCubic)
            return a

        self._anim_group = QParallelAnimationGroup()
        self._anim_group.addAnimation(_make_anim(b"panel_w"))
        self._anim_group.start()
