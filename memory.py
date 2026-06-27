"""
ATLAS Memory System — HERMES-inspired three-layer persistent memory

Layer 1 — Working Memory  (session scope, max 30 messages — in-memory, flushed on shutdown)
Layer 2 — Episodic Memory (session summaries — one vault file per session)
Layer 3 — Semantic Memory (permanent facts — atlas-knows-you.md)

Storage: Obsidian vault via VaultBrain (single source of truth)
  ATLAS/Memory/Working/current-session.md  ← live session log
  ATLAS/Memory/Episodic/YYYY-MM-DD-HH-MM.md
  ATLAS/Memory/Semantic/atlas-knows-you.md
  ATLAS/Skills/[slug].md

Voice commands:
  "ATLAS what do you remember about me"   → semantic memory highlights
  "ATLAS what did we work on yesterday"   → search episodic memory
  "ATLAS what did we talk about last week"→ 7-day episode summary
  "ATLAS I am back"                       → load last session summary
  "ATLAS remind me where we left off"     → full recap of last session
  "ATLAS what skills do you have"         → list learned skill files
  "ATLAS remember this"                   → explicit semantic memory save
  "ATLAS forget everything"               → clear all layers (triple confirm)
  "ATLAS forget this session"             → clear working memory only
"""

from __future__ import annotations

import logging
import threading
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_MAX_WORKING         = 30
_MAX_EPISODES        = 365
_MAX_RETRIEVAL_CHARS = 3200   # ≈ 800 tokens


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _keyword_match(text: str, query: str, threshold: int = 2) -> bool:
    words = {w for w in query.lower().split() if len(w) > 3}
    hits  = sum(1 for w in words if w in text.lower())
    return hits >= min(threshold, len(words))


class MemoryModule:
    """
    Three-layer persistent memory for ATLAS.
    All data is stored in the Obsidian vault via VaultBrain.

    Wire into main.py after Brain is created:
        memory = MemoryModule(config, brain=brain, obsidian=obsidian_mod,
                              vault_brain=vb)
    """

    def __init__(self, config: dict, brain=None, obsidian=None, vault_brain=None):
        self._cfg       = config
        self._brain     = brain
        self._obsidian  = obsidian
        self._vb        = vault_brain   # VaultBrain instance
        self._user_name = config.get("user_name", "Boss")

        self._max_working   = int(config.get("memory_working_max_messages", _MAX_WORKING))
        self._max_episodes  = int(config.get("memory_episodic_max_episodes", _MAX_EPISODES))
        self._skill_writing = bool(config.get("memory_skill_writing_enabled", True))
        self._obs_export    = bool(config.get("memory_obsidian_weekly_export", True))

        self._lock = threading.Lock()
        self._session_start = datetime.now()

        # Layer 1: working memory lives only in RAM (flush to vault on shutdown)
        self._working_msgs: List[dict] = []

        # Forget confirmation states
        self._forget_all_count = 0

        # Privacy flags
        self._privacy = False
        self._dark    = False

        n_episodes = self._count_episodes()
        log.info("MemoryModule: vault storage %s, %d episodes on record.",
                 "on" if self._vb else "off", n_episodes)

    # ── Layer 1: Working Memory ────────────────────────────────────────────────

    def add_message(self, role: str, content: str) -> None:
        if self._privacy or self._dark:
            return
        with self._lock:
            self._working_msgs.append({"role": role, "content": content, "ts": _ts()})
            if len(self._working_msgs) > self._max_working:
                self._working_msgs = self._working_msgs[-self._max_working:]
            msg_count = len(self._working_msgs)
        # Append line to vault working note (non-blocking, best-effort)
        if self._vb:
            threading.Thread(
                target=self._flush_working_line,
                args=(role, content),
                daemon=True, name="atlas-working-flush",
            ).start()
        # Auto-save episode every 10 messages so force-kills don't lose the session
        if role == "assistant" and msg_count > 0 and msg_count % 10 == 0:
            threading.Thread(
                target=self.save_session_episode,
                daemon=True, name="atlas-auto-episode",
            ).start()

    def _flush_working_line(self, role: str, content: str) -> None:
        try:
            path = self._vb.working_dir / "current-session.md"
            now  = datetime.now().strftime("%H:%M")
            line = f"\n**{now} {role.upper()}**: {content[:300]}"
            self._vb.append_note(path, line)
        except Exception as exc:
            log.debug("Working flush: %s", exc)

    def get_working_messages(self) -> List[dict]:
        return [{"role": m["role"], "content": m["content"]}
                for m in self._working_msgs]

    # ── Layer 2: Episodic Memory ───────────────────────────────────────────────

    def save_session_episode(self, forced_summary: str = "") -> None:
        if self._privacy or self._dark:
            return
        msgs = self._working_msgs
        if not msgs:
            return

        elapsed = int((datetime.now() - self._session_start).total_seconds() / 60)
        summary = forced_summary

        if not summary and self._brain:
            try:
                dialogue = "\n".join(
                    f"{m['role'].upper()}: {m['content'][:200]}"
                    for m in msgs[-20:]
                )
                summary = self._brain.ask(
                    "Summarise this ATLAS session in 2-3 concise sentences. "
                    "Mention what was worked on, any decisions made, and the overall mood. "
                    f"Plain prose only.\n\n{dialogue}"
                ) or ""
            except Exception as exc:
                log.warning("Episode summary failed: %s", exc)
                summary = f"Session with {len(msgs)} messages."

        if not summary:
            summary = f"Session: {len(msgs)} messages over {elapsed} minutes."

        mood     = self._infer_mood(msgs)
        projects = self._extract_projects(msgs)
        tags     = self._extract_tags(msgs)
        learned: List[str] = []

        if self._vb:
            self._vb.write_episode(
                summary=summary,
                duration_min=elapsed,
                mood=mood,
                projects=projects,
                tags=tags,
                learned=learned,
            )

        log.info("Episode saved to vault — %s", summary[:80])

        # Extract semantic facts asynchronously
        threading.Thread(target=self._extract_semantic_from_session,
                         args=(msgs,), daemon=True, name="atlas-semantic-extract").start()

        # Clear the live working session file
        if self._vb:
            path = self._vb.working_dir / "current-session.md"
            try:
                from datetime import datetime as _dt
                day = _dt.now().strftime("%A %d %B %Y")
                self._vb.write_note(
                    path, {"session_date": _today(), "tags": ["atlas", "working"]},
                    f"# Working Memory — {day}\n\nSession complete. See Episodic for summary.\n"
                )
            except Exception:
                pass

    def _extract_projects(self, msgs: List[dict]) -> List[str]:
        projects = set()
        for m in msgs:
            c = m.get("content", "").lower()
            if "atlas" in c:
                projects.add("ATLAS")
            for word in m.get("content", "").split():
                if word.istitle() and len(word) > 3 and "python" not in word.lower():
                    projects.add(word)
        return list(projects)[:5]

    def _infer_mood(self, msgs: List[dict]) -> str:
        user_msgs = " ".join(m["content"] for m in msgs if m["role"] == "user").lower()
        if any(w in user_msgs for w in ("great", "perfect", "excellent", "thanks", "brilliant")):
            return "productive"
        if any(w in user_msgs for w in ("wrong", "error", "problem", "issue", "not working")):
            return "troubleshooting"
        if any(w in user_msgs for w in ("tired", "frustrated", "ugh")):
            return "frustrated"
        return "casual"

    def _extract_tags(self, msgs: List[dict]) -> List[str]:
        tags = set()
        text = " ".join(m.get("content", "") for m in msgs).lower()
        for tag, keywords in {
            "coding":   ("python", "function", "code", "bug", "import"),
            "market":   ("stock", "price", "trade", "market", "invest"),
            "obsidian": ("note", "vault", "task", "obsidian"),
            "camera":   ("camera", "webcam", "look at"),
        }.items():
            if any(kw in text for kw in keywords):
                tags.add(tag)
        return list(tags)

    def _count_episodes(self) -> int:
        if not self._vb:
            return 0
        try:
            return len(self._vb.list_notes(self._vb.episodic_dir))
        except Exception:
            return 0

    def search_episodes(self, query: str, max_results: int = 3) -> List[dict]:
        if not self._vb:
            return []
        results = self._vb.search_episodes(query, max_results=max_results)
        # Convert vault tuples to legacy dict format
        out = []
        for fm, body in results:
            summary_lines = [l.strip() for l in body.splitlines()
                             if l.strip() and not l.startswith("#")]
            summary = summary_lines[0] if summary_lines else ""
            out.append({
                "date":               fm.get("date", ""),
                "summary":            summary,
                "mood":               fm.get("mood", "casual"),
                "tags":               list(fm.get("tags", [])),
                "projects_worked_on": list(fm.get("projects", [])),
                "duration_minutes":   int(fm.get("duration_minutes", 0)),
            })
        return out

    def get_last_episode(self) -> Optional[dict]:
        if not self._vb:
            return None
        r = self._vb.get_last_episode()
        if not r:
            return None
        fm, body = r
        summary_lines = [l.strip() for l in body.splitlines()
                         if l.strip() and not l.startswith("#")]
        summary = summary_lines[0] if summary_lines else ""
        return {
            "date":               fm.get("date", ""),
            "summary":            summary,
            "mood":               fm.get("mood", "casual"),
            "tags":               list(fm.get("tags", [])),
            "projects_worked_on": list(fm.get("projects", [])),
            "duration_minutes":   int(fm.get("duration_minutes", 0)),
        }

    # ── Layer 3: Semantic Memory ───────────────────────────────────────────────

    def add_fact(self, fact: str, source: str = "inferred", confidence: float = 0.8) -> None:
        if self._privacy or self._dark:
            return
        if self._vb:
            section = "Notes"
            if any(w in fact.lower() for w in ("prefer", "like", "love", "hate", "dislike")):
                section = "Preferences"
            elif any(w in fact.lower() for w in ("building", "working on", "project")):
                section = "Projects"
            self._vb.add_fact(fact, section=section)
        else:
            log.debug("add_fact (no vault): %s", fact[:80])

    def add_preference(self, preference: str, strength: str = "medium") -> None:
        if self._privacy or self._dark:
            return
        conf = {"strong": 0.9, "medium": 0.75, "weak": 0.6}.get(strength, 0.75)
        if self._vb:
            self._vb.add_preference(preference, strength=strength, confidence=conf)

    def get_semantic_context(self, query: str = "", max_facts: int = 10) -> str:
        if self._vb:
            raw = self._vb.get_semantic_context(max_chars=1200)
            # Normalise to legacy format for brain.py compatibility
            lines = []
            for line in raw.splitlines():
                if line.startswith("• ["):
                    category = line.split("]")[0].lstrip("• [")
                    content  = "] ".join(line.split("]")[1:]).strip().lstrip(" ")
                    if category in ("Notes",):
                        lines.append(f"• FACT: {content}")
                    elif category in ("Preferences",):
                        lines.append(f"• PREFERENCE: {content}")
                    else:
                        lines.append(f"• {category.upper()}: {content}")
                elif line and not line.startswith("ABOUT"):
                    lines.append(line)
            return "\n".join(lines)
        return ""

    def _extract_semantic_from_session(self, msgs: List[dict]) -> None:
        if not self._brain or not msgs:
            return
        try:
            sample = "\n".join(
                f"{m['role'].upper()}: {m['content'][:200]}"
                for m in msgs[-15:]
            )
            raw = self._brain.ask(
                "Extract factual information about the user from this conversation. "
                "Reply with a JSON object only:\n"
                '{"facts":["fact 1","fact 2"],"preferences":["preference 1"]}\n\n'
                f"Conversation:\n{sample}"
            )
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            data  = json.loads(clean)
            for fact in data.get("facts", [])[:5]:
                if fact and len(fact) > 10:
                    self.add_fact(fact, source="inferred")
            for pref in data.get("preferences", [])[:3]:
                if pref and len(pref) > 10:
                    self.add_preference(pref)
        except Exception as exc:
            log.debug("Semantic extraction: %s", exc)

    # ── Skill Writing ──────────────────────────────────────────────────────────

    def write_skill(self, skill_name: str, trigger: str,
                    steps: str, outcome: str, pitfalls: str) -> None:
        if not self._skill_writing or self._privacy or self._dark:
            return
        if self._vb:
            self._vb.write_skill(
                name=skill_name, task_type=trigger[:40],
                trigger=trigger, steps=steps,
                outcome=outcome, pitfalls_text=pitfalls,
            )
            log.info("Skill written to vault: %s", skill_name)

    def load_skill(self, query: str) -> Optional[str]:
        if self._vb:
            return self._vb.get_skill(query)
        return None

    def list_skills(self) -> List[str]:
        if self._vb:
            return self._vb.list_skills()
        return []

    # ── Startup context ────────────────────────────────────────────────────────

    def get_startup_context(self) -> dict:
        last   = self.get_last_episode()
        n_ep   = self._count_episodes()
        facts: List[str] = []
        if self._vb:
            result = self._vb.read_note(self._vb.semantic_path)
            if result:
                _, body = result
                for line in body.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- ") and len(stripped) > 4:
                        facts.append(stripped[2:])
                        if len(facts) >= 3:
                            break
        return {
            "last_date":     last["date"] if last else None,
            "last_summary":  last["summary"] if last else None,
            "last_projects": last.get("projects_worked_on", []) if last else [],
            "top_facts":     facts,
            "session_count": n_ep,
        }

    def generate_greeting(self) -> str:
        ctx = self.get_startup_context()

        # Fallback: if vault has no recent episode (e.g. last session was force-killed),
        # read from the brain's conversations/ JSON files instead.
        if not ctx["last_date"] or self._startup_context_is_stale(ctx):
            fallback = self._read_last_conversation_file()
            if fallback:
                ctx = fallback

        if not ctx["last_date"]:
            return f"Good to see you, {self._user_name}. I'm fully online and ready."
        try:
            last_dt  = datetime.fromisoformat(ctx["last_date"])
            days_ago = (date.today() - last_dt.date()).days
            time_str = ("earlier today" if days_ago == 0
                        else "yesterday" if days_ago == 1
                        else f"{days_ago} days ago")
        except Exception:
            time_str = ctx["last_date"]

        projects = ctx.get("last_projects", [])
        proj_str = f" working on {', '.join(projects[:2])}" if projects else ""

        if ctx["last_summary"]:
            short = ctx["last_summary"][:120].rstrip(".")
            return (f"Welcome back, {self._user_name}. "
                    f"Last time we spoke was {time_str}{proj_str}. "
                    f"{short}. Want to continue?")

        return (f"Welcome back, {self._user_name}. "
                f"Last session was {time_str}{proj_str}. Ready when you are.")

    def _startup_context_is_stale(self, ctx: dict) -> bool:
        """Return True if the vault's last episode is older than 1 day."""
        if not ctx.get("last_date"):
            return True
        try:
            last_dt  = datetime.fromisoformat(ctx["last_date"])
            days_ago = (date.today() - last_dt.date()).days
            return days_ago > 1
        except Exception:
            return True

    def _read_last_conversation_file(self) -> Optional[dict]:
        """
        Read the most recent conversations/YYYY-MM-DD.json saved by brain.py.
        Returns a startup-context-compatible dict, or None.
        """
        import os as _os
        root = Path(_os.environ.get("ATLAS_ROOT", "."))
        conv_dir = root / "conversations"
        if not conv_dir.exists():
            return None
        json_files = sorted(conv_dir.glob("*.json"), reverse=True)
        for f in json_files[:3]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                msgs = data.get("messages", [])
                if not msgs:
                    continue
                # Build a rough summary from the last few assistant turns
                assistant_msgs = [m["content"] for m in msgs if m.get("role") == "assistant"]
                summary = assistant_msgs[-1][:120] if assistant_msgs else ""
                # Extract project names (capitalized words from user turns)
                user_text = " ".join(m["content"] for m in msgs if m.get("role") == "user")
                projects  = list({w for w in user_text.split()
                                   if w.istitle() and len(w) > 3})[:3]
                return {
                    "last_date":     data.get("date", f.stem),
                    "last_summary":  summary,
                    "last_projects": projects,
                    "top_facts":     [],
                    "session_count": 0,
                }
            except Exception:
                continue
        return None

    # ── Prompt context for AI calls ────────────────────────────────────────────

    def get_prompt_context(self, query: str = "") -> str:
        if self._dark:
            return ""

        blocks: List[str] = []

        if self._vb:
            semantic_ctx = self._vb.get_semantic_context(max_chars=1000)
            if semantic_ctx:
                blocks.append(semantic_ctx)
        else:
            sc = self.get_semantic_context(query)
            if sc:
                blocks.append("ABOUT THE USER:\n" + sc)

        # Search episodic memory if query references past events
        past_keywords = ("yesterday", "last week", "last time", "before", "previously",
                         "earlier", "we worked on", "remember when")
        if any(kw in query.lower() for kw in past_keywords):
            episodes = self.search_episodes(query, max_results=2)
            if episodes:
                ep_lines = [f"• {ep['date']}: {ep['summary'][:150]}" for ep in episodes]
                blocks.append("RELEVANT PAST SESSIONS:\n" + "\n".join(ep_lines))

        result = "\n\n".join(blocks)
        return result[:_MAX_RETRIEVAL_CHARS]

    # ── Obsidian export ────────────────────────────────────────────────────────

    def export_to_obsidian(self) -> str:
        if not self._vb and not self._obsidian:
            return f"Obsidian not connected, {self._user_name}."
        try:
            result = self._vb.read_note(self._vb.semantic_path) if self._vb else None
            if result:
                _, body = result
                if self._vb:
                    # Link from Notes folder for easy access
                    self._vb.write_note(
                        self._vb.atlas / "Notes" / "ATLAS-Memory.md",
                        {"date": _today(), "tags": ["atlas", "memory-export"]},
                        f"# ATLAS Memory Export — {_today()}\n\n"
                        f"See [[ATLAS/Memory/Semantic/atlas-knows-you]] for live data.\n\n"
                        f"{body[:2000]}"
                    )
            return f"Memory exported to Obsidian, {self._user_name}."
        except Exception as exc:
            log.warning("Obsidian export failed: %s", exc)
            return f"Export failed: {exc}"

    def weekly_obsidian_export(self) -> None:
        if not self._obs_export or not self._vb:
            return
        try:
            cutoff   = date.today() - timedelta(days=7)
            episodes = self._get_recent_episodes_since(cutoff)
            if not episodes:
                return
            if self._brain:
                ep_text = "\n".join(f"- {ep['date']}: {ep['summary'][:120]}"
                                    for ep in episodes)
                summary = self._brain.ask(
                    f"Write a 3-sentence weekly review of these ATLAS sessions:\n{ep_text}"
                ) or ""
            else:
                summary = f"{len(episodes)} sessions this week."
            self._vb.write_weekly_review(summary, [({"date": ep["date"]}, ep["summary"])
                                                    for ep in episodes])
            log.info("Weekly vault review written.")
        except Exception as exc:
            log.warning("Weekly export failed: %s", exc)

    def _get_recent_episodes_since(self, cutoff: date) -> List[dict]:
        results = []
        if not self._vb:
            return results
        for p in sorted(self._vb.episodic_dir.glob("*.md"), reverse=True):
            r = self._vb.read_note(p)
            if not r:
                continue
            fm, body = r
            try:
                ep_date = date.fromisoformat(fm.get("date", ""))
                if ep_date < cutoff:
                    break
            except Exception:
                continue
            lines = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
            results.append({
                "date":    fm.get("date", ""),
                "summary": lines[0] if lines else "",
                "mood":    fm.get("mood", "casual"),
            })
        return results

    # ── Session shutdown ───────────────────────────────────────────────────────

    def on_shutdown(self) -> None:
        log.info("MemoryModule: saving session on shutdown...")
        self.save_session_episode()
        self.weekly_obsidian_export()
        # Update session count in semantic note
        if self._vb:
            try:
                result = self._vb.read_note(self._vb.semantic_path)
                if result:
                    fm, body = result
                    fm["total_sessions"] = int(fm.get("total_sessions", 0)) + 1
                    self._vb.write_note(self._vb.semantic_path, fm, body)
            except Exception:
                pass
        log.info("MemoryModule: shutdown complete.")

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas what do you remember about me",
                                     "what do you remember about me",
                                     "what do you know about me")):
            return self._cmd_semantic_summary()

        if any(p in lower for p in ("atlas what did we work on yesterday",
                                     "what did we work on yesterday")):
            return self._cmd_search_episodes("yesterday")

        if any(p in lower for p in ("what did we talk about last week",
                                     "atlas what did we do last week",
                                     "last week summary")):
            return self._cmd_last_week()

        if any(p in lower for p in ("atlas i am back", "atlas i'm back",
                                     "i am back", "i'm back")):
            return self._cmd_welcome_back()

        if any(p in lower for p in ("atlas remind me where we left off",
                                     "remind me where we left off",
                                     "where did we leave off",
                                     "what were we doing")):
            return self._cmd_last_session_recap()

        if any(p in lower for p in ("atlas what skills do you have",
                                     "what skills have you learned",
                                     "atlas skills", "list your skills")):
            return self._cmd_list_skills()

        if any(p in lower for p in ("atlas remember this", "remember this",
                                     "save this to memory", "atlas save this")):
            return self._cmd_save_context()

        if any(p in lower for p in ("atlas save my memory to obsidian",
                                     "export memory to obsidian",
                                     "save memory to vault")):
            return self.export_to_obsidian()

        if any(p in lower for p in ("atlas forget everything",
                                     "clear all memory", "wipe memory")):
            return self._cmd_forget_all()

        if any(p in lower for p in ("atlas forget this session",
                                     "forget this session", "clear session memory",
                                     "clear working memory")):
            return self._cmd_forget_session()

        return None

    def _cmd_semantic_summary(self) -> str:
        ctx = self.get_semantic_context()
        if not ctx:
            return (f"I don't have much on file about you yet, {self._user_name}. "
                    "Keep using me and I'll learn.")
        lines = [l for l in ctx.splitlines() if l.strip()][:5]
        clean = ". ".join(l.lstrip("•- ").split(": ", 1)[-1][:80] for l in lines)
        return f"What I know about you, {self._user_name}: {clean}."

    def _cmd_search_episodes(self, query: str) -> str:
        results = self.search_episodes(query)
        if not results:
            return f"I don't have a session matching that, {self._user_name}."
        ep = results[0]
        return f"On {ep['date']} we worked on: {ep['summary']}"

    def _cmd_last_week(self) -> str:
        cutoff   = date.today() - timedelta(days=7)
        episodes = self._get_recent_episodes_since(cutoff)
        if not episodes:
            return f"No sessions recorded in the last week, {self._user_name}."
        summaries = ". ".join(f"{ep['date']}: {ep['summary'][:80]}" for ep in episodes[-5:])
        return f"Last week's sessions, {self._user_name}: {summaries}."

    def _cmd_welcome_back(self) -> str:
        return self.generate_greeting()

    def _cmd_last_session_recap(self) -> str:
        last = self.get_last_episode()
        if not last:
            return f"No previous session found, {self._user_name}."
        proj = ", ".join(last.get("projects_worked_on", [])[:3]) or "general tasks"
        return (f"Last session on {last['date']}, {self._user_name}. "
                f"We worked on {proj}. {last['summary'][:200]}")

    def _cmd_list_skills(self) -> str:
        skills = self.list_skills()
        if not skills:
            return (f"I haven't written any skill files yet, {self._user_name}. "
                    "They accumulate as I complete complex tasks.")
        return (f"I have {len(skills)} learned skills, {self._user_name}: "
                + ", ".join(skills[:8]) + ".")

    def _cmd_save_context(self) -> str:
        msgs = self._working_msgs
        if not msgs:
            return f"Nothing in working memory to save, {self._user_name}."
        last_user = next((m["content"] for m in reversed(msgs)
                          if m["role"] == "user"), "")
        if last_user:
            self.add_fact(last_user[:200], source="explicit", confidence=0.95)
            return f"Saved to memory, {self._user_name}."
        return f"Nothing specific to save, {self._user_name}."

    def _cmd_forget_all(self) -> str:
        self._forget_all_count = getattr(self, "_forget_all_count", 0) + 1
        if self._forget_all_count >= 3:
            self._forget_all_count = 0
            with self._lock:
                self._working_msgs = []
            if self._vb:
                # Reset the semantic note to blank
                self._vb._ensure_semantic_note()
            return f"All memory layers cleared, {self._user_name}. I remember nothing."
        remaining = 3 - self._forget_all_count
        return (f"This will erase all my memory of you permanently, {self._user_name}. "
                f"Say 'atlas forget everything' {remaining} more "
                f"{'time' if remaining == 1 else 'times'} to confirm.")

    def _cmd_forget_session(self) -> str:
        with self._lock:
            self._working_msgs = []
        if self._vb:
            path = self._vb.working_dir / "current-session.md"
            try:
                from datetime import datetime as _dt
                day = _dt.now().strftime("%A %d %B %Y")
                self._vb.write_note(
                    path, {"session_date": _today(), "tags": ["atlas", "working"]},
                    f"# Working Memory — {day}\n\nSession cleared by voice command.\n"
                )
            except Exception:
                pass
        return f"Working memory cleared, {self._user_name}. Starting this session fresh."

    # ── Public setters ─────────────────────────────────────────────────────────

    def set_brain(self, brain) -> None:
        self._brain = brain

    def set_obsidian(self, obsidian) -> None:
        self._obsidian = obsidian

    def set_vault_brain(self, vb) -> None:
        self._vb = vb
