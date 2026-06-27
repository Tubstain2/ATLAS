"""ATLAS Resource Manager — M3 16GB adaptive throttling."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import psutil

log = logging.getLogger(__name__)

_MAX_AGENTS = {"PERFORMANCE": 4, "BALANCED": 2, "POWER_SAVE": 1, "CRITICAL": 0}
_USE_MLX    = {"PERFORMANCE": True, "BALANCED": True, "POWER_SAVE": False, "CRITICAL": False}


class ResourceManager:
    def __init__(self, config: dict, speak_cb: Callable):
        self._speak            = speak_cb
        self._mode             = "PERFORMANCE"
        self._last_mode        = ""
        self._bat_balanced     = int(config.get("battery_balanced_threshold",  30))
        self._bat_powersave    = int(config.get("battery_powersave_threshold", 10))

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def max_parallel_agents(self) -> int:
        return _MAX_AGENTS[self._mode]

    @property
    def use_mlx(self) -> bool:
        return _USE_MLX[self._mode]

    def start(self) -> None:
        threading.Thread(target=self._monitor_loop, daemon=True,
                         name="atlas-resources").start()
        log.info("ResourceManager: started (M3 16GB mode).")

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(30)
            try:
                self._update_mode()
            except Exception as exc:
                log.debug("ResourceManager: %s", exc)

    def _update_mode(self) -> None:
        bat     = psutil.sensors_battery()
        pct     = int(bat.percent)    if bat else 100
        plugged = bat.power_plugged   if bat else True

        if pct <= 10 and not plugged:
            new_mode = "CRITICAL"
        elif pct <= self._bat_powersave and not plugged:
            new_mode = "POWER_SAVE"
        elif pct <= self._bat_balanced and not plugged:
            new_mode = "BALANCED"
        else:
            new_mode = "PERFORMANCE"

        if new_mode != self._last_mode:
            self._mode      = new_mode
            self._last_mode = new_mode
            self._on_mode_change(new_mode, pct)

    def _on_mode_change(self, mode: str, pct: int) -> None:
        msgs = {
            "CRITICAL":   "Boss battery critical. Background systems suspended. Voice only mode active.",
            "POWER_SAVE": f"Boss battery below {self._bat_powersave} percent. Switching to power save mode.",
            "BALANCED":   "Boss on battery. Balanced mode active.",
        }
        if mode in msgs:
            self._speak(msgs[mode])
        log.info("ResourceManager: mode → %s (battery %d%%).", mode, pct)

    def get_status(self) -> dict:
        bat = psutil.sensors_battery()
        return {
            "mode":             self._mode,
            "battery_pct":      int(bat.percent)       if bat else 100,
            "plugged":          bat.power_plugged       if bat else True,
            "cpu_pct":          psutil.cpu_percent(),
            "ram_available_gb": round(psutil.virtual_memory().available / 1e9, 1),
            "max_agents":       self.max_parallel_agents,
        }

    def handle(self, text: str) -> Optional[str]:
        return None


if __name__ == "__main__":
    rm = ResourceManager({}, speak_cb=print)
    status = rm.get_status()
    assert "mode" in status
    print(f"resources: ok — mode={status['mode']}, ram={status['ram_available_gb']:.1f}GB free")
