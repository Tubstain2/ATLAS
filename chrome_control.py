"""
ATLAS Chrome Control Module

Provides full Playwright-based programmatic control of Google Chrome.
Connects to an existing Chrome session via CDP (remote debugging port 9222).

Falls back to AppleScript for simple URL-open commands if Playwright
is unavailable or Chrome is not running with the debug flag.

Safety:
  - Never fills password / payment / SSN fields
  - Never clicks purchase/checkout buttons without explicit confirmation
  - Never submits forms without confirmation (configurable)
  - Always confirms before closing multiple tabs
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
import urllib.parse
from typing import Optional

log = logging.getLogger(__name__)

# ── Safety constants ───────────────────────────────────────────────────────────

_SENSITIVE_FIELDS = frozenset({
    "password", "passwd", "ssn", "social security",
    "credit card", "card number", "cvv", "cvc",
    "bank account", "routing number", "pin",
})

_PURCHASE_BUTTONS = frozenset({
    "buy now", "purchase", "confirm order", "place order",
    "complete purchase", "pay now", "checkout",
})

# URL-indicator tokens — "open X" is only claimed by chrome if these appear
_URL_TOKENS = frozenset({
    ".com", ".org", ".net", ".io", ".co", ".edu", ".gov", ".uk",
    "http://", "https://", "www.",
})
_BROWSER_WORDS = frozenset({
    "tab", "chrome", "browser", "website", "page", "url", "link",
})

# ── Site search map ────────────────────────────────────────────────────────────

_SITE_SEARCH = {
    "youtube":  "https://youtube.com/results?search_query={q}",
    "amazon":   "https://amazon.com/s?k={q}",
    "github":   "https://github.com/search?q={q}",
    "reddit":   "https://reddit.com/search/?q={q}",
    "twitter":  "https://twitter.com/search?q={q}",
    "x":        "https://x.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "ebay":     "https://ebay.com/sch/i.html?_nkw={q}",
    "netflix":  "https://netflix.com/search?q={q}",
}


class ChromeControl:
    """Voice-driven Chrome controller via Playwright CDP."""

    def __init__(self, config: dict, speak_cb=None, brain=None):
        chrome_cfg = config.get("chrome", {})

        self._debug_port      = int(chrome_cfg.get("chrome_debug_port", 9222))
        self._auto_relaunch   = chrome_cfg.get("chrome_auto_relaunch", True)
        self._form_safety     = chrome_cfg.get("form_safety_enabled", True)
        self._confirm_submit  = chrome_cfg.get("require_confirmation_before_submit", True)
        self._enabled         = chrome_cfg.get("chrome_control_enabled", True)

        self._speak_cb   = speak_cb
        self._brain      = brain
        self._user_name  = config.get("user_name", "Boss")

        self._pw      = None   # Playwright instance
        self._browser = None   # CDP-connected browser
        self._page    = None   # active page shortcut
        self._context = None   # persistent context (when not using CDP)

        self._pending_submit       = False
        self._pending_close_others = False

        # Worker thread — all Playwright calls must run in this thread
        import queue as _q
        self._cmd_q:    _q.Queue = _q.Queue()
        self._worker:   object   = None
        self._ready:    bool     = False

        if self._enabled:
            log.info("ChromeControl ready (port %d). Call connect() to attach to Chrome.",
                     self._debug_port)

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Connect Playwright to Chrome via a persistent worker thread.

        All Playwright objects live in that thread — Playwright's greenlet-based
        sync API cannot cross thread boundaries, so every subsequent call is
        dispatched through _cmd_q to the worker.

        Tries three strategies in order:
          1. CDP to existing Chrome on debug port (fastest)
          2. launch_persistent_context with real Chrome profile (uses cookies/logins)
          3. Fresh Chromium (no logins, last resort)
        """
        if not self._enabled:
            return False

        import threading, queue as _q
        ready_q: _q.Queue = _q.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, args=(ready_q,),
            daemon=True, name="atlas-chrome-worker",
        )
        self._worker.start()
        try:
            return ready_q.get(timeout=30)
        except _q.Empty:
            log.error("ChromeControl: worker thread timed out after 30s.")
            return False

    def _worker_loop(self, ready_q) -> None:
        """Persistent Playwright thread — initialises once, then services _cmd_q forever."""
        import asyncio, queue as _q
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Try CDP first (Chrome already running with debug port)
            if self._try_cdp():
                connected = True
            elif self._auto_relaunch:
                # Relaunch Chrome with debug port, then retry CDP
                log.info("ChromeControl: relaunching Chrome with debug port %d …", self._debug_port)
                self._relaunch_chrome()
                connected = self._try_cdp() or self._try_fresh_browser()
            else:
                # Silent mode: CDP failed and auto_relaunch is off — don't open anything
                log.info("ChromeControl: Chrome not running and auto_relaunch is off — "
                         "standing by. Say 'ATLAS open Chrome' to launch it.")
                connected = False
            ready_q.put(connected)
            if not connected:
                return
            while True:
                try:
                    item = self._cmd_q.get(timeout=1.0)
                    if item is None:       # stop sentinel
                        break
                    fn, result_q = item
                    try:
                        result_q.put(("ok", fn()))
                    except Exception as exc:
                        result_q.put(("err", exc))
                except _q.Empty:
                    continue
        finally:
            if loop and not loop.is_closed():
                loop.close()

    def _try_cdp(self) -> bool:
        p = None
        try:
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            browser = p.chromium.connect_over_cdp(
                f"http://localhost:{self._debug_port}", timeout=3000
            )
            self._pw, self._browser = p, browser
            pages = self._all_pages()
            self._page = pages[0] if pages else None
            log.info("ChromeControl: connected via CDP (%d pages).", len(pages))
            return True
        except Exception as exc:
            log.info("ChromeControl: CDP connect failed: %s", str(exc).split("\n")[0])
            if p is not None:
                try:
                    p.stop()
                except Exception:
                    pass
            return False

    def _try_persistent_context(self) -> bool:
        """Launch Chrome with the user's real profile — preserves cookies and logins.

        Called directly from _worker_loop, so no inner thread needed.
        """
        import pathlib
        p = None
        try:
            from playwright.sync_api import sync_playwright
            user_data = str(pathlib.Path.home() / "Library/Application Support/Google/Chrome")
            p = sync_playwright().start()
            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data,
                channel="chrome",
                headless=False,
                args=["--no-first-run", "--no-default-browser-check"],
                timeout=15000,
            )
            self._pw = p
            self._browser = None
            self._context = context
            self._page = context.pages[0] if context.pages else context.new_page()
            log.info("ChromeControl: persistent context ready (real Chrome profile).")
            return True
        except Exception as exc:
            log.debug("Persistent context unavailable (Chrome already running): %s", str(exc).split("\n")[0])
            if p is not None:
                try:
                    p.stop()
                except Exception:
                    pass
            return False

    def _try_fresh_browser(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            browser = p.chromium.launch(headless=False)
            self._pw, self._browser = p, browser
            context = browser.new_context()
            self._context = context
            self._page = context.new_page()
            log.info("ChromeControl: fresh Chromium ready (no user profile).")
            return True
        except Exception as exc:
            log.error("Fresh browser failed: %s", exc)
            return False

    def _relaunch_chrome(self) -> None:
        """Launch a dedicated ATLAS Chrome instance with a separate profile + debug port.

        Does NOT kill the user's existing Chrome — runs alongside it.
        Profile is stored at ~/.atlas/chrome-profile so cookies/logins persist
        between ATLAS sessions.
        """
        import pathlib
        atlas_profile = pathlib.Path.home() / ".atlas" / "chrome-profile"
        atlas_profile.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.Popen(
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    f"--remote-debugging-port={self._debug_port}",
                    f"--remote-allow-origins=http://localhost:{self._debug_port}",
                    f"--user-data-dir={atlas_profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-sync",
                    "--homepage=about:blank",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("ChromeControl: ATLAS Chrome launched (profile: %s)", atlas_profile)
        except FileNotFoundError:
            log.error("Chrome not found at /Applications/Google Chrome.app")
            return
        time.sleep(4.0)   # give Chrome time to open the debug port

    def _connected(self) -> bool:
        return self._page is not None

    def _active_page(self):
        try:
            pages = self._all_pages()
            return pages[-1] if pages else self._page
        except Exception:
            return self._page

    def _all_pages(self) -> list:
        pages = []
        try:
            # Persistent context stores pages directly on self._context
            if hasattr(self, "_context") and self._context is not None:
                pages.extend(self._context.pages)
            elif self._browser is not None:
                for ctx in self._browser.contexts:
                    pages.extend(ctx.pages)
        except Exception:
            pass
        return pages

    def _new_page(self):
        """Open a new page in the current context."""
        try:
            if hasattr(self, "_context") and self._context is not None:
                return self._context.new_page()
            if self._browser is not None:
                return self._browser.contexts[0].new_page()
        except Exception as exc:
            log.error("Could not open new page: %s", exc)
        return None

    # ── AppleScript fallback ───────────────────────────────────────────────────

    def _applescript_open(self, url: str) -> str:
        try:
            script = f'tell application "Google Chrome" to open location "{url}"'
            subprocess.run(["osascript", "-e", script], timeout=5, check=False)
            return f"Opening {url} in Chrome."
        except Exception as exc:
            log.error("AppleScript fallback failed: %s", exc)
            return f"Couldn't open {url}."

    # ── Main voice router ──────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        """Match voice command → execute → return spoken response. None if no match."""
        if not self._enabled:
            return None

        # If the Playwright worker thread is alive, all Playwright objects live
        # there — dispatch into it via _cmd_q so greenlets stay in their thread.
        if self._worker is not None and self._worker.is_alive():
            import queue as _q
            result_q: _q.Queue = _q.Queue()
            self._cmd_q.put((lambda t=text: self._dispatch(t), result_q))
            try:
                status, value = result_q.get(timeout=15.0)
                if status == "err":
                    log.error("ChromeControl dispatch error: %s", value)
                    return f"Chrome control error: {value}"
                return value
            except _q.Empty:
                log.error("ChromeControl: dispatch timed out")
                return None
        # Fallback: direct (not connected yet or worker exited)
        return self._dispatch(text)

    def _dispatch(self, text: str) -> Optional[str]:
        """Command matching — runs inside the Playwright worker thread."""
        lower = text.lower().strip()
        # Strip "atlas " prefix for matching
        clean = re.sub(r"^atlas\s+", "", lower)

        page = self._active_page() or self._page

        # ── Pending confirmations ──────────────────────────────────────────────
        if self._pending_submit and any(p in clean for p in ("yes submit", "confirm submit", "yes, submit")):
            return self._confirm_submit_action(page)

        if self._pending_close_others and any(p in clean for p in ("confirm close", "yes close", "yes, close")):
            return self._confirm_close_others(page)

        # ── Navigation ─────────────────────────────────────────────────────────
        if clean in ("go back", "back"):
            return self._nav(page, "back")
        if clean in ("go forward", "forward"):
            return self._nav(page, "forward")
        if any(clean == p for p in ("refresh", "reload", "refresh the page", "reload the page")):
            return self._nav(page, "reload")
        if any(p in clean for p in ("new tab", "open new tab", "open a new tab")):
            return self._new_tab()
        if any(p == clean for p in ("close tab", "close this tab")):
            return self._close_tab(page)
        if any(p in clean for p in ("what tabs", "list tabs", "tabs do i have", "show tabs", "all tabs")):
            return self._list_tabs()
        if "switch to tab" in clean:
            return self._switch_tab(clean)
        if "find the tab about" in clean:
            return self._find_tab(clean)

        # "go to X" / "navigate to X"
        if clean.startswith("go to ") or clean.startswith("navigate to "):
            term = re.sub(r"^(go to |navigate to )", "", clean).strip()
            return self._navigate(term, page)

        # "open X" — only claim if URL-like or browser keyword
        if clean.startswith("open "):
            term = clean[5:].strip()
            if self._is_url_like(term) or any(bw in clean for bw in _BROWSER_WORDS):
                return self._navigate(term, page)
            return None  # let system control handle "open Chrome" etc.

        # ── Scrolling ──────────────────────────────────────────────────────────
        if any(p in clean for p in ("scroll down",)):
            return self._scroll(page, 600)
        if any(p in clean for p in ("scroll up",)):
            return self._scroll(page, -600)
        if "scroll to top" in clean or clean == "go to top":
            return self._scroll_to(page, "top")
        if "scroll to bottom" in clean or "go to bottom" in clean:
            return self._scroll_to(page, "bottom")

        # ── Page interaction ───────────────────────────────────────────────────
        if clean.startswith("click ") and "click to" not in clean:
            target = re.sub(r"^click (the |on |on the )?", "", clean).strip()
            if target:
                return self._click(page, target)

        # "type X" — avoid matching "type of", "type a", short noise
        if re.match(r"^type (in |into |out )?(.{3,})", clean):
            m = re.match(r"^type (?:in |into |out )?(.+)", clean)
            if m:
                return self._type_text(page, m.group(1).strip())

        if clean.startswith("fill in ") or re.match(r"^fill \w", clean):
            return self._fill_form(page, clean)

        if any(p in clean for p in ("submit this form", "submit the form")):
            return self._submit_form(page)

        if "dropdown" in clean and clean.startswith("select "):
            return self._select_dropdown(page, clean)

        if any(p in clean for p in ("check that box", "check the box", "tick the box")):
            return self._check_box(page, clean)
        if re.match(r"^tick \w", clean):
            return self._check_box(page, clean)

        # ── Content reading ────────────────────────────────────────────────────
        if any(p in clean for p in ("read this page", "read the page", "read page")):
            return self._read_page(page)
        if any(p in clean for p in ("summarise this page", "summarize this page",
                                     "summarise the page", "summarize the page",
                                     "summarise page", "summarize page")):
            return self._summarise_page(page)
        if "what does this page say about" in clean:
            query = clean.split("what does this page say about", 1)[-1].strip()
            if query:
                return self._page_search(page, query)
        if any(p in clean for p in ("get the price", "what is the price", "what's the price")):
            return self._get_price(page)
        if any(p in clean for p in ("what is this page", "what page is this",
                                     "what's this page", "describe this page")):
            return self._page_info(page)

        # ── Search ─────────────────────────────────────────────────────────────
        if "search google for" in clean:
            query = clean.split("search google for", 1)[-1].strip()
            if query:
                return self._search_google(query, page)
        if re.match(r"^search .+ on \w", clean):
            return self._search_site(clean, page)

        # ── Tab management ─────────────────────────────────────────────────────
        if any(p in clean for p in ("close all tabs except this", "close other tabs",
                                     "close all other tabs")):
            return self._close_others(page)
        if any(p in clean for p in ("bookmark this", "bookmark this page", "save this page")):
            return self._bookmark(page)

        return None

    # ── Navigation helpers ─────────────────────────────────────────────────────

    def _nav(self, page, action: str) -> str:
        if not page:
            return self._no_chrome()
        try:
            if action == "back":
                page.go_back(timeout=8000)
                return "Going back."
            if action == "forward":
                page.go_forward(timeout=8000)
                return "Going forward."
            if action == "reload":
                page.reload(timeout=15000)
                return "Page refreshed."
        except Exception as exc:
            log.warning("Nav %s failed: %s", action, exc)
            return f"Navigation failed: {exc}"
        return ""

    def _navigate(self, term: str, page) -> str:
        term = term.strip().strip('"').strip("'")
        if not term:
            return f"Where should I navigate to, {self._user_name}?"

        if self._is_url_like(term):
            url = term if term.startswith("http") else f"https://{term}"
        else:
            url = f"https://www.google.com/search?q={urllib.parse.quote(term)}"

        domain = urllib.parse.urlparse(url).netloc or url[:40]

        if not self._connected():
            return self._applescript_open(url)
        if not page:
            try:
                page = self._new_page()
            except Exception:
                return self._applescript_open(url)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return f"Navigating to {domain}."
        except Exception as exc:
            log.warning("Navigate to %s failed: %s", url, exc)
            return f"Couldn't navigate to {domain}: {exc}"

    def _new_tab(self) -> str:
        if not self._connected():
            return self._no_chrome()
        try:
            page = self._new_page()
            if page:
                page.goto("about:blank")
                self._page = page
            return "New tab opened."
        except Exception as exc:
            return f"Couldn't open new tab: {exc}"

    def _close_tab(self, page) -> str:
        if not page:
            return self._no_chrome()
        try:
            page.close()
            pages = self._all_pages()
            if pages:
                self._page = pages[-1]
                self._page.bring_to_front()
            return "Tab closed."
        except Exception as exc:
            return f"Couldn't close tab: {exc}"

    def _list_tabs(self) -> str:
        if not self._connected():
            return self._no_chrome()
        pages = self._all_pages()
        if not pages:
            return "No tabs open."
        n     = len(pages)
        shown = pages[:8]
        parts = [f"Tab {i+1} is {p.title() or 'untitled'}" for i, p in enumerate(shown)]
        suffix = f", and {n - 8} more" if n > 8 else ""
        return f"You have {n} tab{'s' if n != 1 else ''} open, {self._user_name}. " + ", ".join(parts) + suffix + "."

    def _switch_tab(self, text: str) -> str:
        if not self._connected():
            return self._no_chrome()
        term = re.sub(r".*(switch to tab|go to tab)\s*", "", text).strip()
        pages = self._all_pages()
        # Numeric
        if term.isdigit():
            idx = int(term) - 1
            if 0 <= idx < len(pages):
                pages[idx].bring_to_front()
                self._page = pages[idx]
                return f"Switched to tab {int(term)}."
            return f"I only see {len(pages)} tabs, {self._user_name}."
        # Name match
        for p in pages:
            if term in p.title().lower():
                p.bring_to_front()
                self._page = p
                return f"Switched to {p.title()}."
        return f"No tab matching '{term}' found, {self._user_name}."

    def _find_tab(self, text: str) -> str:
        if not self._connected():
            return self._no_chrome()
        query = text.split("find the tab about", 1)[-1].strip()
        pages = self._all_pages()
        # Title search
        for p in pages:
            if query in p.title().lower():
                p.bring_to_front()
                self._page = p
                return f"Found and switched to: {p.title()}."
        # Content search (first 3 pages, expensive)
        for p in pages[:3]:
            try:
                if query in p.content().lower():
                    p.bring_to_front()
                    self._page = p
                    return f"Found content about '{query}' in: {p.title()}."
            except Exception:
                continue
        return f"No tab found about '{query}', {self._user_name}."

    # ── Scrolling ──────────────────────────────────────────────────────────────

    def _scroll(self, page, delta: int) -> str:
        if not page:
            return self._no_chrome()
        try:
            page.evaluate(f"window.scrollBy(0, {delta})")
            return "Scrolled down." if delta > 0 else "Scrolled up."
        except Exception as exc:
            return f"Couldn't scroll: {exc}"

    def _scroll_to(self, page, position: str) -> str:
        if not page:
            return self._no_chrome()
        try:
            if position == "top":
                page.evaluate("window.scrollTo(0, 0)")
                return "At the top."
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return "At the bottom."
        except Exception as exc:
            return f"Couldn't scroll: {exc}"

    # ── Page interaction ───────────────────────────────────────────────────────

    def _click(self, page, target: str) -> str:
        if not page:
            return self._no_chrome()
        # Safety check — no purchase buttons
        if any(pb in target.lower() for pb in _PURCHASE_BUTTONS):
            return (f"I won't click purchase buttons without your explicit confirmation, "
                    f"{self._user_name}.")
        strategies = [
            lambda: page.get_by_text(target, exact=False).first.click(timeout=3000),
            lambda: page.get_by_role("button", name=target).first.click(timeout=3000),
            lambda: page.get_by_role("link", name=target).first.click(timeout=3000),
            lambda: page.locator(f"[aria-label*='{target}' i]").first.click(timeout=3000),
        ]
        for strategy in strategies:
            try:
                strategy()
                return f"Clicked '{target}'."
            except Exception:
                continue
        return f"I couldn't find '{target}' on the page, {self._user_name}."

    def _type_text(self, page, content: str) -> str:
        if not page:
            return self._no_chrome()
        try:
            page.keyboard.type(content)
            return f"Typed: {content[:40]}{'…' if len(content) > 40 else ''}."
        except Exception as exc:
            return f"Couldn't type: {exc}"

    def _fill_form(self, page, text: str) -> str:
        if not page:
            return self._no_chrome()
        # Parse "fill in FIELD with VALUE" or "fill FIELD with VALUE"
        m = re.search(r"fill (?:in )?(.+?)\s+with\s+(.+)", text, re.IGNORECASE)
        if not m:
            return f"Try: 'fill in field name with value', {self._user_name}."
        field, value = m.group(1).strip(), m.group(2).strip()

        # Safety check
        if self._form_safety:
            field_lower = field.lower()
            if any(sf in field_lower for sf in _SENSITIVE_FIELDS):
                return (f"I won't fill in sensitive fields like '{field}' for your security, "
                        f"{self._user_name}. Please enter that manually.")

        try:
            locator = page.get_by_label(field, exact=False).first
            locator.fill(value)
            return f"Filled '{field}' with '{value}'."
        except Exception:
            pass
        try:
            locator = page.get_by_placeholder(field, exact=False).first
            locator.fill(value)
            return f"Filled '{field}' with '{value}'."
        except Exception as exc:
            return f"Couldn't find field '{field}': {exc}"

    def _submit_form(self, page) -> str:
        if not page:
            return self._no_chrome()
        if self._confirm_submit:
            self._pending_submit = True
            return (f"Ready to submit this form, {self._user_name}. "
                    "Say 'confirm submit' to proceed.")
        return self._do_submit(page)

    def _confirm_submit_action(self, page) -> str:
        self._pending_submit = False
        if not page:
            return self._no_chrome()
        return self._do_submit(page)

    def _do_submit(self, page) -> str:
        try:
            page.locator("button[type=submit], input[type=submit]").first.click(timeout=3000)
            return "Form submitted."
        except Exception:
            pass
        try:
            page.keyboard.press("Enter")
            return "Form submitted."
        except Exception as exc:
            return f"Couldn't submit form: {exc}"

    def _select_dropdown(self, page, text: str) -> str:
        if not page:
            return self._no_chrome()
        # "select VALUE from the dropdown" or "select VALUE from FIELD dropdown"
        m = re.search(r"select (.+?) from (?:the )?(.+?)\s*dropdown", text, re.IGNORECASE)
        if m:
            value, field = m.group(1).strip(), m.group(2).strip()
        else:
            m2 = re.search(r"select (.+?) from", text, re.IGNORECASE)
            if m2:
                value, field = m2.group(1).strip(), ""
            else:
                return f"Try: 'select option from the dropdown', {self._user_name}."

        try:
            if field and field not in ("the", "a"):
                page.get_by_label(field, exact=False).select_option(label=value)
            else:
                page.locator("select").first.select_option(label=value)
            return f"Selected '{value}'."
        except Exception as exc:
            return f"Couldn't select '{value}': {exc}"

    def _check_box(self, page, text: str) -> str:
        if not page:
            return self._no_chrome()
        # Extract target: "check the X box" / "tick X"
        m = re.search(r"(?:check the |tick )(.+?)(?:\s+box)?$", text, re.IGNORECASE)
        target = m.group(1).strip() if m else ""
        if not target:
            return f"Which checkbox, {self._user_name}?"
        try:
            page.get_by_label(target, exact=False).first.check()
            return f"Checked '{target}'."
        except Exception as exc:
            return f"Couldn't check '{target}': {exc}"

    # ── Content reading ────────────────────────────────────────────────────────

    def _read_page(self, page) -> str:
        if not page:
            return self._no_chrome()
        try:
            text = page.evaluate("document.body.innerText") or ""
            text = text.strip()[:2000]
            if self._brain and len(text) > 300:
                summary = self._brain.ask(
                    f"Summarise this web page content in 3 sentences for voice output. "
                    f"Plain prose only, no markdown.\n\n{text}"
                )
                return summary or text[:400]
            return text[:400]
        except Exception as exc:
            return f"Couldn't read page: {exc}"

    def _summarise_page(self, page) -> str:
        if not page:
            return self._no_chrome()
        try:
            text = page.evaluate("document.body.innerText") or ""
            text = text.strip()
            if not text:
                return f"The page appears to have no readable text, {self._user_name}."
            if self._brain:
                summary = self._brain.ask(
                    f"Summarise this web page in 4 to 5 sentences for voice. "
                    f"Plain prose only.\n\n{text[:4000]}"
                )
                return summary or text[:500]
            return text[:500]
        except Exception as exc:
            return f"Couldn't summarise page: {exc}"

    def _page_search(self, page, query: str) -> str:
        if not page:
            return self._no_chrome()
        try:
            page_text = page.evaluate("document.body.innerText") or ""
            idx = page_text.lower().find(query.lower())
            if idx < 0:
                return f"I couldn't find anything about '{query}' on this page, {self._user_name}."
            start   = max(0, idx - 100)
            end     = min(len(page_text), idx + 300)
            excerpt = page_text[start:end].strip()
            return f"Found this about '{query}': {excerpt}"
        except Exception as exc:
            return f"Couldn't search page: {exc}"

    def _get_price(self, page) -> str:
        if not page:
            return self._no_chrome()
        selectors = [
            "[class*='price']",
            "[class*='Price']",
            "[itemprop='price']",
            ".price",
            "#price",
            "[data-price]",
        ]
        for sel in selectors:
            try:
                el   = page.locator(sel).first
                text = el.inner_text(timeout=2000).strip()
                if text:
                    return f"The price shown is {text}."
            except Exception:
                continue
        return self._page_search(page, "price")

    def _page_info(self, page) -> str:
        if not page:
            return self._no_chrome()
        try:
            title = page.title()
            url   = page.url
            domain = urllib.parse.urlparse(url).netloc or url
            try:
                meta = page.locator("meta[name='description']").get_attribute(
                    "content", timeout=2000
                ) or ""
            except Exception:
                meta = ""
            desc = f" {meta[:100]}" if meta else ""
            return f"This page is '{title}' at {domain}.{desc}"
        except Exception as exc:
            return f"Couldn't read page info: {exc}"

    # ── Search ─────────────────────────────────────────────────────────────────

    def _search_google(self, query: str, page) -> str:
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
        if not self._connected():
            return self._applescript_open(url)
        if not page:
            return self._navigate(url, None)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            # Extract top results
            titles   = page.locator("h3").all_inner_texts()[:3]
            snippets = page.locator(".VwiC3b").all_inner_texts()[:3]
            if titles:
                parts = []
                for i, t in enumerate(titles):
                    s = snippets[i] if i < len(snippets) else ""
                    parts.append(f"{t}: {s[:80]}" if s else t)
                return (f"Top results for '{query}', {self._user_name}: "
                        + ". Next, ".join(parts[:2]) + ".")
            return f"Searched for '{query}'."
        except Exception as exc:
            log.warning("Google search failed: %s", exc)
            return f"Searched for '{query}'."

    def _search_site(self, text: str, page) -> str:
        # "search QUERY on SITE"
        m = re.search(r"search (.+?) on (\w+)", text, re.IGNORECASE)
        if not m:
            return f"Try: 'search topic on youtube', {self._user_name}."
        query, site = m.group(1).strip(), m.group(2).strip().lower()
        q = urllib.parse.quote(query)

        if site in _SITE_SEARCH:
            url = _SITE_SEARCH[site].format(q=q)
        else:
            url = f"https://www.google.com/search?q=site:{site}+{q}"

        return self._navigate(url, page)

    # ── Tab management ─────────────────────────────────────────────────────────

    def _close_others(self, page) -> str:
        if not self._connected():
            return self._no_chrome()
        pages = self._all_pages()
        other = [p for p in pages if p != page]
        if not other:
            return f"No other tabs to close, {self._user_name}."
        self._pending_close_others = True
        return (f"This will close {len(other)} other tab{'s' if len(other) != 1 else ''}, "
                f"{self._user_name}. Say 'confirm close' to proceed.")

    def _confirm_close_others(self, page) -> str:
        self._pending_close_others = False
        pages  = self._all_pages()
        others = [p for p in pages if p != page]
        closed = 0
        for p in others:
            try:
                p.close()
                closed += 1
            except Exception:
                pass
        return f"Closed {closed} tab{'s' if closed != 1 else ''}, {self._user_name}."

    def _bookmark(self, page) -> str:
        if not page:
            return self._no_chrome()
        try:
            page.keyboard.press("Meta+d")
            return f"Bookmarked, {self._user_name}."
        except Exception as exc:
            return f"Couldn't bookmark: {exc}"

    # ── Setup status ───────────────────────────────────────────────────────────

    def check_setup(self) -> str:
        lines = []

        # Check playwright
        try:
            import playwright  # noqa: F401
            lines.append("✅ playwright installed")
        except ImportError:
            lines.append("❌ playwright not installed — run: pip install playwright && playwright install chromium")

        # Check Chrome debug port
        import socket
        try:
            s = socket.create_connection(("localhost", self._debug_port), timeout=1)
            s.close()
            lines.append(f"✅ Chrome reachable on port {self._debug_port}")
        except OSError:
            lines.append(
                f"❌ Chrome not on port {self._debug_port} — "
                f"quit Chrome and relaunch with: "
                f"'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                f"--remote-debugging-port={self._debug_port} "
                f"--remote-allow-origins=http://localhost:{self._debug_port}'"
            )
            if self._auto_relaunch:
                lines.append("   (ATLAS will auto-relaunch Chrome when chrome_control.connect() is called)")

        return "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_url_like(term: str) -> bool:
        lower = term.lower()
        return (
            lower.startswith("http")
            or lower.startswith("www.")
            or any(tok in lower for tok in _URL_TOKENS)
        )

    def _no_chrome(self) -> str:
        return (
            f"Chrome isn't connected, {self._user_name}. "
            "Say 'atlas connect chrome' or check that Chrome is running."
        )

    def _speak(self, text: str) -> None:
        if self._speak_cb:
            try:
                self._speak_cb(text)
            except Exception:
                pass
