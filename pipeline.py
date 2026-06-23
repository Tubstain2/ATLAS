"""
ATLAS Voice Pipeline — Pipecat-inspired frame-based audio processing.

Adapts Pipecat's core insight: voice data flows as typed frames through
processors running simultaneously, not sequentially.

Pipeline stages (all run in overlapping fashion):
  Mic → NoiseFilter → VADProcessor → Whisper → IntentClassifier → AI → TTS → Speaker

Key innovations borrowed from Pipecat:
  • Frame types: AudioFrame, VADFrame, TranscriptFrame, AIResponseFrame, TTSFrame
  • Simultaneous processing: each stage starts as soon as its input frame arrives
  • Interruption handling: VAD fires during TTS → InterruptionEvent stops playback
  • Bot-speaking flag: tracked here, checked by VAD to detect user takeover
  • Noise cancellation: optional noisereduce pass on mic input

Wiring (from main.py, no voice.py changes needed):
    pipeline = ATLASVoicePipeline(config, speak_cb=vm.speak)
    pipeline.set_interrupt_callback(vm._tts.stop_speaking)
    pipeline.start()

Voice commands:
  "ATLAS stop"               → immediate interruption
  "ATLAS noise cancel on/off" → toggle noise reduction
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

# ── Frame types (borrowed from Pipecat's frames/frames.py) ───────────────────

@dataclass
class AudioFrame:
    """Raw audio chunk from microphone (20–30 ms of PCM float32 at 16 kHz)."""
    audio: np.ndarray
    sample_rate: int = 16000
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class VADFrame:
    """Voice activity decision for an audio chunk."""
    is_speech: bool
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class TranscriptFrame:
    """Text produced by Whisper STT."""
    text: str
    is_final: bool = True
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class IntentFrame:
    """Classified intent with optional screenshot context."""
    text: str
    intent: str = "general"
    needs_vision: bool = False
    screenshot_path: Optional[str] = None


@dataclass
class AIResponseFrame:
    """Text response from the AI brain (may be partial / streaming)."""
    text: str
    is_final: bool = True
    route_used: str = "smart"


@dataclass
class TTSAudioFrame:
    """Audio chunk synthesised by Piper TTS, ready for playback."""
    audio: np.ndarray
    sample_rate: int = 22050
    sentence_index: int = 0


@dataclass
class InterruptionFrame:
    """Signals that the current TTS output should stop immediately.
    Emitted when VAD detects user speech while bot is speaking.
    Pattern directly from Pipecat's pipecat/frames/frames.py."""
    reason: str = "user_interruption"
    timestamp: float = field(default_factory=time.monotonic)


# ── VAD state (mirrors Pipecat's VADState enum) ───────────────────────────────

class VADState(Enum):
    QUIET    = "quiet"
    STARTING = "starting"
    SPEAKING = "speaking"
    STOPPING = "stopping"


# ── Noise reduction (optional — graceful fallback) ────────────────────────────

_noisereduce_available = False
try:
    import noisereduce as _nr
    _noisereduce_available = True
    log.info("pipeline: noisereduce available — noise cancellation enabled.")
except ImportError:
    log.info("pipeline: noisereduce not installed — noise cancellation disabled. "
             "Run: pip install noisereduce")


def reduce_noise(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Apply spectral noise reduction. Returns original if noisereduce not installed."""
    if not _noisereduce_available:
        return audio
    try:
        return _nr.reduce_noise(y=audio, sr=sample_rate, prop_decrease=0.8,
                                stationary=False)
    except Exception as exc:
        log.debug("noise reduction failed: %s", exc)
        return audio


# ── VAD processor ─────────────────────────────────────────────────────────────

class VADProcessor:
    """
    Wraps ATLAS's existing WebRTCVAD and adds Pipecat-style state tracking.

    Events (callbacks):
        on_speech_started()      — user begins speaking
        on_speech_stopped()      — user stops speaking (triggers STT)
        on_speech_activity()     — user still speaking (every 200ms)

    Pipecat equivalents:
        VADUserStartedSpeakingFrame  → on_speech_started
        VADUserStoppedSpeakingFrame  → on_speech_stopped
        UserSpeakingFrame            → on_speech_activity
    """

    # Pipecat defaults: start_secs=0.2, stop_secs=0.2
    _START_FRAMES = 6    # ~180ms of speech → confirmed speaking (6 × 30ms)
    _STOP_FRAMES  = 10   # ~300ms of silence → confirmed stopped

    def __init__(self):
        self._state           = VADState.QUIET
        self._speech_count    = 0
        self._silence_count   = 0
        self._last_activity   = 0.0
        self._activity_period = 0.2  # seconds between on_speech_activity events

        self.on_speech_started:  Optional[Callable] = None
        self.on_speech_stopped:  Optional[Callable] = None
        self.on_speech_activity: Optional[Callable] = None

    def process(self, is_speech: Optional[bool]) -> VADState:
        """Feed one VAD decision; return updated state."""
        if is_speech is None:
            return self._state

        if is_speech:
            self._silence_count = 0
            self._speech_count += 1
        else:
            self._speech_count = 0
            self._silence_count += 1

        old_state = self._state

        if self._state == VADState.QUIET:
            if self._speech_count >= self._START_FRAMES:
                self._state = VADState.SPEAKING
                self._speech_count = 0
                if self.on_speech_started:
                    try:
                        self.on_speech_started()
                    except Exception as exc:
                        log.debug("on_speech_started error: %s", exc)

        elif self._state == VADState.SPEAKING:
            if not is_speech:
                if self._silence_count >= self._STOP_FRAMES:
                    self._state = VADState.QUIET
                    self._silence_count = 0
                    if self.on_speech_stopped:
                        try:
                            self.on_speech_stopped()
                        except Exception as exc:
                            log.debug("on_speech_stopped error: %s", exc)
            else:
                now = time.monotonic()
                if now - self._last_activity >= self._activity_period:
                    self._last_activity = now
                    if self.on_speech_activity:
                        try:
                            self.on_speech_activity()
                        except Exception as exc:
                            log.debug("on_speech_activity error: %s", exc)

        return self._state

    def reset(self):
        self._state        = VADState.QUIET
        self._speech_count = 0
        self._silence_count = 0


# ── Main pipeline orchestrator ────────────────────────────────────────────────

class ATLASVoicePipeline:
    """
    Pipecat-inspired frame pipeline for ATLAS voice processing.

    Manages:
      1. Bot-speaking state (set by TTS, read by VAD interrupt check)
      2. Interruption detection and signaling
      3. Noise cancellation pass on incoming audio
      4. Simultaneous stage processing via threading

    Usage (from main.py — no voice.py changes needed):
        pipeline = ATLASVoicePipeline(config)
        pipeline.set_interrupt_callback(lambda: vm_tts.stop_if_speaking())
        pipeline.set_speak_callback(vm.speak)
        pipeline.start()

        # Wire from voice module VAD callbacks:
        pipeline.notify_user_speech_detected()   # call when VAD fires
        pipeline.notify_tts_started()            # call when TTS begins
        pipeline.notify_tts_done()               # call when TTS finishes
    """

    def __init__(self, config: dict = None, speak_cb=None):
        self._config             = config or {}
        self._speak              = speak_cb
        self._lock               = threading.Lock()

        self._bot_speaking       = False      # true while TTS is active
        self._noise_cancel_on    = self._config.get("noise_cancellation_enabled", True)
        self._interrupt_enabled  = self._config.get("pipeline_interruption_enabled", True)

        # Interrupt callback — called to stop TTS immediately
        self._interrupt_cb: Optional[Callable] = None

        # VAD processor for tracking state
        self._vad = VADProcessor()
        self._vad.on_speech_started  = self._on_user_started
        self._vad.on_speech_stopped  = self._on_user_stopped
        self._vad.on_speech_activity = self._on_user_activity

        # Interruption bookkeeping
        self._last_interrupt_time = 0.0
        self._interrupt_cooldown  = 1.5    # seconds between interrupts

        self._started = False
        log.info("ATLASVoicePipeline: initialized (noise=%s, interrupt=%s).",
                 self._noise_cancel_on, self._interrupt_enabled)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._started = True
        log.info("ATLASVoicePipeline: started.")

    def stop(self) -> None:
        self._started = False

    # ── Callback wiring ───────────────────────────────────────────────────────

    def set_interrupt_callback(self, cb: Callable) -> None:
        """Register the function to call to stop TTS immediately."""
        self._interrupt_cb = cb

    def set_speak_callback(self, cb: Callable) -> None:
        self._speak = cb

    # ── External notifications (called from voice module / main.py) ───────────

    def notify_tts_started(self) -> None:
        with self._lock:
            self._bot_speaking = True

    def notify_tts_done(self) -> None:
        with self._lock:
            self._bot_speaking = False

    def notify_user_speech_detected(self) -> None:
        """Called from voice module when VAD fires — triggers interrupt if bot speaking."""
        self._on_user_started()

    def process_audio_frame(self, audio: np.ndarray,
                             vad_result: Optional[bool] = None) -> AudioFrame:
        """
        Apply noise reduction and update VAD state.
        Returns a processed AudioFrame.

        This is the simultaneous processing entry point — called from the
        voice module's recording loop for every 20ms chunk.
        """
        if self._noise_cancel_on and audio is not None and len(audio) > 0:
            audio = reduce_noise(audio)

        if vad_result is not None:
            self._vad.process(vad_result)

        return AudioFrame(audio=audio)

    # ── Interruption handling (Pipecat pattern) ───────────────────────────────

    def _on_user_started(self) -> None:
        """User started speaking — check if bot is speaking and interrupt."""
        with self._lock:
            bot_speaking    = self._bot_speaking
            now             = time.monotonic()
            cooldown_ok     = (now - self._last_interrupt_time) >= self._interrupt_cooldown

        if bot_speaking and self._interrupt_enabled and cooldown_ok:
            log.info("ATLASVoicePipeline: user interruption detected — stopping TTS.")
            with self._lock:
                self._last_interrupt_time = time.monotonic()
                self._bot_speaking = False

            if self._interrupt_cb:
                try:
                    self._interrupt_cb()
                except Exception as exc:
                    log.debug("interrupt callback error: %s", exc)

            if self._speak:
                # Brief acknowledgment — speak in background so it doesn't block
                threading.Thread(
                    target=self._speak,
                    args=("Go ahead, Boss.",),
                    daemon=True,
                ).start()

    def _on_user_stopped(self) -> None:
        """User finished speaking — pipeline ready for STT."""
        pass

    def _on_user_activity(self) -> None:
        """User still speaking — no action needed."""
        pass

    # ── Bot-speaking state ────────────────────────────────────────────────────

    @property
    def bot_speaking(self) -> bool:
        with self._lock:
            return self._bot_speaking

    # ── Noise cancellation toggle ─────────────────────────────────────────────

    def set_noise_cancel(self, enabled: bool) -> None:
        self._noise_cancel_on = enabled
        log.info("Noise cancellation: %s", "ON" if enabled else "OFF")

    # ── Voice commands ────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas noise cancel on", "atlas enable noise cancel",
                                     "atlas turn on noise cancel")):
            self.set_noise_cancel(True)
            return "Noise cancellation enabled, Boss."

        if any(p in lower for p in ("atlas noise cancel off", "atlas disable noise cancel",
                                     "atlas turn off noise cancel")):
            self.set_noise_cancel(False)
            return "Noise cancellation disabled, Boss."

        if any(p in lower for p in ("atlas stop", "atlas quiet", "atlas shut up")):
            with self._lock:
                self._bot_speaking = False
            if self._interrupt_cb:
                try:
                    self._interrupt_cb()
                except Exception:
                    pass
            return None   # silence: don't speak after stop command

        return None


# ── Convenience: echo-gate (prevents ATLAS hearing itself) ────────────────────

class EchoGate:
    """
    Suppress mic input for N seconds after TTS starts — prevents ATLAS
    from hearing its own voice and wake-wording itself.

    Pipecat solves this with proper audio routing; we solve it with timing.
    """

    def __init__(self, gate_duration_secs: float = 2.0):
        self._gate_until = 0.0
        self._duration   = gate_duration_secs

    def on_tts_started(self) -> None:
        self._gate_until = time.monotonic() + self._duration

    def on_tts_done(self) -> None:
        self._gate_until = 0.0

    def is_gated(self) -> bool:
        return time.monotonic() < self._gate_until
