"""
ATLAS 3D Holographic Visualization Module.

Handles voice command routing, data preparation, and the Python→JS bridge.
All 3D rendering happens in Three.js inside ui/atlas_ui.html.

Voice commands:
  "ATLAS show hologram"               → default 3D orb scene
  "ATLAS close hologram"              → dismiss
  "ATLAS fullscreen hologram"         → state 3 (full panel)
  "ATLAS normal view"                 → return to state 1
  "ATLAS show AAPL in hologram"       → stock bar chart
  "ATLAS show stock chart"            → stock chart (last ticker)
  "ATLAS show knowledge graph"        → Obsidian vault note graph
  "ATLAS visualize your architecture" → ATLAS module diagram
  "ATLAS show world map"              → rotating 3D globe
  "ATLAS visualize my codebase"       → Python file treemap
  "ATLAS show DNA helix"              → animated double helix
  "ATLAS show solar system"           → orbiting planets
  "ATLAS show a brain"                → neural network
  "ATLAS show a city"                 → procedural skyline
  "ATLAS show the atom"               → atomic orbital model
  "ATLAS show my network"             → network topology
  "ATLAS build me a 3D model of X"    → AI-generated geometry
  "ATLAS make it bigger / smaller"    → scale current viz
  "ATLAS show wireframe"              → wireframe mode toggle
  "ATLAS reset view"                  → reset camera
  "ATLAS rotate it"                   → auto-rotate toggle
  "ATLAS screenshot hologram"         → capture canvas
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


class ATLASHologram:
    """3D holographic visualization system — voice commands + data bridge."""

    def __init__(self, config: dict, speak_cb: Callable,
                 brain, vault_brain=None, window=None, market_mod=None):
        self._config       = config
        self._speak        = speak_cb
        self._brain        = brain
        self._vault_brain  = vault_brain
        self._window       = window
        self._market       = market_mod
        self._enabled      = config.get("hologram_enabled", True)
        self._active       = False
        self._current_viz  = ""
        self._last_ticker  = "AAPL"

        log.info("ATLASHologram: ready (enabled=%s).", self._enabled)

    def set_window(self, window) -> None:
        self._window = window

    def set_market(self, market) -> None:
        self._market = market

    # ── JS bridge ─────────────────────────────────────────────────────────────

    def _js(self, code: str) -> None:
        if self._window:
            self._window._js(code)

    # ── Voice router ──────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        if not self._enabled:
            return None
        lower = text.lower().strip()
        lc = re.sub(r"^atlas\s+", "", lower)

        # Show / dismiss / fullscreen
        if any(p in lc for p in ("show hologram", "open hologram", "activate hologram",
                                  "bring up hologram", "hologram on")):
            return self._show_default()

        if any(p in lc for p in ("close hologram", "hide hologram", "dismiss hologram",
                                  "clear hologram", "hologram off")):
            return self._hide()

        if "normal view" in lc and self._active:
            return self._hide()

        if any(p in lc for p in ("fullscreen hologram", "hologram fullscreen",
                                  "full screen hologram")):
            return self._fullscreen()

        # Stock chart
        m = re.search(r"show (.{1,8}?) (?:in hologram|stock chart in hologram)", lc)
        if m:
            ticker = m.group(1).strip().upper()
            if re.match(r'^[A-Z]{1,5}$', ticker):
                return self._show_stock(ticker)

        if re.search(r"show stock chart|hologram.*stock|stock.*hologram", lc):
            return self._show_stock(self._last_ticker)

        # Knowledge graph
        if any(p in lc for p in ("show knowledge graph", "knowledge graph",
                                  "show my knowledge", "obsidian graph in hologram")):
            return self._show_knowledge_graph()

        # Architecture
        if any(p in lc for p in ("visualize your architecture", "show your architecture",
                                  "atlas architecture", "show architecture")):
            return self._show_architecture()

        # Globe
        if any(p in lc for p in ("show world map", "show globe", "world map in hologram",
                                  "globe in hologram")):
            return self._show_globe()

        # Code treemap
        if any(p in lc for p in ("visualize my codebase", "show codebase",
                                  "code treemap", "show my code")):
            return self._show_treemap()

        # Quick models
        if any(p in lc for p in ("show dna helix", "dna helix", "show dna")):
            return self._show_quick("dna", "DNA double helix")
        if any(p in lc for p in ("show solar system", "solar system")):
            return self._show_quick("solar", "solar system")
        if any(p in lc for p in ("show a brain", "show brain", "neural network")):
            return self._show_quick("brain", "neural network")
        if any(p in lc for p in ("show a city", "show city", "city skyline")):
            return self._show_quick("city", "city skyline")
        if any(p in lc for p in ("show the atom", "show atom", "atomic orbital")):
            return self._show_quick("atom", "atomic orbital model")
        if any(p in lc for p in ("show my network", "network topology", "show network")):
            return self._show_quick("network", "network topology")

        # AI-generated model
        m = re.search(r"(?:build|make|create|generate) (?:me )?a? ?3d model of (.+?)$", lc)
        if m:
            return self._generate_model(m.group(1).strip())

        # Manipulation (only when hologram is active)
        if self._active:
            if "make it bigger" in lc:
                self._js("hologramScale(1.35)")
                return "Scaling up, Boss."
            if "make it smaller" in lc:
                self._js("hologramScale(0.75)")
                return "Scaling down, Boss."
            if "show wireframe" in lc:
                self._js("hologramWireframe(true)")
                return "Wireframe mode, Boss."
            if "solid mode" in lc or "hide wireframe" in lc:
                self._js("hologramWireframe(false)")
                return "Solid mode, Boss."
            if "reset view" in lc:
                self._js("hologramResetView()")
                return "View reset, Boss."
            if "rotate it" in lc or "auto rotate" in lc:
                self._js("hologramAutoRotate(true)")
                return "Auto-rotating, Boss."
            if "stop rotating" in lc:
                self._js("hologramAutoRotate(false)")
                return "Rotation stopped, Boss."
            if "screenshot hologram" in lc:
                self._js("hologramScreenshot()")
                return "Screenshot taken, Boss."
            if "show full chat" in lc:
                self._js("hologramShowChat()")
                return None

        return None

    # ── Scene commands ────────────────────────────────────────────────────────

    def _show_default(self) -> str:
        self._active = True
        self._current_viz = "default"
        self._js("showHologram('default', {})")
        return "Bringing up the hologram, Boss."

    def _hide(self) -> str:
        self._active = False
        self._current_viz = ""
        self._js("hideHologram()")
        return "Hologram closed, Boss."

    def _fullscreen(self) -> str:
        self._active = True
        self._js("hologramFullscreen()")
        return "Fullscreen hologram, Boss."

    # ── Stock chart ───────────────────────────────────────────────────────────

    def _show_stock(self, ticker: str) -> str:
        self._active = True
        self._current_viz = "stock"
        self._last_ticker = ticker
        seed_data = json.dumps({"ticker": ticker, "bars": self._dummy_bars(ticker)})
        self._js(f"showHologram('stock', {seed_data})")
        threading.Thread(target=self._fetch_stock_async, args=(ticker,),
                         daemon=True, name="holo-stock").start()
        return f"Bringing up {ticker} chart in hologram, Boss."

    def _fetch_stock_async(self, ticker: str) -> None:
        try:
            bars = None
            if self._market and hasattr(self._market, "get_candles"):
                candles = self._market.get_candles(ticker)
                if candles:
                    bars = [{"t": c.get("t", 0), "c": float(c.get("c", 0)),
                             "o": float(c.get("o", 0))} for c in candles[-30:]]
            if not bars:
                bars = self._dummy_bars(ticker)
            data = json.dumps({"ticker": ticker, "bars": bars})
            self._js(f"hologramUpdateData({data})")
        except Exception as exc:
            log.debug("Hologram: stock fetch: %s", exc)

    def _dummy_bars(self, ticker: str) -> list:
        import random, time as _time
        random.seed(hash(ticker) % 999)
        base = 150.0
        bars, t = [], int(_time.time()) - 30 * 86400
        for i in range(30):
            base += random.uniform(-4, 4)
            bars.append({"t": t + i * 86400, "c": round(max(1, base), 2),
                         "o": round(max(1, base - random.uniform(-2, 2)), 2)})
        return bars

    # ── Knowledge graph ───────────────────────────────────────────────────────

    def _show_knowledge_graph(self) -> str:
        self._active = True
        self._current_viz = "knowledge"
        self._js("showHologram('knowledge', {'nodes':[],'edges':[]})")
        threading.Thread(target=self._build_knowledge_async,
                         daemon=True, name="holo-knowledge").start()
        return "Building your knowledge graph in hologram, Boss."

    def _build_knowledge_async(self) -> None:
        nodes, edges = [], []
        try:
            if self._vault_brain:
                vault_root = self._vault_brain.atlas.parent
                md_files = list(vault_root.rglob("*.md"))[:80]
                link_re = re.compile(r"\[\[([^\]|#\n]+)")
                node_map: dict[str, int] = {}
                for i, p in enumerate(md_files):
                    cat = self._note_category(str(p))
                    node_map[p.stem] = i
                    size = min(1.8, max(0.3, p.stat().st_size / 2500))
                    nodes.append({"id": i, "label": p.stem[:22],
                                  "category": cat, "size": size})
                for p in md_files:
                    try:
                        text = p.read_text(encoding="utf-8", errors="ignore")
                        for m in link_re.finditer(text):
                            tgt = m.group(1).strip()
                            if tgt in node_map and p.stem in node_map:
                                edges.append({"s": node_map[p.stem],
                                              "t": node_map[tgt]})
                    except Exception:
                        pass
        except Exception as exc:
            log.debug("Hologram: knowledge graph: %s", exc)
        data = json.dumps({"nodes": nodes[:80], "edges": edges[:200]})
        self._js(f"hologramUpdateData({data})")

    @staticmethod
    def _note_category(path: str) -> str:
        p = path.lower()
        if "skill" in p:    return "skill"
        if "memory" in p:   return "memory"
        if "research" in p: return "research"
        if "playbook" in p: return "playbook"
        if "coaching" in p: return "coaching"
        if "daily" in p:    return "daily"
        return "note"

    # ── Architecture diagram ──────────────────────────────────────────────────

    def _show_architecture(self) -> str:
        self._active = True
        self._current_viz = "architecture"
        mods = [
            {"id": 0,  "label": "voice",       "x": 0,    "y": 0,   "ring": 0},
            {"id": 1,  "label": "brain",        "x": 2.5,  "y": 0,   "ring": 1},
            {"id": 2,  "label": "core",         "x": -2.5, "y": 0,   "ring": 1},
            {"id": 3,  "label": "obsidian",     "x": 0,    "y": 2.5, "ring": 1},
            {"id": 4,  "label": "market",       "x": 0,    "y": -2.5,"ring": 1},
            {"id": 5,  "label": "chrome",       "x": 1.8,  "y": 1.8, "ring": 2},
            {"id": 6,  "label": "smart_card",   "x": -1.8, "y": 1.8, "ring": 2},
            {"id": 7,  "label": "research",     "x": -1.8, "y": -1.8,"ring": 2},
            {"id": 8,  "label": "debate",       "x": 1.8,  "y": -1.8,"ring": 2},
            {"id": 9,  "label": "tutor",        "x": 3.5,  "y": 1.5, "ring": 2},
            {"id": 10, "label": "coach",        "x": 3.5,  "y": -1.5,"ring": 2},
            {"id": 11, "label": "recorder",     "x": -3.5, "y": 1.5, "ring": 2},
            {"id": 12, "label": "planner",      "x": -3.5, "y": -1.5,"ring": 2},
        ]
        conns = [(0,1),(0,2),(0,3),(0,4),(1,5),(1,6),(1,7),(1,8),
                 (1,9),(1,10),(2,11),(2,12),(3,6),(4,5)]
        data = json.dumps({"modules": mods, "connections": conns})
        self._js(f"showHologram('architecture', {data})")
        return "Visualizing ATLAS architecture in hologram, Boss."

    # ── Globe, treemap, quick models ──────────────────────────────────────────

    def _show_globe(self) -> str:
        self._active = True
        self._current_viz = "globe"
        self._js("showHologram('globe', {})")
        return "Bringing up the world map in hologram, Boss."

    def _show_treemap(self) -> str:
        self._active = True
        self._current_viz = "treemap"
        files = []
        try:
            base = Path(__file__).parent
            for p in base.glob("*.py"):
                try:
                    lines = p.read_text(encoding="utf-8", errors="ignore").count("\n")
                    files.append({"name": p.stem[:16], "lines": lines,
                                  "mtime": p.stat().st_mtime})
                except Exception:
                    pass
            files.sort(key=lambda f: f["lines"], reverse=True)
        except Exception:
            pass
        data = json.dumps({"files": files[:28]})
        self._js(f"showHologram('treemap', {data})")
        return "Visualizing your codebase in hologram, Boss."

    def _show_quick(self, model_type: str, label: str) -> str:
        self._active = True
        self._current_viz = model_type
        self._js(f"showHologram('quick', {json.dumps({'model': model_type})})")
        return f"Rendering {label} in hologram, Boss."

    # ── AI-generated model ────────────────────────────────────────────────────

    def _generate_model(self, subject: str) -> str:
        self._active = True
        self._current_viz = "generated"
        self._js(f"showHologram('loading', {json.dumps({'label': subject})})")
        threading.Thread(target=self._generate_async, args=(subject,),
                         daemon=True, name="holo-gen").start()
        return f"Generating a 3D model of {subject}, Boss. One moment."

    def _generate_async(self, subject: str) -> None:
        prompt = (
            f"Write Three.js r163 code to build a 3D model of: {subject}\n\n"
            "Rules:\n"
            "- Use ONLY built-in Three.js geometry: BoxGeometry, SphereGeometry, "
            "CylinderGeometry, TorusGeometry, ConeGeometry, TorusKnotGeometry, "
            "OctahedronGeometry, IcosahedronGeometry.\n"
            "- Use THREE.Group to combine parts.\n"
            "- Return ONLY a self-contained function named buildMesh() that takes "
            "no arguments and returns a THREE.Group.\n"
            "- Do NOT include imports, THREE declarations, or renderer code.\n"
            "- Keep it under 35 lines.\n"
            "Example output:\n"
            "function buildMesh() {\n"
            "  const g = new THREE.Group();\n"
            "  const body = new THREE.Mesh(new THREE.SphereGeometry(1,16,16), new THREE.MeshBasicMaterial());\n"
            "  g.add(body);\n"
            "  return g;\n"
            "}"
        )
        try:
            code = self._brain.ask(prompt)
            m = re.search(r'(function buildMesh\(\)\s*\{.*?\})', code, re.DOTALL)
            fn_code = m.group(1).strip() if m else code.strip()
            self._js(f"hologramBuildMesh({json.dumps(fn_code)})")
        except Exception as exc:
            log.error("Hologram: model generation failed: %s", exc)
            self._js("showHologram('quick', {'model':'atom'})")
            self._speak("Model generation ran into an issue, Boss. Showing a placeholder.")

    # ── Live data push ────────────────────────────────────────────────────────

    def push_amplitude(self, value: float) -> None:
        """Called by main.py amplitude signal when hologram is active."""
        if self._active:
            self._js(f"hologramAmplitude({value:.4f})")

    def push_state(self, state: str) -> None:
        """Called by main.py state changes when hologram is active."""
        if self._active:
            self._js(f"hologramState({json.dumps(state)})")
