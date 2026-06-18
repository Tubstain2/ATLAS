# Deprecated — replaced by brain.py (OpenRouter backend).
# This shim keeps any stale imports working.
from brain import Brain as ClaudeBrain  # noqa: F401

__all__ = ["ClaudeBrain"]
