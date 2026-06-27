"""ATLAS Safety Layer — blocks autonomous actions that must never run without Boss confirmation."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BLOCKED_TYPES: frozenset = frozenset({
    "send_email", "delete_file_permanent", "purchase", "post_social", "sudo",
    "share_external", "modify_system_settings", "access_passwords",
})

_HEADER = "| Timestamp | Agent | Action | Confidence | Confirmed | Outcome |\n|---|---|---|---|---|---|\n"


class SafetyLayer:
    def __init__(self, config: dict, atlas_root: str = "."):
        self._log_path = Path(atlas_root) / "ATLAS" / "Safety" / "audit_log.md"
        log.info("SafetyLayer: ready (%d blocked action types).", len(BLOCKED_TYPES))

    def check(self, action_type: str, reversible: bool = True,
              risk: str = "low") -> tuple[bool, str]:
        if action_type in BLOCKED_TYPES:
            return False, f"blocked: {action_type}"
        return True, "ok"

    def log_action(self, agent: str, action: str, action_type: str,
                   confidence: float, confirmed: bool, outcome: str) -> None:
        threading.Thread(
            target=self._write_log,
            args=(agent, action, action_type, confidence, confirmed, outcome),
            daemon=True, name="atlas-safety-log",
        ).start()

    def _write_log(self, agent: str, action: str, action_type: str,
                   confidence: float, confirmed: bool, outcome: str) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            ts   = time.strftime("%Y-%m-%d %H:%M:%S")
            line = (f"| {ts} | {agent} | {action[:60]} | {confidence:.2f} | "
                    f"{'yes' if confirmed else 'no'} | {outcome} |\n")
            if not self._log_path.exists():
                self._log_path.write_text(_HEADER, encoding="utf-8")
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception as exc:
            log.debug("SafetyLayer: log_action failed: %s", exc)

    def handle(self, text: str) -> Optional[str]:
        lc = text.lower().strip()
        if any(p in lc for p in ("atlas show audit log", "atlas what did you do today")):
            try:
                lines = self._log_path.read_text(encoding="utf-8").splitlines()
                data  = [l for l in lines
                         if l.startswith("|") and "---" not in l and "Timestamp" not in l]
                if not data:
                    return "No actions logged yet, Boss."
                return "Recent actions: " + " | ".join(data[-5:])
            except Exception:
                return "No audit log found yet, Boss."
        return None


if __name__ == "__main__":
    sl = SafetyLayer({}, atlas_root="/tmp")
    assert sl.check("read_file") == (True, "ok")
    assert sl.check("send_email")[0] is False
    print("safety: ok")
