"""
ATLAS Smart Card — floating glassmorphism visualizer widget.

Auto-appears whenever ATLAS detects visual content:
  products, stocks, news, comparisons, recipes, weather, media, lists

Architecture:
  SmartCardManager    → orchestrates up to 3 cards, voice commands
  SmartCardWindow     → single floating card (native title bar + QWebEngineView)
  ContentClassifier   → keyword-based content detection (zero AI calls, zero latency)
  CardDataBuilder     → parses AI text into structured card JSON
  ImageFetcher        → async DuckDuckGo image download + base64 cache

Constraints:
  • Never modifies core.py or voice.py
  • All heavy work on background threads; only QTimer.singleShot() posts to Qt
  • Cards never block the voice response — shown simultaneously with speech

Voice commands:
  "ATLAS show that visually"    → force card for last response
  "ATLAS hide that"             → dismiss top card
  "ATLAS clear all cards"       → dismiss all cards
  "ATLAS keep that up"          → pin top card (no auto-dismiss)
  "ATLAS tell me more about N"  → elaborate on item N from current card
  "ATLAS open number N"         → trigger action button on item N
  "ATLAS compare those"         → switch list card to comparison template
  "ATLAS save that to Obsidian" → save card content to vault
  "ATLAS visualize this"        → force card for any response type
  "ATLAS chart that"            → build mini chart if response has numbers
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, QRect, QPoint, QUrl, pyqtSignal, QObject, QEvent
from PyQt6.QtGui import QColor, QFont, QCursor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QApplication, QSizePolicy,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage

log = logging.getLogger(__name__)

_CARD_WIDTH        = 420
_CARD_MAX_HEIGHT   = 680
_CARD_MARGIN       = 20
_CARD_STACK_GAP    = 12
_HEADER_HEIGHT     = 52

# ── Content Classifier ────────────────────────────────────────────────────────

class ContentClassifier:
    """
    Detects the card template type from an AI response.
    Pure keyword + structure matching — zero latency, no AI call.
    """

    _NUMBERED = re.compile(r'^\s*\d+[.)]\s+\S', re.MULTILINE)
    _PRICE    = re.compile(r'\$[\d,]+\.?\d*')
    _TICKER   = re.compile(r'\b[A-Z]{2,5}\b')
    _TEMP     = re.compile(r'\d+\s*°\s*[CF]\b')

    _STOCK_KW    = {'stock', 'share', 'nasdaq', 'nyse', 'ticker', 'earnings',
                    'market cap', 'dividend', 'crypto', 'bitcoin', 'ethereum',
                    'btc', 'eth', 'trading at', 'price target', 'analyst', 'p/e'}
    _WEATHER_KW  = {'weather', 'forecast', 'humidity', 'precipitation', 'uv index',
                    'celsius', 'fahrenheit', 'cloudy', 'sunny', 'rainy', 'wind speed',
                    'monday', 'tuesday', 'wednesday', 'thursday', 'friday'}
    _RECIPE_KW   = {'ingredient', 'tablespoon', 'teaspoon', 'cup of', 'grams', 'ounces',
                    'preheat', 'bake', 'simmer', 'boil', 'recipe', 'serving', 'calories',
                    'preparation', 'cook time', 'stir', 'mix', 'chop'}
    _COMPARE_KW  = {' vs ', ' versus ', 'compared to', 'comparison', 'pros and cons',
                    'pros:', 'cons:', 'better than', 'difference between', 'which is better'}
    _NEWS_KW     = {'headline', 'breaking', 'reuters', 'bloomberg', 'techcrunch', 'bbc',
                    'hours ago', 'minutes ago', 'reported', 'according to', 'announced today'}
    _MEDIA_KW    = {'movie', 'film', 'television', 'series', 'episode', 'game', 'album',
                    'director', 'studio', 'release date', 'imdb', 'metacritic', 'steam',
                    'rotten tomatoes', 'playstation', 'xbox', 'nintendo'}
    _PRODUCT_KW  = {'laptop', 'smartphone', 'headphone', 'tablet', 'monitor', 'keyboard',
                    'graphics card', 'processor', 'camera', 'speaker', 'specifications',
                    'best laptop', 'recommended', 'top pick', 'review', 'benchmark',
                    'gaming', 'rtx', 'gtx', 'gpu', 'ram', 'ssd', 'storage', 'display',
                    'refresh rate', 'performance', 'budget', 'option', 'recommendation',
                    'under $', 'priced at', 'costs', 'starting at', 'gigabyte', 'asus',
                    'razer', 'msi', 'alienware', 'lenovo legion', 'dell xps', 'macbook'}

    def classify(self, text: str) -> Optional[str]:
        lower = text.lower()
        if self._is_weather(lower, text): return 'weather'
        if self._is_stock(lower, text):   return 'stock'
        if self._is_recipe(lower):        return 'recipe'
        if self._is_comparison(lower):    return 'comparison'
        if self._is_news(lower):          return 'news'
        if self._is_media(lower):         return 'media'
        if self._is_product(lower, text): return 'product'
        if self._is_list(text):           return 'list'
        return None

    def _score(self, lower: str, kw_set: set) -> int:
        return sum(1 for kw in kw_set if kw in lower)

    def _is_stock(self, lower: str, orig: str) -> bool:
        score = self._score(lower, self._STOCK_KW)
        has_price = bool(self._PRICE.search(orig))
        return score >= 2 or (score >= 1 and has_price and self._TICKER.search(orig))

    def _is_weather(self, lower: str, orig: str) -> bool:
        score = self._score(lower, self._WEATHER_KW)
        return score >= 2 or (score >= 1 and bool(self._TEMP.search(orig)))

    def _is_recipe(self, lower: str) -> bool:
        return self._score(lower, self._RECIPE_KW) >= 3

    def _is_comparison(self, lower: str) -> bool:
        return any(kw in lower for kw in self._COMPARE_KW)

    def _is_news(self, lower: str) -> bool:
        return self._score(lower, self._NEWS_KW) >= 2

    def _is_media(self, lower: str) -> bool:
        return self._score(lower, self._MEDIA_KW) >= 2

    def _is_product(self, lower: str, orig: str) -> bool:
        score = self._score(lower, self._PRODUCT_KW)
        return score >= 2 or (score >= 1 and bool(self._PRICE.search(orig)))

    def _is_list(self, text: str) -> bool:
        return len(self._NUMBERED.findall(text)) >= 3

    def should_show_card(self, text: str) -> bool:
        """Quick check: is this worth showing at all?"""
        lower = text.lower()
        word_count = len(text.split())

        # Always skip very short responses
        if word_count < 15:
            return False

        # Conversational one-liners — skip
        skip_starts = ("sure", "okay", "yes,", "no,", "i'll", "of course",
                       "got it", "done.", "done!", "understood", "alright")
        if any(lower.strip().startswith(s) for s in skip_starts) and word_count < 25:
            return False

        # Data-dense templates can be short (stock quotes, weather readings)
        template = self.classify(text)
        if template in ('stock', 'weather', 'recipe', 'comparison'):
            return True

        # Any matched template with enough content
        if template is not None:
            return True

        # Fallback: long responses (50+ words) always get a list card even if
        # no specific template matched — better to show something than nothing
        return word_count >= 50


# ── Card Data Builder ──────────────────────────────────────────────────────────

class CardDataBuilder:
    """Parses AI response text into structured card JSON for the HTML renderer."""

    _ICONS = {
        'product': '🛍', 'stock': '📈', 'news': '📰',
        'comparison': '⚖️', 'list': '💡', 'weather': '⛅',
        'media': '🎬', 'recipe': '🍳',
    }
    _TITLES = {
        'product': 'Products', 'stock': 'Market Data', 'news': 'Top Headlines',
        'comparison': 'Comparison', 'list': 'Results', 'weather': 'Weather Forecast',
        'media': 'Recommendations', 'recipe': 'Recipe',
    }

    _NUMBERED_RE  = re.compile(
        r'^\s*(\d+)[.)]\s+(.+?)(?:\n(?!\s*\d+[.)]\s)(.+?))*(?=\n\s*\d+[.)]\s|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    _PRICE_RE     = re.compile(r'\$\s*([\d,]+\.?\d*)')
    _RATING_RE    = re.compile(r'(\d\.?\d?)\s*/\s*5|(\d\.?\d?)\s*★|★+')
    _PERCENT_RE   = re.compile(r'([+-]?\d+\.?\d*)\s*%')
    _TICKER_RE    = re.compile(r'\b([A-Z]{1,5})\b')

    def build(self, text: str, template: str,
              query: str = '', dismiss_secs: int = 30) -> dict:
        builder = getattr(self, f'_build_{template}', self._build_list)
        data = builder(text, query)
        data.setdefault('title', self._TITLES.get(template, 'ATLAS'))
        data['icon']             = self._ICONS.get(template, '💡')
        data['template']         = template
        data['auto_dismiss_secs']= dismiss_secs
        return data

    # ── Template builders ──────────────────────────────────────────────────────

    def _build_list(self, text: str, query: str = '') -> dict:
        items = []
        # Try structured numbered list
        for m in self._NUMBERED_RE.finditer(text):
            raw = m.group(0)
            lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
            title  = re.sub(r'^\d+[.)]\s*', '', lines[0]).strip()
            detail = ' '.join(lines[1:])[:120] if len(lines) > 1 else ''
            if title:
                items.append({'title': title, 'detail': detail,
                               'image_query': f"{title}"})

        # Fallback: bullet points or dashes
        if not items:
            for line in text.splitlines():
                line = re.sub(r'^[-•*]\s+', '', line.strip())
                if len(line) > 5:
                    items.append({'title': line, 'detail': '', 'image_query': line})

        # Extract title from first non-list line
        first_line = text.strip().splitlines()[0] if text.strip() else ''
        title = re.sub(r'^\d+[.)]\s*', '', first_line)[:60] or 'Results'

        return {'items': items[:8], 'title': title or 'Results'}

    def _build_product(self, text: str, query: str = '') -> dict:
        items = self._build_list(text, query)['items']
        # Enrich with price and rating if detectable
        for item in items:
            # Look for price near item title in full text
            pattern = re.escape(item['title'][:20])
            ctx_match = re.search(pattern + r'.{0,200}', text, re.IGNORECASE | re.DOTALL)
            ctx = ctx_match.group(0) if ctx_match else text[:300]
            price_m  = self._PRICE_RE.search(ctx)
            rating_m = self._RATING_RE.search(ctx)
            pct_m    = self._PERCENT_RE.search(ctx)
            item['price']       = f"${price_m.group(1)}" if price_m else ''
            item['rating']      = float(rating_m.group(1) or rating_m.group(2) or 4.0) \
                                   if rating_m else 0.0
            item['price_change']= f"{pct_m.group(1)}%" if pct_m else ''
            item['image_query'] = f"{item['title']} official product"

        first_line = text.strip().splitlines()[0][:60]
        return {'items': items[:6], 'title': first_line or 'Products'}

    def _build_stock(self, text: str, query: str = '') -> dict:
        items = []
        tickers = self._TICKER_RE.findall(text)
        prices  = self._PRICE_RE.findall(text)
        changes = self._PERCENT_RE.findall(text)

        # Try line-by-line parsing for structured stock output
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            t_m = self._TICKER_RE.search(line)
            p_m = self._PRICE_RE.search(line)
            c_m = self._PERCENT_RE.search(line)
            if t_m and (p_m or c_m):
                ticker = t_m.group(1)
                if ticker in ('A', 'I', 'AT', 'UP', 'OR', 'IS', 'OF', 'BY', 'ON', 'TO'):
                    continue
                items.append({
                    'ticker':  ticker,
                    'price':   f"${p_m.group(1)}" if p_m else 'N/A',
                    'change':  f"{c_m.group(1)}%" if c_m else '0%',
                    'up':      c_m and not c_m.group(1).startswith('-') if c_m else True,
                    'name':    '',
                })

        # Fallback: pair tickers with prices in order
        if not items and tickers and prices:
            common = {'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL',
                      'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'OUT', 'DAY', 'GET'}
            clean_tickers = [t for t in dict.fromkeys(tickers)
                             if t not in common and 2 <= len(t) <= 5]
            for i, ticker in enumerate(clean_tickers[:4]):
                items.append({
                    'ticker': ticker,
                    'price':  f"${prices[i]}" if i < len(prices) else 'N/A',
                    'change': f"{changes[i]}%" if i < len(changes) else '0%',
                    'up':     not changes[i].startswith('-') if i < len(changes) else True,
                    'name':   '',
                })

        return {'items': items[:4] or [{'ticker': 'N/A', 'price': 'N/A',
                                         'change': '0%', 'up': True, 'name': ''}],
                'title': 'Market Data'}

    def _build_weather(self, text: str, query: str = '') -> dict:
        temp_m  = re.search(r'(\d+)\s*°?\s*([CF])', text)
        feels_m = re.search(r'feels?\s+like\s+(\d+)', text, re.IGNORECASE)
        hum_m   = re.search(r'humidity[:\s]+(\d+)\s*%', text, re.IGNORECASE)
        wind_m  = re.search(r'wind[:\s]+(\d+)\s*(km/?h|mph|m/s)', text, re.IGNORECASE)
        uv_m    = re.search(r'uv\s*(?:index)?[:\s]+(\d+)', text, re.IGNORECASE)
        cond_m  = re.search(
            r'\b(sunny|cloudy|rainy|stormy|partly cloudy|overcast|clear|foggy|windy|snowy)\b',
            text, re.IGNORECASE)

        current = {
            'temp':       f"{temp_m.group(1)}°{temp_m.group(2)}" if temp_m else 'N/A',
            'feels_like': f"Feels like {feels_m.group(1)}°" if feels_m else '',
            'humidity':   f"Humidity {hum_m.group(1)}%" if hum_m else '',
            'wind':       f"Wind {wind_m.group(1)} {wind_m.group(2)}" if wind_m else '',
            'uv':         f"UV Index {uv_m.group(1)}" if uv_m else '',
            'condition':  cond_m.group(1).title() if cond_m else 'Clear',
        }

        # Parse forecast days
        days = []
        day_pattern = re.compile(
            r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\b.{0,80}?(\d+)°',
            re.IGNORECASE,
        )
        for m in day_pattern.finditer(text):
            days.append({'day': m.group(1)[:3].upper(), 'temp': f"{m.group(2)}°"})

        location_m = re.search(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*[A-Z]{2,3}\b', text)
        location   = location_m.group(0) if location_m else 'Your Location'

        return {'current': current, 'forecast': days[:5], 'location': location}

    def _build_recipe(self, text: str, query: str = '') -> dict:
        # Extract title
        title_m  = re.search(r'^(.+?)(?:\n|$)', text.strip())
        title    = title_m.group(1).strip()[:60] if title_m else 'Recipe'

        # Time, servings
        time_m    = re.search(r'(\d+)\s*(?:min|minute|hour)', text, re.IGNORECASE)
        serving_m = re.search(r'(\d+)\s*(?:serving|person|people)', text, re.IGNORECASE)

        # Ingredients section
        ingr_section = re.search(
            r'(?:ingredient|ingr)[s:]?\s*\n(.*?)(?:\n\s*\n|\n\s*(?:step|instruction|method|direction))',
            text, re.IGNORECASE | re.DOTALL)
        ingredients = []
        if ingr_section:
            for line in ingr_section.group(1).splitlines():
                line = re.sub(r'^[-•*]\s*', '', line.strip())
                if len(line) > 2:
                    ingredients.append(line)

        # Steps section
        step_section = re.search(
            r'(?:step|instruction|method|direction)[s:]?\s*\n(.*?)$',
            text, re.IGNORECASE | re.DOTALL)
        steps = []
        if step_section:
            for m in re.finditer(r'(?:^\d+[.)]\s*|^step\s*\d+[.):]?\s*)(.+)',
                                  step_section.group(1), re.IGNORECASE | re.MULTILINE):
                steps.append(m.group(1).strip())
        if not steps:
            # Fallback: numbered lines
            for m in re.finditer(r'^\s*\d+[.)]\s+(.+)', text, re.MULTILINE):
                steps.append(m.group(1).strip())

        return {
            'title':       title,
            'time':        f"{time_m.group(1)} min" if time_m else '',
            'servings':    f"{serving_m.group(1)} servings" if serving_m else '',
            'ingredients': ingredients[:12],
            'steps':       steps[:8],
        }

    def _build_comparison(self, text: str, query: str = '') -> dict:
        # Try to find option names (often bold, titled, or before "vs")
        vs_m = re.search(r'(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?:\n|$)', text, re.IGNORECASE)
        option_a = vs_m.group(1).strip()[:25] if vs_m else 'Option A'
        option_b = vs_m.group(2).strip()[:25] if vs_m else 'Option B'

        # Parse comparison rows: "Feature: A_val vs B_val" or table-like rows
        rows = []
        row_pattern = re.compile(
            r'^\s*([^:\n]{3,30}):\s*(.+?)(?:\s+(?:vs\.?|versus|\|)\s*(.+?))?$',
            re.MULTILINE)
        for m in row_pattern.finditer(text):
            feature = m.group(1).strip()
            val_a   = m.group(2).strip()[:30]
            val_b   = (m.group(3) or '').strip()[:30]
            if feature.lower() in ('note', 'source', 'summary', 'winner', 'overall'):
                continue
            rows.append({'feature': feature, 'a': val_a, 'b': val_b, 'winner': ''})

        # Fallback: extract list items and pair them
        if not rows:
            items = self._build_list(text, query)['items']
            mid   = len(items) // 2
            option_a = items[0]['title'] if items else 'Option A'
            option_b = items[mid]['title'] if mid < len(items) else 'Option B'
            rows = [{'feature': 'Option', 'a': option_a, 'b': option_b, 'winner': ''}]

        return {'option_a': option_a, 'option_b': option_b, 'rows': rows[:8]}

    def _build_news(self, text: str, query: str = '') -> dict:
        items = self._build_list(text, query)['items']
        # Try to extract source + time from each item's detail
        for item in items:
            src_m  = re.search(r'\b([A-Z][a-zA-Z]+)\b', item.get('detail', ''))
            time_m = re.search(r'(\d+[hm]?\s*(?:hour|minute|hr|min)?s?\s*ago)',
                                item.get('detail', ''), re.IGNORECASE)
            item['source'] = src_m.group(1) if src_m else ''
            item['time']   = time_m.group(1) if time_m else ''
            item['image_query'] = f"news {item['title'][:40]}"
        return {'items': items[:6], 'title': 'Top Headlines'}

    def _build_media(self, text: str, query: str = '') -> dict:
        items = self._build_list(text, query)['items']
        media_type = 'Movie'
        lower = text.lower()
        if any(w in lower for w in ('game', 'steam', 'playstation', 'xbox')):
            media_type = 'Game'
        elif any(w in lower for w in ('album', 'artist', 'song', 'track', 'playlist')):
            media_type = 'Music'
        elif any(w in lower for w in ('series', 'episode', 'season', 'show')):
            media_type = 'Show'

        for item in items:
            rating_m = self._RATING_RE.search(text)
            item['rating']      = float(rating_m.group(1) or 4.0) if rating_m else 0.0
            item['media_type']  = media_type
            item['image_query'] = f"{item['title']} {media_type} poster official"

        icons = {'Game': '🎮', 'Movie': '🎬', 'Show': '📺', 'Music': '🎵'}
        return {'items': items[:6], 'media_type': media_type,
                'title': f"{media_type} Recommendations",
                'icon': icons.get(media_type, '🎬')}


# ── Image Fetcher ──────────────────────────────────────────────────────────────

class ImageFetcher:
    """
    Async DuckDuckGo image fetch → base64 data URL.
    Cache stored in memory/image_cache/ as base64 text files.
    """

    def __init__(self, cache_dir: Optional[Path] = None, cache_hours: int = 24):
        self._cache_dir  = cache_dir or Path("memory/image_cache")
        self._cache_secs = cache_hours * 3600
        self._lock       = threading.Lock()
        self._mem_cache: Dict[str, str] = {}   # query → data URL

    def fetch_async(self, query: str,
                    callback: Callable[[str, str], None]) -> None:
        """Fetch image for `query` and call callback(query, data_url) on completion."""
        threading.Thread(
            target=self._fetch_worker, args=(query, callback),
            daemon=True, name=f"atlas-img-{query[:20]}",
        ).start()

    def _fetch_worker(self, query: str, callback: Callable) -> None:
        with self._lock:
            if query in self._mem_cache:
                callback(query, self._mem_cache[query])
                return

        data_url = self._fetch(query)
        if data_url:
            with self._lock:
                self._mem_cache[query] = data_url
        callback(query, data_url or '')

    def _fetch(self, query: str) -> Optional[str]:
        try:
            # Check disk cache first
            cache_key = re.sub(r'[^\w]', '_', query)[:60]
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self._cache_dir / f"{cache_key}.b64"
            if cache_file.exists():
                age = time.time() - cache_file.stat().st_mtime
                if age < self._cache_secs:
                    return cache_file.read_text(encoding='utf-8').strip()

            # DuckDuckGo image search
            from ddgs import DDGS
            results = list(DDGS().images(query, max_results=3))
            if not results:
                return None

            img_url = None
            for r in results:
                url = r.get('image', '')
                if url and url.startswith('http') and not any(
                    bad in url for bad in ('ad.', 'click.', 'track.')):
                    img_url = url
                    break

            if not img_url:
                return None

            import urllib.request
            req = urllib.request.Request(
                img_url,
                headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                raw = resp.read()

            # Resize to thumbnail using Pillow
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(raw)).convert('RGB')
                img.thumbnail((120, 90), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, 'JPEG', quality=80)
                raw = buf.getvalue()
            except ImportError:
                pass   # Pillow not installed — use raw image

            b64 = base64.b64encode(raw).decode('ascii')
            data_url = f"data:image/jpeg;base64,{b64}"

            # Cache to disk
            cache_file.write_text(data_url, encoding='utf-8')
            return data_url

        except Exception as exc:
            log.debug("ImageFetcher: failed for '%s': %s", query, exc)
            return None


# ── SmartCard Page (URL interception) ─────────────────────────────────────────

class _SmartCardPage(QWebEnginePage):
    """
    QWebEnginePage that intercepts atlas:// navigation for JS→Python callbacks.
    JS calls: window.location.href = 'atlas://dismiss'  etc.
    """
    atlas_action = pyqtSignal(str, str)   # action, payload

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if url.scheme() == 'atlas':
            action  = url.host()
            payload = url.path().lstrip('/')
            self.atlas_action.emit(action, payload)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


# ── Smart Card Window ──────────────────────────────────────────────────────────

class SmartCardWindow(QWidget):
    """
    A single floating glassmorphism card window.

    Layout:
        QVBoxLayout (no margins)
        ├── _TitleBar  (52px, draggable, native Qt — reliable drag)
        └── QWebEngineView (content area)
    """

    dismissed = pyqtSignal(object)   # emits self on close

    def __init__(self, config: dict, speak_cb=None, vault_brain=None):
        super().__init__(None)
        self._config     = config
        self._speak      = speak_cb or (lambda s: None)
        self._vb         = vault_brain
        self._card_data: Optional[dict] = None
        self._pinned     = False
        self._dismiss_timer: Optional[QTimer] = None
        self._image_fetcher = ImageFetcher(
            cache_dir=Path("memory/image_cache"),
            cache_hours=int(config.get("smart_card_image_cache_hours", 24)),
        )

        # Window flags: frameless, always on top, no taskbar/dock entry
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(_CARD_WIDTH)

        self._build_ui()
        self._set_screen_exclusion()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        self._page_loaded      = False
        self._pending_card_json: Optional[str] = None

        # Title bar (draggable, native)
        self._title_bar = _TitleBar(self)
        self._title_bar.close_clicked.connect(self.dismiss)
        layout.addWidget(self._title_bar)

        # Web content
        self._page = _SmartCardPage()
        self._page.atlas_action.connect(self._on_atlas_action)

        self._web = QWebEngineView()
        self._web.setPage(self._page)
        self._web.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self._web.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._web.setMaximumHeight(_CARD_MAX_HEIGHT - _HEADER_HEIGHT)
        self._web.loadFinished.connect(self._on_page_loaded)
        layout.addWidget(self._web)

        # Load template HTML
        html_path = Path(__file__).parent / "ui" / "smart_card.html"
        if html_path.exists():
            self._web.setUrl(QUrl.fromLocalFile(str(html_path)))
        else:
            self._web.setHtml("<div style='color:white;padding:20px'>Template missing</div>")

    def _on_page_loaded(self, ok: bool) -> None:
        self._page_loaded = True
        if self._pending_card_json:
            js = self._pending_card_json
            self._pending_card_json = None
            self._web.page().runJavaScript(f"renderCard({js})")
            log.debug("SmartCard: rendered after page load.")

    def _set_screen_exclusion(self):
        """Exclude from screen recordings on macOS (NSWindowSharingNone)."""
        QTimer.singleShot(200, self._apply_exclusion)

    def _apply_exclusion(self):
        try:
            from ctypes import c_void_p
            import objc
            from AppKit import NSWindowSharingNone
            ns = objc.objc_object(c_void_p=int(self.winId()))
            ns.setSharingType_(NSWindowSharingNone)
        except Exception:
            pass

    # ── Card display ───────────────────────────────────────────────────────────

    def show_card(self, card_data: dict) -> None:
        """Render card with given data. Must be called from Qt main thread."""
        self._card_data = card_data
        template = card_data.get('template', 'list')
        title    = card_data.get('title', 'ATLAS')
        icon     = card_data.get('icon', '💡')

        self._title_bar.set_content(icon, template.upper(), title)

        # Adjust window height to content
        content_height = self._estimate_height(card_data)
        self.setFixedHeight(min(content_height + _HEADER_HEIGHT, _CARD_MAX_HEIGHT))
        self._web.setFixedHeight(min(content_height, _CARD_MAX_HEIGHT - _HEADER_HEIGHT))

        self._position_card()
        self.show()

        # Render: immediately if page already loaded, else queue for loadFinished
        card_json = json.dumps(card_data, ensure_ascii=False, default=str)
        if self._page_loaded:
            self._web.page().runJavaScript(f"renderCard({card_json})")
        else:
            self._pending_card_json = card_json

        # Auto-dismiss: skipped when secs=0 (manual-close mode) or card is pinned
        secs = int(card_data.get('auto_dismiss_secs',
                   self._config.get('smart_card_dismiss_seconds', 30)))
        if not self._pinned and secs > 0:
            self._start_countdown(secs)

        # Fetch images async
        if self._config.get('smart_card_image_enabled', True):
            threading.Thread(
                target=self._fetch_images,
                args=(card_data,), daemon=True).start()

    def _render(self, card_json: str) -> None:
        self._web.page().runJavaScript(f"renderCard({card_json})")

    def _estimate_height(self, data: dict) -> int:
        template = data.get('template', 'list')
        items    = data.get('items', [])
        n        = len(items)
        base_heights = {
            'weather': 280, 'recipe': 400, 'comparison': 300,
            'stock': max(180, n * 110), 'product': max(200, n * 100),
            'news':  max(200, n * 90),  'media':  max(200, n * 100),
            'list':  max(180, n * 70),
        }
        return min(base_heights.get(template, 300), _CARD_MAX_HEIGHT - _HEADER_HEIGHT)

    def _position_card(self) -> None:
        """Position at top-right corner, offset by stacking order."""
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo  = screen.availableGeometry()
        x    = geo.right() - _CARD_WIDTH - _CARD_MARGIN
        y    = geo.top() + _CARD_MARGIN + self._stack_offset()
        self.move(x, y)

    def _stack_offset(self) -> int:
        """Find this card's index in the manager's stack for vertical offset."""
        parent = self.property('card_index')
        return (int(parent) if parent else 0) * (_CARD_MAX_HEIGHT + _CARD_STACK_GAP)

    # ── Image fetching ─────────────────────────────────────────────────────────

    def _fetch_images(self, data: dict) -> None:
        items = data.get('items', [])
        for i, item in enumerate(items):
            query = item.get('image_query', item.get('title', ''))
            if query:
                idx = i   # capture by value
                self._image_fetcher.fetch_async(
                    query,
                    lambda q, url, i=idx: self._on_image(i, url),
                )

    def _on_image(self, index: int, data_url: str) -> None:
        if data_url:
            QTimer.singleShot(0, lambda: self._web.page().runJavaScript(
                f"updateImage({index}, {json.dumps(data_url)})"
            ))

    # ── Auto-dismiss countdown ─────────────────────────────────────────────────

    def _start_countdown(self, seconds: int) -> None:
        if self._dismiss_timer:
            self._dismiss_timer.stop()
        # Only call JS directly if the page is already loaded.
        # If not, renderCard (fired from _on_page_loaded) calls startCountdown itself.
        if self._page_loaded:
            self._web.page().runJavaScript(f"startCountdown({seconds})")
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.dismiss)
        self._dismiss_timer.start(seconds * 1000)

    def pin(self) -> None:
        self._pinned = True
        if self._dismiss_timer:
            self._dismiss_timer.stop()
        self._web.page().runJavaScript("stopCountdown()")

    # ── Atlas:// action handler ────────────────────────────────────────────────

    def _on_atlas_action(self, action: str, payload: str) -> None:
        if action == 'dismiss':
            self.dismiss()
        elif action == 'item':
            self._on_item_click(int(payload) if payload.isdigit() else 0)
        elif action == 'action':
            self._on_button_action(payload)
        elif action == 'pin':
            self.pin()
        elif action == 'hover_start':
            if self._dismiss_timer:
                self._dismiss_timer.stop()
        elif action == 'hover_end':
            if not self._pinned and self._card_data:
                remaining = self._card_data.get('auto_dismiss_secs', 30)
                self._start_countdown(remaining // 2)   # half time remaining

    def _on_item_click(self, index: int) -> None:
        if not self._card_data:
            return
        items = self._card_data.get('items', [])
        if index < len(items):
            item  = items[index]
            title = item.get('title', '')
            self._speak(f"Item {index + 1}: {title}. {item.get('detail', '')}")

    def _on_button_action(self, payload: str) -> None:
        try:
            action = json.loads(payload) if payload.startswith('{') else {'url': payload}
        except Exception:
            action = {'url': payload}

        url = action.get('url', '')
        if url and url.startswith('http'):
            import subprocess
            subprocess.Popen(['open', url])
        voice = action.get('voice', '')
        if voice:
            self._speak(voice)

    def dismiss(self) -> None:
        if self._dismiss_timer:
            self._dismiss_timer.stop()
        self._web.page().runJavaScript("dismissAnimation()")
        QTimer.singleShot(260, self._do_close)

    def _do_close(self) -> None:
        self.hide()
        self.dismissed.emit(self)

    # ── Save to vault ──────────────────────────────────────────────────────────

    def save_to_vault(self) -> str:
        if not self._card_data or not self._vb:
            return "Nothing to save." if not self._card_data else "Vault not connected."
        try:
            from datetime import date
            template = self._card_data.get('template', 'card')
            title    = self._card_data.get('title', 'Smart Card')[:40]
            folder   = self._vb.atlas / "SmartCards"
            folder.mkdir(parents=True, exist_ok=True)
            slug     = re.sub(r'[^\w\s-]', '', title.lower()).replace(' ', '-')
            fname    = f"{date.today()}-{slug}.md"
            items    = self._card_data.get('items', [])
            content  = '\n'.join(
                f"- **{it.get('title','')}**: {it.get('detail','')}"
                for it in items
            )
            (folder / fname).write_text(
                f"---\ntags: [atlas, smart-card, {template}]\n"
                f"date: {date.today()}\n---\n\n# {title}\n\n{content}\n",
                encoding='utf-8',
            )
            return f"Saved to SmartCards/{fname} in your vault, Boss."
        except Exception as exc:
            log.warning("SmartCard: vault save failed: %s", exc)
            return "Could not save to vault."


# ── Title Bar (native, draggable) ──────────────────────────────────────────────

class _TitleBar(QWidget):
    """52px native drag handle + icon + label + close button."""

    close_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(_HEADER_HEIGHT)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setStyleSheet("""
            QWidget {
                background: rgba(12, 12, 20, 220);
                border-top-left-radius: 20px;
                border-top-right-radius: 20px;
            }
        """)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 12, 0)
        row.setSpacing(8)

        self._icon  = QLabel("💡")
        self._icon.setStyleSheet("background: transparent; font-size: 14px;")
        self._icon.setFixedWidth(20)

        self._category = QLabel("RESULTS")
        self._category.setStyleSheet(
            "background: transparent; color: rgba(255,255,255,0.4);"
            "font-size: 10px; letter-spacing: 1.5px; font-weight: 600;")

        self._title = QLabel("Smart Card")
        self._title.setStyleSheet(
            "background: transparent; color: white;"
            "font-size: 13px; font-weight: 600;")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.10);
                border: none;
                border-radius: 14px;
                color: rgba(255,255,255,0.7);
                font-size: 18px;
                font-weight: 300;
            }
            QPushButton:hover {
                background: rgba(255,80,80,0.5);
                color: white;
            }
        """)
        close_btn.clicked.connect(self.close_clicked)

        row.addWidget(self._icon)
        row.addWidget(self._category)
        row.addStretch()
        row.addWidget(self._title)
        row.addStretch()
        row.addWidget(close_btn)

        self._drag_pos: Optional[QPoint] = None

    def set_content(self, icon: str, category: str, title: str) -> None:
        self._icon.setText(icon)
        self._category.setText(category)
        self._title.setText(title[:45])

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - \
                             self.window().frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(
                event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)


# ── Smart Card Manager ─────────────────────────────────────────────────────────

class _CardSignalBridge(QObject):
    """Thread-safe signal bridge: voice worker → Qt main thread."""
    show_card = pyqtSignal(dict)


class SmartCardManager:
    """
    Orchestrates up to 3 simultaneous Smart Cards.
    Wired into brain.handle after every AI response.
    """

    def __init__(self, config: dict, speak_cb=None, vault_brain=None, brain=None):
        self._config       = config
        self._speak        = speak_cb or (lambda s: None)
        self._vb           = vault_brain
        self._brain        = brain
        self._enabled      = bool(config.get('smart_card_enabled', True))
        self._max_cards    = int(config.get('smart_card_max_visible', 3))
        self._dismiss_secs = int(config.get('smart_card_dismiss_seconds', 30))

        self._auto_dismiss = bool(config.get('smart_card_auto_dismiss', True))
        self._classifier   = ContentClassifier()
        self._builder      = CardDataBuilder()
        self._cards: List[SmartCardWindow] = []
        self._last_response: str = ''
        self._last_card: Optional[SmartCardWindow] = None

        # Signal bridge — AutoConnection delivers to Qt main thread from any thread
        self._bridge = _CardSignalBridge()
        self._bridge.show_card.connect(self._show)

        log.info("SmartCardManager: initialised (enabled=%s, max=%d).",
                 self._enabled, self._max_cards)

    # ── Main entry (called after every brain response) ─────────────────────────

    def on_response(self, query: str, response: str) -> None:
        """Safe to call from any thread — signal bridge delivers to Qt main thread."""
        self._last_response = response
        if not self._enabled:
            log.info("SmartCard: disabled in config — skipping.")
            return
        if not self._classifier.should_show_card(response):
            template = self._classifier.classify(response)
            log.info("SmartCard: should_show_card=False (template=%s, words=%d) — skipping.",
                     template, len(response.split()))
            return

        template = self._classifier.classify(response) or 'list'
        log.info("SmartCard: MATCHED template=%s, words=%d — emitting signal.",
                 template, len(response.split()))
        card_data = self._builder.build(
            response, template, query=query,
            dismiss_secs=self._dismiss_secs if self._auto_dismiss else 0,
        )
        self._bridge.show_card.emit(card_data)   # thread-safe; delivered on Qt main thread

    def _show(self, card_data: dict) -> None:
        """Create and show a new card. Must run on Qt main thread."""
        log.info("SmartCard: _show() called on thread=%s, active=%d",
                 __import__('threading').current_thread().name, len(self._cards))
        try:
            if len(self._cards) >= self._max_cards:
                if self._cards:
                    self._cards[0].dismiss()

            card = SmartCardWindow(self._config, self._speak, self._vb)
            card.setProperty('card_index', len(self._cards))
            card.dismissed.connect(self._on_card_dismissed)
            self._cards.append(card)
            self._last_card = card
            card.show_card(card_data)

            log.info("SmartCard: window shown, template=%s, pos=(%d,%d), size=(%d,%d).",
                     card_data.get('template'),
                     card.x(), card.y(), card.width(), card.height())
        except Exception as exc:
            log.exception("SmartCard: _show() crashed: %s", exc)

    def _on_card_dismissed(self, card: SmartCardWindow) -> None:
        if card in self._cards:
            self._cards.remove(card)
        # Re-index remaining cards for stacking
        for i, c in enumerate(self._cards):
            c.setProperty('card_index', i)

    def force_show(self, response: Optional[str] = None,
                   template: Optional[str] = None) -> None:
        """Force show a card for any response, bypassing auto-detection."""
        text = response or self._last_response
        if not text:
            return
        tmpl     = template or self._classifier.classify(text) or 'list'
        card_data = self._builder.build(text, tmpl, dismiss_secs=self._dismiss_secs)
        QTimer.singleShot(0, lambda: self._show(card_data))

    def dismiss_top(self) -> None:
        if self._cards:
            self._cards[-1].dismiss()

    def dismiss_all(self) -> None:
        for card in list(self._cards):
            card.dismiss()

    def pin_top(self) -> None:
        if self._cards:
            self._cards[-1].pin()

    def save_top_to_vault(self) -> str:
        if self._last_card:
            return self._last_card.save_to_vault()
        return "No card is currently visible, Boss."

    # ── Voice commands ────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas show that visually", "atlas visualize this",
                                     "atlas show card", "atlas make a card")):
            self.force_show()
            return "Showing that visually, Boss."

        if any(p in lower for p in ("atlas hide that", "atlas close card",
                                     "atlas dismiss card")):
            self.dismiss_top()
            return None   # silent dismiss

        if any(p in lower for p in ("atlas clear all cards", "atlas close all cards",
                                     "atlas hide all cards")):
            self.dismiss_all()
            return "Cards cleared, Boss."

        if any(p in lower for p in ("atlas keep that up", "atlas pin that card",
                                     "atlas don't dismiss")):
            self.pin_top()
            return "Card pinned — it will stay until you close it, Boss."

        if any(p in lower for p in ("atlas save that to obsidian", "atlas save card",
                                     "atlas save this card")):
            return self.save_top_to_vault()

        if any(p in lower for p in ("atlas compare those", "atlas show comparison")):
            self.force_show(template='comparison')
            return "Switching to comparison view, Boss."

        if any(p in lower for p in ("atlas chart that", "atlas show chart",
                                     "atlas graph that")):
            self.force_show(template='list')
            return "Showing that as a chart, Boss."

        # "atlas tell me more about number 3" / "atlas open number 2"
        tell_m = re.search(r'atlas (?:tell me more about|open|select) (?:number\s*)?(\d+)',
                            lower)
        if tell_m:
            idx = int(tell_m.group(1)) - 1
            if self._last_card and self._last_card._card_data:
                items = self._last_card._card_data.get('items', [])
                if 0 <= idx < len(items):
                    item = items[idx]
                    title  = item.get('title', '')
                    detail = item.get('detail', '')
                    if 'open' in lower:
                        actions = item.get('actions', [])
                        if actions:
                            url = actions[0].get('url', '')
                            if url:
                                import subprocess
                                subprocess.Popen(['open', url])
                                return f"Opening {title}, Boss."
                    return f"Item {idx+1}: {title}. {detail}" if detail \
                           else f"Item {idx+1}: {title}."
            return "No card is visible right now, Boss."

        return None
