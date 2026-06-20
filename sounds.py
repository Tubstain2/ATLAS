"""
ATLAS Iron Man Sound Design — all sounds generated programmatically

No downloads required. All audio created with numpy sine waves, harmonics,
and ADSR envelopes.

Sound catalogue:
  STARTUP        — ascending electronic tone, suit powering up
  WAKE           — subtle two-tone chime, wake word detected
  PROCESSING     — quiet low hum while AI thinks
  RESPONSE_READY — soft click before ATLAS speaks
  SUCCESS        — satisfying confirmation tone
  ERROR          — low descending tone
  SCREENSHOT     — subtle camera shutter
  AMBIENT_HUM    — barely audible background electronic ambience

Usage in main.py:
    sounds = SoundEngine(config)
    sounds.play("WAKE")
    sounds.start_ambient()
    sounds.stop_ambient()
    sounds.stop()
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

_SR = 44_100   # sample rate for all generated sounds


class SoundEngine:
    """
    Iron Man / JARVIS sound design engine.
    All sounds are numpy arrays generated once at init and cached.
    """

    def __init__(self, config: dict):
        self._enabled        = config.get("sound_effects_enabled", True)
        self._volume         = float(config.get("sound_volume", 0.3))
        self._ambient_vol    = float(config.get("ambient_hum_volume", 0.05))
        self._ambient_enabled = config.get("ambient_hum_enabled", True)
        self._stop_event     = threading.Event()
        self._ambient_thread: Optional[threading.Thread] = None
        self._lock           = threading.Lock()

        # Pre-generate all sounds
        self._sounds: dict[str, np.ndarray] = {}
        self._generate_all()
        log.info("Sound engine ready (%d sounds generated).", len(self._sounds))

    # ── Sound generation ──────────────────────────────────────────────────────

    def _generate_all(self):
        self._sounds["STARTUP"]        = self._gen_startup()
        self._sounds["WAKE"]           = self._gen_wake()
        self._sounds["PROCESSING"]     = self._gen_processing()
        self._sounds["RESPONSE_READY"] = self._gen_response_ready()
        self._sounds["SUCCESS"]        = self._gen_success()
        self._sounds["ERROR"]          = self._gen_error()
        self._sounds["SCREENSHOT"]     = self._gen_screenshot()

    def _adsr(self, length: int, attack: float, decay: float,
               sustain: float, release: float) -> np.ndarray:
        """Generate an ADSR envelope of `length` samples."""
        env = np.zeros(length)
        a   = int(attack   * length)
        d   = int(decay    * length)
        r   = int(release  * length)
        s   = length - a - d - r

        if a > 0:
            env[:a] = np.linspace(0, 1, a)
        if d > 0:
            env[a:a+d] = np.linspace(1, sustain, d)
        if s > 0:
            env[a+d:a+d+s] = sustain
        if r > 0:
            env[a+d+s:] = np.linspace(sustain, 0, r)
        return env

    def _tone(self, freq: float, duration: float, volume: float = 1.0,
               harmonics: Optional[list] = None) -> np.ndarray:
        t      = np.linspace(0, duration, int(_SR * duration), endpoint=False)
        wave   = np.sin(2 * np.pi * freq * t)
        if harmonics:
            for h_freq, h_amp in harmonics:
                wave += h_amp * np.sin(2 * np.pi * h_freq * t)
            wave /= max(1.0, 1 + sum(a for _, a in harmonics))
        return (wave * volume).astype(np.float32)

    def _silence(self, duration: float) -> np.ndarray:
        return np.zeros(int(_SR * duration), dtype=np.float32)

    # ── Individual sounds ─────────────────────────────────────────────────────

    def _gen_startup(self) -> np.ndarray:
        """Ascending tri-tone — suit powering up."""
        parts = []
        for i, freq in enumerate([220, 330, 440, 660, 880]):
            dur   = 0.12
            t     = np.linspace(0, dur, int(_SR * dur), endpoint=False)
            tone  = np.sin(2 * np.pi * freq * t) * 0.5
            env   = self._adsr(len(tone), 0.1, 0.1, 0.7, 0.3)
            parts.append((tone * env).astype(np.float32))
            parts.append(self._silence(0.04))
        result = np.concatenate(parts)
        return result * self._volume

    def _gen_wake(self) -> np.ndarray:
        """Subtle two-tone chime."""
        dur1  = 0.12
        dur2  = 0.18
        t1    = np.linspace(0, dur1, int(_SR * dur1), endpoint=False)
        t2    = np.linspace(0, dur2, int(_SR * dur2), endpoint=False)
        note1 = np.sin(2 * np.pi * 880 * t1) * self._adsr(len(t1), 0.05, 0.1, 0.6, 0.4)
        note2 = np.sin(2 * np.pi * 1320 * t2) * self._adsr(len(t2), 0.05, 0.1, 0.5, 0.5)
        gap   = self._silence(0.05)
        result = np.concatenate([note1.astype(np.float32), gap, note2.astype(np.float32)])
        return result * self._volume * 0.7

    def _gen_processing(self) -> np.ndarray:
        """1-second quiet electronic hum with slight LFO modulation."""
        dur    = 1.0
        t      = np.linspace(0, dur, int(_SR * dur), endpoint=False)
        lfo    = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
        wave   = (np.sin(2 * np.pi * 110 * t) * 0.4
                  + np.sin(2 * np.pi * 165 * t) * 0.2) * lfo
        env    = self._adsr(len(wave), 0.1, 0.0, 1.0, 0.15)
        return (wave * env * self._volume * 0.3).astype(np.float32)

    def _gen_response_ready(self) -> np.ndarray:
        """Soft click/blip before ATLAS speaks."""
        dur  = 0.06
        t    = np.linspace(0, dur, int(_SR * dur), endpoint=False)
        wave = np.sin(2 * np.pi * 1760 * t)
        env  = self._adsr(len(wave), 0.02, 0.3, 0.0, 0.5)
        return (wave * env * self._volume * 0.5).astype(np.float32)

    def _gen_success(self) -> np.ndarray:
        """Ascending two-note confirmation."""
        parts = []
        for freq, dur in [(660, 0.10), (990, 0.16)]:
            t   = np.linspace(0, dur, int(_SR * dur), endpoint=False)
            w   = np.sin(2 * np.pi * freq * t)
            env = self._adsr(len(w), 0.05, 0.1, 0.7, 0.4)
            parts.append((w * env).astype(np.float32))
            parts.append(self._silence(0.04))
        result = np.concatenate(parts)
        return result * self._volume * 0.6

    def _gen_error(self) -> np.ndarray:
        """Low descending tone."""
        parts = []
        for freq, dur in [(440, 0.12), (330, 0.18)]:
            t   = np.linspace(0, dur, int(_SR * dur), endpoint=False)
            w   = np.sin(2 * np.pi * freq * t)
            env = self._adsr(len(w), 0.05, 0.1, 0.6, 0.4)
            parts.append((w * env).astype(np.float32))
            parts.append(self._silence(0.03))
        result = np.concatenate(parts)
        return result * self._volume * 0.5

    def _gen_screenshot(self) -> np.ndarray:
        """Camera shutter — brief white-noise burst with click."""
        dur    = 0.08
        n      = int(_SR * dur)
        noise  = np.random.uniform(-0.3, 0.3, n)
        click_dur = 0.02
        t_c    = np.linspace(0, click_dur, int(_SR * click_dur), endpoint=False)
        click  = np.sin(2 * np.pi * 4400 * t_c) * 0.8
        env_n  = self._adsr(n, 0.02, 0.6, 0.0, 0.4)
        env_c  = self._adsr(len(click), 0.01, 0.5, 0.0, 0.5)
        result = np.concatenate([
            (click * env_c).astype(np.float32),
            (noise * env_n).astype(np.float32),
        ])
        return result * self._volume * 0.4

    def _gen_ambient_chunk(self, chunk_dur: float = 2.0) -> np.ndarray:
        """Generate one chunk of ambient electronic hum."""
        import random
        t    = np.linspace(0, chunk_dur, int(_SR * chunk_dur), endpoint=False)
        freq = random.uniform(48, 58)   # subtle variation
        wave = (np.sin(2 * np.pi * freq * t) * 0.5
                + np.sin(2 * np.pi * (freq * 2) * t) * 0.2
                + np.sin(2 * np.pi * (freq * 3) * t) * 0.08)
        return (wave * self._ambient_vol).astype(np.float32)

    # ── Playback ──────────────────────────────────────────────────────────────

    def play(self, sound_name: str):
        """Play a named sound non-blocking on a daemon thread."""
        if not self._enabled:
            return
        audio = self._sounds.get(sound_name)
        if audio is None:
            log.debug("Unknown sound: %s", sound_name)
            return
        threading.Thread(
            target=self._play_array, args=(audio,),
            daemon=True, name=f"atlas-sound-{sound_name.lower()}"
        ).start()

    def _play_array(self, audio: np.ndarray):
        try:
            import sounddevice as sd
            sd.play(audio, samplerate=_SR, blocking=True)
        except Exception as exc:
            log.debug("Sound playback error: %s", exc)

    # ── Ambient hum ───────────────────────────────────────────────────────────

    def start_ambient(self):
        if not self._ambient_enabled or self._ambient_thread:
            return
        self._stop_event.clear()
        self._ambient_thread = threading.Thread(
            target=self._ambient_loop, daemon=True, name="atlas-ambient-hum"
        )
        self._ambient_thread.start()
        log.info("Ambient hum started.")

    def stop_ambient(self):
        self._stop_event.set()
        self._ambient_thread = None
        log.info("Ambient hum stopped.")

    def _ambient_loop(self):
        try:
            import sounddevice as sd
        except ImportError:
            return

        while not self._stop_event.is_set():
            chunk = self._gen_ambient_chunk(chunk_dur=2.0)
            try:
                sd.play(chunk, samplerate=_SR, blocking=True)
            except Exception:
                break

    # ── Volume / enable controls ──────────────────────────────────────────────

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        log.info("Sound effects %s.", "enabled" if enabled else "disabled")

    def set_ambient_enabled(self, enabled: bool):
        self._ambient_enabled = enabled
        if enabled:
            self.start_ambient()
        else:
            self.stop_ambient()

    def set_volume(self, volume: float):
        self._volume = max(0.0, min(1.0, volume))

    def stop(self):
        self.stop_ambient()

    # ── Voice command handler ─────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas mute sounds", "atlas disable sounds",
                                     "mute sound effects")):
            self.set_enabled(False)
            return "Sound effects muted."

        if any(p in lower for p in ("atlas enable sounds", "atlas unmute sounds",
                                     "enable sound effects")):
            self.set_enabled(True)
            return "Sound effects enabled."

        if any(p in lower for p in ("atlas mute ambient", "atlas disable ambient hum",
                                     "mute the ambient")):
            self.set_ambient_enabled(False)
            return "Ambient hum disabled."

        if any(p in lower for p in ("atlas enable ambient", "atlas ambient hum on")):
            self.set_ambient_enabled(True)
            return "Ambient hum enabled."

        return None
