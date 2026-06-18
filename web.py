"""
ATLAS Web Module (Step 4)

Provides three capabilities:
  1. DuckDuckGoSearch  — full-text and news search (no API key needed)
  2. PageFetcher       — URL fetch + BeautifulSoup text extraction
  3. WebModule         — public API; used by ATLASCore for context augmentation

Data flow:
  user utterance
      │
      ▼ (ATLASCore detects web-requiring query)
  WebModule.build_context(query)
      ├─ DuckDuckGoSearch.text() or .news()
      └─ optional PageFetcher.fetch(top_url)
      │
      ▼ (formatted search context string)
  ATLASCore._call() injects context into Gemini prompt
      │
      ▼
  Gemini summarises + answers

WebModule.answer(query, summarizer_fn) is also available for standalone use
(e.g. self_editor.py asks a research question).

No API key required for any part of this module.
"""

from __future__ import annotations

import logging
import re
import time
import warnings
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Suppress the duckduckgo_search → ddgs rename warning
warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")

# ── tunables ──────────────────────────────────────────────────────────────────
_DEFAULT_MAX_RESULTS    = 5
_DEFAULT_MAX_PAGE_CHARS = 5_000   # chars of page text sent to Gemini
_FETCH_TIMEOUT          = (5, 12) # (connect, read) seconds
_SEARCH_DELAY           = 1.2     # minimum seconds between DDG requests
_SEARCH_RETRIES         = 2       # retry attempts on rate-limit (403/429)
_SEARCH_RETRY_BACKOFF   = 2.5     # extra seconds per retry

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}

# Tags whose entire content is noise when extracting page text
_STRIP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header",
    "aside", "form", "iframe", "svg", "figure", "button",
    "input", "select", "textarea", "dialog",
}


# ══════════════════════════════════════════════════════════════════════════════
# Text cleaner
# ══════════════════════════════════════════════════════════════════════════════

def _clean(text: str, max_chars: int = 0) -> str:
    """Collapse whitespace, limit length."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " …"
    return text


# ══════════════════════════════════════════════════════════════════════════════
# DuckDuckGo search
# ══════════════════════════════════════════════════════════════════════════════

class DuckDuckGoSearch:
    """
    Wraps duckduckgo_search.DDGS.
    All methods return plain Python dicts — no DDG objects leak out.
    """

    def __init__(self):
        self._last_call = 0.0
        try:
            from duckduckgo_search import DDGS   # noqa: F401
            self._available = True
        except ImportError:
            log.error("duckduckgo-search not installed — pip install duckduckgo-search")
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ── rate-limit guard ─────────────────────────────────────────────────────

    def _throttle(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < _SEARCH_DELAY:
            time.sleep(_SEARCH_DELAY - elapsed)
        self._last_call = time.monotonic()

    # ── public search methods ─────────────────────────────────────────────────

    def text(self, query: str, max_results: int = _DEFAULT_MAX_RESULTS) -> list[dict]:
        """
        Return up to max_results web results.
        Each dict: {title, href, body}
        """
        if not self._available:
            return []
        return self._call("text", query, max_results)

    def news(self, query: str, max_results: int = _DEFAULT_MAX_RESULTS) -> list[dict]:
        """
        Return up to max_results news articles.
        Each dict: {date, title, body, url, source}
        """
        if not self._available:
            return []
        results = self._call("news", query, max_results)
        # Normalise: news API uses 'url', text API uses 'href'
        for r in results:
            r.setdefault("href", r.get("url", ""))
        return results

    def _call(self, kind: str, query: str, max_results: int) -> list[dict]:
        """Throttled DDG call with retry-on-rate-limit."""
        from duckduckgo_search import DDGS

        for attempt in range(_SEARCH_RETRIES + 1):
            self._throttle()
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    ddgs = DDGS()
                    if kind == "news":
                        return ddgs.news(query, max_results=max_results) or []
                    return ddgs.text(query, max_results=max_results) or []
            except Exception as exc:
                msg = str(exc).lower()
                rate_limited = "403" in msg or "429" in msg or "ratelimit" in msg
                if rate_limited and attempt < _SEARCH_RETRIES:
                    wait = _SEARCH_RETRY_BACKOFF * (attempt + 1)
                    log.warning("DDG rate-limited; retrying in %.1fs …", wait)
                    time.sleep(wait)
                    continue
                log.warning("DDG %s search failed: %s", kind, exc)
                return []
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Page fetcher
# ══════════════════════════════════════════════════════════════════════════════

class PageFetcher:
    """Fetches a URL and returns clean readable text via BeautifulSoup."""

    def __init__(self):
        self._available = True
        try:
            import requests          # noqa: F401
            from bs4 import BeautifulSoup  # noqa: F401
        except ImportError as exc:
            log.warning("PageFetcher unavailable: %s", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def fetch(self, url: str, max_chars: int = _DEFAULT_MAX_PAGE_CHARS) -> str:
        """
        Fetch `url` and return extracted text, limited to max_chars.
        Returns empty string on any error.
        """
        if not self._available:
            return ""

        domain = urlparse(url).netloc
        try:
            import requests
            from bs4 import BeautifulSoup

            resp = requests.get(
                url,
                headers=_REQUEST_HEADERS,
                timeout=_FETCH_TIMEOUT,
                allow_redirects=True,
            )
            resp.raise_for_status()

            # Only parse HTML content
            content_type = resp.headers.get("content-type", "")
            if "html" not in content_type:
                log.debug("Non-HTML content-type at %s: %s", domain, content_type)
                return ""

            soup = BeautifulSoup(resp.text, "lxml")

            # Strip noise elements
            for tag in soup(_STRIP_TAGS):
                tag.decompose()

            # Prefer main article content
            for selector in ["article", "main", "[role='main']", ".content", "#content"]:
                block = soup.select_one(selector)
                if block:
                    text = block.get_text(separator="\n", strip=True)
                    if len(text) > 200:
                        return _clean(text, max_chars)

            # Fall back to full body text
            body = soup.find("body")
            text = (body or soup).get_text(separator="\n", strip=True)
            return _clean(text, max_chars)

        except Exception as exc:
            log.warning("Fetch failed for %s: %s", domain, exc)
            return ""


# ══════════════════════════════════════════════════════════════════════════════
# Result formatter
# ══════════════════════════════════════════════════════════════════════════════

def _format_results(results: list[dict], query: str, kind: str = "web") -> str:
    """
    Render search results as a plain-text context block for Gemini.

    kind: 'web' | 'news'
    """
    if not results:
        return ""

    ts  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"[Web search results — {kind} — query: \"{query}\" — retrieved {ts}]\n"]

    for i, r in enumerate(results, 1):
        title  = r.get("title", "").strip()
        url    = r.get("href") or r.get("url", "")
        body   = _clean(r.get("body", ""), max_chars=300)
        source = r.get("source", "")
        date   = r.get("date", "")

        lines.append(f"[{i}] {title}")
        if source:
            lines.append(f"Source: {source}" + (f" — {date[:10]}" if date else ""))
        if url:
            lines.append(f"URL: {url}")
        if body:
            lines.append(body)
        lines.append("")

    return "\n".join(lines).strip()


# ══════════════════════════════════════════════════════════════════════════════
# WebModule — public API
# ══════════════════════════════════════════════════════════════════════════════

class WebModule:
    """
    Façade used by ATLASCore (context augmentation) and by direct callers
    (self_editor, future tool calls).

    main.py wires:
        web = WebModule(config)
        core.set_web_module(web)
    """

    # Queries that clearly need live web data rather than LLM parametric knowledge
    _WEB_TRIGGERS = frozenset({
        # Time-sensitive / current events
        "latest",       "breaking",     "current news",  "today's news",
        "right now",    "this week",    "this month",    "recent news",
        "what happened","what's happening","trending",    "in the news",
        # Explicit search intent
        "search for",   "search the web","look up online","find online",
        "browse to",    "go to website", "read this article","open this link",
        # Live data
        "weather in",   "temperature in","what's the weather",
        "stock price",  "price of",      "exchange rate", "crypto price",
        "bitcoin",      "ethereum",
        # Current events
        "who won",      "match score",   "election result","poll result",
        "news about",   "update on",
    })

    def __init__(self, config: dict):
        cfg             = config.get("web", {})
        self._max_res   = cfg.get("max_results",    _DEFAULT_MAX_RESULTS)
        self._max_chars = cfg.get("max_page_chars",  _DEFAULT_MAX_PAGE_CHARS)
        self._fetch_en  = cfg.get("enable_fetch",    True)

        self._ddg     = DuckDuckGoSearch()
        self._fetcher = PageFetcher() if self._fetch_en else None

    # ── Classification ────────────────────────────────────────────────────────

    def needs_web(self, text: str) -> bool:
        """Return True if the query should be augmented with live web data."""
        lower = text.lower()
        return any(kw in lower for kw in self._WEB_TRIGGERS)

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, max_results: int | None = None) -> list[dict]:
        """Raw DuckDuckGo text search results."""
        n = max_results or self._max_res
        return self._ddg.text(query, max_results=n)

    def news(self, query: str, max_results: int | None = None) -> list[dict]:
        """Raw DuckDuckGo news results."""
        n = max_results or self._max_res
        return self._ddg.news(query, max_results=n)

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetch_page(self, url: str) -> str:
        """Fetch a URL and return clean extracted text."""
        if not self._fetcher:
            return ""
        return self._fetcher.fetch(url, max_chars=self._max_chars)

    # ── Context builder (used by ATLASCore) ───────────────────────────────────

    def build_context(self, query: str) -> str:
        """
        Search for query; optionally deep-fetch the top result.
        Returns a formatted context string ready for Gemini injection.

        ATLASCore calls this before building the Gemini prompt.
        """
        # Detect if this is a news query
        news_terms = {"news", "latest", "breaking", "today", "update", "trending"}
        is_news = any(t in query.lower() for t in news_terms)

        if is_news:
            results = self._ddg.news(query, max_results=self._max_res)
            kind = "news"
        else:
            results = self._ddg.text(query, max_results=self._max_res)
            kind = "web"

        if not results:
            log.warning("No search results for: %r", query)
            return ""

        context = _format_results(results, query, kind)

        # Deep-fetch top result for extra detail (if enabled and result has URL)
        if self._fetcher and results:
            top_url = results[0].get("href") or results[0].get("url", "")
            if top_url and top_url.startswith("http"):
                log.debug("Fetching top result: %s", top_url)
                page_text = self._fetcher.fetch(top_url, max_chars=self._max_chars)
                if page_text:
                    domain = urlparse(top_url).netloc
                    context += (
                        f"\n\n[Full page content from {domain}]\n{page_text}"
                    )

        return context

    # ── answer() — standalone summarisation (used by self_editor etc.) ────────

    def answer(self, query: str, summarizer: Callable[[str], str]) -> str:
        """
        Search → format → pass to summarizer (core.ask).
        Returns the summarised answer.

        summarizer = lambda prompt: core.ask(prompt)
        """
        context = self.build_context(query)
        if not context:
            return summarizer(query)   # fall through to parametric knowledge

        prompt = (
            f"Answer the following question using the web search results below.\n"
            f"Be concise and accurate. Do not include markdown formatting.\n\n"
            f"{context}\n\n"
            f"Question: {query}"
        )
        return summarizer(prompt)

    # ── Convenience: fetch + summarise a URL directly ─────────────────────────

    def summarise_url(self, url: str, question: str, summarizer: Callable[[str], str]) -> str:
        """
        Fetch a specific URL, extract its text, then ask Gemini a question about it.
        """
        page = self.fetch_page(url)
        if not page:
            return f"I couldn't read the page at {url}."

        domain = urlparse(url).netloc
        prompt = (
            f"The following is the text content from {domain}.\n"
            f"Answer this question based only on the text: {question}\n\n"
            f"{page}"
        )
        return summarizer(prompt)
