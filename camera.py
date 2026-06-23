"""
ATLAS Camera Module — Webcam visual input

Lets ATLAS see what the user holds up to the camera.
Uses OpenCV for capture and the same Qwen3 vision model as VisionModule.

Voice triggers (handled by Brain via keyword matching):
  "open camera" / "camera on"
  "close camera" / "camera off"
  "take a look" / "look at this" / "what do you see"
  "what is this" / "can you see this" / "identify this"

Python API:
  camera.start()           — open webcam, update UI dot
  camera.stop()            — close webcam, update UI dot
  camera.capture_and_ask(question) — grab frame + ask vision model

main.py wires window._camera_module = camera so the UI button can call start/stop.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_CAMERA_SYSTEM_PROMPT = """\
You are ATLAS, an AI assistant with an active webcam feed.
The user has held something up to the camera. Describe and identify it clearly.

Rules:
- Be specific: brand names, titles, colours, text you can read
- If it is a book or magazine: read the title and author if visible
- If it is a product: name it and any visible details
- If it is handwriting or a document: read the text
- If the image is unclear: say so honestly
- Keep voice response to 2-3 sentences; put detail in the feed panel

Respond in plain prose. Address the user as Boss."""

_VOICE_TRIGGERS = frozenset({
    "take a look", "look at this", "what is this", "what do you see",
    "can you see this", "identify this", "what am i holding",
    "what is that", "do you see this", "camera look",
})

_OPEN_TRIGGERS = frozenset({
    "open camera", "camera on", "turn on camera", "enable camera",
    "start camera", "activate camera",
})

_CLOSE_TRIGGERS = frozenset({
    "close camera", "camera off", "turn off camera", "disable camera",
    "stop camera", "deactivate camera",
})


class CameraModule:
    """Webcam capture + visual AI for ATLAS."""

    def __init__(self, config: dict, brain=None):
        self._cfg    = config
        self._brain  = brain

        self._speak_cb:    Optional[Callable[[str], None]] = None
        self._state_cb:    Optional[Callable[[str], None]] = None
        self._ui_cam_cb:   Optional[Callable[[bool], None]] = None  # notifies UI dot

        self._enabled  = config.get("camera_enabled", True)
        self._cam_idx  = int(config.get("camera_index", 0))
        self._active   = False
        self._cap      = None   # cv2.VideoCapture

        self._client = None
        self._model  = config.get("api", {}).get(
            "vision_model", "qwen/qwen2.5-vl-7b-instruct:free"
        )
        self._init_client()

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _init_client(self):
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            log.warning("CameraModule: OPENROUTER_API_KEY not set — visual AI disabled.")
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=key,
            )
            log.info("CameraModule: vision model ready (%s).", self._model)
        except Exception as exc:
            log.warning("CameraModule: OpenAI client init failed: %s", exc)

    def set_speak_callback(self, cb: Callable[[str], None]):
        self._speak_cb = cb

    def set_state_callback(self, cb: Callable[[str], None]):
        self._state_cb = cb

    def set_ui_camera_callback(self, cb: Callable[[bool], None]):
        """Called with True/False when camera turns on/off — updates UI dot."""
        self._ui_cam_cb = cb

    def set_brain(self, brain):
        self._brain = brain

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Open webcam. Returns True on success."""
        if not self._enabled:
            return False
        if self._active:
            return True
        try:
            import cv2
            cap = cv2.VideoCapture(self._cam_idx)
            if not cap.isOpened():
                log.warning("CameraModule: could not open webcam (index %d).", self._cam_idx)
                return False
            # warm up — first frame is often dark
            for _ in range(5):
                cap.read()
                time.sleep(0.05)
            self._cap    = cap
            self._active = True
            self._notify_ui(True)
            log.info("CameraModule: webcam started (index %d).", self._cam_idx)
            return True
        except ImportError:
            log.warning("CameraModule: opencv-python not installed. Run: pip install opencv-python")
            return False
        except Exception as exc:
            log.warning("CameraModule: webcam start failed: %s", exc)
            return False

    def stop(self):
        """Close webcam."""
        self._active = False
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._notify_ui(False)
        log.info("CameraModule: webcam stopped.")

    def _notify_ui(self, on: bool):
        if self._ui_cam_cb:
            try:
                self._ui_cam_cb(on)
            except Exception:
                pass

    # ── Capture & analyse ──────────────────────────────────────────────────────

    def grab_frame_b64(self) -> Optional[str]:
        """Grab one webcam frame and return as base64-encoded JPEG."""
        if not self._active or self._cap is None:
            return None
        try:
            import cv2
            ret, frame = self._cap.read()
            if not ret or frame is None:
                log.warning("CameraModule: frame capture failed.")
                return None
            # encode as JPEG for smaller payload
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return base64.b64encode(buf.tobytes()).decode()
        except Exception as exc:
            log.warning("CameraModule: frame encode failed: %s", exc)
            return None

    def capture_and_ask(self, question: str = "What do you see?") -> str:
        """
        Grab a frame and send it to the vision model.
        Returns the model's text response.
        """
        if not self._active:
            if not self.start():
                return "Camera isn't available right now, Boss."

        img_b64 = self.grab_frame_b64()
        if not img_b64:
            return "I couldn't grab a frame from the webcam, Boss."

        if not self._client:
            return "Visual AI isn't configured — set OPENROUTER_API_KEY, Boss."

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _CAMERA_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_b64}"
                                },
                            },
                            {"type": "text", "text": question},
                        ],
                    },
                ],
                max_tokens=300,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            log.warning("CameraModule: vision API call failed: %s", exc)
            return "I had trouble reading the image, Boss."

    # ── Voice command routing (called by Brain) ────────────────────────────────

    def handles(self, text: str) -> bool:
        """Return True if this module should handle this voice command."""
        t = text.lower()
        return (
            any(kw in t for kw in _OPEN_TRIGGERS)
            or any(kw in t for kw in _CLOSE_TRIGGERS)
            or (self._active and any(kw in t for kw in _VOICE_TRIGGERS))
            or (not self._active and any(kw in t for kw in _VOICE_TRIGGERS))
        )

    def handle(self, text: str) -> str:
        """Route voice command to appropriate action."""
        t = text.lower()

        if any(kw in t for kw in _CLOSE_TRIGGERS):
            self.stop()
            return "Camera off, Boss."

        if any(kw in t for kw in _OPEN_TRIGGERS):
            ok = self.start()
            return "Camera on, Boss." if ok else "Couldn't open the webcam, Boss."

        # Vision triggers
        if any(kw in t for kw in _VOICE_TRIGGERS):
            if self._state_cb:
                self._state_cb("detecting")
            question = text if text else "What do you see?"
            result = self.capture_and_ask(question)
            return result

        return ""

    def handle_async(self, text: str, done_cb: Callable[[str], None]):
        """Non-blocking version for use from Brain."""
        def _run():
            try:
                response = self.handle(text)
                done_cb(response)
            except Exception as exc:
                log.error("CameraModule.handle_async: %s", exc)
                done_cb("Camera error, Boss.")
        threading.Thread(target=_run, daemon=True, name="camera-handle").start()
