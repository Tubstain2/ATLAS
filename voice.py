"""
ATLAS Voice Module (Step 2)

Pipeline (all audio processing runs off the main thread):

  Microphone
      │
      ├─► AmplitudeProcessor ──► OrbWidget (60 fps via Qt signal)
      │
      ├─► WakeWordEngine (sherpa-onnx keyword spotter / energy fallback)
      │       └── wake word detected
      │               ▼
      ├─► RecordingVAD  (energy-based silence detection)
      │       └── utterance complete
      │               ▼
      ├─► WhisperSTT   (local, no API)
      │       └── transcription text
      │               ▼
      ├─► response_callback  (stub here; replaced by core.py in Step 3)
      │       └── response text
      │               ▼
      └─► PiperTTS     (local, no API; pyttsx3 fallback)

Thread model:
  - VoiceWorker extends QThread; emits Qt signals for all UI updates
  - PiperTTS.speak() is called from a daemon thread so it never blocks
    the worker's recording loop
  - Whisper inference blocks the worker thread (acceptable — it's off-main)

Wake word:
  sherpa-onnx keyword spotter — fully offline, no API key required.
  Model (~20 MB) is downloaded automatically on first run to ~/.atlas/wake_word/
  Say "atlas" or "hey atlas" to trigger.
  Falls back to energy-threshold detection if sherpa-onnx is unavailable.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger(__name__)

# ── Audio constants ────────────────────────────────────────────────────────────
SAMPLE_RATE  = 16_000   # Hz — required by both sherpa-onnx and Whisper
CHANNELS     = 1        # mono
DTYPE        = "float32"

# ── Model download locations ───────────────────────────────────────────────────
_VOICES_DIR = Path.home() / ".atlas" / "voices"
_DEFAULT_VOICE = "en_US-amy-medium"

_PIPER_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
_VOICE_PATHS = {
    "en_GB-alan-medium":   "en/en_GB/alan/medium",
    "en_US-amy-medium":    "en/en_US/amy/medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium",
    "en_US-ryan-medium":   "en/en_US/ryan/medium",
    "en_US-kusal-medium":  "en/en_US/kusal/medium",
}
_AVAILABLE_VOICES = ["en_GB-alan-medium", "en_US-amy-medium", "en_US-lessac-medium", "en_US-ryan-medium"]

_WAKE_WORD_DIR  = Path.home() / ".atlas" / "wake_word"
_KWS_MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
_KWS_MODEL_URL  = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2"
)


# ── BPE helpers ────────────────────────────────────────────────────────────────

def _load_bpe_vocab(tokens_file: Path) -> set:
    """Return the set of BPE token strings from a SentencePiece tokens.txt."""
    vocab: set = set()
    with open(tokens_file, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if parts:
                vocab.add(parts[0])
    return vocab


def _bpe_tokenize(word: str, vocab: set) -> str:
    """
    Greedy longest-match BPE tokenization.
    Prepends ▁ (SentencePiece word-boundary) and upper-cases the word.
    Returns space-separated token string, or empty string if nothing matches.
    """
    text   = "▁" + word.upper()
    tokens: list = []
    i = 0
    while i < len(text):
        best = None
        for end in range(len(text), i, -1):
            if text[i:end] in vocab:
                best = text[i:end]
                i    = end
                break
        if best:
            tokens.append(best)
        else:
            i += 1
    return " ".join(tokens)


# ══════════════════════════════════════════════════════════════════════════════
# Amplitude
# ══════════════════════════════════════════════════════════════════════════════

def _rms_amplitude(chunk: np.ndarray) -> float:
    """Return 0.0–1.0 amplitude from a float32 PCM chunk."""
    rms = float(np.sqrt(np.mean(chunk ** 2)))
    # Map typical speech (rms ≈ 0.01–0.08) to roughly 0.1–1.0
    return min(1.0, rms / 0.07)


# ══════════════════════════════════════════════════════════════════════════════
# Wake-word engine
# ══════════════════════════════════════════════════════════════════════════════

class WakeWordEngine:
    """
    sherpa-onnx keyword-spotter wake word engine.

    Detects "atlas" and "hey atlas" — fully offline, no API key required.
    Model (~20 MB) is downloaded automatically on first run to
    ~/.atlas/wake_word/ and reused on subsequent launches.

    Falls back to energy-threshold triggering if sherpa-onnx is unavailable
    or the model download fails.
    """

    _ENERGY_THRESHOLD = 0.14
    _ENERGY_RUN       = 10

    def __init__(self, keyword: str = "atlas"):
        self._spotter      = None
        self._stream       = None
        self._frame_length = 512
        self._use_fallback = False
        self._energy_run   = 0

        try:
            import sherpa_onnx as _check  # noqa: F401
            self._init_sherpa(keyword)
        except ImportError:
            log.warning("sherpa-onnx not installed — using energy fallback. "
                        "Install with: pip install sherpa-onnx")
            self._use_fallback = True
        except Exception as exc:
            log.error("sherpa-onnx init failed (%s) — using energy fallback", exc)
            self._use_fallback = True

    # ── sherpa-onnx setup ─────────────────────────────────────────────────────

    def _init_sherpa(self, keyword: str):
        model_dir = self._ensure_model()
        wake_file, stop_file = self._write_keywords(keyword, model_dir)

        import sherpa_onnx

        encoder = model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"
        decoder = model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
        joiner  = model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"
        tokens  = model_dir / "tokens.txt"

        for f in (encoder, decoder, joiner, tokens):
            if not f.exists():
                raise FileNotFoundError(f"Model file not found: {f}")

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(tokens),
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            keywords_file=str(wake_file),
            num_threads=1,
            max_active_paths=4,
            keywords_score=1.5,
            keywords_threshold=0.25,
            num_trailing_blanks=1,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()

        # Separate stream for stop phrases — active only during recording
        stop_keywords = stop_file.read_text(encoding="utf-8")
        self._stop_stream = self._spotter.create_stream(keywords=stop_keywords)

        log.info("Wake word ready — say 'atlas' or 'hey atlas' to trigger, "
                 "'end' / 'done' / 'stop' to finish")

    def _ensure_model(self) -> Path:
        """Download and extract the model on first run."""
        model_dir = _WAKE_WORD_DIR / _KWS_MODEL_NAME
        if (model_dir / "tokens.txt").exists():
            return model_dir

        log.info("Downloading wake word model (~20 MB) …")
        _WAKE_WORD_DIR.mkdir(parents=True, exist_ok=True)

        import tarfile
        import urllib.request

        archive = _WAKE_WORD_DIR / f"{_KWS_MODEL_NAME}.tar.bz2"
        urllib.request.urlretrieve(_KWS_MODEL_URL, archive)

        with tarfile.open(archive, "r:bz2") as tf:
            tf.extractall(_WAKE_WORD_DIR)
        archive.unlink()

        log.info("Wake word model ready at %s", model_dir)
        return model_dir

    def _write_keywords(self, keyword: str, model_dir: Path):
        """
        Generate wake and stop keyword files using the model's BPE vocabulary.
        Returns (wake_file, stop_file).
        """
        vocab      = _load_bpe_vocab(model_dir / "tokens.txt")
        kw_tokens  = _bpe_tokenize(keyword, vocab)
        hey_tokens = _bpe_tokenize("hey", vocab)

        # Wake keywords
        wake_lines = []
        if kw_tokens:
            wake_lines.append(f"{kw_tokens} @wake")
        if kw_tokens and hey_tokens:
            wake_lines.append(f"{hey_tokens} {kw_tokens} @wake")

        wake_file = model_dir / "atlas_wake_keywords.txt"
        wake_file.write_text("\n".join(wake_lines) + "\n", encoding="utf-8")

        # Stop keywords — "end", "done", "stop", "end prompt"
        stop_lines = []
        for word in ("end", "done", "stop"):
            t = _bpe_tokenize(word, vocab)
            if t:
                stop_lines.append(f"{t} @stop")
        # Two-word phrase: "end prompt"
        end_t    = _bpe_tokenize("end", vocab)
        prompt_t = _bpe_tokenize("prompt", vocab)
        if end_t and prompt_t:
            stop_lines.append(f"{end_t} {prompt_t} @stop")

        stop_file = model_dir / "atlas_stop_keywords.txt"
        stop_file.write_text("\n".join(stop_lines) + "\n", encoding="utf-8")

        log.debug("Wake keywords: %s", wake_lines)
        log.debug("Stop keywords: %s", stop_lines)
        return wake_file, stop_file

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def frame_length(self) -> int:
        return self._frame_length

    def process(self, chunk_f32: np.ndarray) -> bool:
        """Return True when wake word detected (call during IDLE)."""
        if self._use_fallback:
            return self._energy_trigger(chunk_f32)

        self._stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=chunk_f32)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)

        keyword = self._spotter.get_result(self._stream)
        if keyword:
            log.info("Wake word detected: %r", keyword)
            self._spotter.reset_stream(self._stream)
            return True
        return False

    def check_stop(self, chunk_f32: np.ndarray) -> bool:
        """Return True when stop phrase detected (call during RECORDING)."""
        if self._use_fallback or self._stop_stream is None:
            return False

        self._stop_stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=chunk_f32)
        while self._spotter.is_ready(self._stop_stream):
            self._spotter.decode_stream(self._stop_stream)

        keyword = self._spotter.get_result(self._stop_stream)
        if keyword:
            log.info("Stop phrase detected: %r", keyword)
            self._spotter.reset_stream(self._stop_stream)
            return True
        return False

    def _energy_trigger(self, chunk: np.ndarray) -> bool:
        if _rms_amplitude(chunk) > self._ENERGY_THRESHOLD:
            self._energy_run += 1
        else:
            self._energy_run = 0
        if self._energy_run >= self._ENERGY_RUN:
            self._energy_run = 0
            return True
        return False

    def cleanup(self):
        self._spotter     = None
        self._stream      = None
        self._stop_stream = None


# ══════════════════════════════════════════════════════════════════════════════
# Speech-to-text (Whisper, local)
# ══════════════════════════════════════════════════════════════════════════════

class WhisperSTT:
    """
    Wraps openai-whisper.  Model is loaded lazily on first transcription call
    to keep startup fast.
    """

    def __init__(self, model_name: str = "base"):
        self._model_name = model_name
        self._model      = None
        self._lock       = threading.Lock()

    # Called once; subsequent calls return immediately
    def _ensure_loaded(self):
        if self._model is not None:
            return
        log.info("Loading Whisper '%s' model …", self._model_name)
        import ssl, whisper
        # macOS Python.org builds often lack system certs; patch for download only
        _orig = ssl._create_default_https_context
        ssl._create_default_https_context = ssl._create_unverified_context
        try:
            self._model = whisper.load_model(self._model_name)
        finally:
            ssl._create_default_https_context = _orig
        log.info("Whisper ready.")

    def transcribe(self, audio_f32: np.ndarray) -> str:
        """Transcribe float32 16 kHz mono audio; return stripped text."""
        with self._lock:
            self._ensure_loaded()
            result = self._model.transcribe(
                audio_f32,
                language="en",
                fp16=False,
                task="transcribe",
                condition_on_previous_text=False,
            )
        return result.get("text", "").strip()


# ══════════════════════════════════════════════════════════════════════════════
# Text-to-speech (Piper, local; pyttsx3 fallback)
# ══════════════════════════════════════════════════════════════════════════════

class PiperTTS:
    """
    Local TTS via piper-tts Python package.
    On first use, downloads the voice model to ~/.atlas/voices/ if absent.
    Falls back to pyttsx3 (system TTS) if piper is unavailable.
    """

    def __init__(self, voice: str = _DEFAULT_VOICE, speech_rate: float = 1.0,
                 voice_enabled: bool = True):
        self._voice         = voice
        self._piper         = None     # PiperVoice instance
        self._pyttsx3       = None
        self._backend       = None     # 'piper' | 'pyttsx3' | 'none'
        self._lock          = threading.Lock()
        self._speaking      = False
        self._speech_rate   = max(0.25, min(4.0, speech_rate))
        self._voice_enabled = voice_enabled
        self._voice_index   = (_AVAILABLE_VOICES.index(voice)
                               if voice in _AVAILABLE_VOICES else 0)

    # ── Initialization ────────────────────────────────────────────────────────

    def _ensure_ready(self):
        if self._backend is not None:
            return
        if self._try_piper():
            self._backend = "piper"
        elif self._try_pyttsx3():
            self._backend = "pyttsx3"
        else:
            log.error("No TTS backend available — ATLAS will be silent.")
            self._backend = "none"

    def _try_piper(self) -> bool:
        try:
            from piper.voice import PiperVoice
            model_path  = _VOICES_DIR / f"{self._voice}.onnx"
            config_path = _VOICES_DIR / f"{self._voice}.onnx.json"

            if not model_path.exists():
                self._download_voice(model_path, config_path)

            self._piper = PiperVoice.load(str(model_path), str(config_path))
            log.info("Piper TTS ready (%s).", self._voice)
            return True
        except Exception as exc:
            log.warning("Piper TTS unavailable: %s", exc)
            return False

    def _download_voice(self, model_path: Path, config_path: Path):
        import ssl, urllib.request
        _VOICES_DIR.mkdir(parents=True, exist_ok=True)

        voice_subpath = _VOICE_PATHS.get(self._voice, "en/en_US/amy/medium")
        base_url = f"{_PIPER_HF_BASE}/{voice_subpath}"

        ctx = ssl._create_unverified_context()
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

        for suffix, dest in [(".onnx", model_path), (".onnx.json", config_path)]:
            url = f"{base_url}/{self._voice}{suffix}"
            log.info("Downloading %s …", url)
            with opener.open(url) as resp, open(dest, "wb") as fh:
                fh.write(resp.read())

        log.info("Voice model saved to %s", _VOICES_DIR)

    def _try_pyttsx3(self) -> bool:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 170)
            self._pyttsx3 = engine
            log.info("pyttsx3 TTS ready (system voice).")
            return True
        except Exception as exc:
            log.warning("pyttsx3 unavailable: %s", exc)
            return False

    # ── Public speak API ──────────────────────────────────────────────────────

    def speak(self, text: str, amplitude_cb=None):
        """Blocking: synthesize and play audio, then return."""
        if not self._voice_enabled:
            return
        with self._lock:
            self._ensure_ready()
            self._speaking = True
            try:
                if self._backend == "piper":
                    self._speak_piper(text, amplitude_cb)
                elif self._backend == "pyttsx3":
                    self._speak_pyttsx3(text)
            finally:
                self._speaking = False
                if amplitude_cb:
                    amplitude_cb(0.0)

    def _speak_piper(self, text: str, amplitude_cb=None):
        import sounddevice as sd
        from piper.config import SynthesisConfig

        length_scale = 1.0 / max(0.25, self._speech_rate)
        syn_cfg = SynthesisConfig(length_scale=length_scale)
        chunks = list(self._piper.synthesize(text, syn_config=syn_cfg))
        if not chunks:
            return

        sr    = chunks[0].sample_rate
        audio = np.concatenate([c.audio_float_array for c in chunks])

        block_size = 2048
        pos = 0
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32") as stream:
            while pos < len(audio):
                block = audio[pos : pos + block_size]
                if amplitude_cb:
                    amp = min(1.0, float(np.sqrt(np.mean(block ** 2))) / 0.07)
                    amplitude_cb(amp)
                stream.write(block.reshape(-1, 1).astype(np.float32))
                pos += block_size

    def _speak_pyttsx3(self, text: str):
        self._pyttsx3.say(text)
        self._pyttsx3.runAndWait()

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def speech_rate(self) -> float:
        return self._speech_rate

    def set_speech_rate(self, rate: float) -> float:
        self._speech_rate = max(0.25, min(4.0, rate))
        log.info("Speech rate set to %.2f", self._speech_rate)
        return self._speech_rate

    def set_voice_enabled(self, enabled: bool):
        self._voice_enabled = enabled
        log.info("Voice output %s.", "enabled" if enabled else "disabled")

    def cycle_voice(self) -> str:
        self._voice_index = (self._voice_index + 1) % len(_AVAILABLE_VOICES)
        self._voice = _AVAILABLE_VOICES[self._voice_index]
        with self._lock:
            self._piper = None
            self._backend = None
        log.info("Voice cycled to: %s", self._voice)
        return self._voice


# ══════════════════════════════════════════════════════════════════════════════
# Voice Worker (QThread)
# ══════════════════════════════════════════════════════════════════════════════

class VoiceWorker(QThread):
    """
    Runs the full voice pipeline off the main thread.
    All UI updates go through Qt signals (thread-safe).
    """

    # ── Signals ───────────────────────────────────────────────────────────────
    amplitude_changed   = pyqtSignal(float)   # 0.0–1.0, ~60 fps
    wake_word_detected  = pyqtSignal()         # → switch orb to 'listening'
    transcription_ready = pyqtSignal(str)      # user utterance text
    response_ready      = pyqtSignal(str)      # ATLAS response text
    speaking_started    = pyqtSignal()         # → orb 'responding'
    speaking_done       = pyqtSignal()         # → orb 'idle'
    status_message      = pyqtSignal(str)      # info / warning for HUD
    error_occurred      = pyqtSignal(str)      # non-fatal error string

    # ── VAD tuning ────────────────────────────────────────────────────────────
    _SILENCE_THRESHOLD = 0.05     # fallback — overridden by adaptive calibration
    _SILENCE_FRAMES    = 30       # ≈ 1 s of silence → stop recording
    _MAX_RECORD_SECS   = 12.0
    _MIN_RECORD_SECS   = 0.3

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        vc = config.get("voice", {})
        self._whisper_model  = vc.get("whisper_model", "base")
        self._tts_voice      = vc.get("piper_voice", vc.get("tts_model", _DEFAULT_VOICE))
        self._sr             = vc.get("sample_rate", SAMPLE_RATE)

        self._wake_word      = vc.get("wake_word", "atlas")
        self._muted          = False
        self._stop_event     = threading.Event()
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=300)
        self._response_cb: Optional[Callable[[str], str]] = None

        self._wake  = None
        self._stt   = WhisperSTT(self._whisper_model)
        self.tts    = PiperTTS(
            voice=self._tts_voice,
            speech_rate=vc.get("speech_rate", 1.0),
            voice_enabled=vc.get("voice_enabled", True),
        )

    # ── Adaptive noise calibration ────────────────────────────────────────────

    def _calibrate_silence_threshold(self) -> float:
        """
        Sample ~1.5 s of ambient audio and return a silence threshold set
        just above the room's noise floor.  Adapts to any mic or environment.
        """
        amps     = []
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                chunk = self._audio_q.get(timeout=0.15)
                amps.append(_rms_amplitude(chunk))
            except queue.Empty:
                pass

        if not amps:
            return self._SILENCE_THRESHOLD

        # 75th percentile avoids being skewed by brief spikes during calibration
        noise     = float(np.percentile(amps, 75))
        threshold = max(0.04, min(0.25, noise * 2.8))
        log.info("Noise floor: %.3f → silence threshold: %.3f", noise, threshold)
        return threshold

    # ── Thread entry point ────────────────────────────────────────────────────

    def run(self):
        try:
            import sounddevice as sd
        except ImportError:
            self.error_occurred.emit("sounddevice not installed — voice module disabled")
            return

        self._wake = WakeWordEngine(self._wake_word)
        frame_len  = self._wake.frame_length

        def _audio_cb(indata, _frames, _time, status):
            if status:
                log.debug("sounddevice status: %s", status)
            if not self._muted:
                try:
                    self._audio_q.put_nowait(indata[:, 0].copy())
                except queue.Full:
                    pass   # drop frame rather than block the audio thread

        def _make_stream():
            return sd.InputStream(
                samplerate=self._sr,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=frame_len,
                callback=_audio_cb,
            )

        # Measure ambient noise before the main loop
        self.status_message.emit("CALIBRATING MIC...")
        with _make_stream():
            silence_threshold = self._calibrate_silence_threshold()

        log.info("Voice pipeline running (frame=%d, sr=%d)", frame_len, self._sr)
        self.status_message.emit("VOICE ONLINE")

        rec_buf      = []
        silence_run  = 0
        rec_start    = 0.0
        recording    = False

        with _make_stream():
            while not self._stop_event.is_set():
                try:
                    chunk = self._audio_q.get(timeout=0.15)
                except queue.Empty:
                    continue

                # Always feed amplitude to the UI
                amp = _rms_amplitude(chunk)
                self.amplitude_changed.emit(amp)

                if self._muted:
                    continue

                if not recording:
                    # ── IDLE: check for wake word ──────────────────────────
                    if self._wake.process(chunk):
                        log.info("Wake word detected → recording")
                        self.wake_word_detected.emit()
                        recording   = True
                        rec_buf     = []
                        silence_run = 0
                        rec_start   = time.monotonic()

                else:
                    # ── RECORDING: accumulate audio until stop/silence/timeout
                    rec_buf.append(chunk)
                    elapsed = time.monotonic() - rec_start

                    stop_triggered = self._wake.check_stop(chunk)

                    if amp < silence_threshold:
                        silence_run += 1
                    else:
                        silence_run = 0

                    done = (
                        stop_triggered
                        or (silence_run >= self._SILENCE_FRAMES
                            and elapsed >= self._MIN_RECORD_SECS)
                        or elapsed >= self._MAX_RECORD_SECS
                    )

                    if done:
                        recording = False
                        if elapsed < 0.25:
                            # Too short — noise burst; reset orb to idle
                            log.debug("Utterance too short (%.2fs), ignoring", elapsed)
                            self.speaking_done.emit()
                        else:
                            audio = np.concatenate(rec_buf)
                            self._handle_utterance(audio)

        self._wake.cleanup()
        log.info("Voice pipeline stopped.")

    # ── Utterance handling ────────────────────────────────────────────────────

    def _handle_utterance(self, audio: np.ndarray):
        # 1. STT
        try:
            text = self._stt.transcribe(audio)
        except Exception as exc:
            log.error("STT error: %s", exc)
            self.error_occurred.emit(f"STT error: {exc}")
            self.speaking_done.emit()   # reset orb to idle
            return

        if not text:
            log.debug("Empty transcription — ignoring")
            self.speaking_done.emit()   # reset orb to idle
            return

        log.info("Transcribed: %r", text)
        self.transcription_ready.emit(text)

        # 2. Voice command interception (before Groq)
        cmd_response = self._check_voice_command(text)
        if cmd_response is not None:
            if cmd_response:
                self.response_ready.emit(cmd_response)
                self._tts_play(cmd_response)
            return

        # 3. Response (callback injected by VoiceModule / core)
        response = self._get_response(text)
        if not response:
            return

        self.response_ready.emit(response)
        self._tts_play(response)

    def _tts_play(self, text: str):
        """Launch TTS in a daemon thread; streams amplitude back to the orb."""
        def _play():
            self.speaking_started.emit()
            try:
                self.tts.speak(text, amplitude_cb=self.amplitude_changed.emit)
            except Exception as exc:
                log.error("TTS error: %s", exc)
                self.error_occurred.emit(f"TTS error: {exc}")
            finally:
                self.speaking_done.emit()

        threading.Thread(target=_play, daemon=True, name="atlas-tts").start()

    def _check_voice_command(self, text: str) -> Optional[str]:
        """Return a response string for built-in voice commands, or None."""
        lower = text.lower().strip()

        if "atlas change voice" in lower:
            new_voice = self.tts.cycle_voice()
            return f"Switching to {new_voice.replace('-', ' ')}."

        if "atlas speak faster" in lower:
            self.tts.set_speech_rate(self.tts.speech_rate + 0.25)
            return "Speaking faster now."

        if "atlas speak slower" in lower:
            self.tts.set_speech_rate(self.tts.speech_rate - 0.25)
            return "Speaking slower now."

        if "atlas unmute" in lower:
            self.tts.set_voice_enabled(True)
            return "Voice output re-enabled."

        if "atlas mute" in lower:
            self.tts.set_voice_enabled(False)
            return "Voice muted."  # shown in transcript; not spoken (muted now)

        return None

    def _get_response(self, text: str) -> str:
        if self._response_cb:
            try:
                return self._response_cb(text) or ""
            except Exception as exc:
                log.error("Response callback error: %s", exc)
                self.error_occurred.emit(f"Response error: {exc}")
        # Step 2 stub — replaced by core.py in Step 3
        return f"You said: {text}"

    # ── Public control ────────────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()
        self.wait(4_000)

    def set_muted(self, muted: bool):
        self._muted = muted

    def set_response_callback(self, cb: Callable[[str], str]):
        self._response_cb = cb


# ══════════════════════════════════════════════════════════════════════════════
# VoiceModule — public API used by main.py and core.py
# ══════════════════════════════════════════════════════════════════════════════

class VoiceModule:
    """
    Manages VoiceWorker lifecycle and wires signals to the UI window.

    Usage (in main.py):
        vm = VoiceModule(config, window)
        vm.start()                      # non-blocking
        vm.set_response_callback(fn)    # injected by core in Step 3
        vm.stop()                       # on shutdown

    Called by core.py (Step 3):
        vm.speak(text)                  # direct TTS without STT pipeline
    """

    def __init__(self, config: dict, window=None):
        self._cfg         = config
        self._window      = window
        self._worker: Optional[VoiceWorker] = None
        self._response_cb: Optional[Callable[[str], str]] = None  # stored until worker exists

    def start(self):
        if self._worker and self._worker.isRunning():
            return

        self._worker = VoiceWorker(self._cfg)
        self._wire_signals()
        if self._response_cb:                                      # apply stored callback
            self._worker.set_response_callback(self._response_cb)
        self._worker.start()
        log.info("VoiceModule started.")

    def stop(self):
        if self._worker:
            self._worker.stop()
            self._worker = None
        log.info("VoiceModule stopped.")

    def set_muted(self, muted: bool):
        if self._worker:
            self._worker.set_muted(muted)

    def set_response_callback(self, cb: Callable[[str], str]):
        """Injected by core.py (Step 3) to replace the echo stub."""
        self._response_cb = cb                                     # always store
        if self._worker:
            self._worker.set_response_callback(cb)

    def speak(self, text: str):
        """Direct TTS call from core agent (bypasses STT pipeline)."""
        if not self._worker:
            return

        def _play():
            self._worker.speaking_started.emit()
            try:
                self._worker.tts.speak(
                    text, amplitude_cb=self._worker.amplitude_changed.emit
                )
            except Exception as exc:
                log.error("TTS direct error: %s", exc)
            finally:
                self._worker.speaking_done.emit()

        threading.Thread(target=_play, daemon=True, name="atlas-tts-direct").start()

    def set_voice_enabled(self, enabled: bool):
        if self._worker:
            self._worker.tts.set_voice_enabled(enabled)

    def set_speech_rate(self, rate: float) -> float:
        if self._worker:
            return self._worker.tts.set_speech_rate(rate)
        return 1.0

    def cycle_voice(self) -> str:
        if self._worker:
            return self._worker.tts.cycle_voice()
        return _DEFAULT_VOICE

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _wire_signals(self):
        if not self._window or not self._worker:
            return

        w = self._window
        wr = self._worker

        wr.amplitude_changed.connect(w.set_amplitude)

        wr.wake_word_detected.connect(lambda: w.set_state("listening"))

        wr.transcription_ready.connect(lambda t: (
            w.add_entry(t, is_atlas=False),
            w.set_state("thinking"),
        ))

        wr.response_ready.connect(lambda r: w.show_response(r))

        wr.speaking_started.connect(lambda: w.set_state("responding"))
        wr.speaking_done.connect(lambda: (
            w.set_state("idle"),
            w.set_amplitude(0.0),
        ))

        wr.status_message.connect(
            lambda msg: log.info("[VOICE STATUS] %s", msg)
        )
        wr.error_occurred.connect(
            lambda err: log.error("[VOICE ERROR] %s", err)
        )

        # Light up the VOICE badge in the HUD
        wr.started.connect(lambda: w.set_module_active("VOICE", True))
