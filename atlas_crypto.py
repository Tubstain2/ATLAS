"""
ATLAS Encryption Helper — Fernet symmetric encryption for memory files.

Key stored in macOS Keychain via keyring (service="ATLAS", username="memory_key").
On first run macOS will show a one-time Keychain access dialog — click Allow.

Usage:
    from atlas_crypto import CryptoLayer
    crypto = CryptoLayer(config)
    encrypted_bytes = crypto.encrypt(plaintext_str)
    plaintext_str   = crypto.decrypt(encrypted_bytes)
    # or just use encrypt_file / decrypt_file which handle path I/O
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SERVICE  = "ATLAS"
_KEY_USER = "memory_key"


class CryptoLayer:
    """
    Fernet symmetric encryption for ATLAS memory files.
    Key is auto-generated on first run and stored in macOS Keychain.
    If encryption is disabled (memory_encryption_enabled: false), all
    methods are no-ops and data passes through unchanged.
    """

    def __init__(self, config: dict):
        self._enabled = bool(config.get("memory_encryption_enabled", True))
        self._fernet  = None

        if not self._enabled:
            log.info("CryptoLayer: encryption disabled — memory files stored in plaintext.")
            return

        try:
            self._fernet = self._load_or_create_key()
            log.info("CryptoLayer: encryption ready.")
        except Exception as exc:
            log.warning("CryptoLayer: could not initialise encryption (%s). "
                        "Falling back to plaintext.", exc)
            self._fernet  = None
            self._enabled = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt a UTF-8 string. Returns raw bytes."""
        if not self._fernet:
            return plaintext.encode("utf-8")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, data: bytes) -> str:
        """Decrypt bytes back to a UTF-8 string."""
        if not self._fernet:
            return data.decode("utf-8") if isinstance(data, bytes) else data
        try:
            return self._fernet.decrypt(data).decode("utf-8")
        except Exception:
            # Fallback: treat as unencrypted (handles transition from plaintext)
            return data.decode("utf-8") if isinstance(data, bytes) else data

    def encrypt_file(self, path: Path, plaintext: str) -> None:
        """Write encrypted content to path."""
        if not self._fernet:
            path.write_text(plaintext, encoding="utf-8")
            return
        path.write_bytes(self.encrypt(plaintext))

    def decrypt_file(self, path: Path) -> str:
        """Read and decrypt content from path."""
        if not path.exists():
            return ""
        if not self._fernet:
            return path.read_text(encoding="utf-8")
        try:
            return self.decrypt(path.read_bytes())
        except Exception:
            # File was written as plaintext before encryption was enabled
            return path.read_text(encoding="utf-8", errors="replace")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Key management ─────────────────────────────────────────────────────────

    def _load_or_create_key(self):
        """Load key from Keychain, or generate + store a new one."""
        from cryptography.fernet import Fernet
        import keyring

        stored = keyring.get_password(_SERVICE, _KEY_USER)
        if stored:
            return Fernet(stored.encode())

        # Generate new key and store it
        key = Fernet.generate_key()
        keyring.set_password(_SERVICE, _KEY_USER, key.decode())
        log.info("CryptoLayer: new encryption key generated and stored in Keychain.")
        return Fernet(key)
