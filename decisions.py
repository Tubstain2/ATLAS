"""ATLAS Decision Confidence Engine — scores autonomous actions 0.0-1.0."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CLARITY       = {"clear": 0.3,  "mostly_clear": 0.15, "ambiguous": 0.0}
REVERSIBILITY = {"full":  0.3,  "mostly": 0.2,        "partial": 0.1, "none": 0.0}
PRECEDENT     = {"exact": 0.2,  "similar": 0.1,       "novel": 0.0}
RISK          = {"minimal": 0.2, "low": 0.1,           "medium": 0.05, "high": 0.0}

ASK_THRESHOLD = 0.6
ACT_REPORT    = 0.85


class ConfidenceEngine:
    def __init__(self, config: dict, safety_layer=None):
        self._ask_t  = float(config.get("decision_ask_threshold",         ASK_THRESHOLD))
        self._act_t  = float(config.get("decision_act_report_threshold",  ACT_REPORT))
        self._prec_path: Optional[Path] = None
        self._precedents: dict[str, int] = {}
        log.info("ConfidenceEngine: ready (ask<%.2f, report<%.2f).", self._ask_t, self._act_t)

    def load_precedents(self, atlas_root: str = ".") -> None:
        self._prec_path = Path(atlas_root) / "ATLAS" / "Playbook" / "decision_thresholds.json"
        try:
            if self._prec_path.exists():
                self._precedents = json.loads(self._prec_path.read_text(encoding="utf-8"))
        except Exception:
            self._precedents = {}

    def score(self, clarity: str = "clear", reversible: str = "full",
              precedent: str = "novel", risk: str = "low") -> float:
        s = (CLARITY.get(clarity, 0.0)       + REVERSIBILITY.get(reversible, 0.0) +
             PRECEDENT.get(precedent, 0.0)   + RISK.get(risk, 0.0))
        return round(min(1.0, max(0.0, s)), 4)

    def decision(self, score: float) -> str:
        if score < self._ask_t:
            return "ask"
        if score < self._act_t:
            return "act_report"
        return "act_silent"

    def record_outcome(self, action_type: str, confirmed: bool) -> None:
        self._precedents[action_type] = self._precedents.get(action_type, 0) + (1 if confirmed else -1)
        if self._prec_path:
            try:
                self._prec_path.parent.mkdir(parents=True, exist_ok=True)
                self._prec_path.write_text(
                    json.dumps(self._precedents, indent=2), encoding="utf-8"
                )
            except Exception:
                pass

    def handle(self, text: str) -> Optional[str]:
        return None


if __name__ == "__main__":
    ce = ConfidenceEngine({}, None)
    s = ce.score("clear", "full", "exact", "minimal")
    assert s == 1.0, s
    s2 = ce.score("ambiguous", "none", "novel", "high")
    assert s2 == 0.0, s2
    print("decisions: ok")
