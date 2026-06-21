"""
ATLAS Feed Manager — Mission Control Data Backend

Drives the TARS-style side panel with live data from:
  - Weather    (Open-Meteo, free, no key needed)
  - System     (psutil — CPU / RAM / battery / network)
  - Spotify    (AppleScript — current track)
  - News       (DuckDuckGo — headlines)
  - Context    (frontmost macOS app via AppleScript)

FeedManager emits Qt signals so the UI thread can update safely.
All background loops run as daemon threads and respect self._active.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

log = logging.getLogger(__name__)

# ── Category colours and icons ────────────────────────────────────────────────

CATEGORY_COLORS: dict[str, str] = {
    "system":   "#4488ff",
    "ai":       "#00eeff",
    "news":     "#c8d8ff",
    "weather":  "#88ccff",
    "music":    "#ffcc44",
    "code":     "#44ff88",
    "reminder": "#ff8844",
    "error":    "#ff4455",
    "voice":    "#00eeff",
    "web":      "#aa88ff",
}

CATEGORY_ICONS: dict[str, str] = {
    "system":   "⚙",
    "ai":       "◈",
    "news":     "◉",
    "weather":  "◎",
    "music":    "♫",
    "code":     "⌨",
    "reminder": "⏰",
    "error":    "⚠",
    "voice":    "◈",
    "web":      "◉",
}


def _wmo_condition(code: int) -> str:
    if code == 0:   return "Clear"
    if code <= 3:   return "Partly Cloudy"
    if code <= 9:   return "Foggy"
    if code <= 29:  return "Rain"
    if code <= 39:  return "Snowing"
    if code <= 49:  return "Foggy"
    if code <= 59:  return "Drizzle"
    if code <= 69:  return "Sleet"
    if code <= 79:  return "Snow"
    if code <= 84:  return "Rain Showers"
    if code <= 94:  return "Thunderstorm"
    return "Severe Storm"


def _wmo_icon(code: int) -> str:
    if code == 0:   return "☀"
    if code <= 3:   return "⛅"
    if code <= 9:   return "🌫"
    if code <= 29:  return "🌧"
    if code <= 39:  return "❄"
    if code <= 49:  return "🌫"
    if code <= 59:  return "🌦"
    if code <= 69:  return "🌨"
    if code <= 79:  return "❄"
    if code <= 84:  return "🌧"
    if code <= 94:  return "⛈"
    return "🌪"


class FeedManager(QObject):
    """
    Central data manager for the ATLAS feed panel.
    Emits Qt signals consumed by FeedPanel (UI thread safe).
    """

    item_added       = pyqtSignal(dict)
    weather_updated  = pyqtSignal(dict)
    stats_updated    = pyqtSignal(dict)
    spotify_updated  = pyqtSignal(dict)
    context_updated  = pyqtSignal(dict)
    news_updated     = pyqtSignal(list)
    obsidian_updated = pyqtSignal(dict)

    def __init__(self, config: dict):
        super().__init__()
        self._cfg    = config
        self._lat    = config.get("location_lat", 51.5)
        self._lon    = config.get("location_lon", -0.1)
        self._active = False

        root = Path(os.environ.get("ATLAS_ROOT", "."))
        self._layout_path    = root / "feed_layout.json"
        self._reminders_path = root / "feed_reminders.json"
        self._layout = self._load_json(self._layout_path, {})

        self._hidden_widgets: set[str] = set(self._layout.get("hidden_widgets", []))
        self._feed_side = self._layout.get("feed_side", "right")
        self._feed_mode = self._layout.get("feed_mode", "full")

        self._reminders: list[dict] = self._load_json(self._reminders_path, [])

        # cached spotify track to detect changes
        self._last_track: str = ""

        # cached net bytes to compute throughput
        self._last_net_recv: float = 0.0

        # obsidian module (injected after construction)
        self._obsidian_mod = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_obsidian_module(self, mod) -> None:
        """Inject the ObsidianModule so the feed can poll vault data."""
        self._obsidian_mod = mod

    def start(self) -> None:
        self._active = True
        threading.Thread(target=self._weather_loop,   daemon=True, name="feed-weather").start()
        threading.Thread(target=self._stats_loop,     daemon=True, name="feed-stats").start()
        threading.Thread(target=self._spotify_loop,   daemon=True, name="feed-spotify").start()
        threading.Thread(target=self._context_loop,   daemon=True, name="feed-context").start()
        threading.Thread(target=self._news_loop,      daemon=True, name="feed-news").start()
        threading.Thread(target=self._obsidian_loop,  daemon=True, name="feed-obsidian").start()
        log.info("FeedManager: all background threads started.")

    def stop(self) -> None:
        self._active = False

    # ── Public API ────────────────────────────────────────────────────────────

    def add_item(self, category: str, content: str) -> None:
        """Push a live feed item — safe to call from any thread."""
        item = {
            "ts":       datetime.now().strftime("%H:%M:%S"),
            "category": category,
            "color":    CATEGORY_COLORS.get(category, "#c8d8ff"),
            "icon":     CATEGORY_ICONS.get(category, "◈"),
            "content":  content,
        }
        self.item_added.emit(item)

    @property
    def feed_side(self) -> str:
        return self._feed_side

    @property
    def feed_mode(self) -> str:
        return self._feed_mode

    def set_feed_side(self, side: str) -> None:
        self._feed_side = side
        self._save_layout()

    def set_feed_mode(self, mode: str) -> None:
        self._feed_mode = mode
        self._save_layout()

    def hide_widget(self, name: str) -> None:
        self._hidden_widgets.add(name)
        self._save_layout()

    def show_widget(self, name: str) -> None:
        self._hidden_widgets.discard(name)
        self._save_layout()

    def is_widget_hidden(self, name: str) -> bool:
        return name in self._hidden_widgets

    # ── Reminders ─────────────────────────────────────────────────────────────

    def add_reminder(self, text: str) -> None:
        self._reminders.append({"text": text, "done": False})
        self._save_json(self._reminders_path, self._reminders)
        self.add_item("reminder", f"Reminder set: {text}")

    def complete_reminder(self, index: int = -1) -> None:
        if not self._reminders:
            return
        idx = index if index >= 0 else next(
            (i for i, r in enumerate(self._reminders) if not r["done"]), None
        )
        if idx is not None and idx < len(self._reminders):
            self._reminders[idx]["done"] = True
            self._save_json(self._reminders_path, self._reminders)

    def get_active_reminders(self) -> list[dict]:
        return [r for r in self._reminders if not r["done"]][:3]

    # ── Background loops ──────────────────────────────────────────────────────

    def _weather_loop(self) -> None:
        while self._active:
            data = self._fetch_weather()
            if data:
                self.weather_updated.emit(data)
                self.add_item("weather", f"{data['condition']} · {data['temp']}° · "
                              f"{data['humidity']}% humidity")
            time.sleep(1800)

    def _stats_loop(self) -> None:
        while self._active:
            data = self._fetch_stats()
            if data:
                self.stats_updated.emit(data)
                # Emit system alert only for extremes
                if data.get("cpu", 0) >= 90:
                    self.add_item("error", f"High CPU usage: {data['cpu']}%")
                if data.get("battery") is not None and data["battery"] <= 10 and not data.get("charging"):
                    self.add_item("error", f"Low battery: {data['battery']}%")
            time.sleep(3)

    def _spotify_loop(self) -> None:
        while self._active:
            data = self._fetch_spotify()
            if data:
                track = data.get("track", "")
                if track and track != self._last_track:
                    self._last_track = track
                    self.spotify_updated.emit(data)
                elif not track and self._last_track:
                    self._last_track = ""
                    self.spotify_updated.emit(data)
            time.sleep(5)

    def _context_loop(self) -> None:
        while self._active:
            data = self._fetch_context()
            if data:
                self.context_updated.emit(data)
            time.sleep(5)

    def _news_loop(self) -> None:
        while self._active:
            headlines = self._fetch_news()
            if headlines:
                self.news_updated.emit(headlines)
                for h in headlines[:3]:
                    self.add_item("news", h)
            time.sleep(1800)

    def _obsidian_loop(self) -> None:
        time.sleep(5)   # let everything start up first
        while self._active:
            if self._obsidian_mod is not None:
                try:
                    data = self._obsidian_mod.get_widget_data()
                    if data.get("ready"):
                        self.obsidian_updated.emit(data)
                except Exception as exc:
                    log.debug("Obsidian feed poll error: %s", exc)
            time.sleep(60)

    # ── Data fetchers ─────────────────────────────────────────────────────────

    def _fetch_weather(self) -> Optional[dict]:
        try:
            import requests
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={self._lat}&longitude={self._lon}"
                f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
                f"&daily=temperature_2m_max,temperature_2m_min,weather_code"
                f"&temperature_unit=celsius&wind_speed_unit=kmh&timezone=auto&forecast_days=2"
            )
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            d   = r.json()
            cur = d.get("current", {})
            day = d.get("daily", {})
            code = cur.get("weather_code", 0)
            return {
                "temp":      round(cur.get("temperature_2m", 0)),
                "humidity":  round(cur.get("relative_humidity_2m", 0)),
                "wind":      round(cur.get("wind_speed_10m", 0)),
                "condition": _wmo_condition(code),
                "icon":      _wmo_icon(code),
                "today_hi":  round((day.get("temperature_2m_max") or [0])[0]),
                "today_lo":  round((day.get("temperature_2m_min") or [0])[0]),
                "tmrw_hi":   round((day.get("temperature_2m_max") or [0, 0])[1]),
                "tmrw_lo":   round((day.get("temperature_2m_min") or [0, 0])[1]),
                "tmrw_cond": _wmo_condition((day.get("weather_code") or [0, 0])[1]),
            }
        except Exception as exc:
            log.debug("Weather fetch: %s", exc)
            return None

    def _fetch_stats(self) -> Optional[dict]:
        try:
            import psutil
            cpu  = psutil.cpu_percent(interval=None)
            ram  = psutil.virtual_memory()
            batt = psutil.sensors_battery()
            net  = psutil.net_io_counters()

            recv_mb = net.bytes_recv / 1e6
            throughput = max(0.0, recv_mb - self._last_net_recv)
            self._last_net_recv = recv_mb

            return {
                "cpu":        round(cpu),
                "ram":        round(ram.percent),
                "ram_used":   round(ram.used / 1e9, 1),
                "ram_total":  round(ram.total / 1e9, 1),
                "battery":    round(batt.percent) if batt else None,
                "charging":   bool(batt.power_plugged) if batt else False,
                "net_recv":   round(recv_mb, 1),
                "throughput": round(throughput, 2),
            }
        except Exception as exc:
            log.debug("Stats fetch: %s", exc)
            return None

    def _fetch_spotify(self) -> Optional[dict]:
        try:
            script = (
                'tell application "System Events"\n'
                '  if exists (process "Spotify") then\n'
                '    tell application "Spotify"\n'
                '      if player state is playing then\n'
                '        return (name of current track) & "|" & (artist of current track)\n'
                '      end if\n'
                '    end tell\n'
                '  end if\n'
                'end tell\n'
                'return ""\n'
            )
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=3,
            )
            out = r.stdout.strip()
            if out and "|" in out:
                track, _, artist = out.partition("|")
                return {"track": track.strip(), "artist": artist.strip(), "playing": True}
            return {"track": "", "artist": "", "playing": False}
        except Exception:
            return None

    def _fetch_context(self) -> Optional[dict]:
        try:
            script = (
                'tell application "System Events"\n'
                '  return name of first process whose frontmost is true\n'
                'end tell\n'
            )
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=3,
            )
            app = r.stdout.strip()
            return {"app": app, "detail": ""}
        except Exception:
            return None

    def _fetch_news(self) -> list[str]:
        try:
            from ddgs import DDGS
            topics  = self._cfg.get("news_topics", ["technology", "world"])
            query   = " OR ".join(topics)
            results = list(DDGS().news(query, max_results=6))
            return [r.get("title", "") for r in results if r.get("title")]
        except Exception as exc:
            log.debug("News fetch: %s", exc)
            return []

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_layout(self) -> None:
        self._save_json(self._layout_path, {
            "hidden_widgets": list(self._hidden_widgets),
            "feed_side":      self._feed_side,
            "feed_mode":      self._feed_mode,
        })

    @staticmethod
    def _load_json(path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return default

    @staticmethod
    def _save_json(path: Path, data) -> None:
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Feed JSON save failed (%s): %s", path.name, exc)
