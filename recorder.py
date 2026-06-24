"""
ATLAS Screen Recorder — real-time screen recording with AI commentary.

Modes:
  auto    — AI watches screen every N seconds and narrates meaningful changes
  guided  — user narrates, ATLAS enhances phrasing
  silent  — records screen only, no narration

Voice commands:
  "ATLAS record my screen"        → start auto mode
  "ATLAS start recording"         → same
  "ATLAS record silently"         → silent mode
  "ATLAS guided recording"        → guided mode
  "ATLAS new chapter [title]"     → add chapter marker
  "ATLAS pause recording"         → pause
  "ATLAS resume recording"        → resume
  "ATLAS stop recording"          → stop and process
  "ATLAS cancel recording"        → discard
  "ATLAS how long have I been recording" → duration
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

_COMMENTARY_PROMPT = (
    "You are narrating a screen recording tutorial. "
    "Describe what is happening on screen in one clear sentence as if narrating for a viewer. "
    "Be specific about what app and action is visible. Keep it under 15 words. "
    "Only narrate if something meaningful is happening — respond with exactly 'IDLE' "
    "if nothing significant is happening."
)


@dataclass
class Chapter:
    timestamp_secs: float
    title: str


@dataclass
class RecordingSession:
    title: str
    mode: str                          # auto | guided | silent
    start_time: float = field(default_factory=time.monotonic)
    chapters: List[Chapter] = field(default_factory=list)
    commentary_log: List[str] = field(default_factory=list)
    video_path: Optional[Path] = None
    paused: bool = False
    pause_start: float = 0.0
    total_paused: float = 0.0

    def elapsed(self) -> float:
        base = time.monotonic() - self.start_time - self.total_paused
        if self.paused:
            base -= (time.monotonic() - self.pause_start)
        return max(0.0, base)

    def elapsed_str(self) -> str:
        secs = int(self.elapsed())
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ATLASRecorder:
    """Screen recorder with AI commentary narration."""

    def __init__(self, config: dict, speak_cb: Callable,
                 brain, vault_brain=None, smart_card_mgr=None):
        self._config          = config
        self._speak           = speak_cb
        self._brain           = brain
        self._vault_brain     = vault_brain
        self._smart_card_mgr  = smart_card_mgr

        self._session: Optional[RecordingSession] = None
        self._ffmpeg          = shutil.which("ffmpeg")
        self._stop_event      = threading.Event()
        self._commentary_thread: Optional[threading.Thread] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None

        # Vision client (same as vision.py pattern)
        self._vision_client = None
        self._vision_model  = config.get("api", {}).get(
            "vision_model", "qwen/qwen2.5-vl-7b-instruct:free")
        self._init_vision()

        rec_folder = config.get("recordings_folder",
                                "~/Desktop/ATLAS_Projects/Recordings")
        self._output_dir = Path(rec_folder).expanduser()
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._interval = int(config.get("commentary_interval_seconds", 3))
        log.info("ATLASRecorder: ready (ffmpeg=%s).", "yes" if self._ffmpeg else "NO")

    # ── Init ──────────────────────────────────────────────────────────────────

    def _init_vision(self):
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            return
        try:
            from openai import OpenAI
            self._vision_client = OpenAI(
                base_url="https://openrouter.ai/api/v1", api_key=key)
        except Exception as exc:
            log.warning("Recorder: vision client init failed: %s", exc)

    # ── Voice command router ───────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()
        lower = re.sub(r"^atlas\s+", "", lower)

        if any(p in lower for p in ("record my screen", "start recording")):
            return self._start("auto")
        if "record silently" in lower or "silent recording" in lower:
            return self._start("silent")
        if "guided recording" in lower:
            return self._start("guided")
        if lower.startswith("new chapter"):
            title = re.sub(r"^new chapter\s*", "", lower).strip() or "Chapter"
            return self._add_chapter(title)
        if "pause recording" in lower:
            return self._pause()
        if "resume recording" in lower:
            return self._resume()
        if "stop recording" in lower:
            return self._stop()
        if "cancel recording" in lower:
            return self._cancel()
        if any(p in lower for p in ("how long have i been recording",
                                     "how long recording", "recording duration")):
            return self._duration()
        return None

    # ── Recording control ─────────────────────────────────────────────────────

    def _start(self, mode: str) -> str:
        if self._session:
            return "Already recording, Boss. Say 'ATLAS stop recording' first."
        if not self._ffmpeg:
            return ("ffmpeg is required for screen recording. "
                    "Install it with: brew install ffmpeg")

        ts = datetime.now().strftime("%Y-%m-%d-%H-%M")
        title = f"ATLAS-{ts}"
        vid_path = self._output_dir / f"{title}.mp4"

        self._session = RecordingSession(title=title, mode=mode,
                                         video_path=vid_path)
        self._stop_event.clear()

        # Start ffmpeg in background thread
        threading.Thread(target=self._run_ffmpeg, args=(vid_path,),
                         daemon=True, name="atlas-rec-ffmpeg").start()

        if mode != "silent":
            self._commentary_thread = threading.Thread(
                target=self._commentary_loop, daemon=True, name="atlas-rec-commentary")
            self._commentary_thread.start()

        mode_label = {"auto": "auto commentary", "guided": "guided",
                      "silent": "silent"}.get(mode, mode)
        return (f"Recording started in {mode_label} mode, Boss. "
                f"Say 'ATLAS stop recording' when done.")

    def _run_ffmpeg(self, out: Path) -> None:
        fps = self._config.get("recording_fps", 30)
        cmd = [
            self._ffmpeg, "-y",
            "-f", "avfoundation",
            "-framerate", str(fps),
            "-i", "1:none",           # screen only, no audio track from ffmpeg
            "-vcodec", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            str(out),
        ]
        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._ffmpeg_proc.wait()
        except Exception as exc:
            log.error("Recorder: ffmpeg error: %s", exc)
        finally:
            self._ffmpeg_proc = None

    def _commentary_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break
            if not self._session or self._session.paused:
                continue
            if self._session.mode == "auto":
                self._auto_narrate()

    def _auto_narrate(self) -> None:
        b64 = self._capture_screenshot()
        if not b64:
            return
        narration = self._ask_vision(b64)
        if narration and narration.strip().upper() != "IDLE" and narration.strip():
            self._session.commentary_log.append(narration)
            self._speak(narration)

    def _capture_screenshot(self) -> Optional[str]:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            # Resize to 1280x800 to reduce token cost
            img.thumbnail((1280, 800))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:
            log.debug("Recorder screenshot: %s", exc)
            return None

    def _ask_vision(self, b64: str) -> Optional[str]:
        if not self._vision_client:
            return None
        try:
            resp = self._vision_client.chat.completions.create(
                model=self._vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _COMMENTARY_PROMPT},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }],
                max_tokens=60,
                timeout=10.0,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            log.debug("Recorder vision: %s", exc)
            return None

    def _add_chapter(self, title: str) -> str:
        if not self._session:
            return "No recording in progress, Boss."
        ch = Chapter(timestamp_secs=self._session.elapsed(), title=title)
        self._session.chapters.append(ch)
        ts = self._session.elapsed_str()
        return f"Chapter '{title}' added at {ts}, Boss."

    def _pause(self) -> str:
        if not self._session:
            return "No recording in progress, Boss."
        if self._session.paused:
            return "Already paused, Boss."
        self._session.paused = True
        self._session.pause_start = time.monotonic()
        return "Recording paused, Boss."

    def _resume(self) -> str:
        if not self._session:
            return "No recording in progress, Boss."
        if not self._session.paused:
            return "Recording is already running, Boss."
        self._session.total_paused += time.monotonic() - self._session.pause_start
        self._session.paused = False
        return "Recording resumed, Boss."

    def _stop(self) -> str:
        if not self._session:
            return "No recording in progress, Boss."
        session = self._session
        self._stop_event.set()

        if self._ffmpeg_proc:
            self._ffmpeg_proc.terminate()

        duration = session.elapsed_str()
        threading.Thread(target=self._post_process, args=(session,),
                         daemon=True, name="atlas-rec-post").start()
        self._session = None
        return f"Recording stopped, Boss. Processing {duration} recording now."

    def _cancel(self) -> str:
        if not self._session:
            return "No recording in progress, Boss."
        self._stop_event.set()
        if self._ffmpeg_proc:
            self._ffmpeg_proc.terminate()
        vid = self._session.video_path
        self._session = None
        if vid and vid.exists():
            vid.unlink(missing_ok=True)
        return "Recording cancelled and discarded, Boss."

    def _duration(self) -> str:
        if not self._session:
            return "No recording in progress, Boss."
        return f"You have been recording for {self._session.elapsed_str()}, Boss."

    # ── Post-processing ───────────────────────────────────────────────────────

    def _post_process(self, session: RecordingSession) -> None:
        time.sleep(1.0)   # let ffmpeg flush
        vid = session.video_path
        duration = session.elapsed_str()

        # Generate summary from commentary log
        summary = ""
        if session.commentary_log and self._brain:
            log_text = "\n".join(session.commentary_log[:40])
            prompt = (f"Based on these narration notes from a screen recording, "
                      f"write a concise 3-sentence summary of what was recorded. "
                      f"Notes:\n{log_text}")
            try:
                summary = self._brain.ask(prompt)
            except Exception:
                summary = "Recording summary unavailable."

        # Save summary to Obsidian
        if summary and self._vault_brain:
            try:
                folder = self._vault_brain.atlas / "Recordings"
                folder.mkdir(parents=True, exist_ok=True)
                fname = f"{session.title}.md"
                chapters_md = ""
                if session.chapters:
                    chapters_md = "\n## Chapters\n" + "\n".join(
                        f"- {_fmt_secs(c.timestamp_secs)} — {c.title}"
                        for c in session.chapters)
                (folder / fname).write_text(
                    f"---\ntags: [atlas, recording]\ndate: {datetime.now().date()}\n---\n\n"
                    f"# Recording: {session.title}\n\n"
                    f"**Duration:** {duration}  \n"
                    f"**File:** {vid}\n\n"
                    f"## Summary\n{summary}\n"
                    f"{chapters_md}\n",
                    encoding="utf-8")
            except Exception as exc:
                log.warning("Recorder: vault save failed: %s", exc)

        # Smart Card
        if self._smart_card_mgr:
            card_text = (
                f"Recording saved. Duration: {duration}. "
                f"File: {vid.name if vid else 'unknown'}. "
                + (f"Chapters: {len(session.chapters)}. " if session.chapters else "")
                + (summary[:200] if summary else "")
            )
            try:
                self._smart_card_mgr.on_response("recording complete", card_text)
            except Exception:
                pass

        size_mb = ""
        if vid and vid.exists():
            size_mb = f" ({vid.stat().st_size / 1_048_576:.1f} MB)"

        self._speak(
            f"Recording saved, Boss. {duration} recording{size_mb} "
            f"saved to your Recordings folder."
        )

    def set_model(self, model: str) -> None:
        pass   # compat shim


def _fmt_secs(secs: float) -> str:
    s = int(secs)
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"
