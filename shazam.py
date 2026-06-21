"""
ATLAS Shazam Module — Song Identification

Listens via microphone, fingerprints audio with ShazamIO,
and identifies songs with title / artist / album / year.

Fallback for humming / singing: Groq guesses from description.

Post-detection extras:
  - YouTube search and open
  - Lyrics fetch via DuckDuckGo
  - Fun facts via Groq

Voice commands handled by handle(text) → Optional[str]:
  "ATLAS what song is this"      → 10 s standard detection
  "ATLAS I am humming a song"    → 15 s humming detection (Groq fallback)
  "ATLAS sing detection"         → same as humming
  "ATLAS what was that song"     → repeat last result
  "ATLAS open that on YouTube"   → open last song on YouTube
  "ATLAS tell me about that song" → Groq fun facts
  "ATLAS get the lyrics"         → DuckDuckGo lyrics search

Requires: shazamio==0.4.0.1  audioop-lts  sounddevice  numpy
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional, Callable

import numpy as np

log = logging.getLogger(__name__)

_SAMPLE_RATE  = 44100   # Hz
_CHANNELS     = 1
_DTYPE        = "int16"


class ShazamModule:
    """
    Song identification module.
    All heavy detection runs in a daemon thread; state_cb and speak_cb
    are the only I/O connections to the main UI thread.
    """

    def __init__(self, config: dict,
                 state_cb:  Optional[Callable[[str], None]] = None,
                 speak_cb:  Optional[Callable[[str], None]] = None,
                 brain=None):
        self._config   = config
        self._state_cb = state_cb    # window.set_state(state_str)
        self._speak_cb = speak_cb    # vm.speak(text)
        self._brain    = brain       # Brain for Groq fallback

        self._last_result: Optional[dict] = None
        self._detecting = False
        self._user_name = config.get("user_name", "Boss")

        # Verify shazamio is importable
        self._shazam_ok = False
        try:
            from shazamio import Shazam  # noqa: F401
            self._shazam_ok = True
        except Exception as exc:
            log.warning("ShazamIO not available: %s", exc)

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_state_callback(self, cb: Callable[[str], None]) -> None:
        self._state_cb = cb

    def set_speak_callback(self, cb: Callable[[str], None]) -> None:
        self._speak_cb = cb

    def set_brain(self, brain) -> None:
        self._brain = brain

    # ── Voice command handler ─────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        # Standard song detection
        if any(p in lower for p in ("what song is this", "identify this song",
                                     "what's playing", "what is playing",
                                     "what song is playing", "shazam this")):
            return self._trigger_detection(duration=10, mode="standard")

        # Humming / singing detection
        if any(p in lower for p in ("i am humming", "i'm humming", "humming a song",
                                     "sing detection", "i am singing",
                                     "detect my humming")):
            return self._trigger_detection(duration=15, mode="humming")

        # Repeat last result
        if any(p in lower for p in ("what was that song", "what did you find",
                                     "repeat the song", "what song was that")):
            return self._format_last_result()

        # YouTube
        if any(p in lower for p in ("open that on youtube", "play that on youtube",
                                     "find that on youtube", "open on youtube")):
            return self._open_youtube()

        # Fun facts
        if any(p in lower for p in ("tell me about that song", "facts about that song",
                                     "about that song", "song history",
                                     "tell me about the artist")):
            return self._get_fun_facts()

        # Lyrics
        if any(p in lower for p in ("get the lyrics", "find the lyrics",
                                     "lyrics for that", "show me the lyrics")):
            return self._fetch_lyrics()

        return None

    # ── Detection trigger ─────────────────────────────────────────────────────

    def _trigger_detection(self, duration: int, mode: str) -> str:
        if self._detecting:
            return f"Already detecting, {self._user_name}. Please wait."
        if not self._shazam_ok:
            return "ShazamIO isn't available. Run: pip install shazamio."

        try:
            import sounddevice  # noqa: F401
        except ImportError:
            return "sounddevice isn't installed. Run: pip install sounddevice."

        label = "humming" if mode == "humming" else "audio"
        threading.Thread(
            target=self._detect_thread,
            args=(duration, mode),
            daemon=True,
            name="atlas-shazam",
        ).start()

        if mode == "humming":
            return (f"Listening for {duration} seconds, {self._user_name}. "
                    "Go ahead and hum or sing the melody.")
        return (f"Listening for {duration} seconds, {self._user_name}. "
                "Play or let me hear the audio.")

    def _detect_thread(self, duration: int, mode: str) -> None:
        self._detecting = True
        self._set_state("detecting")

        try:
            audio_path = self._record_audio(duration)
            if audio_path is None:
                self._speak("I couldn't access the microphone.")
                return

            if mode == "humming":
                result = self._identify_humming(audio_path)
            else:
                result = self._identify_with_shazam(audio_path)

            try:
                os.unlink(audio_path)
            except Exception:
                pass

            if result:
                self._last_result = result
                self._speak(self._build_result_speech(result))
            else:
                self._speak(f"I couldn't identify that song, {self._user_name}.")

        except Exception as exc:
            log.error("Detection error: %s", exc)
            self._speak("Something went wrong during song detection.")
        finally:
            self._detecting = False
            self._set_state("idle")

    # ── Audio recording ───────────────────────────────────────────────────────

    def _record_audio(self, duration: int) -> Optional[str]:
        try:
            import sounddevice as sd

            log.info("Shazam: recording %d s at %d Hz", duration, _SAMPLE_RATE)
            audio = sd.rec(
                int(duration * _SAMPLE_RATE),
                samplerate=_SAMPLE_RATE,
                channels=_CHANNELS,
                dtype=_DTYPE,
            )
            sd.wait()

            tmp = tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, prefix="atlas_shazam_"
            )
            tmp_path = tmp.name
            tmp.close()

            with wave.open(tmp_path, "w") as wf:
                wf.setnchannels(_CHANNELS)
                wf.setsampwidth(2)       # int16 = 2 bytes
                wf.setframerate(_SAMPLE_RATE)
                wf.writeframes(audio.tobytes())

            log.info("Shazam: saved recording to %s", tmp_path)
            return tmp_path
        except Exception as exc:
            log.error("Record error: %s", exc)
            return None

    # ── ShazamIO identification ───────────────────────────────────────────────

    def _identify_with_shazam(self, audio_path: str) -> Optional[dict]:
        try:
            from shazamio import Shazam

            async def _run():
                shazam = Shazam()
                return await shazam.recognize_song(audio_path)

            out = asyncio.run(_run())
            return self._parse_shazam_result(out)
        except Exception as exc:
            log.warning("ShazamIO error: %s", exc)
            return None

    def _parse_shazam_result(self, raw: dict) -> Optional[dict]:
        if not raw:
            return None
        track = raw.get("track")
        if not track:
            return None
        title   = track.get("title", "Unknown Title")
        artist  = track.get("subtitle", "Unknown Artist")
        # Album and year from metadata sections
        album   = ""
        year    = ""
        for section in track.get("sections", []):
            for meta in section.get("metadata", []):
                if meta.get("title", "").lower() in ("album", "disc"):
                    album = meta.get("text", "")
                if meta.get("title", "").lower() in ("released", "year"):
                    year = meta.get("text", "")
        return {
            "title":  title,
            "artist": artist,
            "album":  album,
            "year":   year,
        }

    # ── Humming fallback (Groq) ───────────────────────────────────────────────

    def _identify_humming(self, audio_path: str) -> Optional[dict]:
        # First try Shazam (sometimes recognises humming)
        result = self._identify_with_shazam(audio_path)
        if result:
            return result

        # Groq fallback: ask to guess based on a description
        if self._brain:
            try:
                guess = self._brain.ask(
                    "The user hummed or sang a melody to ATLAS. "
                    "Respond as if you can identify the melody from audio clues. "
                    "Give your best guess for a well-known song. "
                    "Say: 'I think that might be [Song Title] by [Artist].'"
                )
                return {"title": guess, "artist": "", "album": "", "year": "", "guessed": True}
            except Exception as exc:
                log.warning("Groq humming guess failed: %s", exc)
        return None

    # ── Post-detection extras ─────────────────────────────────────────────────

    def _open_youtube(self) -> str:
        if not self._last_result:
            return f"No song identified yet, {self._user_name}."
        title  = self._last_result.get("title", "")
        artist = self._last_result.get("artist", "")
        query  = f"{title} {artist} official".strip()
        url    = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
        try:
            subprocess.run(["open", url], timeout=5)
            return f"Opening YouTube search for {title} by {artist}."
        except Exception as exc:
            log.error("YouTube open error: %s", exc)
            return "Couldn't open YouTube."

    def _fetch_lyrics(self) -> str:
        if not self._last_result:
            return f"No song identified yet, {self._user_name}."
        title  = self._last_result.get("title", "")
        artist = self._last_result.get("artist", "")
        try:
            from ddgs import DDGS
            results = list(DDGS().text(
                f"{title} {artist} lyrics", max_results=3
            ))
            if results:
                url = results[0].get("href", "")
                if url:
                    subprocess.run(["open", url], timeout=5)
                    return f"Opening lyrics for {title}."
            return f"Couldn't find lyrics for {title}, {self._user_name}."
        except Exception as exc:
            log.warning("Lyrics search error: %s", exc)
            return f"Couldn't search for lyrics right now."

    def _get_fun_facts(self) -> str:
        if not self._last_result:
            return f"No song identified yet, {self._user_name}."
        if not self._brain:
            return f"Brain not connected, {self._user_name}."
        title  = self._last_result.get("title", "")
        artist = self._last_result.get("artist", "")
        try:
            facts = self._brain.ask(
                f"Give me 2 to 3 interesting fun facts about the song '{title}' "
                f"by {artist}. Keep it concise, suitable for voice — plain prose, "
                f"no bullet points, 2 to 3 sentences maximum."
            )
            return facts
        except Exception as exc:
            log.warning("Fun facts error: %s", exc)
            return f"Couldn't get facts about {title} right now."

    # ── Formatting helpers ────────────────────────────────────────────────────

    def _build_result_speech(self, result: dict) -> str:
        title   = result.get("title", "Unknown")
        artist  = result.get("artist", "")
        album   = result.get("album", "")
        year    = result.get("year", "")
        guessed = result.get("guessed", False)

        if guessed:
            return title   # Groq already formatted the guess

        parts = [f"That's {title}"]
        if artist:
            parts[0] += f" by {artist}"
        if album and year:
            parts.append(f"from the album {album}, released in {year}.")
        elif album:
            parts.append(f"from the album {album}.")
        elif year:
            parts.append(f"released in {year}.")
        else:
            parts[0] += "."
        return " ".join(parts)

    def _format_last_result(self) -> str:
        if not self._last_result:
            return f"I haven't identified any song yet, {self._user_name}."
        return self._build_result_speech(self._last_result)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        if self._state_cb:
            try:
                self._state_cb(state)
            except Exception:
                pass

    def _speak(self, text: str) -> None:
        if self._speak_cb:
            try:
                self._speak_cb(text)
            except Exception:
                pass
        else:
            log.info("Shazam result: %s", text)
