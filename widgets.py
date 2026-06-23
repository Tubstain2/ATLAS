"""
ATLAS Widget Dashboard — Module 4

A toggleable floating dashboard with 7 modular, draggable widgets:
  ClockWidget          Large digital clock + date
  WeatherWidget        Open-Meteo API (free, no key)
  SystemStatsWidget    CPU / RAM / battery / disk via psutil
  NewsWidget           DuckDuckGo headlines (scrolling ticker)
  CryptoStocksWidget   CoinGecko + yfinance price feeds
  RemindersWidget      Plain text task list, Obsidian-compatible
  DailyBriefingWidget  Morning briefing with all of the above

Voice commands (handled in ClaudeBrain._handle_meta or DashboardWindow.handle()):
  "ATLAS show dashboard"     → show()
  "ATLAS hide dashboard"     → hide()
  "ATLAS morning briefing"   → trigger briefing
  "ATLAS add reminder X"     → add reminder
  "ATLAS mark that done"     → tick last reminder

Layout is saved to dashboard_layout.json between sessions.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Colour palette (matches ATLAS dark theme) ─────────────────────────────────
_BG      = "#0a0a1a"
_CARD    = "#111128"
_ACCENT  = "#0055FF"
_TEXT    = "#e0e8ff"
_DIM     = "#4455aa"
_GREEN   = "#00cc66"
_RED     = "#ff4455"
_YELLOW  = "#ffcc00"
_FONT    = "Share Tech Mono"

_STYLE_CARD = f"""
    background: {_CARD};
    border: 1px solid {_DIM};
    border-radius: 10px;
    color: {_TEXT};
    font-family: '{_FONT}', 'Menlo';
"""


def _label(text: str = "", size: int = 13, color: str = _TEXT, bold: bool = False) -> "QLabel":
    from PyQt6.QtWidgets import QLabel
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(
        f"color: {color}; font-family: '{_FONT}', 'Menlo'; "
        f"font-size: {size}px; font-weight: {weight}; background: transparent;"
    )
    lbl.setWordWrap(True)
    return lbl


# ══════════════════════════════════════════════════════════════════════════════
# Base widget
# ══════════════════════════════════════════════════════════════════════════════

class _BaseWidget:
    """Mixin providing update_interval (seconds) and a start() method."""
    update_interval: int = 60   # override in subclasses

    def start(self) -> None:
        """Start periodic refresh on a background daemon thread."""
        if self.update_interval <= 0:
            return
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self) -> None:
        import time
        while True:
            try:
                self.refresh()
            except RuntimeError:
                break  # Qt C++ object deleted — widget destroyed, stop loop
            except Exception as exc:
                log.warning("%s refresh error: %s", type(self).__name__, exc)
            time.sleep(self.update_interval)

    def refresh(self) -> None:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Clock widget
# ══════════════════════════════════════════════════════════════════════════════

class ClockWidget(_BaseWidget):
    update_interval = 1

    def __init__(self):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout
        from PyQt6.QtCore    import Qt

        self.widget = QWidget()
        self.widget.setStyleSheet(_STYLE_CARD)
        self.widget.setFixedWidth(260)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(16, 12, 16, 12)

        self._time_lbl = _label("00:00:00", size=32, color=_ACCENT, bold=True)
        self._time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._date_lbl = _label("", size=13, color=_DIM)
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self._time_lbl)
        layout.addWidget(self._date_lbl)
        self.refresh()

    def refresh(self) -> None:
        now = datetime.now()
        self._safe_set(self._time_lbl, now.strftime("%H:%M:%S"))
        self._safe_set(self._date_lbl, now.strftime("%A, %B %d %Y"))

    @staticmethod
    def _safe_set(label, text: str) -> None:
        try:
            from PyQt6.QtCore import QMetaObject, Qt
            label.setProperty("_text", text)
            QMetaObject.invokeMethod(label, "setText",
                                     Qt.ConnectionType.QueuedConnection,
                                     *_q_args(text))
        except Exception:
            try:
                label.setText(text)
            except Exception:
                pass


def _q_args(text: str):
    from PyQt6.QtCore import Q_ARG
    return (Q_ARG(str, text),)


# ══════════════════════════════════════════════════════════════════════════════
# Weather widget — Open-Meteo (free, no key)
# ══════════════════════════════════════════════════════════════════════════════

_WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Showers", 82: "Heavy showers",
    95: "Thunderstorm", 99: "Thunderstorm with hail",
}


class WeatherWidget(_BaseWidget):
    update_interval = 1800  # 30 min

    def __init__(self, lat: float = 51.5, lon: float = -0.1):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout

        self._lat = lat
        self._lon = lon
        self.widget = QWidget()
        self.widget.setStyleSheet(_STYLE_CARD)
        self.widget.setFixedWidth(260)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(16, 12, 16, 12)

        self._title = _label("WEATHER", size=10, color=_DIM, bold=True)
        self._temp  = _label("--°C", size=28, color=_ACCENT, bold=True)
        self._cond  = _label("Loading...", size=13, color=_TEXT)
        self._extra = _label("", size=11, color=_DIM)

        layout.addWidget(self._title)
        layout.addWidget(self._temp)
        layout.addWidget(self._cond)
        layout.addWidget(self._extra)

    def refresh(self) -> None:
        try:
            import requests
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={self._lat}&longitude={self._lon}"
                f"&current=temperature_2m,weathercode,relative_humidity_2m,windspeed_10m"
                f"&temperature_unit=celsius&windspeed_unit=kmh"
            )
            r    = requests.get(url, timeout=10)
            data = r.json().get("current", {})
            temp = data.get("temperature_2m", "--")
            code = data.get("weathercode", 0)
            hum  = data.get("relative_humidity_2m", "--")
            wind = data.get("windspeed_10m", "--")
            cond = _WMO_CODES.get(code, "Unknown")

            self._temp.setText(f"{temp}°C")
            self._cond.setText(cond)
            self._extra.setText(f"Humidity {hum}%  ·  Wind {wind} km/h")
        except Exception as exc:
            log.warning("Weather fetch error: %s", exc)
            self._cond.setText("Weather unavailable")


# ══════════════════════════════════════════════════════════════════════════════
# System stats widget — psutil
# ══════════════════════════════════════════════════════════════════════════════

class SystemStatsWidget(_BaseWidget):
    update_interval = 5

    def __init__(self):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout, QGridLayout

        self.widget = QWidget()
        self.widget.setStyleSheet(_STYLE_CARD)
        self.widget.setFixedWidth(260)

        outer = QVBoxLayout(self.widget)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.addWidget(_label("SYSTEM", size=10, color=_DIM, bold=True))

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        outer.addLayout(grid)

        self._cpu  = _label("--", size=16, color=_GREEN, bold=True)
        self._ram  = _label("--", size=16, color=_GREEN, bold=True)
        self._disk = _label("--", size=16, color=_TEXT,  bold=True)
        self._bat  = _label("--", size=16, color=_YELLOW,bold=True)

        grid.addWidget(_label("CPU",  size=11, color=_DIM), 0, 0)
        grid.addWidget(self._cpu,  0, 1)
        grid.addWidget(_label("RAM",  size=11, color=_DIM), 1, 0)
        grid.addWidget(self._ram,  1, 1)
        grid.addWidget(_label("Disk", size=11, color=_DIM), 2, 0)
        grid.addWidget(self._disk, 2, 1)
        grid.addWidget(_label("Bat",  size=11, color=_DIM), 3, 0)
        grid.addWidget(self._bat,  3, 1)

        self.refresh()

    def refresh(self) -> None:
        try:
            import psutil
            cpu   = psutil.cpu_percent(interval=0)
            ram   = psutil.virtual_memory().percent
            disk  = psutil.disk_usage("/").percent
            bat   = psutil.sensors_battery()
            bat_s = f"{bat.percent:.0f}%" if bat else "N/A"
            cpu_c = _GREEN if cpu < 70 else _RED
            ram_c = _GREEN if ram < 80 else _RED
            self._cpu.setText(f"{cpu:.0f}%")
            self._cpu.setStyleSheet(
                f"color:{cpu_c};font-family:'{_FONT}','Menlo';font-size:16px;font-weight:bold;background:transparent;")
            self._ram.setText(f"{ram:.0f}%")
            self._ram.setStyleSheet(
                f"color:{ram_c};font-family:'{_FONT}','Menlo';font-size:16px;font-weight:bold;background:transparent;")
            self._disk.setText(f"{disk:.0f}%")
            self._bat.setText(bat_s)
        except ImportError:
            self._cpu.setText("psutil needed")
        except Exception as exc:
            log.warning("Stats refresh error: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# News widget — DuckDuckGo
# ══════════════════════════════════════════════════════════════════════════════

class NewsWidget(_BaseWidget):
    update_interval = 1800  # 30 min

    def __init__(self, topics: list[str] | None = None):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout

        self._topics  = topics or ["technology", "world"]
        self._headlines: list[str] = []

        self.widget = QWidget()
        self.widget.setStyleSheet(_STYLE_CARD)
        self.widget.setFixedWidth(260)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addWidget(_label("NEWS", size=10, color=_DIM, bold=True))

        self._labels: list = []
        for _ in range(5):
            lbl = _label("", size=11, color=_TEXT)
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            self._labels.append(lbl)

        self.refresh()

    def refresh(self) -> None:
        try:
            from ddgs import DDGS
            headlines: list[str] = []
            with DDGS() as ddgs:
                for topic in self._topics[:2]:
                    for r in ddgs.news(topic, max_results=3):
                        headlines.append(r.get("title", ""))
                        if len(headlines) >= 5:
                            break
                    if len(headlines) >= 5:
                        break
            self._headlines = headlines[:5]
            for i, lbl in enumerate(self._labels):
                txt = f"• {self._headlines[i]}" if i < len(self._headlines) else ""
                lbl.setText(txt)
        except Exception as exc:
            log.warning("News fetch error: %s", exc)
            self._labels[0].setText("News unavailable")

    def get_headline(self, index: int = 0) -> str:
        if self._headlines and index < len(self._headlines):
            return self._headlines[index]
        return "No headlines loaded."


# ══════════════════════════════════════════════════════════════════════════════
# Crypto + Stocks widget
# ══════════════════════════════════════════════════════════════════════════════

class CryptoStocksWidget(_BaseWidget):
    update_interval = 60

    def __init__(self, cryptos: list[str] | None = None, stocks: list[str] | None = None):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout, QGridLayout

        self._cryptos = cryptos or ["bitcoin", "ethereum"]
        self._stocks  = stocks  or ["AAPL", "TSLA"]

        self.widget = QWidget()
        self.widget.setStyleSheet(_STYLE_CARD)
        self.widget.setFixedWidth(260)

        outer = QVBoxLayout(self.widget)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.addWidget(_label("CRYPTO · STOCKS", size=10, color=_DIM, bold=True))

        self._grid   = QGridLayout()
        self._rows: list[tuple] = []  # (name_lbl, price_lbl, change_lbl)
        outer.addLayout(self._grid)

        items = self._cryptos[:2] + self._stocks[:2]
        for i, sym in enumerate(items):
            n = _label(sym.upper(), size=11, color=_DIM)
            p = _label("--", size=13, color=_TEXT, bold=True)
            c = _label("", size=11, color=_DIM)
            self._grid.addWidget(n, i, 0)
            self._grid.addWidget(p, i, 1)
            self._grid.addWidget(c, i, 2)
            self._rows.append((n, p, c))

    def refresh(self) -> None:
        self._fetch_crypto()
        self._fetch_stocks()

    def _fetch_crypto(self) -> None:
        try:
            import requests
            ids  = ",".join(self._cryptos[:2])
            url  = (
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={ids}&vs_currencies=usd&include_24hr_change=true"
            )
            data = requests.get(url, timeout=10).json()
            for i, sym in enumerate(self._cryptos[:2]):
                info = data.get(sym, {})
                price  = info.get("usd", 0)
                change = info.get("usd_24h_change", 0)
                price_s  = f"${price:,.0f}" if price > 100 else f"${price:.4f}"
                change_s = f"{change:+.1f}%"
                color    = _GREEN if change >= 0 else _RED
                if i < len(self._rows):
                    _, p, c = self._rows[i]
                    p.setText(price_s)
                    c.setText(change_s)
                    c.setStyleSheet(
                        f"color:{color};font-family:'{_FONT}','Menlo';"
                        f"font-size:11px;background:transparent;")
        except Exception as exc:
            log.warning("Crypto fetch error: %s", exc)

    def _fetch_stocks(self) -> None:
        try:
            import yfinance as yf
            offset = len(self._cryptos[:2])
            for j, sym in enumerate(self._stocks[:2]):
                ticker  = yf.Ticker(sym)
                info    = ticker.fast_info
                price   = getattr(info, "last_price", None) or 0
                prev    = getattr(info, "previous_close", None) or price
                change  = ((price - prev) / prev * 100) if prev else 0
                price_s  = f"${price:.2f}"
                change_s = f"{change:+.1f}%"
                color    = _GREEN if change >= 0 else _RED
                idx      = offset + j
                if idx < len(self._rows):
                    _, p, c = self._rows[idx]
                    p.setText(price_s)
                    c.setText(change_s)
                    c.setStyleSheet(
                        f"color:{color};font-family:'{_FONT}','Menlo';"
                        f"font-size:11px;background:transparent;")
        except Exception as exc:
            log.warning("Stocks fetch error: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# Reminders widget
# ══════════════════════════════════════════════════════════════════════════════

class RemindersWidget(_BaseWidget):
    update_interval = 0   # manual only

    def __init__(self, obsidian_path: str | None = None):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout

        self._reminders: list[dict] = []   # {"text": ..., "done": bool}
        self._obsidian  = Path(obsidian_path) if obsidian_path else None

        self.widget = QWidget()
        self.widget.setStyleSheet(_STYLE_CARD)
        self.widget.setFixedWidth(260)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addWidget(_label("REMINDERS", size=10, color=_DIM, bold=True))

        self._labels: list = []
        for _ in range(6):
            lbl = _label("", size=11, color=_TEXT)
            layout.addWidget(lbl)
            self._labels.append(lbl)

        self._load()
        self._render()

    def add(self, text: str) -> str:
        self._reminders.append({"text": text, "done": False})
        self._save()
        self._render()
        return f"Reminder added: {text}"

    def mark_done(self, index: int = -1) -> str:
        pending = [i for i, r in enumerate(self._reminders) if not r["done"]]
        if not pending:
            return "No pending reminders."
        idx = pending[index] if index < 0 else (pending[index] if index < len(pending) else pending[-1])
        self._reminders[idx]["done"] = True
        self._save()
        self._render()
        return f"Marked done: {self._reminders[idx]['text']}"

    def _render(self) -> None:
        pending = [r for r in self._reminders if not r["done"]]
        for i, lbl in enumerate(self._labels):
            if i < len(pending):
                lbl.setText(f"○ {pending[i]['text']}")
                lbl.setStyleSheet(
                    f"color:{_TEXT};font-family:'{_FONT}','Menlo';font-size:11px;background:transparent;")
            else:
                lbl.setText("")

    def _save(self) -> None:
        try:
            root = Path(os.environ.get("ATLAS_ROOT", "."))
            path = root / "reminders.json"
            path.write_text(json.dumps(self._reminders, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Reminders save failed: %s", exc)

    def _load(self) -> None:
        try:
            root = Path(os.environ.get("ATLAS_ROOT", "."))
            path = root / "reminders.json"
            if path.exists():
                self._reminders = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._reminders = []


# ══════════════════════════════════════════════════════════════════════════════
# Daily briefing widget
# ══════════════════════════════════════════════════════════════════════════════

class DailyBriefingWidget(_BaseWidget):
    update_interval = 0

    def __init__(self, config: dict, weather: WeatherWidget, news: NewsWidget):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout

        self._config  = config
        self._weather = weather
        self._news    = news

        self.widget = QWidget()
        self.widget.setStyleSheet(_STYLE_CARD)
        self.widget.setFixedWidth(540)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.addWidget(_label("DAILY BRIEFING", size=10, color=_DIM, bold=True))

        self._content = _label("Press 'Morning Briefing' to start.", size=12, color=_TEXT)
        self._content.setWordWrap(True)
        layout.addWidget(self._content)

    def generate(self) -> str:
        """Build and display the full morning briefing. Returns text for TTS."""
        now       = datetime.now()
        user_name = self._config.get("user_name", "Boss")
        greeting  = self._time_greeting(now.hour)

        parts: list[str] = [
            f"{greeting}, {user_name}.",
            f"Today is {now.strftime('%A, %B %d %Y')} and the time is {now.strftime('%I:%M %p')}.",
        ]

        # Weather
        try:
            self._weather.refresh()
            w_txt = self._weather._cond.text()
            w_tmp = self._weather._temp.text()
            w_ext = self._weather._extra.text()
            parts.append(f"Outside it is {w_tmp}, {w_txt}. {w_ext}.")
        except Exception:
            parts.append("Weather data is unavailable right now.")

        # News
        try:
            self._news.refresh()
            headlines = self._news._headlines[:3]
            if headlines:
                parts.append("Top headlines: " + "; ".join(headlines) + ".")
        except Exception:
            pass

        # Motivational quote
        try:
            import requests
            r     = requests.get("https://api.quotable.io/random", timeout=5)
            quote = r.json()
            parts.append(
                f"And your quote for today from {quote.get('author','')}: "
                f"{quote.get('content','')}."
            )
        except Exception:
            pass

        briefing = " ".join(parts)
        self._content.setText(briefing)
        return briefing

    @staticmethod
    def _time_greeting(hour: int) -> str:
        if hour < 12:
            return "Good morning"
        if hour < 17:
            return "Good afternoon"
        return "Good evening"


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard window — toggleable floating panel
# ══════════════════════════════════════════════════════════════════════════════

class DashboardWindow:
    """
    Floating, toggleable dashboard window containing all widgets.

    Wire-up in main.py:
        dash = DashboardWindow(config)
        dash.show()

    Voice commands fed through ClaudeBrain → handle(text).
    """

    _LAYOUT_FILE = "dashboard_layout.json"

    def __init__(self, config: dict):
        from PyQt6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QPushButton
        )
        from PyQt6.QtCore import Qt

        self._config = config
        api_cfg      = config.get("api", {})
        lat          = float(config.get("location_lat", 51.5))
        lon          = float(config.get("location_lon", -0.1))
        cryptos      = api_cfg.get("tracked_crypto",  ["bitcoin", "ethereum"])
        stocks       = api_cfg.get("tracked_stocks",   ["AAPL", "TSLA"])
        topics       = config.get("news_topics",       ["technology", "world"])
        obsidian     = config.get("obsidian_tasks_path", None)

        # Build all widget instances
        self._clock    = ClockWidget()
        self._weather  = WeatherWidget(lat, lon)
        self._stats    = SystemStatsWidget()
        self._news     = NewsWidget(topics)
        self._crypto   = CryptoStocksWidget(cryptos, stocks)
        self._remind   = RemindersWidget(obsidian)
        self._briefing = DailyBriefingWidget(config, self._weather, self._news)

        # Main window
        self._win = QWidget()
        self._win.setWindowTitle("ATLAS Dashboard")
        self._win.setStyleSheet(f"background: {_BG}; border: none;")
        self._win.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self._win.resize(580, 900)

        # Scroll area for widget column
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background: transparent;")

        container = QWidget()
        container.setStyleSheet(f"background: {_BG};")
        col = QVBoxLayout(container)
        col.setContentsMargins(12, 12, 12, 12)
        col.setSpacing(10)

        # Row 1: clock + weather side by side
        row1 = QHBoxLayout()
        row1.addWidget(self._clock.widget)
        row1.addWidget(self._weather.widget)
        col.addLayout(row1)

        # Row 2: briefing full width
        col.addWidget(self._briefing.widget)

        # Row 3: stats + crypto/stocks
        row3 = QHBoxLayout()
        row3.addWidget(self._stats.widget)
        row3.addWidget(self._crypto.widget)
        col.addLayout(row3)

        # Row 4: news full width
        col.addWidget(self._news.widget)

        # Row 5: reminders full width
        col.addWidget(self._remind.widget)

        col.addStretch(1)

        scroll.setWidget(container)

        # Close button
        close_btn = QPushButton("✕  Hide")
        close_btn.setFixedHeight(32)
        close_btn.setStyleSheet(
            f"background: {_CARD}; color: {_DIM}; border: 1px solid {_DIM}; "
            f"border-radius: 6px; font-family: '{_FONT}', 'Menlo'; font-size: 11px;"
        )
        close_btn.clicked.connect(self.hide)

        outer = QVBoxLayout(self._win)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        outer.addWidget(close_btn)

        # Position to the right of the screen
        self._position_window()
        self._load_layout()

        # Start all periodic updaters
        for w in (self._clock, self._weather, self._stats,
                  self._news, self._crypto):
            w.start()

        log.info("DashboardWindow ready.")

    # ── Visibility ────────────────────────────────────────────────────────────

    def show(self) -> None:
        self._win.show()
        self._win.raise_()

    def hide(self) -> None:
        self._win.hide()

    def toggle(self) -> None:
        if self._win.isVisible():
            self.hide()
        else:
            self.show()

    def is_visible(self) -> bool:
        return self._win.isVisible()

    # ── Voice command handler ─────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        """Return response string if text is a dashboard command, else None."""
        lower = text.lower().strip()

        if any(p in lower for p in ("show dashboard", "open dashboard", "dashboard on")):
            self.show()
            return "Dashboard is now visible."

        if any(p in lower for p in ("hide dashboard", "close dashboard", "dashboard off")):
            self.hide()
            return "Dashboard hidden."

        if any(p in lower for p in ("morning briefing", "daily briefing", "give me a briefing")):
            briefing = self._briefing.generate()
            self.show()
            return briefing

        if lower.startswith("atlas add reminder ") or lower.startswith("add reminder "):
            item = text.split("reminder", 1)[-1].strip().lstrip(":")
            return self._remind.add(item)

        if any(p in lower for p in ("mark that done", "done with that", "tick that off",
                                     "check that off", "mark done")):
            return self._remind.mark_done(-1)

        if any(p in lower for p in ("read that headline", "what was that headline",
                                     "tell me more")):
            return self._news.get_headline(0)

        if any(p in lower for p in ("reset dashboard", "default dashboard")):
            self._save_layout({})
            return "Dashboard reset to default layout."

        return None

    # ── Layout persistence ────────────────────────────────────────────────────

    def _position_window(self) -> None:
        try:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen().availableGeometry()
            self._win.move(screen.width() - 610, 40)
        except Exception:
            pass

    def _load_layout(self) -> None:
        try:
            root = Path(os.environ.get("ATLAS_ROOT", "."))
            path = root / self._LAYOUT_FILE
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if "x" in data and "y" in data:
                    self._win.move(data["x"], data["y"])
        except Exception:
            pass

    def _save_layout(self, data: dict | None = None) -> None:
        try:
            root = Path(os.environ.get("ATLAS_ROOT", "."))
            path = root / self._LAYOUT_FILE
            if data is None:
                pos  = self._win.pos()
                data = {"x": pos.x(), "y": pos.y()}
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Layout save failed: %s", exc)

    def closeEvent(self, event) -> None:
        self._save_layout()
        event.accept()
