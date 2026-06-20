"""Music skill — Spotify control via AppleScript (macOS)."""

import platform
import subprocess


def skill_info():
    return {
        "name": "music",
        "triggers": ["atlas play music", "atlas pause music", "atlas skip song",
                     "atlas next track", "atlas previous track", "atlas stop music",
                     "atlas what song", "what song is playing",
                     "atlas volume up", "atlas volume down"],
        "description": "Spotify playback control via AppleScript",
    }


def execute(query: str, context: dict) -> str:
    if platform.system() != "Darwin":
        return "Music control via AppleScript is only available on macOS."

    lower = query.lower()

    if any(p in lower for p in ("pause", "stop music")):
        return _applescript('tell application "Spotify" to pause', "Paused.")

    if any(p in lower for p in ("play music", "resume", "atlas play")):
        return _applescript('tell application "Spotify" to play', "Playing.")

    if any(p in lower for p in ("skip song", "next track", "next song")):
        return _applescript('tell application "Spotify" to next track', "Skipped to next track.")

    if any(p in lower for p in ("previous track", "previous song", "go back")):
        return _applescript('tell application "Spotify" to previous track', "Going back.")

    if any(p in lower for p in ("what song", "what is playing", "now playing")):
        return _get_now_playing()

    if "volume up" in lower:
        return _applescript(
            'tell application "Spotify" to set sound volume to '
            '(sound volume + 10)', "Volume up."
        )

    if "volume down" in lower:
        return _applescript(
            'tell application "Spotify" to set sound volume to '
            '(sound volume - 10)', "Volume down."
        )

    return "I didn't recognise that music command."


def _applescript(script: str, success_msg: str) -> str:
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=5)
        return success_msg
    except subprocess.CalledProcessError:
        return "Spotify doesn't seem to be running, Boss."
    except Exception as exc:
        return f"Music control error: {exc}"


def _get_now_playing() -> str:
    try:
        name   = subprocess.run(
            ["osascript", "-e", 'tell application "Spotify" to name of current track'],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        artist = subprocess.run(
            ["osascript", "-e", 'tell application "Spotify" to artist of current track'],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if name:
            return f"Currently playing {name} by {artist}."
        return "Nothing is playing right now."
    except Exception:
        return "I couldn't read the current track."
