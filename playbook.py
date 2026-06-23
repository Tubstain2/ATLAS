"""
ATLAS Playbook System — ACE-inspired living strategy document

Three agents:
  Generator  — enriches AI prompts with relevant entries before every request (≤500 tokens)
  Reflector  — analyses interaction quality after each turn (async background thread)
  Curator    — applies delta-only updates to playbook based on Reflector output

Storage: Obsidian vault via VaultBrain (single source of truth)
  ATLAS/Playbook/Strategies/[slug].md
  ATLAS/Playbook/Pitfalls/[slug].md
  ATLAS/Playbook/Preferences/[slug].md

Voice commands:
  "ATLAS what have you learned"     → top 5 strategies
  "ATLAS what do you know about me" → user preferences
  "ATLAS remember that"             → mark last interaction positive
  "ATLAS forget that"               → mark last interaction negative / add pitfall
  "ATLAS show your playbook"        → summary in feed panel
  "ATLAS reset your playbook"       → clear after double-confirmation
  "ATLAS privacy mode"              → pause memory writes (reads still work)
  "ATLAS go dark"                   → disable all memory I/O
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.80
_MAX_ENTRY_CHARS      = 200
_INJECTION_MAX_CHARS  = 2000


# ── Signals ───────────────────────────────────────────────────────────────────

_POSITIVE_SIGNALS = frozenset({
    "thanks", "thank you", "good", "great", "perfect", "excellent",
    "that's right", "thats right", "correct", "exactly", "nice", "awesome",
    "well done", "brilliant", "yes exactly", "that's it", "spot on",
})
_NEGATIVE_SIGNALS = frozenset({
    "wrong", "no that's wrong", "not right", "incorrect", "that's wrong",
    "thats wrong", "not what i wanted", "bad", "stop", "not helpful",
    "you misunderstood", "that wasn't right", "not like that",
})


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _detect_category(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("code", "python", "function", "widget", "script",
                                 "bug", "error", "import", "class", "def ")):
        return "coding"
    if any(w in lower for w in ("stock", "market", "price", "trade", "invest",
                                 "ticker", "earnings", "rsi", "macd")):
        return "market"
    if any(w in lower for w in ("search", "look up", "research", "news",
                                 "weather", "find information")):
        return "research"
    if any(w in lower for w in ("note", "task", "obsidian", "vault",
                                 "reminder", "daily", "inbox")):
        return "obsidian"
    if any(w in lower for w in ("remember", "memory", "forget", "recall",
                                 "last time", "last session")):
        return "memory"
    return "general"


def _word_overlap(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _feedback_signal(text: str) -> str:
    lower = text.lower().strip()
    if any(sig in lower for sig in _POSITIVE_SIGNALS):
        return "positive"
    if any(sig in lower for sig in _NEGATIVE_SIGNALS):
        return "negative"
    return "neutral"


# ── PlaybookModule ─────────────────────────────────────────────────────────────

class PlaybookModule:
    """ACE-inspired living playbook with Generator / Reflector / Curator agents.

    Storage backend is VaultBrain — all data lives as readable .md files
    inside the Obsidian vault at ATLAS/Playbook/.
    """

    def __init__(self, config: dict, brain=None, feed_cb=None,
                 vault_brain=None):
        self._cfg       = config
        self._brain     = brain
        self._feed_cb   = feed_cb
        self._vb        = vault_brain     # VaultBrain instance (may be None)
        self._user_name = config.get("user_name", "Boss")

        self._enabled          = bool(config.get("playbook_enabled", True))
        self._max_entries      = int(config.get("playbook_max_entries", 500))
        self._update_threshold = float(config.get("playbook_update_threshold", 0.7))
        self._dedup_days       = int(config.get("playbook_dedup_interval_days", 7))

        self._lock    = threading.Lock()
        self._pending: Optional[dict] = None
        self._privacy  = False
        self._dark     = False
        self._confirm_reset = False

        # In-memory cache — loaded from vault on init
        self._pb: Dict[str, List[dict]] = self._load_from_vault()

        self._schedule_dedup()

        n_s  = len(self._pb.get("strategies", []))
        n_p  = len(self._pb.get("pitfalls", []))
        n_pr = len(self._pb.get("user_preferences", []))
        log.info("PlaybookModule: %d strategies, %d pitfalls, %d preferences "
                 "(vault storage: %s).", n_s, n_p, n_pr,
                 "on" if self._vb else "off")

    # ── Vault I/O ─────────────────────────────────────────────────────────────

    def _load_from_vault(self) -> dict:
        """Build the in-memory _pb cache by reading vault files."""
        pb: Dict[str, list] = {
            "strategies": [], "pitfalls": [], "user_preferences": [],
            "domain_knowledge": [], "_last_dedup": _ts(),
        }
        if not self._vb:
            return pb

        # Strategies
        for p in self._vb.list_notes(self._vb.strat_dir):
            r = self._vb.read_note(p)
            if not r:
                continue
            fm, body = r
            desc = ""
            for line in body.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    desc = stripped[:_MAX_ENTRY_CHARS]
                    break
            pb["strategies"].append({
                "id":            p.stem,
                "category":      fm.get("category", "general"),
                "title":         fm.get("title", p.stem.replace("-", " ").title()),
                "description":   desc,
                "helpful_count": int(fm.get("helpful_count", 0)),
                "harmful_count": int(fm.get("harmful_count", 0)),
                "last_updated":  str(fm.get("last_updated", "")),
                "tags":          list(fm.get("tags", [])),
            })

        # Pitfalls
        for p in self._vb.list_notes(self._vb.pitfall_dir):
            r = self._vb.read_note(p)
            if not r:
                continue
            fm, body = r
            desc = ""
            for line in body.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    desc = stripped[:_MAX_ENTRY_CHARS]
                    break
            pb["pitfalls"].append({
                "id":               p.stem,
                "category":         fm.get("category", "general"),
                "title":            fm.get("title", p.stem.replace("-", " ").title()),
                "description":      desc,
                "occurrence_count": int(fm.get("occurrence_count", 0)),
                "last_seen":        str(fm.get("last_seen", "")),
            })

        # Preferences
        for p in self._vb.list_notes(self._vb.pref_dir):
            r = self._vb.read_note(p)
            if not r:
                continue
            fm, _ = r
            pb["user_preferences"].append({
                "id":           p.stem,
                "preference":   str(fm.get("title", p.stem.replace("-", " "))),
                "confidence":   float(fm.get("confidence", 0.7)),
                "strength":     str(fm.get("strength", "medium")),
                "learned_from": str(fm.get("learned", "")),
            })

        return pb

    def _sync_strategy_to_vault(self, entry: dict) -> None:
        if not self._vb:
            return
        self._vb.write_strategy(
            title       = entry.get("title", ""),
            description = entry.get("description", ""),
            why         = "Learned from interaction history.",
            category    = entry.get("category", "general"),
            helpful     = entry.get("helpful_count", 0),
            harmful     = entry.get("harmful_count", 0),
        )

    def _sync_pitfall_to_vault(self, entry: dict) -> None:
        if not self._vb:
            return
        self._vb.write_pitfall(
            title       = entry.get("title", ""),
            description = entry.get("description", ""),
            category    = entry.get("category", "general"),
        )

    def _sync_preference_to_vault(self, pref: dict) -> None:
        if not self._vb:
            return
        self._vb.add_preference(
            preference = pref.get("preference", ""),
            strength   = pref.get("strength", "medium"),
            confidence = float(pref.get("confidence", 0.7)),
        )

    # ── Generator ─────────────────────────────────────────────────────────────

    def get_prompt_injection(self, query: str, category: str = "") -> str:
        """Returns a compact playbook context block (≤500 tokens)."""
        if not self._enabled or self._dark:
            return ""

        # Delegate to vault if available (reads live files)
        if self._vb:
            return self._vb.get_playbook_context(query, max_chars=_INJECTION_MAX_CHARS)

        # Fallback: use in-memory cache
        cat   = category or _detect_category(query)
        lines: List[str] = []

        strats = [s for s in self._pb.get("strategies", [])
                  if s.get("category") in (cat, "general")]
        strats.sort(key=lambda s: s.get("helpful_count", 0) - s.get("harmful_count", 0),
                    reverse=True)
        for s in strats[:3]:
            desc = s.get("description", s.get("title", ""))[:_MAX_ENTRY_CHARS]
            lines.append(f"• STRATEGY [{s['category']}]: {s['title']} — {desc}")

        pitfalls = [p for p in self._pb.get("pitfalls", [])
                    if p.get("category") in (cat, "general")]
        pitfalls.sort(key=lambda p: p.get("occurrence_count", 0), reverse=True)
        for p in pitfalls[:2]:
            desc = p.get("description", p.get("title", ""))[:_MAX_ENTRY_CHARS]
            lines.append(f"• AVOID [{p['category']}]: {p['title']} — {desc}")

        prefs = sorted(self._pb.get("user_preferences", []),
                       key=lambda p: float(p.get("confidence", 0)), reverse=True)
        for pr in prefs[:3]:
            lines.append(f"• USER PREFERENCE: {pr['preference'][:_MAX_ENTRY_CHARS]}")

        if not lines:
            return ""
        return ("PLAYBOOK (apply these learned strategies):\n" + "\n".join(lines))[:_INJECTION_MAX_CHARS]

    # ── Reflector handshake ────────────────────────────────────────────────────

    def on_atlas_response(self, user_text: str, atlas_response: str) -> None:
        if not self._enabled or self._privacy or self._dark:
            return
        self._pending = {
            "user_text":      user_text,
            "atlas_response": atlas_response,
            "category":       _detect_category(user_text),
            "ts":             _ts(),
        }

    def on_user_turn(self, user_text: str) -> None:
        if not self._pending or self._privacy or self._dark:
            return
        pending         = self._pending
        self._pending   = None
        quality         = _feedback_signal(user_text)
        if quality != "neutral" or len(pending["user_text"].split()) > 5:
            threading.Thread(
                target=self._reflect_and_curate,
                args=(pending, quality),
                daemon=True,
                name="atlas-reflector",
            ).start()

    def mark_positive(self) -> str:
        if self._pending:
            p, self._pending = self._pending, None
            threading.Thread(target=self._reflect_and_curate, args=(p, "positive"),
                             daemon=True, name="atlas-reflector-pos").start()
            return f"Got it, {self._user_name}. I'll remember what worked there."
        return f"No recent interaction to save, {self._user_name}."

    def mark_negative(self) -> str:
        if self._pending:
            p, self._pending = self._pending, None
            threading.Thread(target=self._reflect_and_curate, args=(p, "negative"),
                             daemon=True, name="atlas-reflector-neg").start()
            return f"Understood, {self._user_name}. I'll note that as something to avoid."
        return f"No recent interaction to flag, {self._user_name}."

    # ── Reflector (background) ─────────────────────────────────────────────────

    def _reflect_and_curate(self, pending: dict, quality: str) -> None:
        try:
            reflection = self._reflect(pending, quality)
            if reflection:
                self._curate(reflection)
        except Exception as exc:
            log.warning("Reflector error: %s", exc)

    def _reflect(self, pending: dict, quality: str) -> Optional[dict]:
        cat        = pending.get("category", "general")
        utxt       = pending.get("user_text", "")
        atxt       = pending.get("atlas_response", "")
        confidence = 0.5
        new_insight    = None
        pref_signal    = None
        strats_worked: List[str] = []
        strats_failed: List[str] = []

        if quality == "positive":
            confidence = 0.85
            for s in self._pb.get("strategies", []):
                if s.get("category") == cat:
                    strats_worked.append(s["id"])
                    break

        elif quality == "negative":
            confidence  = 0.90
            new_insight = f"Response approach caused negative feedback for '{cat}' queries"
            pref_signal = f"User disliked: {atxt[:80]}"
            for s in self._pb.get("strategies", []):
                if s.get("category") == cat:
                    strats_failed.append(s["id"])
                    break

        elif quality == "neutral" and self._brain and len(utxt.split()) > 8:
            try:
                raw = self._brain.ask(
                    "You are ATLAS's reflection agent. Evaluate this interaction and reply "
                    "with a JSON object ONLY (no markdown):\n\n"
                    f'User: "{utxt[:250]}"\n'
                    f'ATLAS: "{atxt[:250]}"\n\n'
                    'Reply format: {"quality":"positive/negative/neutral",'
                    '"new_insight":"one sentence or null",'
                    '"preference_signal":"one sentence or null",'
                    '"confidence":0.0}'
                )
                clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
                data  = json.loads(clean)
                quality     = data.get("quality", "neutral")
                new_insight = data.get("new_insight") or new_insight
                pref_signal = data.get("preference_signal") or pref_signal
                confidence  = float(data.get("confidence", 0.5))
            except Exception:
                pass

        if confidence < self._update_threshold:
            return None

        return {
            "interaction_quality":    quality,
            "strategies_that_worked": strats_worked,
            "strategies_that_failed": strats_failed,
            "new_insight":            new_insight,
            "user_preference_signal": pref_signal,
            "category":               cat,
            "confidence":             confidence,
            "ts":                     _ts(),
        }

    # ── Curator (delta-only vault updates) ────────────────────────────────────

    def _curate(self, r: dict) -> None:
        quality  = r.get("interaction_quality", "neutral")
        cat      = r.get("category", "general")
        insight  = r.get("new_insight")
        pref_sig = r.get("user_preference_signal")

        with self._lock:
            pb = self._pb

            # Increment strategy counters in vault
            for sid in r.get("strategies_that_worked", []):
                for s in pb.get("strategies", []):
                    if s["id"] == sid:
                        s["helpful_count"] = s.get("helpful_count", 0) + 1
                        s["last_updated"]  = _ts()
                        if self._vb:
                            self._vb.increment_strategy_count(sid, helpful=True)

            for sid in r.get("strategies_that_failed", []):
                for s in pb.get("strategies", []):
                    if s["id"] == sid:
                        s["harmful_count"] = s.get("harmful_count", 0) + 1
                        s["last_updated"]  = _ts()
                        if self._vb:
                            self._vb.increment_strategy_count(sid, helpful=False)

            # Add new strategy to vault + in-memory cache
            if insight and not self._is_dup(insight, pb.get("strategies", []), "description"):
                entry = {
                    "id":            insight[:50].lower().replace(" ", "-"),
                    "category":      cat,
                    "title":         insight[:60],
                    "description":   insight[:_MAX_ENTRY_CHARS],
                    "helpful_count": 1 if quality == "positive" else 0,
                    "harmful_count": 1 if quality == "negative" else 0,
                    "last_updated":  _ts(),
                    "tags":          [cat],
                }
                pb.setdefault("strategies", []).append(entry)
                self._sync_strategy_to_vault(entry)

            # Add pitfall
            if quality == "negative" and insight:
                if not self._is_dup(insight, pb.get("pitfalls", []), "description"):
                    pf = {
                        "id":               insight[:50].lower().replace(" ", "-"),
                        "category":         cat,
                        "title":            insight[:60],
                        "description":      insight[:_MAX_ENTRY_CHARS],
                        "occurrence_count": 1,
                        "last_seen":        _ts(),
                    }
                    pb.setdefault("pitfalls", []).append(pf)
                    self._sync_pitfall_to_vault(pf)
                else:
                    for p in pb.get("pitfalls", []):
                        if _word_overlap(p.get("description", ""), insight) > 0.5:
                            p["occurrence_count"] = p.get("occurrence_count", 0) + 1
                            p["last_seen"] = _ts()
                            if self._vb:
                                self._vb.write_pitfall(
                                    p.get("title", ""), p.get("description", ""),
                                    cat, p["occurrence_count"]
                                )
                            break

            # Add preference
            if pref_sig and not self._is_dup(pref_sig, pb.get("user_preferences", []), "preference"):
                pref_entry = {
                    "id":           pref_sig[:50].lower().replace(" ", "-"),
                    "preference":   pref_sig[:_MAX_ENTRY_CHARS],
                    "confidence":   r.get("confidence", 0.7),
                    "strength":     "medium",
                    "learned_from": _ts(),
                }
                pb.setdefault("user_preferences", []).append(pref_entry)
                self._sync_preference_to_vault(pref_entry)

            self._prune(pb)

        log.info("Curator: updated playbook → vault [quality=%s cat=%s].", quality, cat)

    # ── Dedup ──────────────────────────────────────────────────────────────────

    def _schedule_dedup(self) -> None:
        last = self._pb.get("_last_dedup")
        if last:
            try:
                delta = datetime.now() - datetime.fromisoformat(last)
                if delta.days < self._dedup_days:
                    return
            except Exception:
                pass
        threading.Thread(target=self._dedup, daemon=True, name="atlas-dedup").start()

    def _dedup(self) -> None:
        with self._lock:
            for key, text_field in (("strategies", "description"), ("pitfalls", "description")):
                items  = self._pb.get(key, [])
                merged = []
                used   = set()
                for i, a in enumerate(items):
                    if i in used:
                        continue
                    for j, b in enumerate(items[i + 1:], start=i + 1):
                        if j in used:
                            continue
                        if _word_overlap(a.get(text_field, ""), b.get(text_field, "")) >= _SIMILARITY_THRESHOLD:
                            a["helpful_count"]    = a.get("helpful_count", 0) + b.get("helpful_count", 0)
                            a["harmful_count"]    = a.get("harmful_count", 0) + b.get("harmful_count", 0)
                            a["occurrence_count"] = a.get("occurrence_count", 0) + b.get("occurrence_count", 0)
                            a["last_updated"]     = _ts()
                            used.add(j)
                    merged.append(a)
                self._pb[key] = merged
            self._pb["_last_dedup"] = _ts()
        log.info("Playbook dedup complete.")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _prune(self, pb: dict) -> None:
        total = sum(len(pb.get(k, [])) for k in ("strategies", "pitfalls",
                                                   "user_preferences", "domain_knowledge"))
        if total <= self._max_entries:
            return
        strats = pb.get("strategies", [])
        strats.sort(key=lambda s: s.get("helpful_count", 0) - s.get("harmful_count", 0))
        while len(strats) > self._max_entries // 2:
            strats.pop(0)
        pb["strategies"] = strats

    def _is_dup(self, text: str, items: list, key: str) -> bool:
        return any(_word_overlap(it.get(key, ""), text) > 0.60 for it in items)

    # ── Voice command handler ──────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("what have you learned", "what did you learn",
                                     "show me what you learned")):
            return self._cmd_top_strategies()

        if any(p in lower for p in ("what do you know about me",
                                     "what have you learned about me")):
            return self._cmd_user_preferences()

        if any(p in lower for p in ("atlas remember that", "remember that",
                                     "save that response", "remember this")):
            return self.mark_positive()

        if any(p in lower for p in ("atlas forget that", "forget that",
                                     "that was wrong", "don't do that again")):
            return self.mark_negative()

        if any(p in lower for p in ("show your playbook", "show the playbook",
                                     "open playbook", "atlas show playbook")):
            return self._cmd_show_playbook()

        if any(p in lower for p in ("atlas privacy mode", "privacy mode on",
                                     "pause memory")):
            self._privacy = not self._privacy
            state = "enabled" if self._privacy else "disabled"
            return (f"Privacy mode {state}, {self._user_name}. "
                    f"Memory writes {'paused' if self._privacy else 'resumed'}.")

        if any(p in lower for p in ("atlas go dark", "go dark mode", "atlas dark mode")):
            self._dark = not self._dark
            state = "engaged" if self._dark else "disengaged"
            return (f"Dark mode {state}, {self._user_name}. "
                    f"All memory {'offline' if self._dark else 'back online'}.")

        if any(p in lower for p in ("atlas reset your playbook", "reset the playbook",
                                     "clear the playbook", "reset playbook")):
            if self._confirm_reset:
                self._confirm_reset = False
                self._pb = {
                    "strategies": [], "pitfalls": [], "user_preferences": [],
                    "domain_knowledge": [], "_last_dedup": _ts(),
                }
                return f"Playbook cleared, {self._user_name}. Starting fresh."
            self._confirm_reset = True
            return (f"Are you sure you want to clear the entire playbook, {self._user_name}? "
                    "Say 'atlas reset your playbook' again to confirm.")

        return None

    def _cmd_top_strategies(self) -> str:
        strats = sorted(self._pb.get("strategies", []),
                        key=lambda s: s.get("helpful_count", 0), reverse=True)[:5]
        if not strats:
            return (f"I haven't accumulated strategies yet, {self._user_name}. "
                    "Keep using me and I'll learn from our interactions.")
        parts = [f"{i+1}. {s['title']}" for i, s in enumerate(strats)]
        return f"My top learned strategies, {self._user_name}: " + ". ".join(parts) + "."

    def _cmd_user_preferences(self) -> str:
        prefs = sorted(self._pb.get("user_preferences", []),
                       key=lambda p: float(p.get("confidence", 0)), reverse=True)[:5]
        if not prefs:
            return f"No preferences noted yet, {self._user_name}."
        parts = [p["preference"][:80] for p in prefs]
        return f"What I know about you, {self._user_name}: " + ". ".join(parts) + "."

    def _cmd_show_playbook(self) -> str:
        pb   = self._pb
        n_s  = len(pb.get("strategies", []))
        n_p  = len(pb.get("pitfalls", []))
        n_pr = len(pb.get("user_preferences", []))
        if self._feed_cb:
            rows = "\n".join(
                f"• {s['title']} (helped: {s.get('helpful_count',0)})"
                for s in sorted(pb.get("strategies", []),
                                key=lambda s: s.get("helpful_count", 0), reverse=True)[:10]
            )
            try:
                self._feed_cb("atlas", f"PLAYBOOK — {n_s} strategies / {n_p} pitfalls / {n_pr} preferences\n{rows}")
            except Exception:
                pass
        return (f"Playbook: {n_s} strategies, {n_p} pitfalls, {n_pr} preferences, "
                f"{self._user_name}. Showing in feed panel.")

    # ── Public setters ─────────────────────────────────────────────────────────

    def set_brain(self, brain) -> None:
        self._brain = brain

    def set_feed_callback(self, cb) -> None:
        self._feed_cb = cb

    def set_vault_brain(self, vb) -> None:
        self._vb = vb
        self._pb = self._load_from_vault()
