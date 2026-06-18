"""
ATLAS Self-Improvement Engine — Module 2

Analyses ATLAS source code using Claude and applies surgical improvements
after explicit user voice or text confirmation.

Every change is:
  - Backed up to versions/ with a timestamp before applying
  - Validated with  python -m py_compile  before saving
  - Logged to ATLAS_IMPROVEMENTS.md
  - Instantly reversible via rollback()

Voice commands (detected in ClaudeBrain._handle_meta → self.handle()):
  "ATLAS improve yourself"          → analyse all modules, propose changes
  "ATLAS what did you change"       → read improvement log aloud
  "ATLAS undo last change"          → rollback to previous version
  "ATLAS run self test"             → py_compile check on all modules
  "ATLAS how are you performing"    → full health report
  confirm / yes apply / go ahead    → apply pending improvement (via ClaudeBrain)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Source files ATLAS manages — ordered by criticality (most important last)
_ATLAS_MODULES = [
    "web.py", "control.py", "self_editor.py",
    "self_improve.py", "claude_brain.py", "core.py", "voice.py", "main.py",
]

_MAX_FILE_CHARS = 8_000   # truncate large files sent to Claude


class SelfImproveEngine:
    """
    Autonomous self-improvement engine powered by Claude Sonnet.

    Wire-up in main.py:
        engine = SelfImproveEngine(config, claude_brain, PROJECT_ROOT)
        brain.set_self_improve(engine)
    """

    def __init__(self, config: dict, claude_brain, project_root: Path):
        self._brain     = claude_brain
        self._root      = project_root
        self._versions  = project_root / "versions"
        self._log_path  = project_root / "ATLAS_IMPROVEMENTS.md"
        self._pending: Optional[dict] = None   # proposed improvement awaiting confirm

        self._versions.mkdir(exist_ok=True)

        # Initialise improvement log
        if not self._log_path.exists():
            self._log_path.write_text(
                "# ATLAS Improvement Log\n\n"
                "| Date | File | Change | Why | Status |\n"
                "|------|------|--------|-----|--------|\n",
                encoding="utf-8",
            )

        log.info("SelfImproveEngine ready (root=%s).", project_root)

    # ── Voice command dispatcher ──────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        """
        Return a response string if text is a self-improve command, else None.
        Called by ClaudeBrain.handle() before routing.
        """
        lower = text.lower().strip()

        if any(p in lower for p in ("improve yourself", "self improve", "analyse yourself",
                                     "analyze yourself", "check yourself", "upgrade yourself")):
            return self.analyze()

        if any(p in lower for p in ("what did you change", "what have you changed",
                                     "show me changes", "improvement log", "what changed")):
            return self.read_log()

        if any(p in lower for p in ("undo last change", "rollback", "undo that change",
                                     "revert last change", "undo improvement")):
            return self.rollback()

        if any(p in lower for p in ("run self test", "self test", "test yourself",
                                     "check your modules", "validate modules")):
            return self.run_self_test()

        if any(p in lower for p in ("how are you performing", "health report",
                                     "system health", "performance report",
                                     "how are you doing", "status report")):
            return self.health_report()

        return None

    def has_pending(self) -> bool:
        return self._pending is not None

    # ── Core operations ───────────────────────────────────────────────────────

    def analyze(self) -> str:
        """
        Read all ATLAS source files, ask Claude to identify the single most
        impactful improvement, and store it as a pending proposal.
        """
        if not self._brain.claude_available:
            return ("I need Claude to analyse my code, but ANTHROPIC_API_KEY isn't set. "
                    "Please add it and try again.")

        log.info("[SELF-IMPROVE] Starting code analysis...")

        # Build source summary for Claude
        source_blocks: list[str] = []
        for fname in _ATLAS_MODULES:
            fpath = self._root / fname
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(encoding="utf-8")
                if len(content) > _MAX_FILE_CHARS:
                    content = content[:_MAX_FILE_CHARS] + "\n... [truncated]"
                source_blocks.append(f"=== {fname} ===\n{content}")
            except Exception as exc:
                log.warning("Could not read %s: %s", fname, exc)

        if not source_blocks:
            return "I couldn't read my own source files."

        prompt = (
            "You are analysing ATLAS, a voice-activated macOS AI assistant written in Python.\n\n"
            "Review the source files below and identify the SINGLE most impactful improvement "
            "that is:\n"
            "  1. Safe to apply automatically (no UI redesigns, no dependency changes)\n"
            "  2. A surgical change — replace one function or fix one specific bug\n"
            "  3. Immediately testable with python -m py_compile\n\n"
            "Respond with a JSON object ONLY — no markdown, no explanation outside the JSON:\n"
            "{\n"
            '  "file": "filename.py",\n'
            '  "description": "plain English one-sentence description of the change",\n'
            '  "why": "one sentence explaining the benefit",\n'
            '  "old": "exact verbatim string to replace (copy precisely)",\n'
            '  "new": "replacement string"\n'
            "}\n\n"
            "If there are no safe improvements, respond with:\n"
            '{"file": null, "description": "No safe improvements found.", "why": "", "old": "", "new": ""}\n\n'
            "Source files:\n\n"
            + "\n\n".join(source_blocks)
        )

        raw = self._brain.ask(prompt)
        log.info("[SELF-IMPROVE] Claude response: %s", raw[:200])

        try:
            # Strip markdown fences if Claude added them
            clean = raw.strip()
            if clean.startswith("```"):
                clean = "\n".join(clean.split("\n")[1:])
            if clean.endswith("```"):
                clean = clean.rsplit("```", 1)[0]
            proposal = json.loads(clean.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("Could not parse improvement JSON: %s\nRaw: %s", exc, raw[:500])
            return "I analysed my code but couldn't format the proposal correctly. Try again."

        if not proposal.get("file") or not proposal.get("old"):
            desc = proposal.get("description", "No improvements found.")
            return f"After reviewing my code: {desc}"

        self._pending = proposal
        desc = proposal.get("description", "")
        why  = proposal.get("why", "")
        fname = proposal.get("file", "")

        return (
            f"I found an improvement in {fname}. {desc} {why} "
            f"Say confirm or yes to apply it, or ignore to skip."
        )

    def apply_pending(self) -> str:
        """Apply the pending improvement after user confirmation."""
        if self._pending is None:
            return "There's no pending improvement to apply."

        proposal = self._pending
        self._pending = None

        fname = proposal.get("file", "")
        old   = proposal.get("old",  "")
        new   = proposal.get("new",  "")
        desc  = proposal.get("description", "")
        why   = proposal.get("why", "")

        if not fname or not old:
            return "The improvement proposal was incomplete. Nothing applied."

        fpath = self._root / fname
        if not fpath.exists():
            return f"I couldn't find {fname} — nothing applied."

        try:
            original = fpath.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Couldn't read {fname}: {exc}"

        if old not in original:
            return (f"The code I planned to change in {fname} no longer matches "
                    "the current file. Nothing applied.")

        # Backup before changing
        backup_path = self._backup(fpath, original)

        # Apply the change
        updated = original.replace(old, new, 1)
        fpath.write_text(updated, encoding="utf-8")

        # Syntax validation
        ok, err = self._validate_syntax(fpath)
        if not ok:
            # Restore backup
            fpath.write_text(original, encoding="utf-8")
            return (f"The change to {fname} introduced a syntax error: {err}. "
                    "I've restored the original.")

        # Log the improvement
        self._log(fname, desc, why, success=True)
        log.info("[SELF-IMPROVE] Applied: %s → %s", fname, desc)

        return (f"Done. I've improved {fname}. {desc} "
                f"The previous version is saved at {backup_path.name}.")

    def rollback(self) -> str:
        """Restore the most recent backup from versions/."""
        backups = sorted(self._versions.glob("*.py.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            return "No backups found — nothing to roll back."

        latest = backups[0]
        # Reconstruct original filename from backup name: filename_TIMESTAMP.py.bak
        parts  = latest.stem.rsplit("_", 1)   # stem = filename_TIMESTAMP.py
        fname  = parts[0] + ".py" if len(parts) == 2 else latest.stem

        target = self._root / fname
        try:
            shutil.copy2(str(latest), str(target))
            self._log(fname, "Rolled back to previous version", "User requested rollback", success=True)
            return f"Done. I've restored {fname} from {latest.name}."
        except Exception as exc:
            return f"Rollback failed: {exc}"

    def run_self_test(self) -> str:
        """Run py_compile on every ATLAS Python module and report."""
        results: list[str] = []
        passed = 0
        failed = 0

        for fname in _ATLAS_MODULES:
            fpath = self._root / fname
            if not fpath.exists():
                results.append(f"{fname}: not found")
                continue
            ok, err = self._validate_syntax(fpath)
            if ok:
                results.append(f"{fname}: OK")
                passed += 1
            else:
                results.append(f"{fname}: FAIL — {err}")
                failed += 1

        summary = f"{passed} modules passed, {failed} failed."
        details = "; ".join(results)
        log.info("[SELF-TEST] %s", summary)

        if failed == 0:
            return f"All modules passed the syntax check. {summary}"
        return f"Self-test complete. {summary} Issues: {details}"

    def health_report(self) -> str:
        """Full health report: module status, recent improvements, uptime."""
        try:
            import psutil
            cpu    = psutil.cpu_percent(interval=0.5)
            ram    = psutil.virtual_memory()
            disk   = psutil.disk_usage("/")
            stats  = (f"CPU at {cpu:.0f}%, RAM {ram.percent:.0f}% used, "
                      f"disk {disk.percent:.0f}% full.")
        except ImportError:
            stats = "System stats unavailable (psutil not installed)."
        except Exception as exc:
            stats  = f"System stats error: {exc}"

        # Count improvements
        try:
            log_text = self._log_path.read_text(encoding="utf-8")
            n_improvements = log_text.count("| ✅")
        except Exception:
            n_improvements = 0

        # Module check
        test_summary = self.run_self_test()

        return (f"Health report: {stats} "
                f"I have made {n_improvements} logged improvements. "
                f"{test_summary}")

    def read_log(self) -> str:
        """Read the last 5 entries from ATLAS_IMPROVEMENTS.md aloud."""
        try:
            text = self._log_path.read_text(encoding="utf-8")
            rows = [ln for ln in text.splitlines() if ln.startswith("|") and "Date" not in ln and "---" not in ln]
            if not rows:
                return "I haven't made any logged improvements yet."
            recent = rows[-5:]
            entries = []
            for row in recent:
                parts = [p.strip() for p in row.strip("|").split("|")]
                if len(parts) >= 4:
                    entries.append(f"On {parts[0]}, {parts[2]} in {parts[1]}.")
            return " ".join(entries) if entries else "No improvements logged yet."
        except Exception as exc:
            return f"Couldn't read the improvement log: {exc}"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _backup(self, fpath: Path, content: str) -> Path:
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
        bname  = f"{fpath.stem}_{ts}{fpath.suffix}.bak"
        bpath  = self._versions / bname
        bpath.write_text(content, encoding="utf-8")
        log.info("[SELF-IMPROVE] Backed up %s → %s", fpath.name, bname)
        return bpath

    @staticmethod
    def _validate_syntax(fpath: Path) -> tuple[bool, str]:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(fpath)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout).strip()[:200]

    def _log(self, fname: str, desc: str, why: str, success: bool) -> None:
        try:
            ts     = datetime.now().strftime("%Y-%m-%d %H:%M")
            status = "✅" if success else "❌"
            row    = f"| {ts} | {fname} | {desc} | {why} | {status} |\n"
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception as exc:
            log.warning("Log write failed: %s", exc)
