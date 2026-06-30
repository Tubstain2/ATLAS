"""
ATLAS Voice Module (Step 2 + JARVIS upgrade)

Pipeline (all audio processing runs off the main thread):

  Microphone
      │
      ├─► AmplitudeProcessor ──► OrbWidget (60 fps via Qt signal)
      │
      ├─► WakeWordEngine (sherpa-onnx keyword spotter / energy fallback)
      │       └── wake word detected
      │               ▼
      ├─► WebRTCVAD  (instant speech-end detection <50 ms)
      │       └── utterance complete
      │               ▼
      ├─► WhisperSTT   (local, no API; auto-selects model per chip)
      │       └── transcription text
      │               ▼
      ├─► SpeechFormatter  (remove markdown, expand abbrevs, trim to voice)
      │       └── clean text
      │               ▼
      ├─► ResponseCache  (50-command cache, 1-hour TTL)
      │       └── hit → instant reply / miss → response_callback
      │               ▼
      ├─► response_callback  (injected by brain.py)
      │       └── response text / sentence stream
      │               ▼
      └─► PiperTTS (JARVIS en_GB-jarvis-high / pyttsx3 fallback)
              └─► sentence-level streaming: speak s1 while synth s2

Thread model:
  - VoiceWorker extends QThread; emits Qt signals for all UI updates
  - PiperTTS.speak() is called from a daemon thread so it never blocks
    the worker's recording loop
  - Whisper inference blocks the worker thread (acceptable — it's off-main)
  - WebRTC VAD runs synchronously in the recording hot-loop (<1 ms per frame)

Wake word:
  sherpa-onnx keyword spotter — fully offline, no API key required.
  Model (~20 MB) is downloaded automatically on first run to ~/.atlas/wake_word/
  Say "atlas" or "hey atlas" to trigger.
  Falls back to energy-threshold detection if sherpa-onnx is unavailable.

JARVIS voice:
  en_GB-jarvis-high Piper model (~65 MB) downloaded on first TTS call to
  ~/.atlas/voices/.  Falls back to en_US-ryan-high, then pyttsx3.
"""

from __future__ import annotations

import collections
import hashlib
import io
import json
import logging
import os
import platform
import queue
import re
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger(__name__)

# ── Audio constants ────────────────────────────────────────────────────────────
SAMPLE_RATE  = 16_000   # Hz — required by both sherpa-onnx and Whisper
CHANNELS     = 1        # mono
DTYPE        = "float32"

# ── Model download locations ───────────────────────────────────────────────────
_VOICES_DIR   = Path.home() / ".atlas" / "voices"
_LOCAL_VOICES  = Path(__file__).resolve().parent / "voices"   # project-local fallback

_DEFAULT_VOICE  = "en_GB-jarvis-high"   # authentic JARVIS voice
_FALLBACK_VOICE = "en_US-ryan-high"     # fallback if JARVIS model fails

_PIPER_HF_BASE  = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
_JARVIS_HF_BASE = "https://huggingface.co/jgkawell/jarvis/resolve/main"

_VOICE_PATHS = {
    "en_GB-alan-medium":   "en/en_GB/alan/medium",
    "en_US-amy-medium":    "en/en_US/amy/medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium",
    "en_US-ryan-medium":   "en/en_US/ryan/medium",
    "en_US-kusal-medium":  "en/en_US/kusal/medium",
    "en_US-ryan-high":     "en/en_US/ryan/high",
}

_AVAILABLE_VOICES = [
    "en_GB-jarvis-high",    # JARVIS — primary
    "en_US-ryan-high",      # high-quality fallback
    "en_GB-alan-medium",
    "en_US-amy-medium",
    "en_US-lessac-medium",
    "en_US-ryan-medium",
]

# ── JARVIS voice quality settings ─────────────────────────────────────────────
_JARVIS_LENGTH_SCALE = 0.95   # slightly slower for clarity
_JARVIS_NOISE_SCALE  = 0.333  # reduces robotic artifacts
_JARVIS_NOISE_W      = 0.333  # smoother prosody

# ── Wake-word model ────────────────────────────────────────────────────────────
_WAKE_WORD_DIR  = Path.home() / ".atlas" / "wake_word"
_KWS_MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
_KWS_MODEL_URL  = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2"
)

# ── Response cache path ────────────────────────────────────────────────────────
_RESPONSE_CACHE_PATH = Path(__file__).resolve().parent / "memory" / "response_cache.json"


# ══════════════════════════════════════════════════════════════════════════════
# BPE helpers
# ══════════════════════════════════════════════════════════════════════════════

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
    return min(1.0, rms / 0.07)


# ══════════════════════════════════════════════════════════════════════════════
# Speech Formatter
# ══════════════════════════════════════════════════════════════════════════════

class SpeechFormatter:
    """
    Prepare text for JARVIS voice output.
    - Strips markdown (bold, italic, headers, code fences, bullets, links)
    - Expands common abbreviations to spoken form (API → A.P.I.)
    - Formats numbers (1200 → twelve hundred)
    - Converts camelCase to spaced words
    - Trims to first N sentences for voice (full detail goes to feed panel)
    """

    _ABBREVS = {
        "API":   "A.P.I.",
        "URL":   "U.R.L.",
        "UI":    "U.I.",
        "UX":    "U.X.",
        "AI":    "A.I.",
        "ML":    "M.L.",
        "CPU":   "C.P.U.",
        "GPU":   "G.P.U.",
        "RAM":   "R.A.M.",
        "SDK":   "S.D.K.",
        "CLI":   "C.L.I.",
        "JSON":  "J.S.O.N.",
        "HTML":  "H.T.M.L.",
        "CSS":   "C.S.S.",
        "SQL":   "S.Q.L.",
        "HTTP":  "H.T.T.P.",
        "HTTPS": "H.T.T.P.S.",
        "IDE":   "I.D.E.",
        "OS":    "O.S.",
        "VM":    "V.M.",
        "SSH":   "S.S.H.",
        "VPN":   "V.P.N.",
        "PR":    "P.R.",
        "CI":    "C.I.",
        "CD":    "C.D.",
    }

    _MD_SUBS = [
        (re.compile(r'\*\*(.+?)\*\*', re.S), r'\1'),            # **bold**
        (re.compile(r'\*(.+?)\*',     re.S), r'\1'),            # *italic*
        (re.compile(r'_{1,2}(.+?)_{1,2}', re.S), r'\1'),        # _italic_
        (re.compile(r'#+\s*'),               ''),                # ## headers
        (re.compile(r'`{3}[^\n]*\n.*?`{3}', re.S), ''),        # ```fenced```
        (re.compile(r'`([^`]+)`'),           r'\1'),            # `inline code`
        (re.compile(r'^\s*[-*+]\s+', re.M), ''),               # bullet list
        (re.compile(r'^\s*\d+\.\s+', re.M), ''),               # numbered list
        (re.compile(r'\[([^\]]+)\]\([^\)]+\)'), r'\1'),        # [text](url)
        (re.compile(r'!\[([^\]]*)\]\([^\)]+\)'), r'\1'),       # ![alt](img)
        (re.compile(r'---+'),                ', '),             # --- hr
        (re.compile(r'\n{3,}'),              '\n\n'),           # excess blank lines
    ]

    _ONES = [
        '', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
        'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen',
        'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen',
    ]
    _TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty',
             'sixty', 'seventy', 'eighty', 'ninety']

    def _num_words(self, n: int) -> str:
        if n < 0:
            return f'minus {self._num_words(-n)}'
        if n < 20:
            return self._ONES[n]
        if n < 100:
            t, o = divmod(n, 10)
            return self._TENS[t] + (f' {self._ONES[o]}' if o else '')
        # "twelve hundred", "fifteen hundred", etc. — for 1100–1999 divisible by 100
        if 1100 <= n <= 1999 and n % 100 == 0:
            return f'{self._ONES[n // 100]} hundred'
        if n % 1000 == 0 and 1000 <= n < 1_000_000:
            return f'{self._num_words(n // 1000)} thousand'
        if n % 1_000_000 == 0 and n < 1_000_000_000:
            return f'{self._num_words(n // 1_000_000)} million'
        return str(n)

    def _replace_numbers(self, text: str) -> str:
        def _sub(m: re.Match) -> str:
            try:
                return self._num_words(int(m.group(0)))
            except (ValueError, OverflowError):
                return m.group(0)
        return re.sub(r'\b\d{3,}\b', _sub, text)

    def format(self, text: str) -> str:
        """Full format pass: strip markdown, expand abbreviations, normalise numbers."""
        for pattern, repl in self._MD_SUBS:
            text = pattern.sub(repl, text)
        for abbrev, expansion in self._ABBREVS.items():
            text = re.sub(r'\b' + re.escape(abbrev) + r'\b', expansion, text)
        text = self._replace_numbers(text)
        # camelCase → "camel case"
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
        return text.strip()

    def to_voice(self, text: str, max_sentences: int = 3) -> str:
        """Format and trim to first N sentences — voice gets the summary only."""
        formatted = self.format(text)
        sentences = re.split(r'(?<=[.!?])\s+', formatted)
        trimmed = ' '.join(sentences[:max_sentences])
        return trimmed

    @staticmethod
    def split_sentences(text: str) -> list[str]:
        """Split text into individual sentences for streaming TTS."""
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        return [s.strip() for s in sentences if s.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# Voice Emotion Detector
# ══════════════════════════════════════════════════════════════════════════════

class VoiceEmotionDetector:
    """
    Detect urgency / frustration from recent voice amplitude patterns.
    - Urgent: high mean amplitude → ATLAS speaks faster, skips wit
    - Relaxed: low amplitude, stable → more conversational
    - Frustrated: high variance + elevated amplitude → extra concise
    """

    def __init__(self, window: int = 30):
        self._amps: collections.deque = collections.deque(maxlen=window)

    def update(self, amp: float):
        self._amps.append(amp)

    def _stats(self):
        if len(self._amps) < 5:
            return 0.0, 0.0
        arr  = list(self._amps)
        mean = sum(arr) / len(arr)
        var  = sum((a - mean) ** 2 for a in arr) / len(arr)
        return mean, var

    @property
    def urgency(self) -> float:
        mean, _ = self._stats()
        return min(1.0, mean / 0.25)

    @property
    def is_urgent(self) -> bool:
        return self.urgency > 0.65

    @property
    def is_frustrated(self) -> bool:
        mean, var = self._stats()
        return var > 0.015 and mean > 0.18

    @property
    def is_relaxed(self) -> bool:
        return self.urgency < 0.25

    def tts_speed_multiplier(self) -> float:
        """1.0 = normal; >1.0 = faster (urgent); <1.0 = slower (relaxed)."""
        u = self.urgency
        if u > 0.65:
            return 1.15
        if u < 0.25:
            return 0.92
        return 1.0

    def allow_wit(self) -> bool:
        return not self.is_urgent and not self.is_frustrated


# ══════════════════════════════════════════════════════════════════════════════
# WebRTC VAD
# ══════════════════════════════════════════════════════════════════════════════

class WebRTCVAD:
    """
    WebRTC voice activity detector.
    Detects speech-end in <50 ms — far faster than energy-based silence counting.
    Falls back gracefully if webrtcvad is not installed.

    Frame size: 30 ms = 480 samples @ 16 kHz.
    aggressiveness: 0 (least) – 3 (most aggressive noise rejection)
    """

    _FRAME_SAMPLES = 480   # 30 ms at 16 kHz — must be exactly 480, 320, or 160

    def __init__(self, aggressiveness: int = 2):
        self._vad     = None
        self._ready   = False
        self._buf_i16 = np.array([], dtype=np.int16)

        try:
            import webrtcvad as _wvad
            self._vad   = _wvad.Vad(aggressiveness)
            self._ready = True
            log.info("WebRTC VAD ready (aggressiveness=%d)", aggressiveness)
        except ImportError:
            log.warning("webrtcvad not installed — energy VAD active. "
                        "Install with: pip install webrtcvad")

    @property
    def available(self) -> bool:
        return self._ready

    def is_speech(self, chunk_f32: np.ndarray) -> Optional[bool]:
        """
        Returns True/False when a complete 30 ms frame is ready, or None if
        the buffer hasn't accumulated a full frame yet.
        """
        if not self._ready:
            return None

        i16 = (np.clip(chunk_f32, -1.0, 1.0) * 32767).astype(np.int16)
        self._buf_i16 = np.concatenate([self._buf_i16, i16])

        results = []
        while len(self._buf_i16) >= self._FRAME_SAMPLES:
            frame = self._buf_i16[:self._FRAME_SAMPLES]
            self._buf_i16 = self._buf_i16[self._FRAME_SAMPLES:]
            try:
                results.append(self._vad.is_speech(frame.tobytes(), SAMPLE_RATE))
            except Exception:
                pass

        if not results:
            return None
        return any(results)

    def reset(self):
        self._buf_i16 = np.array([], dtype=np.int16)


# ══════════════════════════════════════════════════════════════════════════════
# Response Cache
# ══════════════════════════════════════════════════════════════════════════════

class ResponseCache:
    """
    Persistent LRU cache of the 50 most common AI responses.
    Stored in memory/response_cache.json.
    TTL: 1 hour (time-sensitive commands auto-expire).
    """

    def __init__(self, maxsize: int = 50, ttl: int = 3600,
                 path: Path = _RESPONSE_CACHE_PATH):
        self._maxsize = maxsize
        self._ttl     = ttl
        self._path    = path
        self._lock    = threading.Lock()
        self._cache: collections.OrderedDict = collections.OrderedDict()
        self._times: dict = {}
        self._load()

    def _key(self, text: str) -> str:
        return text.lower().strip()

    def get(self, text: str) -> Optional[str]:
        key = self._key(text)
        with self._lock:
            if key not in self._cache:
                return None
            if time.time() - self._times.get(key, 0) > self._ttl:
                del self._cache[key]
                self._times.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def put(self, text: str, response: str):
        key = self._key(text)
        with self._lock:
            self._cache[key] = response
            self._times[key] = time.time()
            self._cache.move_to_end(key)
            while len(self._cache) > self._maxsize:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
                self._times.pop(oldest, None)
        self._save()

    def _load(self):
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._cache = collections.OrderedDict(data.get("cache", {}))
                self._times = data.get("times", {})
                self._evict_expired()
        except Exception as exc:
            log.debug("Response cache load failed: %s", exc)

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"cache": dict(self._cache), "times": self._times}
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            log.debug("Response cache save failed: %s", exc)

    def _evict_expired(self):
        now = time.time()
        expired = [k for k, t in self._times.items() if now - t > self._ttl]
        for k in expired:
            self._cache.pop(k, None)
            self._times.pop(k, None)


# ══════════════════════════════════════════════════════════════════════════════
# Phrase Cache (TTS audio)
# ══════════════════════════════════════════════════════════════════════════════

class _PhraseCache:
    """
    LRU in-memory cache of the last 20 synthesised audio arrays.
    Enables instant replay of repeated phrases without re-running Piper.
    """

    def __init__(self, maxsize: int = 20):
        self._cache: collections.OrderedDict = collections.OrderedDict()
        self._maxsize = maxsize

    def _key(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def get(self, text: str) -> Optional[tuple]:
        """Return (sample_rate, audio_f32) or None."""
        key = self._key(text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, text: str, sample_rate: int, audio: np.ndarray):
        key = self._key(text)
        self._cache[key] = (sample_rate, audio)
        self._cache.move_to_end(key)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)


# ══════════════════════════════════════════════════════════════════════════════
# Whisper model auto-selection
# ══════════════════════════════════════════════════════════════════════════════

def _detect_whisper_model() -> str:
    """
    Select the best Whisper model for the current hardware.
    M1: base.en  |  M2/M3/M4: small.en  |  Intel Mac / other: tiny.en
    """
    if platform.system() != "Darwin":
        return "base"

    if platform.machine() != "arm64":
        log.info("Intel Mac detected — using Whisper tiny.en")
        return "tiny.en"

    try:
        brand = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        brand = ""

    if any(chip in brand for chip in ("M2", "M3", "M4")):
        log.info("Apple %s detected — using Whisper small.en", brand.split()[-1] if brand else "Silicon")
        return "small.en"

    log.info("Apple M1 detected — using Whisper base.en")
    return "base.en"


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

        stop_keywords = stop_file.read_text(encoding="utf-8")
        self._stop_stream = self._spotter.create_stream(keywords=stop_keywords)

        log.info("Wake word ready — say 'atlas' or 'hey atlas' to trigger, "
                 "'end' / 'done' / 'stop' to finish")

    def _ensure_model(self) -> Path:
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
        vocab      = _load_bpe_vocab(model_dir / "tokens.txt")
        kw_tokens  = _bpe_tokenize(keyword, vocab)
        hey_tokens = _bpe_tokenize("hey", vocab)

        wake_lines = []
        if kw_tokens:
            wake_lines.append(f"{kw_tokens} @wake")
        if kw_tokens and hey_tokens:
            wake_lines.append(f"{hey_tokens} {kw_tokens} @wake")

        wake_file = model_dir / "atlas_wake_keywords.txt"
        wake_file.write_text("\n".join(wake_lines) + "\n", encoding="utf-8")

        stop_lines = []
        for word in ("end", "done", "stop"):
            t = _bpe_tokenize(word, vocab)
            if t:
                stop_lines.append(f"{t} @stop")
        end_t    = _bpe_tokenize("end", vocab)
        prompt_t = _bpe_tokenize("prompt", vocab)
        if end_t and prompt_t:
            stop_lines.append(f"{end_t} {prompt_t} @stop")

        stop_file = model_dir / "atlas_stop_keywords.txt"
        stop_file.write_text("\n".join(stop_lines) + "\n", encoding="utf-8")

        log.debug("Wake keywords: %s", wake_lines)
        log.debug("Stop keywords: %s", stop_lines)
        return wake_file, stop_file

    @property
    def frame_length(self) -> int:
        return self._frame_length

    def process(self, chunk_f32: np.ndarray) -> bool:
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
    Wraps openai-whisper.  Auto-selects model per Apple chip on startup.
    Model is loaded lazily on first transcription call to keep startup fast.
    """

    def __init__(self, model_name: str = "auto"):
        if model_name in ("auto", ""):
            model_name = _detect_whisper_model()
        self._model_name = model_name
        self._model      = None
        self._lock       = threading.Lock()

    def _ensure_loaded(self):
        if self._model is not None:
            return
        log.info("Loading Whisper '%s' model …", self._model_name)
        import ssl, whisper
        _orig = ssl._create_default_https_context
        ssl._create_default_https_context = ssl._create_unverified_context
        try:
            self._model = whisper.load_model(self._model_name)
        finally:
            ssl._create_default_https_context = _orig
        log.info("Whisper ready (%s).", self._model_name)

    def transcribe(self, audio_f32: np.ndarray) -> str:
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
# Text-to-speech (Piper JARVIS voice; pyttsx3 fallback)
# ══════════════════════════════════════════════════════════════════════════════

class PiperTTS:
    """
    Local TTS via piper-tts Python package.

    Primary voice: en_GB-jarvis-high  (authentic JARVIS tone)
    Fallback voice: en_US-ryan-high   (clean American English)
    System fallback: pyttsx3 (macOS/Windows system TTS)

    On first use, downloads the voice model to ~/.atlas/voices/ if absent.
    Checks project-local voices/ folder first for manually placed models.

    Phrase cache: last 20 synthesised audio arrays kept in RAM for instant replay.
    JARVIS quality: length_scale=0.95, noise_scale=0.333, noise_w=0.333
    """

    def __init__(self, voice: str = _DEFAULT_VOICE, speech_rate: float = 1.0,
                 voice_enabled: bool = True,
                 length_scale: Optional[float] = None,
                 noise_scale: Optional[float] = None,
                 noise_w: Optional[float] = None):
        self._voice         = voice
        self._piper         = None
        self._pyttsx3       = None
        self._backend       = None       # 'piper' | 'pyttsx3' | 'none'
        self._lock          = threading.Lock()
        self._speaking      = False
        self._speech_rate   = max(0.25, min(4.0, speech_rate))
        self._voice_enabled = voice_enabled
        self._voice_index   = (_AVAILABLE_VOICES.index(voice)
                               if voice in _AVAILABLE_VOICES else 0)
        self._phrase_cache  = _PhraseCache(maxsize=20)

        # Quality settings — config overrides JARVIS defaults
        self._length_scale  = length_scale if length_scale is not None else _JARVIS_LENGTH_SCALE
        self._noise_scale   = noise_scale  if noise_scale  is not None else _JARVIS_NOISE_SCALE
        self._noise_w       = noise_w      if noise_w      is not None else _JARVIS_NOISE_W

    # ── Initialization ────────────────────────────────────────────────────────

    def _ensure_ready(self):
        if self._backend is not None:
            return
        if self._try_piper(self._voice):
            self._backend = "piper"
        elif self._voice != _FALLBACK_VOICE and self._try_piper(_FALLBACK_VOICE):
            log.warning("JARVIS voice failed — using fallback: %s", _FALLBACK_VOICE)
            self._backend = "piper"
        elif self._try_pyttsx3():
            self._backend = "pyttsx3"
        else:
            log.error("No TTS backend available — ATLAS will be silent.")
            self._backend = "none"

    def _try_piper(self, voice: str) -> bool:
        try:
            from piper.voice import PiperVoice

            model_path  = self._find_model(voice, ".onnx")
            config_path = self._find_model(voice, ".onnx.json")

            if model_path is None:
                self._download_voice(voice)
                model_path  = _VOICES_DIR / f"{voice}.onnx"
                config_path = _VOICES_DIR / f"{voice}.onnx.json"

            self._piper = PiperVoice.load(str(model_path), str(config_path))
            self._voice = voice
            log.info("Piper TTS ready (%s).", voice)

            # Set quality params per voice
            if voice == _DEFAULT_VOICE:
                self._length_scale = _JARVIS_LENGTH_SCALE
                self._noise_scale  = _JARVIS_NOISE_SCALE
                self._noise_w      = _JARVIS_NOISE_W
            else:
                self._length_scale = 1.0
                self._noise_scale  = 0.667
                self._noise_w      = 0.8

            return True
        except Exception as exc:
            log.warning("Piper TTS unavailable for %s: %s", voice, exc)
            return False

    def _find_model(self, voice: str, suffix: str) -> Optional[Path]:
        """Check project voices/ then ~/.atlas/voices/ for an existing model file."""
        for base in (_LOCAL_VOICES, _VOICES_DIR):
            p = base / f"{voice}{suffix}"
            if p.exists():
                return p
        return None

    def _download_voice(self, voice: str):
        import ssl, urllib.request
        _VOICES_DIR.mkdir(parents=True, exist_ok=True)

        ctx    = ssl._create_unverified_context()
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

        if voice == _DEFAULT_VOICE:
            # JARVIS model lives on a different HuggingFace repo
            for suffix in (".onnx", ".onnx.json"):
                url  = f"{_JARVIS_HF_BASE}/{voice}{suffix}"
                dest = _VOICES_DIR / f"{voice}{suffix}"
                log.info("Downloading JARVIS voice model: %s …", url)
                with opener.open(url) as resp, open(dest, "wb") as fh:
                    fh.write(resp.read())
        else:
            voice_subpath = _VOICE_PATHS.get(voice, "en/en_US/ryan/high")
            base_url      = f"{_PIPER_HF_BASE}/{voice_subpath}"
            for suffix in (".onnx", ".onnx.json"):
                url  = f"{base_url}/{voice}{suffix}"
                dest = _VOICES_DIR / f"{voice}{suffix}"
                log.info("Downloading voice model: %s …", url)
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

    def pre_warm(self):
        """Load Piper on a background thread so first response has zero TTS delay."""
        def _warm():
            with self._lock:
                self._ensure_ready()
            log.info("Piper TTS pre-warmed.")
        threading.Thread(target=_warm, daemon=True, name="atlas-tts-warm").start()

    # ── Synthesis config ──────────────────────────────────────────────────────

    def _syn_config(self):
        from piper.config import SynthesisConfig
        length = (1.0 / max(0.25, self._speech_rate)) * self._length_scale
        return SynthesisConfig(
            length_scale=length,
            noise_scale=self._noise_scale,
            noise_w_scale=self._noise_w,
        )

    # ── Core synthesise → (sample_rate, audio_f32) ───────────────────────────

    def _synthesise(self, text: str) -> Optional[tuple]:
        """Synthesise text → (sample_rate, np.ndarray float32). Returns None on failure."""
        cached = self._phrase_cache.get(text)
        if cached:
            return cached

        try:
            syn_cfg = self._syn_config()
            chunks  = list(self._piper.synthesize(text, syn_config=syn_cfg))
            if not chunks:
                return None
            sr    = chunks[0].sample_rate
            audio = np.concatenate([c.audio_float_array for c in chunks])
            self._phrase_cache.put(text, sr, audio)
            return sr, audio
        except Exception as exc:
            log.error("Piper synthesis error: %s", exc)
            return None

    # ── Public speak API ──────────────────────────────────────────────────────

    def speak(self, text: str, amplitude_cb=None):
        """Blocking: synthesize full text and play, then return."""
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

    def speak_sentences(self, sentences: list[str], amplitude_cb=None):
        """
        Streaming TTS: speak each sentence sequentially.
        Sentence 1 plays while sentence 2 is being synthesised in a lookahead thread.
        """
        if not self._voice_enabled or not sentences:
            return

        with self._lock:
            self._ensure_ready()
            self._speaking = True
            try:
                # Pre-synthesise sentence 2 while sentence 1 plays
                lookahead: list = [None]

                def _pre_synth(idx: int):
                    if idx < len(sentences) and self._backend == "piper":
                        lookahead[0] = self._synthesise(sentences[idx])

                for i, sentence in enumerate(sentences):
                    if not sentence.strip():
                        continue

                    # Start pre-synthesising the next sentence immediately
                    if self._backend == "piper":
                        pre = threading.Thread(
                            target=_pre_synth, args=(i + 1,), daemon=True
                        )
                        pre.start()

                    if self._backend == "piper":
                        # Use pre-synthesised if available (i.e., from previous iteration)
                        result = lookahead[0] if i > 0 and lookahead[0] else self._synthesise(sentence)
                        lookahead[0] = None
                        if result:
                            self._play_audio(*result, amplitude_cb=amplitude_cb)
                        if self._backend == "piper":
                            pre.join()   # ensure next sentence is ready
                    elif self._backend == "pyttsx3":
                        self._speak_pyttsx3(sentence)
            finally:
                self._speaking = False
                if amplitude_cb:
                    amplitude_cb(0.0)

    def _speak_piper(self, text: str, amplitude_cb=None):
        result = self._synthesise(text)
        if result:
            self._play_audio(*result, amplitude_cb=amplitude_cb)

    def _play_audio(self, sr: int, audio: np.ndarray, amplitude_cb=None):
        import sounddevice as sd
        block_size = 2048
        pos = 0
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32") as stream:
            while pos < len(audio):
                block = audio[pos: pos + block_size]
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
            self._piper   = None
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

    Upgrade notes:
    - WebRTC VAD replaces pure energy silence detection: speech-end in <50 ms
    - Whisper model auto-selected per Apple chip
    - Streaming sentence-level TTS: first word in <800 ms
    - Response cache: instant reply to the 50 most common commands
    - Voice emotion detector: adjusts TTS speed and wit based on user tone
    """

    # ── Signals ───────────────────────────────────────────────────────────────
    amplitude_changed   = pyqtSignal(float)
    wake_word_detected  = pyqtSignal()
    transcription_ready = pyqtSignal(str)
    response_ready      = pyqtSignal(str)
    speaking_started    = pyqtSignal()
    speaking_done       = pyqtSignal()
    status_message      = pyqtSignal(str)
    error_occurred      = pyqtSignal(str)

    # ── VAD tuning ────────────────────────────────────────────────────────────
    _SILENCE_THRESHOLD   = 0.05
    _SILENCE_FRAMES      = 30     # ≈ 1 s — energy fallback only
    _VAD_SILENCE_FRAMES  = 18     # ≈ 30 ms × 18 = ~540 ms — WebRTC VAD
    _MAX_RECORD_SECS     = 12.0
    _MIN_RECORD_SECS     = 0.3

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        vc = config.get("voice", {})

        # Whisper: "auto" triggers chip detection
        whisper_model = vc.get("whisper_model", "auto")
        if str(whisper_model).lower() == "auto":
            whisper_model = _detect_whisper_model()

        self._whisper_model  = whisper_model
        self._tts_voice      = vc.get("piper_voice", vc.get("tts_model", _DEFAULT_VOICE))
        self._sr             = vc.get("sample_rate", SAMPLE_RATE)
        self._wake_word      = vc.get("wake_word", "atlas")
        self._muted          = False
        self._tts_playing    = False
        self._stop_event     = threading.Event()
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=300)
        self._response_cb: Optional[Callable[[str], str]] = None

        self._silence_min    = float(vc.get("silence_threshold_min", 0.015))

        # Conversation mode
        self._convo_active   = False
        self._convo_ends_at  = 0.0
        self._convo_timeout  = float(vc.get("conversation_timeout", 15))
        self._convo_vad_run  = 0

        # New systems
        self._formatter      = SpeechFormatter()
        self._emotion        = VoiceEmotionDetector()
        self._webrtc_vad     = WebRTCVAD(aggressiveness=2)
        self._cache_enabled  = config.get("response_cache_enabled", True)
        self._resp_cache     = ResponseCache(
            maxsize=config.get("response_cache_size", 50)
        ) if self._cache_enabled else None

        self._wake  = None
        self._stt   = WhisperSTT(self._whisper_model)
        self.tts    = PiperTTS(
            voice=self._tts_voice,
            speech_rate=vc.get("speech_rate", 1.0),
            voice_enabled=vc.get("voice_enabled", True),
            length_scale=vc.get("voice_length_scale"),
            noise_scale=vc.get("voice_noise_scale"),
            noise_w=vc.get("voice_noise_w"),
        )

    # ── Adaptive noise calibration ────────────────────────────────────────────

    def _calibrate_silence_threshold(self) -> float:
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

        noise     = float(np.percentile(amps, 75))
        threshold = max(self._silence_min, min(0.25, noise * 2.8))
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
            if not self._muted and not self._tts_playing:
                try:
                    self._audio_q.put_nowait(indata[:, 0].copy())
                except queue.Full:
                    pass

        def _make_stream():
            return sd.InputStream(
                samplerate=self._sr,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=frame_len,
                callback=_audio_cb,
            )

        self.status_message.emit("CALIBRATING MIC...")
        with _make_stream():
            silence_threshold = self._calibrate_silence_threshold()

        log.info("Voice pipeline running (frame=%d, sr=%d, vad=%s)",
                 frame_len, self._sr,
                 "WebRTC" if self._webrtc_vad.available else "energy")
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

                amp = _rms_amplitude(chunk)
                self.amplitude_changed.emit(amp)
                self._emotion.update(amp)

                if self._muted or self._tts_playing:
                    continue

                if not recording:
                    # ── IDLE: conversation mode or wake word ───────────────
                    if self._convo_active:
                        if time.monotonic() > self._convo_ends_at:
                            self._convo_active  = False
                            self._convo_vad_run = 0
                            log.info("Conversation mode ended — back to wake word.")
                            self.status_message.emit("VOICE ONLINE")
                        elif amp > silence_threshold:
                            self._convo_vad_run += 1
                            if self._convo_vad_run >= 4:
                                self._convo_vad_run = 0
                                log.info("Conversation mode: speech detected → recording")
                                self.wake_word_detected.emit()
                                recording   = True
                                rec_buf     = []
                                silence_run = 0
                                rec_start   = time.monotonic()
                                self._webrtc_vad.reset()
                        else:
                            self._convo_vad_run = 0
                    else:
                        if self._wake.process(chunk):
                            log.info("Wake word detected → recording")
                            self.wake_word_detected.emit()
                            recording   = True
                            rec_buf     = []
                            silence_run = 0
                            rec_start   = time.monotonic()
                            self._webrtc_vad.reset()

                else:
                    # ── RECORDING: accumulate until stop/silence/timeout ───
                    rec_buf.append(chunk)
                    elapsed = time.monotonic() - rec_start

                    stop_triggered = self._wake.check_stop(chunk)

                    # WebRTC VAD silence detection (preferred, faster)
                    if self._webrtc_vad.available:
                        speech = self._webrtc_vad.is_speech(chunk)
                        if speech is False:
                            silence_run += 1
                        elif speech is True:
                            silence_run = 0
                        silence_limit = self._VAD_SILENCE_FRAMES
                    else:
                        # Energy fallback
                        if amp < silence_threshold:
                            silence_run += 1
                        else:
                            silence_run = 0
                        silence_limit = self._SILENCE_FRAMES

                    done = (
                        stop_triggered
                        or (silence_run >= silence_limit
                            and elapsed >= self._MIN_RECORD_SECS)
                        or elapsed >= self._MAX_RECORD_SECS
                    )

                    if done:
                        recording = False
                        if elapsed < 0.25:
                            log.debug("Utterance too short (%.2fs), ignoring", elapsed)
                            # Reset conversation mode — prevents ambient noise loop:
                            # ping → noise → discard → ping → noise → discard → ...
                            self._convo_active  = False
                            self._convo_vad_run = 0
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
            self.speaking_done.emit()
            return

        if not text:
            log.debug("Empty transcription — ignoring")
            self.speaking_done.emit()
            return

        log.info("Transcribed: %r", text)
        self.transcription_ready.emit(text)

        # 2. Voice command interception
        cmd_response = self._check_voice_command(text)
        if cmd_response is not None:
            if cmd_response:
                self.response_ready.emit(cmd_response)
                self._tts_play(cmd_response)
            return

        # 3. Response cache hit?
        if self._resp_cache:
            cached = self._resp_cache.get(text)
            if cached:
                log.info("Response cache hit for: %r", text[:60])
                self.response_ready.emit(cached)
                self._tts_play(cached)
                return

        # 4. AI response
        response = self._get_response(text)
        if not response:
            return

        # Store in cache
        if self._resp_cache:
            self._resp_cache.put(text, response)

        self.response_ready.emit(response)
        self._tts_play(response)

    def _tts_play(self, text: str):
        """Launch streaming sentence-level TTS in a daemon thread."""
        if self._tts_playing:
            log.debug("TTS already playing — skipping queued response")
            return

        self._tts_playing = True

        # Apply emotion-aware speed
        speed_mult = self._emotion.tts_speed_multiplier()
        original_rate = self.tts.speech_rate
        if abs(speed_mult - 1.0) > 0.05:
            self.tts.set_speech_rate(original_rate * speed_mult)

        def _play():
            self.speaking_started.emit()
            try:
                sentences = SpeechFormatter.split_sentences(text)
                if sentences:
                    self.tts.speak_sentences(sentences, amplitude_cb=self.amplitude_changed.emit)
                else:
                    self.tts.speak(text, amplitude_cb=self.amplitude_changed.emit)
            except Exception as exc:
                log.error("TTS error: %s", exc)
                self.error_occurred.emit(f"TTS error: {exc}")
            finally:
                # Restore original speech rate
                if abs(speed_mult - 1.0) > 0.05:
                    self.tts.set_speech_rate(original_rate)
                time.sleep(0.6)
                while not self._audio_q.empty():
                    try:
                        self._audio_q.get_nowait()
                    except queue.Empty:
                        break
                self._tts_playing   = False
                self._convo_active  = True
                self._convo_ends_at = time.monotonic() + self._convo_timeout
                self._convo_vad_run = 0
                log.info("Conversation mode active (%.0fs window).", self._convo_timeout)
                self.status_message.emit("LISTENING...")
                self.speaking_done.emit()

        threading.Thread(target=_play, daemon=True, name="atlas-tts").start()

    def _check_voice_command(self, text: str) -> Optional[str]:
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
            return "Voice muted."

        if "atlas mute sounds" in lower:
            return "Sound effects muted."

        if "atlas enable sounds" in lower:
            return "Sound effects enabled."

        return None

    def _get_response(self, text: str) -> str:
        if self._response_cb:
            try:
                return self._response_cb(text) or ""
            except Exception as exc:
                log.error("Response callback error: %s", exc)
                self.error_occurred.emit(f"Response error: {exc}")
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
        vm.start()
        vm.set_response_callback(fn)
        vm.stop()

    Called by core.py:
        vm.speak(text)       # direct TTS without STT pipeline
        vm.pre_warm()        # load Piper model ahead of first use
    """

    def __init__(self, config: dict, window=None):
        self._cfg         = config
        self._window      = window
        self._worker: Optional[VoiceWorker] = None
        self._response_cb: Optional[Callable[[str], str]] = None

    def start(self):
        if self._worker and self._worker.isRunning():
            return

        self._worker = VoiceWorker(self._cfg)
        self._wire_signals()
        if self._response_cb:
            self._worker.set_response_callback(self._response_cb)
        self._worker.start()
        log.info("VoiceModule started.")

    def pre_warm(self):
        """Pre-warm Piper TTS so first response has zero synthesis delay."""
        if self._worker:
            self._worker.tts.pre_warm()

    def stop(self):
        if self._worker:
            self._worker.stop()
            self._worker = None
        log.info("VoiceModule stopped.")

    def set_muted(self, muted: bool):
        if self._worker:
            self._worker.set_muted(muted)

    def set_response_callback(self, cb: Callable[[str], str]):
        self._response_cb = cb
        if self._worker:
            self._worker.set_response_callback(cb)

    def speak(self, text: str):
        """Direct TTS call from core agent (bypasses STT pipeline)."""
        if self._worker:
            self._worker._tts_play(text)

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

        w  = self._window
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

        wr.started.connect(lambda: w.set_module_active("VOICE", True))
