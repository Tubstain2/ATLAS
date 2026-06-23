"""
ATLAS Context7 — Live library documentation injection for coding requests.

Context7 MCP provides up-to-date documentation for any library/framework.
No API key needed. Two MCP calls:
  1. resolve-library-id  → find library's Context7 ID
  2. get-library-docs    → fetch docs (trimmed to token cap)

Injected into coding requests: the resolved docs become part of the system
prompt so the LLM generates code against the current API, not stale training data.

Source: https://mcp.context7.com/mcp (no auth required)

Cache: 24-hour in-memory cache per library. On ATLAS restart, cache is cold.
Token cap: 5000 tokens (~20 KB); Context7 returns markdown sections, we trim.

Voice commands:
  "ATLAS use latest docs for X"   → force-fetch fresh docs for library X
  "ATLAS refresh docs"             → clear doc cache entirely
  "ATLAS what version of X"       → show cached doc header for library X
  "ATLAS docs for X"              → brief summary of what's cached for X
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_MCP_URL          = "https://mcp.context7.com/mcp"
_MAX_TOKENS       = 5000     # ~4 chars/token → 20 000 chars cap
_CACHE_HOURS      = 24
_CACHE_SECS       = _CACHE_HOURS * 3600
_REQUEST_TIMEOUT  = 12       # seconds per HTTP request

# Libraries auto-detected by keyword matching in user text
_LIBRARY_KEYWORDS: Dict[str, str] = {
    "pandas":       "pandas",
    "numpy":        "numpy",
    "matplotlib":   "matplotlib",
    "seaborn":      "seaborn",
    "sklearn":      "scikit-learn",
    "scikit-learn": "scikit-learn",
    "tensorflow":   "tensorflow",
    "torch":        "pytorch",
    "pytorch":      "pytorch",
    "fastapi":      "fastapi",
    "flask":        "flask",
    "django":       "django",
    "sqlalchemy":   "sqlalchemy",
    "pydantic":     "pydantic",
    "langchain":    "langchain",
    "openai":       "openai",
    "anthropic":    "anthropic",
    "requests":     "requests",
    "httpx":        "httpx",
    "asyncio":      "asyncio",
    "aiohttp":      "aiohttp",
    "pytest":       "pytest",
    "typer":        "typer",
    "rich":         "rich",
    "click":        "click",
    "polars":       "polars",
    "streamlit":    "streamlit",
    "gradio":       "gradio",
    "playwright":   "playwright",
    "pyqt6":        "pyqt6",
    "qt":           "pyqt6",
    "pipecat":      "pipecat",
    "smolagents":   "smolagents",
}


# ── Cache entry ───────────────────────────────────────────────────────────────

class _CacheEntry:
    def __init__(self, library_id: str, docs: str, version_hint: str = ""):
        self.library_id   = library_id
        self.docs         = docs
        self.version_hint = version_hint
        self.fetched_at   = time.monotonic()

    def is_expired(self, ttl_secs: float = _CACHE_SECS) -> bool:
        return (time.monotonic() - self.fetched_at) > ttl_secs


# ── ATLASContext7 ─────────────────────────────────────────────────────────────

class ATLASContext7:
    """
    Live documentation fetcher and injector.

    Usage (from code_agent or brain):
        ctx7 = ATLASContext7(config)
        ctx7.start()
        docs = ctx7.get_docs("fastapi")
        if docs:
            prompt = ctx7.inject_into_prompt(prompt, "fastapi")
    """

    def __init__(self, config: dict = None, offline_mode=None):
        self._config      = config or {}
        self._offline     = offline_mode
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock        = threading.Lock()
        self._enabled     = bool(self._config.get("context7_enabled", True))
        self._max_tokens  = int(self._config.get("context7_max_tokens", _MAX_TOKENS))
        self._cache_hours = int(self._config.get("context7_cache_hours", _CACHE_HOURS))
        self._cache_secs  = self._cache_hours * 3600

        log.info("ATLASContext7: initialised (enabled=%s, max_tokens=%d, cache=%dh).",
                 self._enabled, self._max_tokens, self._cache_hours)

    def start(self) -> None:
        log.info("ATLASContext7: ready.")

    # ── Public API ────────────────────────────────────────────────────────────

    def detect_libraries(self, text: str) -> list[str]:
        """Return library names detected in text via keyword matching."""
        lower = text.lower()
        found = []
        seen  = set()
        for kw, lib in _LIBRARY_KEYWORDS.items():
            if kw in lower and lib not in seen:
                found.append(lib)
                seen.add(lib)
        return found

    def get_docs(self, library: str, force_refresh: bool = False) -> Optional[str]:
        """
        Return cached or freshly-fetched docs for a library.
        Returns None if unavailable (offline, disabled, fetch failed).
        """
        if not self._enabled:
            return None
        if self._offline and not self._offline.context7_fetch_available:
            # Return cached even if stale when offline
            with self._lock:
                entry = self._cache.get(library.lower())
            if entry:
                log.info("Context7: offline — returning cached docs for %s.", library)
                return entry.docs
            return None

        with self._lock:
            entry = self._cache.get(library.lower())
        if entry and not entry.is_expired(self._cache_secs) and not force_refresh:
            return entry.docs

        # Fetch fresh docs
        docs, lib_id, version = self._fetch(library)
        if docs:
            with self._lock:
                self._cache[library.lower()] = _CacheEntry(lib_id, docs, version)
            return docs

        # Fall back to stale cache
        with self._lock:
            entry = self._cache.get(library.lower())
        return entry.docs if entry else None

    def inject_into_prompt(self, prompt: str, *libraries: str) -> str:
        """
        Prepend relevant library docs to a prompt for the coding LLM.
        Trims to max_tokens total across all libraries.
        """
        if not self._enabled:
            return prompt

        doc_sections = []
        remaining_chars = self._max_tokens * 4   # ~4 chars/token

        for lib in libraries:
            docs = self.get_docs(lib)
            if not docs:
                continue
            trimmed = docs[:remaining_chars]
            remaining_chars -= len(trimmed)
            doc_sections.append(
                f"=== {lib.upper()} DOCUMENTATION (from Context7) ===\n{trimmed}\n"
            )
            if remaining_chars <= 0:
                break

        if not doc_sections:
            return prompt

        injection = "\n".join(doc_sections)
        return f"{injection}\n\n---\n\n{prompt}"

    def get_version_hint(self, library: str) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(library.lower())
        return entry.version_hint if entry else None

    def clear_cache(self, library: Optional[str] = None) -> None:
        with self._lock:
            if library:
                self._cache.pop(library.lower(), None)
            else:
                self._cache.clear()
        log.info("Context7: cache cleared (%s).", library or "all")

    # ── MCP fetch ─────────────────────────────────────────────────────────────

    def _fetch(self, library: str) -> Tuple[str, str, str]:
        """
        Call Context7 MCP:
          Step 1: POST resolve-library-id  → get library_id
          Step 2: POST get-library-docs    → get markdown docs

        Returns (docs_text, library_id, version_hint).
        Returns ("", "", "") on any failure.
        """
        try:
            import urllib.request
            import urllib.error
        except ImportError:
            return "", "", ""

        def _post(tool: str, params: dict) -> Optional[dict]:
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": params},
            }).encode()
            req = urllib.request.Request(
                _MCP_URL,
                data=payload,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.URLError as exc:
                log.warning("Context7: request failed (%s): %s", tool, exc)
                return None
            except Exception as exc:
                log.warning("Context7: unexpected error (%s): %s", tool, exc)
                return None

        # Step 1: Resolve library ID
        log.info("Context7: resolving library ID for '%s'.", library)
        resolve_resp = _post("resolve-library-id", {"libraryName": library})
        if not resolve_resp:
            return "", "", ""

        library_id = ""
        version    = ""
        try:
            content = resolve_resp.get("result", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    data = json.loads(block["text"])
                    if isinstance(data, list) and data:
                        library_id = data[0].get("id", "")
                        version    = data[0].get("version", "")
                    elif isinstance(data, dict):
                        library_id = data.get("id", "")
                        version    = data.get("version", "")
                    break
        except Exception as exc:
            log.warning("Context7: could not parse resolve response: %s", exc)

        if not library_id:
            # Try direct name as fallback ID (some libraries use name directly)
            library_id = f"/npm/{library}"
            log.info("Context7: no ID resolved, trying fallback '%s'.", library_id)

        # Step 2: Fetch docs
        log.info("Context7: fetching docs for library_id='%s'.", library_id)
        docs_resp = _post("get-library-docs", {
            "context7CompatibleLibraryID": library_id,
            "tokens": self._max_tokens,
        })
        if not docs_resp:
            return "", library_id, version

        docs_text = ""
        try:
            content = docs_resp.get("result", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    docs_text = block["text"]
                    break
        except Exception as exc:
            log.warning("Context7: could not parse docs response: %s", exc)

        if docs_text:
            log.info("Context7: fetched %d chars for %s (v%s).",
                     len(docs_text), library, version or "?")
        else:
            log.warning("Context7: empty docs for %s.", library)

        return docs_text, library_id, version

    # ── Voice commands ────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        # "atlas use latest docs for X" / "atlas refresh docs for X"
        refresh_m = re.search(
            r"atlas (?:use latest docs for|refresh docs for|fetch docs for) (.+?)$",
            lower)
        if refresh_m:
            lib = refresh_m.group(1).strip()
            docs = self.get_docs(lib, force_refresh=True)
            if docs:
                preview = docs[:120].replace("\n", " ")
                return (f"Fresh documentation loaded for {lib}, Boss. "
                        f"Preview: {preview}…")
            return f"Could not fetch documentation for {lib}, Boss."

        if any(p in lower for p in ("atlas refresh docs", "atlas clear docs",
                                     "atlas wipe doc cache")):
            self.clear_cache()
            return "Documentation cache cleared, Boss. Fresh docs will be fetched on next coding request."

        # "atlas what version of X" / "atlas docs for X"
        version_m = re.search(
            r"atlas (?:what version of|version of) (.+?)(?:\s*are you using)?$",
            lower)
        if version_m:
            lib = version_m.group(1).strip()
            v = self.get_version_hint(lib)
            if v:
                return f"I have {lib} documentation for version {v}, Boss."
            with self._lock:
                cached = lib.lower() in self._cache
            if not cached:
                return f"No {lib} documentation loaded yet. I will fetch it on the next coding request."
            return f"I have cached {lib} documentation but no version tag was provided."

        docs_m = re.search(r"atlas docs for (.+?)$", lower)
        if docs_m:
            lib = docs_m.group(1).strip()
            docs = self.get_docs(lib)
            if not docs:
                return f"No documentation for {lib} loaded yet, Boss."
            first_line = docs.strip().splitlines()[0][:100]
            return f"I have {lib} docs cached. First line: {first_line}"

        return None
