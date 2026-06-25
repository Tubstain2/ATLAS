"""
ATLAS Multi-Agent Debate Engine — parallel FOR/AGAINST sub-agents + synthesis.

Two agents run simultaneously via asyncio, then a synthesis agent produces a verdict.

Debate types:
  stock    — bull vs bear case for a stock
  decision — should I do X vs shouldn't I
  vs       — X vs Y comparison
  idea     — why idea could work vs why it might fail
  generic  — balanced debate on any topic

Voice commands:
  "ATLAS debate this with yourself"     → generic debate on last response
  "ATLAS debate buying X stock"         → stock debate
  "ATLAS debate whether I should X"     → decision debate
  "ATLAS debate X vs Y"                 → comparison debate
  "ATLAS debate this idea: X"           → idea debate
  "ATLAS steelman the against side"     → strengthen AGAINST
  "ATLAS steelman the for side"         → strengthen FOR
  "ATLAS what would change your verdict" → flip conditions
  "ATLAS save that debate"              → manual vault save
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

_FOR_PROMPT = (
    "You are arguing the strongest possible case FOR: {topic}. "
    "Give exactly 3 compelling arguments numbered 1, 2, 3. "
    "Be specific and evidence-based. Max 50 words per argument. "
    "No preamble — start immediately with '1.'"
)

_AGAINST_PROMPT = (
    "You are arguing the strongest possible case AGAINST: {topic}. "
    "Give exactly 3 compelling arguments numbered 1, 2, 3. "
    "Be specific and evidence-based. Max 50 words per argument. "
    "No preamble — start immediately with '1.'"
)

_SYNTH_PROMPT = (
    "You have read arguments FOR and AGAINST: {topic}\n\n"
    "FOR:\n{for_args}\n\nAGAINST:\n{against_args}\n\n"
    "Write a synthesis covering: "
    "1) Which side has stronger evidence (FOR wins X/3 or AGAINST wins X/3), "
    "2) The 2 most important decision factors, "
    "3) A clear one-sentence recommendation. "
    "Max 100 words. Plain prose, no markdown."
)

_STEELMAN_PROMPT = (
    "Make the {side} case much stronger for this debate: {topic}\n\n"
    "Current {side} arguments:\n{current}\n\n"
    "Provide 3 stronger, more specific arguments numbered 1, 2, 3. "
    "Max 50 words each."
)

_FLIP_PROMPT = (
    "Given this debate verdict on '{topic}':\n{verdict}\n\n"
    "What specific evidence, events, or circumstances would completely flip "
    "this verdict to the other side? Give 3 specific conditions. Max 80 words."
)


@dataclass
class DebateResult:
    topic: str
    debate_type: str
    for_args: List[str] = field(default_factory=list)
    against_args: List[str] = field(default_factory=list)
    verdict: str = ""
    recommendation: str = ""
    saved_path: Optional[str] = None


class ATLASDebate:
    """Multi-agent debate engine — parallel FOR/AGAINST with synthesis."""

    def __init__(self, config: dict, speak_cb: Callable, brain,
                 vault_brain=None, smart_card_mgr=None):
        self._config         = config
        self._speak          = speak_cb
        self._brain          = brain
        self._vault_brain    = vault_brain
        self._smart_card_mgr = smart_card_mgr
        self._enabled        = config.get("debate_enabled", True)
        self._last_result: Optional[DebateResult] = None
        self._last_topic: str = ""

        log.info("ATLASDebate: ready (enabled=%s).", self._enabled)

    # ── Voice router ──────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        if not self._enabled:
            return None
        lower = text.lower().strip()
        lower_clean = re.sub(r"^atlas\s+", "", lower)

        # Stock debate
        m = re.search(r"debate (?:buying|selling|investing in|shorting)?\s*(.+?)\s*stock", lower_clean)
        if m:
            topic = f"buying {m.group(1).strip()} stock"
            return self._start_debate(topic, "stock")

        # Decision debate
        m = re.search(r"debate whether i should (.+?)$", lower_clean)
        if m:
            return self._start_debate(m.group(1).strip(), "decision")

        # VS comparison
        m = re.search(r"debate (.+?)\s+vs\.?\s+(.+?)$", lower_clean)
        if m:
            topic = f"{m.group(1).strip()} vs {m.group(2).strip()}"
            return self._start_debate(topic, "vs")

        # Idea debate
        m = re.search(r"debate (?:this )?idea:?\s+(.+?)$", lower_clean)
        if m:
            return self._start_debate(m.group(1).strip(), "idea")

        # Generic
        if any(p in lower_clean for p in ("debate this with yourself",
                                           "debate this", "debate with yourself")):
            topic = self._last_topic or "this topic"
            return self._start_debate(topic, "generic")

        # Steelman
        if "steelman the against side" in lower_clean:
            return self._steelman("AGAINST")
        if "steelman the for side" in lower_clean:
            return self._steelman("FOR")

        # Flip
        if "what would change your verdict" in lower_clean:
            return self._flip_verdict()

        # Manual save
        if "save that debate" in lower_clean:
            return self._manual_save()

        return None

    # ── Debate orchestration ──────────────────────────────────────────────────

    def _start_debate(self, topic: str, dtype: str) -> str:
        self._last_topic = topic
        threading.Thread(
            target=self._run_debate_thread, args=(topic, dtype),
            daemon=True, name="atlas-debate").start()
        return f"Starting debate on {topic}, Boss. One moment while I gather both perspectives."

    def _run_debate_thread(self, topic: str, dtype: str) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(self._debate_async(topic, dtype))
            loop.close()
            self._last_result = result
            self._on_debate_complete(result)
        except Exception as exc:
            log.error("Debate: thread failed: %s", exc)
            self._speak(f"I encountered an issue running the debate on {topic}, Boss.")

    async def _debate_async(self, topic: str, dtype: str) -> DebateResult:
        for_prompt   = _FOR_PROMPT.format(topic=topic)
        against_prompt = _AGAINST_PROMPT.format(topic=topic)

        loop = asyncio.get_running_loop()
        # Run both agents in parallel via thread executors (brain is sync)
        for_fut     = loop.run_in_executor(None, self._call_agent, for_prompt)
        against_fut = loop.run_in_executor(None, self._call_agent, against_prompt)
        for_raw, against_raw = await asyncio.gather(for_fut, against_fut)

        for_args     = self._parse_args(for_raw)
        against_args = self._parse_args(against_raw)

        synth_prompt = _SYNTH_PROMPT.format(
            topic=topic,
            for_args="\n".join(f"{i+1}. {a}" for i, a in enumerate(for_args)),
            against_args="\n".join(f"{i+1}. {a}" for i, a in enumerate(against_args)),
        )
        verdict = self._call_agent(synth_prompt)

        result = DebateResult(
            topic=topic, debate_type=dtype,
            for_args=for_args, against_args=against_args,
            verdict=verdict, recommendation=self._extract_recommendation(verdict),
        )
        return result

    def _call_agent(self, prompt: str) -> str:
        try:
            return self._brain.ask(prompt)
        except Exception as exc:
            log.error("Debate agent error: %s", exc)
            return "Unable to generate arguments at this time."

    def _parse_args(self, raw: str) -> List[str]:
        args = []
        for m in re.finditer(r'^\s*\d+[.)]\s+(.+?)(?=\n\s*\d+[.)]|\Z)', raw,
                              re.MULTILINE | re.DOTALL):
            arg = m.group(1).strip().replace("\n", " ")
            if arg:
                args.append(arg[:200])
        if not args:
            # fallback: split by newlines
            args = [line.strip() for line in raw.splitlines()
                    if line.strip() and len(line.strip()) > 10][:3]
        return args[:3]

    def _extract_recommendation(self, verdict: str) -> str:
        # Last sentence is usually the recommendation
        sentences = re.split(r'[.!?]+', verdict)
        for s in reversed(sentences):
            s = s.strip()
            if len(s) > 20:
                return s + "."
        return verdict[:100]

    def _on_debate_complete(self, result: DebateResult) -> None:
        # Save to vault
        if self._vault_brain and self._config.get("debate_auto_save_obsidian", True):
            self._save_to_vault(result)

        # Speak synthesis
        self._speak(f"Debate on {result.topic} complete, Boss. {result.verdict}")

        # Smart card
        if self._smart_card_mgr:
            card_text = self._format_card_text(result)
            try:
                self._smart_card_mgr.on_response(f"debate: {result.topic}", card_text)
            except Exception as exc:
                log.debug("Debate: smart card error: %s", exc)

    def _format_card_text(self, r: DebateResult) -> str:
        for_block = "\n".join(f"{i+1}. {a}" for i, a in enumerate(r.for_args))
        against_block = "\n".join(f"{i+1}. {a}" for i, a in enumerate(r.against_args))
        return (
            f"DEBATE: {r.topic}\n\n"
            f"FOR:\n{for_block}\n\n"
            f"AGAINST:\n{against_block}\n\n"
            f"VERDICT:\n{r.verdict}"
        )

    def _save_to_vault(self, result: DebateResult) -> Optional[str]:
        if not self._vault_brain:
            return None
        try:
            folder = self._vault_brain.atlas / "Research" / "Debates"
            folder.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^\w\s-]", "", result.topic.lower()).replace(" ", "-")[:40]
            fname = f"{date.today()}-{slug}.md"
            for_md = "\n".join(f"- {a}" for a in result.for_args)
            against_md = "\n".join(f"- {a}" for a in result.against_args)
            (folder / fname).write_text(
                f"---\ntags: [debate, {result.debate_type}]\n"
                f"date: {date.today()}\ntopic: {result.topic}\n---\n\n"
                f"# Debate: {result.topic}\n\n"
                f"## FOR\n{for_md}\n\n"
                f"## AGAINST\n{against_md}\n\n"
                f"## Verdict\n{result.verdict}\n",
                encoding="utf-8")
            result.saved_path = str(folder / fname)
            return str(folder / fname)
        except Exception as exc:
            log.error("Debate: vault save failed: %s", exc)
            return None

    def _steelman(self, side: str) -> str:
        if not self._last_result:
            return "No recent debate to steelman, Boss."
        r = self._last_result
        current = r.for_args if side == "FOR" else r.against_args
        current_text = "\n".join(f"{i+1}. {a}" for i, a in enumerate(current))
        prompt = _STEELMAN_PROMPT.format(
            side=side, topic=r.topic, current=current_text)
        threading.Thread(
            target=self._steelman_async, args=(r, side, prompt),
            daemon=True).start()
        return f"Strengthening the {side} case for {r.topic}, Boss. One moment."

    def _steelman_async(self, r: DebateResult, side: str, prompt: str) -> None:
        try:
            stronger = self._call_agent(prompt)
            new_args = self._parse_args(stronger)
            if side == "FOR":
                r.for_args = new_args
            else:
                r.against_args = new_args
            self._speak(f"Stronger {side} arguments for {r.topic}: {stronger[:200]}")
        except Exception as exc:
            log.error("Steelman error: %s", exc)

    def _flip_verdict(self) -> str:
        if not self._last_result:
            return "No recent debate verdict to flip, Boss."
        r = self._last_result
        prompt = _FLIP_PROMPT.format(topic=r.topic, verdict=r.verdict)
        threading.Thread(target=self._flip_async, args=(r, prompt),
                         daemon=True).start()
        return "Analysing what would flip the verdict, Boss."

    def _flip_async(self, r: DebateResult, prompt: str) -> None:
        try:
            flip = self._call_agent(prompt)
            self._speak(f"To flip the verdict on {r.topic}: {flip}")
        except Exception as exc:
            log.error("Flip error: %s", exc)

    def _manual_save(self) -> str:
        if not self._last_result:
            return "No recent debate to save, Boss."
        path = self._save_to_vault(self._last_result)
        return f"Debate saved to your Obsidian vault, Boss." if path else "Vault save failed, Boss."
