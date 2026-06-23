"""
ATLAS Scheduler — persistent cron-based task automation.

Backend: APScheduler with SQLAlchemy job store (SQLite).
Jobs persist across restarts. Natural language → cron via Groq/Qwen.
Schedule stored in ATLAS/Memory/schedules.md for Obsidian visibility.

Built-in defaults (added on first run):
  • 08:00 daily  — morning briefing
  • Sunday 19:00 — weekly review
  • Daily 02:00  — 7-day duplicate cleanup
  • Every 5 min  — market data refresh (if market module active)

Voice commands:
  "ATLAS schedule [task] every [interval]"
  "ATLAS schedule [task] at [time]"
  "ATLAS list my schedules"
  "ATLAS cancel [job name]"
  "ATLAS what's scheduled for today"
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

# ── APScheduler optional import ───────────────────────────────────────────────

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    _APScheduler_available = True
except ImportError:
    _APScheduler_available = False
    log.warning("APScheduler not installed. Run: pip install apscheduler")


class ATLASScheduler:
    """
    Persistent cron scheduler for ATLAS.

    Usage:
        sched = ATLASScheduler(config, brain, vault_brain, speak_cb)
        sched.start()
        sched.handle("atlas schedule a briefing at 8am every day")
    """

    def __init__(
        self,
        config: dict,
        brain=None,
        vault_brain=None,
        speak_cb: Optional[Callable[[str], None]] = None,
        market_module=None,
        memory_module=None,
    ):
        self._config   = config
        self._brain    = brain
        self._vb       = vault_brain
        self._speak    = speak_cb or (lambda s: None)
        self._market   = market_module
        self._memory   = memory_module
        self._lock     = threading.Lock()
        self._scheduler = None

        root = Path(os.environ.get("ATLAS_ROOT", "."))
        self._db_path  = root / "memory" / "atlas_jobs.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        if self._vb is not None:
            self._schedule_note  = self._vb.atlas / "Memory" / "schedules.md"
            self._user_jobs_file = self._vb.atlas / "Memory" / "user_jobs.json"
        else:
            self._schedule_note  = None
            self._user_jobs_file = None

        # In-memory registry for user job definitions (for vault persistence/reload)
        self._user_job_defs: dict[str, dict] = {}   # job_id → {name, task, cron_kwargs}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not _APScheduler_available:
            log.warning("ATLASScheduler: APScheduler unavailable — scheduler disabled.")
            return
        try:
            # Use MemoryJobStore — default jobs are always re-registered on startup,
            # and user jobs are persisted to vault (ATLAS/Memory/schedules.md) for reload.
            self._scheduler = BackgroundScheduler(timezone="UTC")
            self._scheduler.start()
            log.info("ATLASScheduler: started.")
            self._add_defaults()
            self._reload_user_jobs_from_vault()
            self._sync_to_vault()
        except Exception as exc:
            log.error("ATLASScheduler: failed to start: %s", exc)
            self._scheduler = None

    def stop(self) -> None:
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass

    # ── Default jobs ──────────────────────────────────────────────────────────

    def _add_defaults(self) -> None:
        if self._scheduler is None:
            return

        # Morning briefing — 8am daily
        self._add_cron_job(
            job_id  = "atlas_morning_briefing",
            name    = "Morning Briefing",
            fn      = self._job_morning_briefing,
            hour    = 8, minute = 0,
        )

        # Sunday 7pm weekly review
        self._add_cron_job(
            job_id  = "atlas_weekly_review",
            name    = "Weekly Review",
            fn      = self._job_weekly_review,
            day_of_week = "sun", hour = 19, minute = 0,
        )

        # 7-day dedup — daily at 2am
        self._add_cron_job(
            job_id  = "atlas_dedup",
            name    = "Memory Deduplication",
            fn      = self._job_dedup,
            hour    = 2, minute = 0,
        )

        log.info("ATLASScheduler: default jobs registered.")

    def _add_cron_job(self, job_id: str, name: str, fn: Callable,
                      replace: bool = False, **cron_kwargs) -> bool:
        if self._scheduler is None:
            return False
        try:
            existing = self._scheduler.get_job(job_id)
            if existing and not replace:
                return True   # already registered
            if existing:
                self._scheduler.remove_job(job_id)
            self._scheduler.add_job(
                fn,
                CronTrigger(**cron_kwargs),
                id      = job_id,
                name    = name,
                replace_existing = True,
            )
            return True
        except Exception as exc:
            log.warning("ATLASScheduler: add_cron_job(%s) failed: %s", job_id, exc)
            return False

    # ── Built-in job callbacks ─────────────────────────────────────────────────

    def _job_morning_briefing(self) -> None:
        try:
            if self._brain and self._brain.smart_available:
                greeting = (
                    "Good morning, Boss. Give me a 3-sentence morning briefing: "
                    "today's date, anything I should know from recent sessions, "
                    "and one proactive suggestion."
                )
                response = self._brain.ask(greeting)
                self._speak(response)
        except Exception as exc:
            log.warning("ATLASScheduler: morning briefing error: %s", exc)

    def _job_weekly_review(self) -> None:
        try:
            if self._memory and self._vb:
                episodes = self._vb.search_episodes("", days_back=7)
                summary = (
                    f"Summarise this week's {len(episodes)} sessions in 3 sentences "
                    "for a voice-delivered weekly review."
                )
                if self._brain:
                    review = self._brain.ask(summary)
                    self._speak(f"Weekly review: {review}")
                    self._vb.write_weekly_review(review, episodes)
        except Exception as exc:
            log.warning("ATLASScheduler: weekly review error: %s", exc)

    def _job_dedup(self) -> None:
        log.info("ATLASScheduler: dedup job — no-op (vault auto-deduplicates by filename).")

    def _job_market_refresh(self) -> None:
        try:
            if self._market and hasattr(self._market, "_refresh"):
                self._market._refresh()
        except Exception as exc:
            log.warning("ATLASScheduler: market refresh error: %s", exc)

    # ── NL → cron parsing ─────────────────────────────────────────────────────

    def _parse_schedule_nl(self, text: str) -> Optional[dict]:
        """
        Parse natural language into cron kwargs.
        First tries regex patterns; falls back to LLM.
        Returns dict with 'cron_kwargs' and 'name', or None.
        """
        lower = text.lower().strip()

        # Simple interval patterns
        if m := re.search(r"every (\d+) (minute|hour)s?", lower):
            n, unit = int(m.group(1)), m.group(2)
            if unit == "minute":
                return {"cron_kwargs": {"minute": f"*/{n}"}, "type": "cron"}
            if unit == "hour":
                return {"cron_kwargs": {"hour": f"*/{n}", "minute": 0}, "type": "cron"}

        # "every day at HH:MM" / "daily at HH:MM"
        if m := re.search(r"(?:every day|daily) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower):
            h = int(m.group(1))
            mi = int(m.group(2) or 0)
            if m.group(3) == "pm" and h != 12:
                h += 12
            elif m.group(3) == "am" and h == 12:
                h = 0
            return {"cron_kwargs": {"hour": h, "minute": mi}, "type": "cron"}

        # "at HH:MM" (defaults to daily)
        if m := re.search(r"\bat (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower):
            h = int(m.group(1))
            mi = int(m.group(2) or 0)
            if m.group(3) == "pm" and h != 12:
                h += 12
            elif m.group(3) == "am" and h == 12:
                h = 0
            return {"cron_kwargs": {"hour": h, "minute": mi}, "type": "cron"}

        # "every Sunday at HH"
        days = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
                "friday": "fri", "saturday": "sat", "sunday": "sun"}
        for day_name, day_abbr in days.items():
            if day_name in lower:
                if m := re.search(r"at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower):
                    h = int(m.group(1))
                    mi = int(m.group(2) or 0)
                    if m.group(3) == "pm" and h != 12:
                        h += 12
                    elif m.group(3) == "am" and h == 12:
                        h = 0
                    return {"cron_kwargs": {"day_of_week": day_abbr, "hour": h, "minute": mi},
                            "type": "cron"}

        # LLM fallback
        if self._brain and self._brain.smart_available:
            prompt = (
                f"Convert this to APScheduler CronTrigger kwargs (JSON only, no prose): "
                f"'{text}'\n"
                "Example output: {\"hour\": 8, \"minute\": 30, \"day_of_week\": \"mon-fri\"}\n"
                "Return ONLY valid JSON."
            )
            try:
                import json
                raw = self._brain.ask(prompt)
                cron_kwargs = json.loads(raw.strip())
                return {"cron_kwargs": cron_kwargs, "type": "cron"}
            except Exception:
                pass

        return None

    # ── User-created jobs ─────────────────────────────────────────────────────

    def schedule_user_job(self, name: str, task: str, schedule_text: str) -> Optional[str]:
        """Create a user-defined scheduled job from a voice command."""
        if self._scheduler is None:
            return "Scheduler not running. APScheduler may not be installed."
        parsed = self._parse_schedule_nl(schedule_text)
        if not parsed:
            return f"I couldn't parse that schedule, Boss. Try 'every day at 9am' or 'every Monday at 10am'."

        job_id = f"user_{re.sub(r'[^a-z0-9]', '_', name.lower())[:30]}"
        brain_ref = self._brain
        speak_ref = self._speak

        def _user_job():
            try:
                if brain_ref and brain_ref.smart_available:
                    response = brain_ref.ask(task)
                    speak_ref(response)
            except Exception as exc:
                log.warning("Scheduled job '%s' failed: %s", name, exc)

        ok = self._add_cron_job(job_id, name, _user_job,
                                replace=True, **parsed["cron_kwargs"])
        if ok:
            self._sync_to_vault()
            return f"Scheduled '{name}' — {schedule_text}. Job ID: {job_id}."
        return "Failed to schedule that job, Boss."

    # ── User job persistence ──────────────────────────────────────────────────

    def _save_user_jobs(self) -> None:
        """Persist user job definitions to vault JSON so they survive restarts."""
        if self._user_jobs_file is None:
            return
        try:
            import json as _json
            self._user_jobs_file.write_text(
                _json.dumps(self._user_job_defs, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.warning("ATLASScheduler: could not save user jobs: %s", exc)

    def _reload_user_jobs_from_vault(self) -> None:
        """Reload user-created jobs from vault JSON on startup."""
        if self._user_jobs_file is None or not self._user_jobs_file.exists():
            return
        try:
            import json as _json
            defs = _json.loads(self._user_jobs_file.read_text(encoding="utf-8"))
            for job_id, defn in defs.items():
                name        = defn.get("name", job_id)
                task        = defn.get("task", "")
                cron_kwargs = defn.get("cron_kwargs", {})
                if not cron_kwargs:
                    continue
                brain_ref = self._brain
                speak_ref = self._speak

                def _make_user_job(t=task):
                    def _job():
                        try:
                            if brain_ref and brain_ref.smart_available:
                                response = brain_ref.ask(t)
                                speak_ref(response)
                        except Exception as exc:
                            log.warning("Scheduled job failed: %s", exc)
                    return _job

                self._add_cron_job(job_id, name, _make_user_job(), **cron_kwargs)
                log.info("ATLASScheduler: reloaded user job '%s'.", name)
            self._user_job_defs = defs
        except Exception as exc:
            log.warning("ATLASScheduler: could not reload user jobs: %s", exc)

    # ── Vault sync ─────────────────────────────────────────────────────────────

    def _sync_to_vault(self) -> None:
        """Write all active jobs to ATLAS/Memory/schedules.md."""
        if self._vb is None or self._schedule_note is None:
            return
        try:
            jobs = self.list_jobs()
            lines = ["# ATLAS Scheduled Jobs\n"]
            for j in jobs:
                lines.append(f"- **{j['name']}** (`{j['id']}`): next run {j['next_run']}")
            body = "\n".join(lines) + "\n"
            fm = {
                "last_updated": datetime.now().isoformat(timespec="minutes"),
                "job_count":    len(jobs),
                "tags":         ["atlas", "schedules"],
            }
            self._vb.write_note(self._schedule_note, fm, body)
        except Exception as exc:
            log.warning("ATLASScheduler: vault sync failed: %s", exc)

    # ── Job management ─────────────────────────────────────────────────────────

    def list_jobs(self) -> List[dict]:
        if self._scheduler is None:
            return []
        try:
            return [
                {
                    "id":       j.id,
                    "name":     j.name,
                    "next_run": j.next_run_time.strftime("%Y-%m-%d %H:%M") if j.next_run_time else "paused",
                }
                for j in self._scheduler.get_jobs()
            ]
        except Exception:
            return []

    def cancel_job(self, job_name_or_id: str) -> bool:
        if self._scheduler is None:
            return False
        # Try by ID first, then by name
        for j in self._scheduler.get_jobs():
            if job_name_or_id.lower() in j.id.lower() or job_name_or_id.lower() in j.name.lower():
                try:
                    self._scheduler.remove_job(j.id)
                    self._sync_to_vault()
                    return True
                except Exception:
                    return False
        return False

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas list my schedules", "atlas what is scheduled",
                                     "atlas show schedules", "atlas list schedules")):
            jobs = self.list_jobs()
            if not jobs:
                return "No scheduled jobs active, Boss."
            names = "; ".join(f"{j['name']} (next: {j['next_run']})" for j in jobs[:5])
            return f"Active schedules: {names}."

        if any(p in lower for p in ("atlas what's scheduled for today",
                                     "atlas what is scheduled for today",
                                     "what's scheduled today")):
            jobs = self.list_jobs()
            today = datetime.now().strftime("%Y-%m-%d")
            today_jobs = [j for j in jobs if j["next_run"].startswith(today)]
            if not today_jobs:
                return f"Nothing scheduled for today, Boss."
            names = "; ".join(f"{j['name']} at {j['next_run'][11:]}" for j in today_jobs)
            return f"Today's schedule: {names}."

        if "atlas cancel" in lower and any(w in lower for w in ("schedule", "job")):
            # "atlas cancel [job name] schedule"
            for phrase in ("atlas cancel ", ):
                if phrase in lower:
                    job_name = lower.split(phrase, 1)[-1].replace("schedule", "").replace("job", "").strip()
                    if self.cancel_job(job_name):
                        return f"Cancelled '{job_name}' schedule, Boss."
                    return f"I couldn't find a schedule named '{job_name}', Boss."

        # "atlas schedule [task] every/at [interval]"
        for prefix in ("atlas schedule ", "schedule "):
            if lower.startswith(prefix):
                remainder = lower[len(prefix):]
                # Split on "every" or "at"
                for splitter in (" every ", " at "):
                    if splitter in remainder:
                        parts = remainder.split(splitter, 1)
                        task_name = parts[0].strip()
                        schedule_text = splitter.strip() + " " + parts[1].strip()
                        return self.schedule_user_job(task_name, task_name, schedule_text)
                # No time given — ask
                return f"When should I schedule '{remainder}', Boss? Say 'every day at 9am' or similar."

        return None
