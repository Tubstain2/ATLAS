"""
ATLAS Market Research Module

Real-time stock quotes, technical analysis, portfolio tracking,
market news, earnings data, and AI-powered trading research.

Data sources (priority order):
  1. Finnhub      — real-time quotes, fundamentals, earnings, news (FINNHUB_API_KEY)
  2. Alpha Vantage — technical indicators: RSI, MACD, SMAs, Bollinger Bands (ALPHAVANTAGE_API_KEY)
  3. yfinance     — historical data, fundamentals fallback, RSI fallback (no key)

DISCLAIMER: All analysis is informational only. Not financial advice.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_DISCLAIMER = (
    "This is market data and technical analysis only, Boss. "
    "It is not financial advice. Always conduct your own research "
    "and consider speaking with a financial advisor before making trading decisions."
)

_ET           = ZoneInfo("America/New_York")
_MARKET_OPEN  = (9, 30)
_MARKET_CLOSE = (16, 0)

_INDICES = {
    "S&P 500": "SPY",
    "NASDAQ":  "QQQ",
    "DOW":     "DIA",
    "VIX":     "^VIX",
}

_COMPANY_MAP = {
    "apple":        "AAPL",  "microsoft":    "MSFT",  "google":   "GOOGL",
    "alphabet":     "GOOGL", "amazon":       "AMZN",  "tesla":    "TSLA",
    "nvidia":       "NVDA",  "meta":         "META",  "facebook": "META",
    "netflix":      "NFLX",  "disney":       "DIS",   "spotify":  "SPOT",
    "uber":         "UBER",  "airbnb":       "ABNB",  "palantir": "PLTR",
    "shopify":      "SHOP",  "paypal":       "PYPL",  "block":    "SQ",
    "coinbase":     "COIN",  "robinhood":    "HOOD",  "sofi":     "SOFI",
    "amd":          "AMD",   "intel":        "INTC",  "qualcomm": "QCOM",
    "arm":          "ARM",   "broadcom":     "AVGO",  "tsmc":     "TSM",
    "jpmorgan":     "JPM",   "bank of america": "BAC", "goldman sachs": "GS",
    "morgan stanley": "MS",  "blackrock":   "BLK",   "berkshire": "BRK-B",
    "johnson":      "JNJ",   "pfizer":       "PFE",   "moderna":  "MRNA",
    "exxon":        "XOM",   "chevron":      "CVX",   "boeing":   "BA",
    "lockheed":     "LMT",   "raytheon":    "RTX",   "salesforce": "CRM",
    "oracle":       "ORCL",  "ibm":          "IBM",   "snap":     "SNAP",
    "reddit":       "RDDT",  "pinterest":    "PINS",  "lyft":     "LYFT",
    "doordash":     "DASH",
}


class MarketModule:
    """Full stock market research and tracking for ATLAS voice commands."""

    def __init__(self, config: dict, speak_cb=None, brain=None, obsidian=None):
        mkt = config.get("market", {})

        self._finnhub_key  = os.environ.get("FINNHUB_API_KEY", "")
        self._av_key       = os.environ.get("ALPHAVANTAGE_API_KEY", "")
        self._watchlist: List[str] = list(
            config.get("watchlist",
            mkt.get("watchlist",
            config.get("api", {}).get("tracked_stocks", ["AAPL", "TSLA", "NVDA"])))
        )
        self._update_interval = int(mkt.get("market_update_interval", 300))
        self._hours_only      = bool(mkt.get("market_hours_only_updates", True))
        self._save_obsidian   = bool(mkt.get("save_research_to_obsidian", True))

        self._speak_cb = speak_cb
        self._brain    = brain
        self._obsidian = obsidian

        self._cache: Dict[str, Dict] = {}
        self._cache_ttl = 60

        self._av_calls: List[float] = []
        self._av_lock   = threading.Lock()

        self._last_report: Optional[Tuple[str, str]] = None

        self._stop_ev   = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None

        log.info(
            "MarketModule ready. Watchlist: %s | Finnhub: %s | AlphaVantage: %s",
            self._watchlist,
            "yes" if self._finnhub_key else "no",
            "yes" if self._av_key else "no",
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop_ev.clear()
        self._bg_thread = threading.Thread(
            target=self._bg_update_loop, daemon=True, name="atlas-market-bg"
        )
        self._bg_thread.start()

    def stop(self) -> None:
        self._stop_ev.set()

    # ── Main voice router ──────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        """Route voice commands. Returns None if not a market command."""
        lower = text.lower().strip()
        clean = re.sub(r"^atlas\s+", "", lower)

        # ── Price quote ─────────────────────────────────────────────────────────
        m = re.search(r"what(?:'s| is) (.+?) (?:trading|priced) at", clean)
        if not m:
            m = re.search(r"(?:price of|quote for|price on) (.+)", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_price(tk)

        # ── Full research ───────────────────────────────────────────────────────
        m = re.match(r"research (.+?)(?:\s+stock)?$", clean)
        if not m:
            m = re.match(r"full report (?:on |for )?(.+?)(?:\s+stock)?$", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_research(tk)

        # ── Technical analysis ──────────────────────────────────────────────────
        m = re.match(r"technical analysis (?:of |on |for )?(.+?)(?:\s+stock)?$", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_technical(tk)

        m = re.search(r"is (.+?) overbought", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_rsi_check(tk)

        m = re.search(r"(?:what(?:'s| is) )?the trend (?:on |for )?(.+?)(?:\s+stock)?$", clean)
        if m and "trend" in clean:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_trend(tk)

        # ── Comparison ─────────────────────────────────────────────────────────
        m = re.search(r"compare (.+?) (?:and|vs\.?|versus) (.+?)(?:\s+stocks?)?$", clean)
        if m:
            t1 = self._extract_ticker(m.group(1))
            t2 = self._extract_ticker(m.group(2))
            if t1 and t2:
                return self._cmd_compare(t1, t2)

        # ── Market overview ─────────────────────────────────────────────────────
        if any(p in clean for p in (
            "how is the market", "market today", "market overview",
            "market summary", "check the market", "how are markets",
        )):
            return self._cmd_market_overview()

        if any(p in clean for p in ("is the market open", "market open", "market hours")):
            return self._cmd_market_hours()

        if any(p in clean for p in ("market sentiment", "fear and greed", "market mood")):
            return self._cmd_market_sentiment()

        # ── Portfolio ───────────────────────────────────────────────────────────
        if any(p in clean for p in (
            "check my portfolio", "check my watchlist", "how is my portfolio",
            "portfolio update", "watchlist update", "my stocks",
        )):
            return self._cmd_portfolio()

        if any(p in clean for p in ("best performer", "top gainer", "biggest winner")):
            return self._cmd_best_performer()

        if any(p in clean for p in ("worst performer", "top loser", "biggest loser")):
            return self._cmd_worst_performer()

        m = re.search(r"add (.+?) to (?:my )?watchlist", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_add_watchlist(tk)

        m = re.search(r"remove (.+?) from (?:my )?watchlist", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_remove_watchlist(tk)

        # ── News & sentiment ────────────────────────────────────────────────────
        if clean in ("market news", "financial news", "stock market news"):
            return self._cmd_market_news()

        m = re.search(r"news (?:on |for |about )?(.+?)(?:\s+stock)?$", clean)
        if m and "news" in clean:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_stock_news(tk)

        m = re.search(r"(?:what(?:'s| is) )?(?:the )?sentiment (?:on |for |of )?(.+?)(?:\s+stock)?$", clean)
        if m and "sentiment" in clean:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_sentiment(tk)

        # ── Earnings ────────────────────────────────────────────────────────────
        m = re.search(r"when does (.+?) (?:report|announce) earnings", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_earnings(tk)

        if "earnings this week" in clean or "upcoming earnings" in clean:
            return self._cmd_earnings_calendar()

        m = re.search(r"what happened (?:at |with |to )?(.+?) earnings", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_last_earnings(tk)

        # ── Smart analysis ──────────────────────────────────────────────────────
        m = re.search(r"should i (?:buy|sell|invest in|get) (.+?)(?:\s+stock)?$", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_smart_analysis(tk)

        m = re.match(r"(?:analyse|analyze) (.+?)(?:\s+stock)?$", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_smart_analysis(tk)

        # ── Stock recommendations / discovery ───────────────────────────────────
        # Broad phrases only fire when there's explicit stock/market context.
        # Narrow phrases (containing "stock") always fire.
        _REC_PHRASES_NARROW = (
            "which stocks", "what stocks", "stocks to buy", "stocks to watch",
            "stocks to invest", "hot stocks", "trending stocks", "popping stocks",
            "top stocks", "best stocks", "good stocks", "growth stocks", "rising stocks",
            "stocks that might", "stocks that could", "invest in stocks",
            "investing in stocks", "stock picks", "stock tips", "tech stocks",
        )
        _REC_PHRASES_BROAD = (
            "recommend", "recommendation", "which tech", "are popping", "are surging",
            "are rising", "are up", "going up", "on the rise",
        )
        _STOCK_CONTEXT = ("stock", "market", "invest", "share", "equity", "ticker",
                          "nasdaq", "nyse", "crypto", "bitcoin", "ethereum")
        _NON_MARKET = ("laptop", "phone", "gaming", "headphone", "tablet", "monitor",
                       "keyboard", "speaker", "camera", "product", "gadget", "device",
                       "food", "recipe", "movie", "show", "game", "book", "restaurant")
        if any(p in clean for p in _REC_PHRASES_NARROW):
            return self._cmd_recommendations()
        if any(p in clean for p in _REC_PHRASES_BROAD):
            # Only intercept if there's stock context AND no obvious non-market subject
            has_stock_ctx = any(w in clean for w in _STOCK_CONTEXT)
            has_non_market = any(w in clean for w in _NON_MARKET)
            if has_stock_ctx and not has_non_market:
                return self._cmd_recommendations()

        # ── Obsidian ────────────────────────────────────────────────────────────
        if any(p in clean for p in ("save that research", "save the research", "save last report")):
            return self._cmd_save_research()

        m = re.search(r"what do i know about (.+?)(?:\s+stock)?$", clean)
        if m:
            tk = self._extract_ticker(m.group(1))
            if tk:
                return self._cmd_vault_search(tk)

        if any(p in clean for p in ("log a trade", "log trade", "record a trade")):
            return self._cmd_log_trade(text)

        return None

    # ── Command implementations ────────────────────────────────────────────────

    def _cmd_price(self, ticker: str) -> str:
        q = self._quote(ticker)
        if not q:
            return f"I couldn't get a quote for {ticker}, Boss."
        return f"{ticker} is trading at ${q.get('c',0):.2f}, {self._fmt_change(q.get('d',0), q.get('dp',0))}."

    def _cmd_research(self, ticker: str) -> str:
        lines: List[str] = []

        q = self._quote(ticker)
        if q:
            lines.append(f"Price: ${q.get('c',0):.2f}, {self._fmt_change(q.get('d',0), q.get('dp',0))}.")
            if q.get("h") and q.get("l"):
                lines.append(f"52-week range: ${q['l']:.2f} to ${q['h']:.2f}.")

        f = self._fundamentals(ticker)
        if f:
            parts = []
            if f.get("pe"):   parts.append(f"P/E {f['pe']:.1f}")
            if f.get("eps"):  parts.append(f"EPS ${f['eps']:.2f}")
            if f.get("mcap"): parts.append(f"Market cap {self._fmt_large(f['mcap'])}")
            if parts:
                lines.append("Fundamentals: " + ", ".join(parts) + ".")
            if f.get("analyst"):
                lines.append(f"Analyst consensus: {f['analyst']}.")
            if f.get("target"):
                lines.append(f"Price target: ${f['target']:.2f}.")

        t = self._technicals(ticker)
        if t:
            tp = []
            if t.get("rsi") is not None:
                rsi   = t["rsi"]
                label = "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral")
                tp.append(f"RSI {rsi:.0f} ({label})")
            if t.get("macd_signal"):
                tp.append(f"MACD {t['macd_signal']}")
            if t.get("above_50ma") is not None:
                tp.append(f"{'above' if t['above_50ma'] else 'below'} 50-day MA")
            if tp:
                lines.append("Technicals: " + ", ".join(tp) + ".")

        news = self._news_data(ticker)
        if news:
            lines.append(f"Latest news: {news[0]['headline'][:120]}.")

        report_text = " ".join(lines) if lines else f"No data available for {ticker}."
        ai_summary  = ""
        if self._brain and lines:
            try:
                ai_summary = self._brain.ask(
                    f"You are ATLAS, a voice AI. Summarise this market data for {ticker} "
                    f"in 3-4 sentences as a buy/hold/sell overview. "
                    f"Plain prose only, no markdown. Address the user as Boss.\n\n"
                    + "\n".join(lines)
                ) or ""
            except Exception:
                pass

        full = (ai_summary if ai_summary else report_text) + " " + _DISCLAIMER
        self._last_report = (ticker, "\n".join(lines) + ("\n\nAI Summary:\n" + ai_summary if ai_summary else ""))

        if self._save_obsidian:
            self._save_research_to_obsidian(
                ticker, "\n".join(lines) + ("\n\n## AI Summary\n" + ai_summary if ai_summary else "")
            )
        return full

    def _cmd_technical(self, ticker: str) -> str:
        t = self._technicals(ticker)
        if not t:
            return (f"I couldn't run technical analysis on {ticker}, Boss. "
                    "Set ALPHAVANTAGE_API_KEY for full indicator data.")
        parts: List[str] = []
        if t.get("rsi") is not None:
            rsi   = t["rsi"]
            label = ("overbought — potential selling pressure" if rsi > 70
                     else "oversold — potential buying opportunity" if rsi < 30
                     else "in neutral territory")
            parts.append(f"RSI is {rsi:.0f} — {ticker} appears {label}")
        if t.get("macd") is not None and t.get("macd_signal_line") is not None:
            bull = t["macd"] > t["macd_signal_line"]
            parts.append(f"MACD is {'bullish — upward momentum' if bull else 'bearish — downward momentum'}")
        if t.get("sma50") and t.get("price"):
            rel = "above" if t["price"] > t["sma50"] else "below"
            parts.append(f"price is {rel} the 50-day moving average of ${t['sma50']:.2f}")
        if t.get("sma200") and t.get("price"):
            rel = "above" if t["price"] > t["sma200"] else "below"
            parts.append(f"{rel} the 200-day average of ${t['sma200']:.2f}")
        if t.get("bb_upper") and t.get("price"):
            if t["price"] > t["bb_upper"]:
                parts.append("price is above the upper Bollinger Band — extended move")
            elif t.get("bb_lower") and t["price"] < t["bb_lower"]:
                parts.append("price is below the lower Bollinger Band — compressed")
        if not parts:
            return f"Technical data unavailable for {ticker}, Boss."
        return (f"Technical analysis for {ticker}, Boss. "
                + ". ".join(p.capitalize() for p in parts)
                + ". " + _DISCLAIMER)

    def _cmd_rsi_check(self, ticker: str) -> str:
        t = self._technicals(ticker)
        if not t or t.get("rsi") is None:
            return f"I couldn't get RSI data for {ticker}, Boss."
        rsi = t["rsi"]
        if rsi > 70:
            return f"{ticker} RSI is {rsi:.0f}, Boss — that is in overbought territory."
        if rsi < 30:
            return f"{ticker} RSI is {rsi:.0f}, Boss — that is in oversold territory."
        return f"{ticker} RSI is {rsi:.0f}, Boss — neutral, not overbought."

    def _cmd_trend(self, ticker: str) -> str:
        t = self._technicals(ticker)
        if not t:
            return f"I couldn't get trend data for {ticker}, Boss."
        parts: List[str] = []
        if t.get("sma50") and t.get("price"):
            parts.append(f"{'above' if t['price'] > t['sma50'] else 'below'} its 50-day average of ${t['sma50']:.2f}")
        if t.get("sma200") and t.get("price"):
            parts.append(f"{'above' if t['price'] > t['sma200'] else 'below'} the 200-day average of ${t['sma200']:.2f}")
        if not parts:
            return f"No moving average data for {ticker}, Boss."
        return f"{ticker} is trading " + " and ".join(parts) + "."

    def _cmd_compare(self, t1: str, t2: str) -> str:
        q1, q2 = self._quote(t1), self._quote(t2)
        if not q1 or not q2:
            return f"Couldn't get data for both {t1} and {t2}, Boss."
        p1, ch1 = q1.get("c", 0), q1.get("dp", 0)
        p2, ch2 = q2.get("c", 0), q2.get("dp", 0)
        winner  = t1 if ch1 > ch2 else t2
        return (f"Comparing {t1} and {t2}, Boss. "
                f"{t1} is ${p1:.2f} ({ch1:+.1f}%). "
                f"{t2} is ${p2:.2f} ({ch2:+.1f}%). "
                f"{winner} is outperforming today.")

    def _cmd_market_overview(self) -> str:
        parts: List[str] = []
        for name, sym in _INDICES.items():
            q = self._quote(sym)
            if q:
                pct = q.get("dp", 0)
                parts.append(f"{name} {'up' if pct >= 0 else 'down'} {abs(pct):.1f}%")
        if not parts:
            return "I couldn't get market data right now, Boss."
        status = "open" if self._is_market_open() else "closed"
        return f"Markets are {status}, Boss. " + ", ".join(parts) + "."

    def _cmd_market_hours(self) -> str:
        if self._is_market_open():
            now_et = datetime.now(_ET)
            close  = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
            mins   = max(0, int((close - now_et).total_seconds() / 60))
            return f"Markets are open, Boss. Closing in {mins} minutes at 4 PM Eastern."
        return "Markets are closed, Boss. US markets trade Monday to Friday, 9:30 AM to 4 PM Eastern."

    def _cmd_market_sentiment(self) -> str:
        news  = self._news_data(None)
        if not news:
            return "I couldn't get sentiment data right now, Boss."
        pos   = sum(1 for n in news if n.get("sentiment", 0) > 0.05)
        neg   = sum(1 for n in news if n.get("sentiment", 0) < -0.05)
        label = ("broadly positive" if pos > neg * 1.5
                 else "broadly negative" if neg > pos * 1.5
                 else "mixed")
        return (f"Market sentiment is {label}, Boss — "
                f"{pos} positive to {neg} negative stories in recent headlines.")

    def _cmd_portfolio(self) -> str:
        if not self._watchlist:
            return "Your watchlist is empty, Boss. Add stocks with 'Atlas add X to my watchlist'."
        results: List[Tuple[str, float, float]] = []
        for sym in self._watchlist:
            q = self._quote(sym)
            if q:
                results.append((sym, q.get("c", 0), q.get("dp", 0)))
        if not results:
            return "I couldn't get prices for your watchlist right now, Boss."
        parts     = [f"{s} ${p:.2f} ({c:+.1f}%)" for s, p, c in results]
        by_change = sorted(results, key=lambda x: x[2], reverse=True)
        best, worst = by_change[0], by_change[-1]
        avg_pct   = sum(r[2] for r in results) / len(results)
        direction = "up" if avg_pct >= 0 else "down"
        return (f"Watchlist update, Boss. {', '.join(parts)}. "
                f"Best: {best[0]} at {best[2]:+.1f}%. "
                f"Worst: {worst[0]} at {worst[2]:+.1f}%. "
                f"Overall {direction} {abs(avg_pct):.1f}% today.")

    def _cmd_best_performer(self) -> str:
        if not self._watchlist:
            return "Your watchlist is empty, Boss."
        best = max(
            ((s, self._quote(s)) for s in self._watchlist),
            key=lambda x: x[1].get("dp", -999) if x[1] else -999,
        )
        if not best[1]:
            return "Couldn't get data right now, Boss."
        sym, q = best
        return f"Your best performer today is {sym} at ${q['c']:.2f}, up {q.get('dp',0):.1f}%, Boss."

    def _cmd_worst_performer(self) -> str:
        if not self._watchlist:
            return "Your watchlist is empty, Boss."
        worst = min(
            ((s, self._quote(s)) for s in self._watchlist),
            key=lambda x: x[1].get("dp", 999) if x[1] else 999,
        )
        if not worst[1]:
            return "Couldn't get data right now, Boss."
        sym, q = worst
        pct = q.get("dp", 0)
        return f"Your worst performer today is {sym} at ${q['c']:.2f}, {'down' if pct < 0 else 'up'} {abs(pct):.1f}%, Boss."

    def _cmd_add_watchlist(self, ticker: str) -> str:
        if ticker not in self._watchlist:
            self._watchlist.append(ticker)
            self._persist_watchlist()
            return f"Added {ticker} to your watchlist, Boss."
        return f"{ticker} is already on your watchlist, Boss."

    def _cmd_remove_watchlist(self, ticker: str) -> str:
        if ticker in self._watchlist:
            self._watchlist.remove(ticker)
            self._persist_watchlist()
            return f"Removed {ticker} from your watchlist, Boss."
        return f"{ticker} was not on your watchlist, Boss."

    def _cmd_market_news(self) -> str:
        news = self._news_data(None)
        if not news:
            return "I couldn't fetch market news right now, Boss."
        headlines = [n["headline"][:100] for n in news[:5]]
        return "Top market headlines, Boss: " + ". Next: ".join(headlines) + "."

    def _cmd_stock_news(self, ticker: str) -> str:
        news = self._news_data(ticker)
        if not news:
            return f"No recent news for {ticker}, Boss."
        return f"Recent {ticker} news, Boss: " + ". ".join(n["headline"][:100] for n in news[:3]) + "."

    def _cmd_sentiment(self, ticker: str) -> str:
        news = self._news_data(ticker)
        if not news:
            return f"No sentiment data available for {ticker}, Boss."
        avg   = sum(n.get("sentiment", 0) for n in news) / len(news)
        label = "positive" if avg > 0.1 else "negative" if avg < -0.1 else "neutral"
        return f"News sentiment on {ticker} is {label}, Boss, based on {len(news)} recent articles."

    def _cmd_earnings(self, ticker: str) -> str:
        e = self._earnings_data(ticker)
        if e and e.get("next_date"):
            return f"{ticker} reports earnings on {e['next_date']}, Boss."
        return f"No upcoming earnings date found for {ticker}, Boss."

    def _cmd_earnings_calendar(self) -> str:
        if not self._finnhub_key:
            return "A Finnhub API key is needed for the earnings calendar, Boss."
        try:
            client = self._finnhub_client()
            today  = date.today()
            cal    = client.earnings_calendar(
                _from=today.isoformat(), to=(today + timedelta(days=7)).isoformat(),
                symbol="", international=False,
            )
            events = (cal.get("earningsCalendar") or [])[:8]
            if not events:
                return "No major earnings reports this week, Boss."
            return "Earnings this week, Boss: " + ", ".join(f"{e['symbol']} on {e['date']}" for e in events) + "."
        except Exception as exc:
            log.warning("Earnings calendar: %s", exc)
            return "I couldn't get the earnings calendar right now, Boss."

    def _cmd_last_earnings(self, ticker: str) -> str:
        e = self._earnings_data(ticker)
        if not e or not e.get("last"):
            return f"No previous earnings data for {ticker}, Boss."
        last   = e["last"]
        result = "beat" if last.get("beat") else "missed"
        return (f"{ticker} last reported EPS of ${last.get('actual','N/A')} "
                f"vs estimate of ${last.get('estimate','N/A')}, Boss. They {result} expectations.")

    def _cmd_smart_analysis(self, ticker: str) -> str:
        data_parts: List[str] = []

        q = self._quote(ticker)
        if q:
            data_parts.append(f"Price: ${q.get('c',0):.2f}, change {q.get('dp',0):+.1f}%")

        f = self._fundamentals(ticker)
        if f:
            if f.get("pe"):      data_parts.append(f"P/E ratio: {f['pe']:.1f}")
            if f.get("eps"):     data_parts.append(f"EPS: ${f['eps']:.2f}")
            if f.get("analyst"): data_parts.append(f"Analyst consensus: {f['analyst']}")
            if f.get("target"):  data_parts.append(f"Price target: ${f['target']:.2f}")

        t = self._technicals(ticker)
        if t:
            if t.get("rsi") is not None:
                rsi = t["rsi"]
                data_parts.append(f"RSI: {rsi:.0f} ({'overbought' if rsi>70 else 'oversold' if rsi<30 else 'neutral'})")
            if t.get("macd_signal"):
                data_parts.append(f"MACD: {t['macd_signal']}")
            if t.get("above_50ma") is not None:
                data_parts.append(f"50-day MA: {'above' if t['above_50ma'] else 'below'}")

        news = self._news_data(ticker)
        if news:
            avg = sum(n.get("sentiment", 0) for n in news) / len(news)
            data_parts.append(f"News sentiment: {'positive' if avg>0.1 else 'negative' if avg<-0.1 else 'neutral'}")

        if not data_parts:
            return f"I couldn't gather enough data to analyse {ticker} right now, Boss."

        if self._brain:
            try:
                response = self._brain.ask(
                    f"You are ATLAS, a voice AI assistant. The user named Boss is asking for analysis "
                    f"on {ticker}. Market data:\n"
                    + "\n".join(f"- {p}" for p in data_parts)
                    + "\nProvide a balanced 4-5 sentence analysis. Do NOT give a direct buy/sell "
                    "recommendation. End with 'Based on the data Boss, here is what the indicators "
                    "suggest:' and a one-sentence summary. Plain prose only, no markdown."
                ) or ""
                if response:
                    return response + " " + _DISCLAIMER
            except Exception:
                pass

        return f"Here is the data on {ticker}, Boss: " + ". ".join(data_parts) + ". " + _DISCLAIMER

    def _cmd_recommendations(self) -> str:
        """Return a data-driven market snapshot with top movers from the watchlist."""
        parts: List[str] = []

        # Watchlist performance
        movers: List[tuple] = []
        for tk in list(self._watchlist)[:8]:
            q = self._quote(tk)
            if q and q.get("dp") is not None:
                movers.append((tk, float(q["c"]), float(q["dp"])))

        if movers:
            movers.sort(key=lambda x: x[2], reverse=True)
            top = [f"{tk} {dp:+.1f}%" for tk, _, dp in movers[:3] if dp > 0]
            bot = [f"{tk} {dp:+.1f}%" for tk, _, dp in movers[-2:] if dp < 0]
            if top:
                parts.append("Top movers on your watchlist: " + ", ".join(top))
            if bot:
                parts.append("Lagging: " + ", ".join(bot))

        # Market overview
        overview = self._cmd_market_overview()
        if overview:
            parts.append(overview)

        if not parts:
            return (
                f"I can't pull live data right now, Boss. "
                f"Say 'atlas market overview' to check indices, or 'atlas research AAPL' "
                f"for a full report on any ticker. {_DISCLAIMER}"
            )

        summary = " ".join(parts)
        if self._brain:
            try:
                response = self._brain.ask(
                    "You are ATLAS. The user Boss asked for stock ideas. "
                    "Here is today's live market data:\n" + summary + "\n"
                    "Give a brief 3-4 sentence commentary on what looks interesting based purely "
                    "on the data above. Do not invent data. Do not give direct buy/sell advice. "
                    "Plain prose, no markdown, no asterisks."
                ) or ""
                if response:
                    return response + " " + _DISCLAIMER
            except Exception:
                pass

        return summary + " " + _DISCLAIMER

    def _cmd_save_research(self) -> str:
        if not self._last_report:
            return "No recent research to save, Boss."
        ticker, report = self._last_report
        self._save_research_to_obsidian(ticker, report)
        return f"Research on {ticker} saved to your Obsidian vault, Boss."

    def _cmd_vault_search(self, ticker: str) -> str:
        if not self._obsidian:
            return "Obsidian is not connected, Boss."
        try:
            if hasattr(self._obsidian, "search_notes"):
                notes = self._obsidian.search_notes(ticker)
                if notes:
                    return f"I found {len(notes)} notes on {ticker} in your vault, Boss."
            return f"No previous research on {ticker} in your vault, Boss."
        except Exception:
            return "I couldn't search your vault right now, Boss."

    def _cmd_log_trade(self, text: str) -> str:
        if not self._obsidian:
            return "Obsidian is not connected for trade logging, Boss."
        entry = f"\n## Trade — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{text}\n"
        try:
            if hasattr(self._obsidian, "append_to_note"):
                self._obsidian.append_to_note("Research/Market/trades.md", entry)
            else:
                self._obsidian.write_note("Research/Market/trades.md", entry)
            return "Trade logged to your Obsidian vault, Boss."
        except Exception as exc:
            return f"Couldn't log trade: {exc}"

    # ── Data fetchers ──────────────────────────────────────────────────────────

    def _quote(self, ticker: str) -> Optional[dict]:
        key    = f"quote_{ticker}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        if self._finnhub_key:
            try:
                q = self._finnhub_client().quote(ticker)
                if q and q.get("c", 0) > 0:
                    self._cache_set(key, q)
                    return q
            except Exception as exc:
                log.debug("Finnhub quote %s: %s", ticker, exc)

        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="2d")
            if not hist.empty:
                c  = float(hist["Close"].iloc[-1])
                pc = float(hist["Close"].iloc[-2]) if len(hist) > 1 else c
                d  = c - pc
                dp = (d / pc * 100) if pc else 0
                q  = {"c": c, "d": d, "dp": dp,
                      "h": float(hist["High"].max()), "l": float(hist["Low"].min()),
                      "o": float(hist["Open"].iloc[-1]), "pc": pc}
                self._cache_set(key, q)
                return q
        except Exception as exc:
            log.debug("yfinance quote %s: %s", ticker, exc)

        return None

    def _fundamentals(self, ticker: str) -> Optional[dict]:
        key    = f"fund_{ticker}"
        cached = self._cache_get(key, ttl=3600)
        if cached is not None:
            return cached

        result: dict = {}

        if self._finnhub_key:
            try:
                client = self._finnhub_client()
                bf = client.company_basic_financials(ticker, "all")
                m  = bf.get("metric", {})
                if m.get("peBasicExclExtraTTM"):
                    result["pe"]  = m["peBasicExclExtraTTM"]
                if m.get("epsBasicExclExtraItemsTTM"):
                    result["eps"] = m["epsBasicExclExtraItemsTTM"]

                rec = client.recommendation_trends(ticker)
                if rec:
                    r   = rec[0]
                    sb, b  = r.get("strongBuy", 0), r.get("buy", 0)
                    h, s   = r.get("hold", 0), r.get("sell", 0) + r.get("strongSell", 0)
                    tot    = sb + b + h + s or 1
                    bp     = (sb + b) / tot * 100
                    result["analyst"] = (
                        f"Buy ({bp:.0f}% of analysts bullish)" if bp > 60
                        else "Sell (majority bearish)" if s / tot > 0.4
                        else "Hold (mixed opinion)"
                    )
                pt = client.price_target(ticker)
                if pt and pt.get("targetMean"):
                    result["target"] = pt["targetMean"]
            except Exception as exc:
                log.debug("Finnhub fundamentals %s: %s", ticker, exc)

        if not result.get("pe"):
            try:
                import yfinance as yf
                info = yf.Ticker(ticker).info
                if info.get("trailingPE"):  result["pe"]   = info["trailingPE"]
                if info.get("trailingEps"): result["eps"]  = info["trailingEps"]
                if info.get("marketCap"):   result["mcap"] = info["marketCap"]
            except Exception:
                pass

        if result:
            self._cache_set(key, result, ttl=3600)
        return result or None

    def _technicals(self, ticker: str) -> Optional[dict]:
        key    = f"tech_{ticker}"
        cached = self._cache_get(key, ttl=300)
        if cached is not None:
            return cached

        result: dict = {}
        q = self._quote(ticker)
        if q:
            result["price"] = q.get("c", 0)

        if self._av_key:
            def _av(fn: str, extra=None) -> Optional[dict]:
                if not self._av_rate_ok():
                    return None
                import requests
                try:
                    params = {"function": fn, "symbol": ticker, "interval": "daily",
                              "apikey": self._av_key, **(extra or {})}
                    r    = requests.get("https://www.alphavantage.co/query", params=params, timeout=12)
                    data = r.json()
                    if "Note" in data or "Information" in data:
                        return None
                    return data
                except Exception as e:
                    log.debug("AV %s %s: %s", fn, ticker, e)
                    return None

            data = _av("RSI", {"time_period": "14", "series_type": "close"})
            if data and "Technical Analysis: RSI" in data:
                k = next(iter(data["Technical Analysis: RSI"]))
                result["rsi"] = float(data["Technical Analysis: RSI"][k]["RSI"])

            data = _av("MACD", {"series_type": "close"})
            if data and "Technical Analysis: MACD" in data:
                k   = next(iter(data["Technical Analysis: MACD"]))
                row = data["Technical Analysis: MACD"][k]
                result["macd"]             = float(row.get("MACD", 0))
                result["macd_signal_line"] = float(row.get("MACD_Signal", 0))
                result["macd_signal"]      = "bullish" if result["macd"] > result["macd_signal_line"] else "bearish"

            data = _av("SMA", {"time_period": "50", "series_type": "close"})
            if data and "Technical Analysis: SMA" in data:
                k = next(iter(data["Technical Analysis: SMA"]))
                result["sma50"] = float(data["Technical Analysis: SMA"][k]["SMA"])
                if result.get("price"):
                    result["above_50ma"] = result["price"] > result["sma50"]

            data = _av("SMA", {"time_period": "200", "series_type": "close"})
            if data and "Technical Analysis: SMA" in data:
                k = next(iter(data["Technical Analysis: SMA"]))
                result["sma200"] = float(data["Technical Analysis: SMA"][k]["SMA"])

            data = _av("BBANDS", {"time_period": "20", "series_type": "close"})
            if data and "Technical Analysis: BBANDS" in data:
                k   = next(iter(data["Technical Analysis: BBANDS"]))
                row = data["Technical Analysis: BBANDS"][k]
                result["bb_upper"]  = float(row.get("Real Upper Band", 0))
                result["bb_lower"]  = float(row.get("Real Lower Band", 0))
                result["bb_middle"] = float(row.get("Real Middle Band", 0))

        if "rsi" not in result or not result.get("sma50"):
            try:
                import yfinance as yf
                hist  = yf.Ticker(ticker).history(period="1y")
                close = hist["Close"]
                if "rsi" not in result and len(close) >= 14:
                    delta = close.diff()
                    gain  = delta.where(delta > 0, 0.0).rolling(14).mean()
                    loss  = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
                    rs    = gain / loss
                    result["rsi"] = float((100 - (100 / (1 + rs))).iloc[-1])
                if not result.get("sma50") and len(close) >= 50:
                    result["sma50"] = float(close.rolling(50).mean().iloc[-1])
                    if result.get("price"):
                        result["above_50ma"] = result["price"] > result["sma50"]
                if not result.get("sma200") and len(close) >= 200:
                    result["sma200"] = float(close.rolling(200).mean().iloc[-1])
            except Exception as exc:
                log.debug("yfinance technicals %s: %s", ticker, exc)

        if result:
            self._cache_set(key, result, ttl=300)
        return result or None

    def _news_data(self, ticker: Optional[str]) -> List[dict]:
        key    = f"news_{ticker or 'market'}"
        cached = self._cache_get(key, ttl=1800)
        if cached is not None:
            return cached

        if self._finnhub_key:
            try:
                client   = self._finnhub_client()
                today    = date.today()
                week_ago = today - timedelta(days=7)
                raw      = (client.company_news(ticker, _from=week_ago.isoformat(), to=today.isoformat())
                            if ticker else client.general_news("general", min_id=0))
                news = [{"headline": n.get("headline", ""), "url": n.get("url", ""),
                         "sentiment": n.get("sentiment", 0)}
                        for n in (raw or [])[:10] if n.get("headline")]
                if news:
                    self._cache_set(key, news, ttl=1800)
                    return news
            except Exception as exc:
                log.debug("Finnhub news: %s", exc)

        try:
            from ddgs import DDGS
            query = f"{ticker} stock news" if ticker else "stock market news today"
            with DDGS() as ddgs:
                results = list(ddgs.news(query, max_results=5))
            news = [{"headline": r.get("title", ""), "url": r.get("url", ""), "sentiment": 0}
                    for r in results if r.get("title")]
            self._cache_set(key, news, ttl=1800)
            return news
        except Exception:
            pass

        return []

    def _earnings_data(self, ticker: str) -> Optional[dict]:
        key    = f"earn_{ticker}"
        cached = self._cache_get(key, ttl=3600)
        if cached is not None:
            return cached

        result: dict = {}
        if self._finnhub_key:
            try:
                client = self._finnhub_client()
                today  = date.today()
                cal    = client.earnings_calendar(
                    _from=today.isoformat(), to=(today + timedelta(days=90)).isoformat(),
                    symbol=ticker, international=False,
                )
                events = cal.get("earningsCalendar", [])
                if events:
                    result["next_date"] = events[0]["date"]
                hist = client.company_earnings(ticker, limit=2)
                if hist:
                    last = hist[0]
                    act, est = last.get("actual"), last.get("estimate")
                    result["last"] = {
                        "actual": act, "estimate": est,
                        "beat": bool(act is not None and est is not None and act > est),
                        "date": last.get("period"),
                    }
                self._cache_set(key, result, ttl=3600)
                return result
            except Exception as exc:
                log.debug("Finnhub earnings %s: %s", ticker, exc)

        try:
            import yfinance as yf
            cal = yf.Ticker(ticker).calendar
            if cal is not None and not cal.empty:
                result["next_date"] = str(cal.iloc[0].get("Earnings Date", ""))
                self._cache_set(key, result, ttl=3600)
                return result
        except Exception:
            pass

        return None

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _finnhub_client(self):
        import finnhub
        return finnhub.Client(api_key=self._finnhub_key)

    def _extract_ticker(self, text: str) -> Optional[str]:
        text = text.strip().lower().rstrip("?. ")
        for name, sym in _COMPANY_MAP.items():
            if name in text:
                return sym
        m = re.search(r'\b([A-Z]{1,5})\b', text.upper())
        if m and m.group(1) not in {"I", "A", "AN", "THE", "MY", "IT", "AT", "ON", "IS", "TO"}:
            return m.group(1)
        for w in [w.strip(".,?!") for w in text.split()]:
            if w.isalpha() and 1 <= len(w) <= 5:
                return w.upper()
        return None

    def _is_market_open(self) -> bool:
        now = datetime.now(_ET)
        if now.weekday() >= 5:
            return False
        return _MARKET_OPEN <= (now.hour, now.minute) < _MARKET_CLOSE

    def _fmt_change(self, change: float, pct: float) -> str:
        return f"{'up' if change >= 0 else 'down'} ${abs(change):.2f} ({abs(pct):.2f}%)"

    def _fmt_large(self, n: float) -> str:
        if n >= 1e12: return f"${n/1e12:.1f}T"
        if n >= 1e9:  return f"${n/1e9:.1f}B"
        if n >= 1e6:  return f"${n/1e6:.1f}M"
        return f"${n:.0f}"

    def _cache_get(self, key: str, ttl: int | None = None) -> Any:
        entry = self._cache.get(key)
        if entry and time.time() - entry["ts"] < (ttl or self._cache_ttl):
            return entry["data"]
        return None

    def _cache_set(self, key: str, data: Any, ttl: int | None = None) -> None:
        self._cache[key] = {"data": data, "ts": time.time()}

    def _av_rate_ok(self) -> bool:
        now = time.time()
        with self._av_lock:
            self._av_calls = [t for t in self._av_calls if now - t < 60]
            if len(self._av_calls) >= 5:
                return False
            self._av_calls.append(now)
            return True

    def _save_research_to_obsidian(self, ticker: str, report: str) -> None:
        if not self._obsidian:
            return
        today   = date.today().isoformat()
        path    = f"Research/Market/{ticker}-{today}.md"
        content = f"# {ticker} Research — {today}\n\n{report}\n\n---\n*{_DISCLAIMER}*\n"
        try:
            if hasattr(self._obsidian, "write_note"):
                self._obsidian.write_note(path, content)
                log.info("Market research saved: %s", path)
        except Exception as exc:
            log.warning("Obsidian save failed: %s", exc)

    def _persist_watchlist(self) -> None:
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            cfg["watchlist"] = self._watchlist
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as exc:
            log.warning("Could not persist watchlist: %s", exc)

    def _bg_update_loop(self) -> None:
        while not self._stop_ev.is_set():
            if not self._hours_only or self._is_market_open():
                for sym in list(self._watchlist):
                    self._cache.pop(f"quote_{sym}", None)
                    self._quote(sym)
                    if self._stop_ev.wait(2):
                        return
            self._stop_ev.wait(self._update_interval)
