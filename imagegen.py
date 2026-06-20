"""
ATLAS Image Generation — Local Stable Diffusion

Generates images locally on Apple Silicon using MPS acceleration.
No API key. No internet needed after first download.

First launch: warns about ~4 GB download and waits for voice confirmation
  before downloading ("ATLAS confirm download").

Voice commands handled by handle(text) → Optional[str]:
  "ATLAS draw me X"               → standard mode (512×512, 20 steps)
  "ATLAS generate an image of X"  → standard mode
  "ATLAS show me what X looks like" → standard mode
  "ATLAS high quality image of X" → quality mode (768×768, 50 steps)
  "ATLAS draw me a high quality X" → quality mode
  "ATLAS quickly sketch X"        → quick mode (512×512, 10 steps)
  "ATLAS draw a logo for X"       → logo style
  "ATLAS generate another one"    → regenerate last prompt
  "ATLAS make it more X"          → refine with new instruction
  "ATLAS save that image"         → save last image to Desktop
  "ATLAS open that in preview"    → open in macOS Preview
  "ATLAS hide image"              → hide image panel
  "ATLAS confirm download"        → approve model download

Packages: diffusers  accelerate  torch (MPS)  Pillow
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

log = logging.getLogger(__name__)

# ── Model config ──────────────────────────────────────────────────────────────

_MODEL_ID   = "runwayml/stable-diffusion-v1-5"
_OUTPUT_DIR = Path.home() / "Desktop" / "ATLAS_Projects" / "images"

_MODES = {
    "standard": {"width": 512,  "height": 512,  "steps": 20},
    "quality":  {"width": 768,  "height": 768,  "steps": 50},
    "quick":    {"width": 512,  "height": 512,  "steps": 10},
    "logo":     {"width": 512,  "height": 512,  "steps": 25},
    "banner":   {"width": 768,  "height": 384,  "steps": 25},
    "sprite":   {"width": 256,  "height": 256,  "steps": 20},
}

_STYLE_PROMPTS = {
    "logo":   "logo design, minimal, vector art, clean lines, professional, ",
    "banner": "wide banner image, professional, web design, ",
    "sprite": "pixel art style, game sprite, 16-bit, isolated on white background, ",
}

_QUALITY_SUFFIX = (
    ", high quality, detailed, sharp focus, professional photography, "
    "8k resolution, masterpiece, best quality"
)


class ImageGenModule:
    """
    Local Stable Diffusion image generator.
    All generation runs in daemon threads to avoid blocking the UI.
    """

    def __init__(self, config: dict,
                 state_cb:  Optional[Callable[[str], None]] = None,
                 speak_cb:  Optional[Callable[[str], None]] = None,
                 show_image_cb: Optional[Callable] = None,
                 brain=None):
        self._config        = config
        self._state_cb      = state_cb       # window.set_state
        self._speak_cb      = speak_cb       # vm.speak
        self._show_image_cb = show_image_cb  # ImagePanel.show_image(path, prompt, elapsed)
        self._brain         = brain

        self._user_name     = config.get("user_name", "Boss")
        self._pipeline      = None           # loaded StableDiffusionPipeline
        self._generating    = False
        self._last_image:   Optional[Path]  = None
        self._last_prompt:  Optional[str]   = None
        self._last_mode:    str             = "standard"
        self._pending_prompt: Optional[str] = None  # waiting for download confirm

        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_state_callback(self, cb) -> None:    self._state_cb      = cb
    def set_speak_callback(self, cb) -> None:    self._speak_cb      = cb
    def set_show_image_callback(self, cb) -> None: self._show_image_cb = cb
    def set_brain(self, brain) -> None:          self._brain         = brain

    # ── Voice command handler ─────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        # Download confirmation
        if any(p in lower for p in ("atlas confirm download", "confirm download",
                                     "yes download", "go ahead and download",
                                     "confirm the download")):
            return self._confirm_download()

        # Regenerate
        if any(p in lower for p in ("generate another one", "atlas generate another",
                                     "do another one", "regenerate")):
            if self._last_prompt:
                return self._start_generation(self._last_prompt, self._last_mode)
            return f"No previous generation to repeat, {self._user_name}."

        # Refine
        if lower.startswith(("make it more ", "atlas make it more ")):
            adj = lower.split("make it more ", 1)[-1].strip()
            if self._last_prompt and adj:
                new_prompt = self._last_prompt + f", {adj}"
                return self._start_generation(new_prompt, self._last_mode)
            return f"No previous image to refine, {self._user_name}."

        # Save
        if any(p in lower for p in ("atlas save that image", "save that image",
                                     "save the image", "save last image")):
            return self._save_to_desktop()

        # Open in Preview
        if any(p in lower for p in ("open that in preview", "atlas open in preview",
                                     "open image in preview", "show in preview")):
            return self._open_in_preview()

        # Hide image panel
        if any(p in lower for p in ("atlas hide image", "hide image",
                                     "close image", "dismiss image")):
            if self._show_image_cb:
                self._show_image_cb(None, None, None)
            return "Image panel closed."

        # ── Mode detection from command ───────────────────────────────────────

        # Quality mode
        if any(p in lower for p in ("high quality image of ", "draw me a high quality ",
                                     "generate a high quality ")):
            subject = self._extract_subject(lower, [
                "high quality image of ", "draw me a high quality ",
                "generate a high quality ",
            ])
            if subject:
                return self._start_generation(subject, "quality")

        # Quick / sketch
        if any(p in lower for p in ("quickly sketch ", "quick sketch of ",
                                     "fast sketch of ", "atlas quickly sketch ")):
            subject = self._extract_subject(lower, [
                "quickly sketch ", "quick sketch of ", "fast sketch of ",
                "atlas quickly sketch ",
            ])
            if subject:
                return self._start_generation(subject, "quick")

        # Logo
        if any(p in lower for p in ("draw a logo for ", "generate a logo for ",
                                     "create a logo for ", "logo for ")):
            subject = self._extract_subject(lower, [
                "draw a logo for ", "generate a logo for ",
                "create a logo for ", "logo for ",
            ])
            if subject:
                return self._start_generation(subject, "logo")

        # Banner
        if any(p in lower for p in ("draw a banner for ", "generate a banner for ",
                                     "banner for my website", "website banner")):
            subject = self._extract_subject(lower, [
                "draw a banner for ", "generate a banner for ", "banner for ",
            ]) or "website"
            return self._start_generation(subject, "banner")

        # Sprite
        if any(p in lower for p in ("game sprite of ", "generate a game sprite",
                                     "pixel art of ", "sprite of ")):
            subject = self._extract_subject(lower, [
                "game sprite of ", "pixel art of ", "sprite of ",
            ])
            if subject:
                return self._start_generation(subject, "sprite")

        # Standard — "draw me X" / "generate an image of X" / "show me what X looks like"
        for prefix in ("atlas draw me ", "draw me ", "atlas generate an image of ",
                       "generate an image of ", "atlas show me what ", "show me what ",
                       "create an image of ", "atlas create an image of ",
                       "generate a picture of ", "atlas generate a picture of ",
                       "atlas draw a picture of ", "draw a picture of "):
            if lower.startswith(prefix) or prefix in lower:
                subject = self._extract_subject(lower, [prefix])
                if subject:
                    # strip trailing "looks like" if present
                    subject = subject.removesuffix(" looks like").strip()
                    return self._start_generation(subject, "standard")

        return None

    # ── Generation trigger ────────────────────────────────────────────────────

    def _start_generation(self, subject: str, mode: str) -> str:
        if self._generating:
            return f"Already generating, {self._user_name}. Please wait."

        if not self._is_model_cached():
            self._pending_prompt = (subject, mode)
            return (
                f"The Stable Diffusion model needs to download about 4 gigabytes, {self._user_name}. "
                "This only happens once. "
                "Say 'ATLAS confirm download' to proceed, or wait and I'll skip it."
            )

        threading.Thread(
            target=self._generate_thread,
            args=(subject, mode),
            daemon=True,
            name="atlas-imagegen",
        ).start()

        mode_desc = {"quality": "high quality ", "quick": "quick ", "logo": "logo "}
        return (
            f"Generating {mode_desc.get(mode, '')}image of {subject}, {self._user_name}. "
            f"This takes about {self._eta_seconds(mode)} seconds on Apple Silicon."
        )

    def _confirm_download(self) -> str:
        if not self._pending_prompt:
            return f"No image generation is waiting for download, {self._user_name}."
        subject, mode = self._pending_prompt
        self._pending_prompt = None

        threading.Thread(
            target=self._download_then_generate,
            args=(subject, mode),
            daemon=True,
            name="atlas-imagegen-dl",
        ).start()
        return (
            f"Starting download, {self._user_name}. "
            "This will take a few minutes. I'll let you know when it's done."
        )

    # ── Background threads ────────────────────────────────────────────────────

    def _download_then_generate(self, subject: str, mode: str) -> None:
        self._set_state("thinking")
        self._speak("Downloading Stable Diffusion model. This may take several minutes.")
        try:
            self._load_pipeline(force_download=True)
            self._speak("Download complete. Starting generation now.")
            self._generate_thread(subject, mode)
        except Exception as exc:
            log.error("Download error: %s", exc)
            self._speak(f"Download failed: {exc}")
            self._set_state("idle")

    def _generate_thread(self, subject: str, mode: str) -> None:
        self._generating = True
        self._set_state("thinking")   # purple orb during generation

        try:
            # Enhance prompt with brain
            enhanced = self._enhance_prompt(subject, mode)
            log.info("ImageGen: enhanced prompt = %r", enhanced[:120])

            # Load pipeline if needed
            if self._pipeline is None:
                self._speak("Loading image model, one moment.")
                self._load_pipeline()

            cfg = _MODES.get(mode, _MODES["standard"])
            t0  = time.time()

            import torch
            device = torch.device("mps") if torch.backends.mps.is_available() else torch.device("cpu")
            pipe   = self._pipeline.to(device)

            image = pipe(
                enhanced,
                width=cfg["width"],
                height=cfg["height"],
                num_inference_steps=cfg["steps"],
                guidance_scale=7.5,
            ).images[0]

            elapsed = time.time() - t0

            # Save image
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = _OUTPUT_DIR / f"atlas_{mode}_{ts}.png"
            image.save(str(out_path))
            self._last_image  = out_path
            self._last_prompt = enhanced
            self._last_mode   = mode

            log.info("ImageGen: saved to %s (%.1f s)", out_path, elapsed)

            # Show in UI
            if self._show_image_cb:
                self._show_image_cb(str(out_path), subject, round(elapsed))

            self._speak(
                f"Done! Generated in {round(elapsed)} seconds. "
                f"Image saved to your Desktop."
            )

        except Exception as exc:
            log.error("Generation error: %s", exc)
            self._speak(f"Image generation failed: {type(exc).__name__}.")
        finally:
            self._generating = False
            self._set_state("idle")

    # ── Pipeline management ───────────────────────────────────────────────────

    def _load_pipeline(self, force_download: bool = False) -> None:
        import torch
        from diffusers import StableDiffusionPipeline

        dtype = torch.float16
        log.info("ImageGen: loading pipeline %s", _MODEL_ID)
        pipe = StableDiffusionPipeline.from_pretrained(
            _MODEL_ID,
            torch_dtype=dtype,
            safety_checker=None,   # disabled to avoid extra model download
            requires_safety_checker=False,
        )
        pipe.enable_attention_slicing()  # reduce VRAM / unified memory usage
        self._pipeline = pipe
        log.info("ImageGen: pipeline loaded.")

    def _is_model_cached(self) -> bool:
        cache = Path.home() / ".cache" / "huggingface" / "hub"
        model_dir = _MODEL_ID.replace("/", "--")
        return any(cache.glob(f"*{model_dir}*"))

    # ── Prompt enhancement ────────────────────────────────────────────────────

    def _enhance_prompt(self, subject: str, mode: str) -> str:
        style_prefix = _STYLE_PROMPTS.get(mode, "")

        if self._brain:
            try:
                enhanced = self._brain.ask(
                    f"Convert this image request into a detailed Stable Diffusion prompt. "
                    f"Add lighting, style, composition, and quality descriptors. "
                    f"Keep it under 70 words. Return ONLY the prompt, no explanation.\n"
                    f"Request: {subject}"
                )
                if enhanced and len(enhanced) > 10:
                    return style_prefix + enhanced.strip() + _QUALITY_SUFFIX
            except Exception as exc:
                log.debug("Prompt enhancement failed: %s", exc)

        # Fallback: simple enhancement
        return style_prefix + subject + _QUALITY_SUFFIX

    # ── File operations ───────────────────────────────────────────────────────

    def _save_to_desktop(self) -> str:
        if not self._last_image or not self._last_image.exists():
            return f"No image to save, {self._user_name}."
        dest = Path.home() / "Desktop" / self._last_image.name
        try:
            import shutil
            shutil.copy2(self._last_image, dest)
            return f"Image saved to Desktop as {self._last_image.name}."
        except Exception as exc:
            log.error("Save error: %s", exc)
            return "Couldn't save the image."

    def _open_in_preview(self) -> str:
        if not self._last_image or not self._last_image.exists():
            return f"No image to open, {self._user_name}."
        try:
            subprocess.run(["open", "-a", "Preview", str(self._last_image)], timeout=5)
            return "Opening in Preview."
        except Exception as exc:
            log.error("Preview error: %s", exc)
            return "Couldn't open Preview."

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_subject(lower: str, prefixes: list[str]) -> str:
        for prefix in sorted(prefixes, key=len, reverse=True):
            idx = lower.find(prefix)
            if idx >= 0:
                return lower[idx + len(prefix):].strip()
        return ""

    @staticmethod
    def _eta_seconds(mode: str) -> str:
        eta = {"standard": "15 to 30", "quality": "60 to 90",
               "quick": "under 10", "logo": "20 to 40",
               "banner": "20 to 40", "sprite": "10 to 20"}
        return eta.get(mode, "15 to 30")

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
            log.info("ImageGen: %s", text)
