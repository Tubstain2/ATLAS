"""
ATLAS Trajectory Compressor — context window management.

Protects the first N + last M turns, compresses the middle region into a
single [CONTEXT SUMMARY] message to keep history within token budget.

Trigger: ≥25 messages or ≥4000 estimated tokens.
Protected: first 3 turns + last 4 turns.
Compressed: all turns in between → single summary message injected as user turn.
Saved: compressed session → ATLAS/Memory/Episodic/compressed/YYYY-MM-DD-HH-MM.md
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

_TRIGGER_MESSAGES  = 25
_TRIGGER_TOKENS    = 4000
_HEAD_PROTECT      = 3     # turns (each turn = user+assistant pair → 6 messages)
_TAIL_PROTECT      = 4     # turns (8 messages)
_TOKENS_PER_CHAR   = 0.25  # rough estimate: 4 chars ≈ 1 token


def _estimate_tokens(messages: list[dict]) -> int:
    return int(sum(len(m.get("content", "")) for m in messages) * _TOKENS_PER_CHAR)


class ATLASTrajectoryCompressor:
    """
    Compress ATLAS brain conversation history when it grows too long.

    Usage:
        compressor = ATLASTrajectoryCompressor(brain, vault_brain)
        # Called automatically by brain after each turn, or on demand:
        compressor.maybe_compress()   # compresses if threshold hit
        compressor.compress_now()     # force compress
    """

    def __init__(self, brain=None, vault_brain=None):
        self._brain      = brain
        self._vb         = vault_brain
        self._lock       = threading.Lock()
        self._compressed = False   # True after this session was already compressed

        if self._vb is not None:
            self._compressed_dir = self._vb.episodic_dir / "compressed"
            self._compressed_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._compressed_dir = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def should_compress(self) -> bool:
        if self._brain is None:
            return False
        history = self._brain._history
        if len(history) >= _TRIGGER_MESSAGES:
            return True
        if _estimate_tokens(history) >= _TRIGGER_TOKENS:
            return True
        return False

    def maybe_compress(self) -> bool:
        """Compress if threshold hit. Returns True if compression happened."""
        if not self.should_compress():
            return False
        return self.compress_now()

    def compress_now(self) -> bool:
        """
        Compress middle turns into a summary.
        Rewrites brain._history in-place.
        Returns True on success.
        """
        if self._brain is None:
            return False

        with self._lock:
            history = list(self._brain._history)

            if len(history) < (_HEAD_PROTECT + _TAIL_PROTECT + 2):
                return False

            # Find head and tail boundaries
            head_msgs = history[:_HEAD_PROTECT * 2]   # pairs → 2 messages each
            tail_msgs = history[-(_TAIL_PROTECT * 2):]
            middle    = history[_HEAD_PROTECT * 2: -(_TAIL_PROTECT * 2)]

            if not middle:
                return False

            # Summarise the middle
            summary = self._summarize(middle)
            if not summary:
                return False

            summary_message = {
                "role": "user",
                "content": f"[CONTEXT SUMMARY]: {summary}",
            }

            new_history = head_msgs + [summary_message] + tail_msgs
            self._brain._history = new_history

        # Persist compressed session
        self._save_compressed(middle, summary)
        log.info(
            "TrajectoryCompressor: compressed %d→%d messages.",
            len(history), len(new_history),
        )
        return True

    def get_compression_status(self) -> str:
        if self._brain is None:
            return "Brain not connected."
        history  = self._brain._history
        tokens   = _estimate_tokens(history)
        return (
            f"{len(history)} messages, ~{tokens} tokens. "
            f"Threshold: {_TRIGGER_MESSAGES} messages or {_TRIGGER_TOKENS} tokens."
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _summarize(self, messages: list[dict]) -> Optional[str]:
        """Use brain's smart engine to summarise a chunk of history."""
        if not messages:
            return None
        transcript = "\n".join(
            f"{m['role'].upper()}: {m.get('content','')[:300]}"
            for m in messages
        )
        prompt = (
            "Summarise the following conversation excerpt in 3-6 concise sentences. "
            "Preserve key decisions, topics, and conclusions. "
            "Write in third person: 'User asked... ATLAS answered...'\n\n"
            + transcript
        )
        try:
            if self._brain and self._brain.smart_available:
                return self._brain.ask(prompt)
            # Fallback: extract first line of each user message
            user_lines = [m["content"][:80] for m in messages if m["role"] == "user"]
            return "Discussed: " + "; ".join(user_lines[:5]) + "."
        except Exception as exc:
            log.warning("TrajectoryCompressor: summarize failed (%s).", exc)
            return None

    def _save_compressed(self, middle: list[dict], summary: str) -> None:
        """Persist the compressed middle turns to vault."""
        if self._vb is None or self._compressed_dir is None:
            return
        try:
            now      = datetime.now()
            filename = now.strftime("%Y-%m-%d-%H-%M") + "-compressed.md"
            path     = self._compressed_dir / filename
            fm = {
                "date":     now.date().isoformat(),
                "messages": len(middle),
                "tags":     ["atlas", "compressed", "episodic"],
            }
            body = (
                f"# Compressed Context — {now.strftime('%A %d %B %Y %H:%M')}\n\n"
                f"## Summary\n{summary}\n\n"
                f"## Original Turns ({len(middle)} messages)\n"
                + "\n".join(
                    f"**{m['role'].upper()}**: {m.get('content','')[:200]}"
                    for m in middle
                )
            )
            self._vb.write_note(path, fm, body)
            log.info("TrajectoryCompressor: saved compressed context to %s", filename)
        except Exception as exc:
            log.warning("TrajectoryCompressor: save failed (%s).", exc)

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas compress this conversation",
                                     "atlas compress the conversation",
                                     "compress conversation")):
            ok = self.compress_now()
            if ok:
                return (f"Done, Boss. Conversation compressed. "
                        f"Now at {len(self._brain._history)} messages.")
            return "Nothing to compress — history is short enough."

        if any(p in lower for p in ("atlas what have we covered so far",
                                     "atlas summarise what we covered",
                                     "atlas summarize what we covered",
                                     "what have we covered")):
            if self._brain is None:
                return "Brain not connected."
            return self._brain._summarize_session()

        if any(p in lower for p in ("atlas context status",
                                     "atlas how long is our conversation",
                                     "atlas how full is context")):
            return self.get_compression_status()

        return None
