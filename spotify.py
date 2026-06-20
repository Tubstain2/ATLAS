"""
ATLAS Spotify Module

Uses Spotify Web API (Client Credentials) to search the catalog,
then controls playback via the spotify: URI scheme on macOS.

No Spotify Premium required for search. URI-based playback works on
the free desktop app.

Env vars:
  SPOTIFY_CLIENT_ID      — from developer.spotify.com
  SPOTIFY_CLIENT_SECRET  — from developer.spotify.com
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)

_TOKEN_URL  = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"

# Genre / mood words → playlist search wins
_GENRE_WORDS = frozenset({
    "hip hop", "rap", "lo-fi", "lofi", "jazz", "rock", "pop", "rnb", "r&b",
    "classical", "electronic", "edm", "country", "reggae", "metal", "indie",
    "chill", "chilled", "relaxing", "workout", "party", "study", "sleep",
    "focus", "sad", "happy", "upbeat", "mellow", "acoustic", "instrumental",
    "vibes", "mood", "playlist", "mix", "hits", "throwback", "oldies",
})

_PLAY_BY_RE = re.compile(r"play\s+(.+?)\s+by\s+(.+)", re.I)
_PLAY_RE    = re.compile(r"(?:play|put on|stream|listen to)\s+(.+)", re.I)
_GENERIC_RE = re.compile(
    r"(?:play|start|resume)\s+(?:something|music|spotify|a song|some music)?$", re.I
)

_TRIGGERS = frozenset({"play ", "put on ", "stream ", "listen to ", "shuffle "})


class SpotifyModule:
    """
    Detects Spotify play commands and executes them via the Spotify URI scheme.

    Wire-up in main.py:
        spotify = SpotifyModule(config)
        brain.set_spotify(spotify)
    """

    def __init__(self, config: dict):
        self._client_id     = os.environ.get("SPOTIFY_CLIENT_ID",     "").strip()
        self._client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
        self._token         = ""
        self._token_expiry  = 0.0

        if self._client_id and self._client_secret:
            log.info("Spotify module ready — catalog search enabled.")
        else:
            log.info("Spotify module ready — no API keys, basic playback only.")

    @property
    def available(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # ── Public handler ────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        """Return a voice response if this is a Spotify command, else None."""
        lower = text.lower().strip()

        triggered = (
            any(kw in lower for kw in _TRIGGERS)
            or any(lower.startswith(w.rstrip()) for w in _TRIGGERS)
        )
        if not triggered:
            return None

        # "play" / "play something" / "play music" — generic resume
        if _GENERIC_RE.match(lower):
            return self._play_generic()

        # "play X by Y" — specific track + artist
        m = _PLAY_BY_RE.search(lower)
        if m:
            track_q  = m.group(1).strip()
            artist_q = m.group(2).strip()
            return self._play_specific_track(
                query=f"track:{track_q} artist:{artist_q}",
                label=f"{track_q} by {artist_q}",
            )

        # "play X" / "put on X" / "listen to X"
        m = _PLAY_RE.search(lower)
        if m:
            query = m.group(1).strip()
            query = re.sub(r"\s+(?:on|in|via)\s+spotify$", "", query, flags=re.I).strip()
            if not query or query in ("something", "music", "a song", "some music"):
                return self._play_generic()
            return self._smart_search_and_play(query)

        return None

    # ── Smart search routing ──────────────────────────────────────────────────

    def _smart_search_and_play(self, query: str) -> str:
        """Choose search order based on the query type."""
        if not self.available:
            return self._play_generic()

        lower = query.lower()
        words = lower.split()

        # Genre / mood / playlist keywords → playlists first
        if any(g in lower for g in _GENRE_WORDS):
            return self._try_in_order(query, ["playlist", "track"])

        # Short query (1–2 words) → likely an artist name → artist first
        if len(words) <= 2:
            return self._try_in_order(query, ["artist", "track", "playlist"])

        # Longer query → likely a song title → track first
        return self._try_in_order(query, ["track", "artist", "playlist"])

    def _try_in_order(self, query: str, kinds: list[str]) -> str:
        for kind in kinds:
            result = self._search(kind, query)
            if result:
                uri, name = result
                return self._play_uri(uri, name, kind)
        log.warning("Spotify: no results for %r", query)
        return self._play_generic()

    def _play_specific_track(self, query: str, label: str) -> str:
        result = self._search("track", query)
        if result:
            return self._play_uri(result[0], result[1], "track")
        return self._smart_search_and_play(label)

    # ── Playback ──────────────────────────────────────────────────────────────

    def _play_generic(self) -> str:
        """Open Spotify and resume whatever was last playing."""
        try:
            subprocess.run(["open", "-a", "Spotify"], timeout=5)
            time.sleep(1.0)
            subprocess.run(
                ["osascript", "-e", 'tell application "Spotify" to play'],
                timeout=5,
            )
            return "Starting Spotify."
        except Exception as exc:
            log.error("Spotify open error: %s", exc)
            return "Couldn't start Spotify."

    def _play_uri(self, uri: str, label: str, kind: str = "track") -> str:
        """Play a Spotify URI using the most reliable method for each type."""
        try:
            if kind == "track":
                # AppleScript play track is the most reliable for instant playback
                subprocess.run(["open", "-a", "Spotify"], timeout=5)
                time.sleep(1.2)
                script = f'tell application "Spotify" to play track "{uri}"'
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=8)
                if r.returncode != 0:
                    # Fallback: open via URI scheme
                    subprocess.run(["open", uri], timeout=5)
            else:
                # Artist / playlist / album — open via URI scheme
                subprocess.run(["open", uri], timeout=5)

            return f"Playing {label}."
        except Exception as exc:
            log.error("Spotify play error: %s", exc)
            return f"Couldn't play {label}."

    # ── Search ────────────────────────────────────────────────────────────────

    def _search(self, kind: str, query: str) -> Optional[tuple[str, str]]:
        try:
            import requests
        except ImportError:
            return None

        token = self._get_token()
        if not token:
            return None

        try:
            resp = requests.get(
                _SEARCH_URL,
                params={"q": query, "type": kind, "limit": 3, "market": "US"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            resp.raise_for_status()
            items = resp.json().get(f"{kind}s", {}).get("items") or []
            # Filter out None entries (Spotify API can return nulls)
            items = [i for i in items if i]
            if not items:
                return None

            item = items[0]
            uri  = item.get("uri", "")
            name = item.get("name", query)

            if kind == "track":
                artists = ", ".join(
                    a["name"] for a in (item.get("artists") or []) if a
                )
                name = f"{name} by {artists}" if artists else name
            elif kind == "artist":
                # For artists, use their top tracks context URI for better playback
                uri = f"spotify:artist:{item['id']}"

            log.info("Spotify %s %r → %r", kind, query, name)
            return uri, name

        except Exception as exc:
            log.warning("Spotify search error (%s %r): %s", kind, query, exc)
            return None

    # ── Auth — Client Credentials ─────────────────────────────────────────────

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        try:
            import requests
            creds = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode()
            ).decode()
            resp = requests.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=8,
            )
            resp.raise_for_status()
            data               = resp.json()
            self._token        = data["access_token"]
            self._token_expiry = time.monotonic() + data.get("expires_in", 3600) - 30
            return self._token
        except Exception as exc:
            log.error("Spotify auth error: %s", exc)
            return ""
