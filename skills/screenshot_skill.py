"""Screenshot skill — capture and describe the current screen."""


def skill_info():
    return {
        "name": "screenshot",
        "triggers": ["atlas take a screenshot", "atlas screenshot",
                     "atlas capture the screen", "take screenshot"],
        "description": "Capture screen and optionally describe it with vision AI",
    }


def execute(query: str, context: dict) -> str:
    vision = context.get("vision")
    lower  = query.lower()

    if vision is None:
        # Fallback: just take and save the screenshot
        return _save_screenshot()

    describe = any(p in lower for p in ("describe", "analyse", "analyze", "what is on"))
    if describe:
        return vision.ask_with_screenshot("Describe what is on screen in detail.")

    # Just take a screenshot and save it
    b64 = vision.capture_screenshot()
    if b64:
        path = _save_b64_screenshot(b64)
        return f"Screenshot saved to {path}."
    return "I couldn't capture the screen."


def _save_screenshot() -> str:
    try:
        from PIL import ImageGrab
        from datetime import datetime
        from pathlib import Path
        import platform

        img  = ImageGrab.grab()
        home = Path.home()
        if platform.system() == "Darwin":
            dest = home / "Desktop" / f"atlas_screenshot_{datetime.now().strftime('%H%M%S')}.png"
        else:
            dest = home / f"atlas_screenshot_{datetime.now().strftime('%H%M%S')}.png"
        img.save(str(dest))
        return f"Screenshot saved to {dest.name} on your Desktop."
    except Exception as exc:
        return f"Screenshot failed: {exc}"


def _save_b64_screenshot(b64: str) -> str:
    import base64
    from datetime import datetime
    from pathlib import Path
    import platform

    data = base64.b64decode(b64)
    home = Path.home()
    if platform.system() == "Darwin":
        dest = home / "Desktop" / f"atlas_screenshot_{datetime.now().strftime('%H%M%S')}.png"
    else:
        dest = home / f"atlas_screenshot_{datetime.now().strftime('%H%M%S')}.png"
    dest.write_bytes(data)
    return str(dest)
