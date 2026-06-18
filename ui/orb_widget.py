"""
ATLAS Orb Widget — the central animated visual core.

Renders a multi-layered glowing sphere that reacts to voice amplitude
and changes color between states. Uses QPainter at 60 fps; no OpenGL
dependency so it runs identically on macOS and Windows.

Public API (called by voice module):
    set_amplitude(float)   0.0–1.0 microphone level
    set_state(str)         'idle' | 'listening' | 'responding' | 'thinking'
"""

import math
import random
from enum import Enum
from typing import List

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import QTimer, Qt, QRectF
from PyQt6.QtGui import (
    QPainter, QColor, QRadialGradient, QPen, QBrush, QPainterPath,
)


class OrbState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    RESPONDING = "responding"
    THINKING = "thinking"


# Per-state color palettes — (r, g, b) tuples for fast interpolation
_PALETTES = {
    OrbState.IDLE: {
        "core_hi":  (130, 190, 255),
        "core_mid": (10,  90,  230),
        "core_lo":  (0,   25,  110),
        "glow":     (0,   55,  200, 70),
        "ring":     (0,   110, 255, 130),
        "particle": (90,  170, 255),
    },
    OrbState.LISTENING: {
        "core_hi":  (175, 220, 255),
        "core_mid": (10,  140, 255),
        "core_lo":  (0,   45,  165),
        "glow":     (0,   110, 255, 120),
        "ring":     (0,   170, 255, 165),
        "particle": (130, 210, 255),
    },
    OrbState.RESPONDING: {
        "core_hi":  (210, 255, 255),
        "core_mid": (0,   230, 245),
        "core_lo":  (0,   85,  130),
        "glow":     (0,   190, 225, 130),
        "ring":     (0,   230, 245, 165),
        "particle": (110, 245, 255),
    },
    OrbState.THINKING: {
        "core_hi":  (195, 175, 255),
        "core_mid": (90,  50,  210),
        "core_lo":  (35,  12,  105),
        "glow":     (90,  50,  210, 105),
        "ring":     (130, 90,  255, 145),
        "particle": (170, 150, 255),
    },
}


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _lerp_color(c1: tuple, c2: tuple, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    r = int(c1[0] + (c2[0] - c1[0]) * t)
    g = int(c1[1] + (c2[1] - c1[1]) * t)
    b = int(c1[2] + (c2[2] - c1[2]) * t)
    if len(c1) == 4 and len(c2) == 4:
        a = int(c1[3] + (c2[3] - c1[3]) * t)
        return QColor(r, g, b, a)
    return QColor(r, g, b)


# ── Particle ──────────────────────────────────────────────────────────────────

class _Particle:
    __slots__ = (
        "x", "y", "vx", "vy",
        "orbit_angle", "orbit_radius", "orbit_speed",
        "life", "decay", "size", "orbital",
    )

    def __init__(self, cx: float, cy: float, orb_r: float):
        angle = random.uniform(0.0, math.tau)
        dist  = orb_r * random.uniform(0.9, 1.7)

        self.orbit_angle  = angle
        self.orbit_radius = dist
        self.orbit_speed  = random.uniform(-0.009, 0.009)
        if abs(self.orbit_speed) < 0.002:
            self.orbit_speed = 0.002 * (1 if self.orbit_speed >= 0 else -1)

        self.x = cx + math.cos(angle) * dist
        self.y = cy + math.sin(angle) * dist

        da = random.uniform(0.0, math.tau)
        sp = random.uniform(0.1, 0.6)
        self.vx = math.cos(da) * sp
        self.vy = math.sin(da) * sp

        self.life    = random.uniform(0.35, 1.0)
        self.decay   = random.uniform(0.003, 0.013)
        self.size    = random.uniform(1.0, 3.2)
        self.orbital = random.random() > 0.28   # 72 % orbit, 28 % drift

    def update(self, cx: float, cy: float, amp: float) -> bool:
        if self.orbital:
            self.orbit_angle += self.orbit_speed * (1.0 + amp * 2.2)
            self.x = cx + math.cos(self.orbit_angle) * self.orbit_radius
            self.y = cy + math.sin(self.orbit_angle) * self.orbit_radius
        else:
            self.x += self.vx * (1.0 + amp * 0.8)
            self.y += self.vy * (1.0 + amp * 0.8)
        self.life -= self.decay
        return self.life > 0.0


# ── Energy ring ───────────────────────────────────────────────────────────────

class _Ring:
    def __init__(self, speed: float, phase: float):
        self.speed = speed
        self.phase = phase

    def eval(self, t: float, base_r: float, amp: float):
        """Return (radius, alpha) for the current time."""
        frac = ((t * self.speed + self.phase) % math.tau) / math.tau
        radius = base_r * (1.1 + frac * 0.65 * (1.0 + amp * 0.55))
        alpha  = max(0.0, 1.0 - frac) * (0.28 + amp * 0.42)
        return radius, alpha


# ── OrbWidget ─────────────────────────────────────────────────────────────────

class OrbWidget(QWidget):
    """Central animated orb."""

    _WAVE_SEGS = 128
    _MAX_PARTICLES = 82

    def __init__(self, orb_radius: int = 170, parent=None):
        super().__init__(parent)
        self._base_r = float(orb_radius)

        self._state        = OrbState.IDLE
        self._amp          = 0.0
        self._target_amp   = 0.0
        self._t            = 0.0       # running seconds
        self._pulse        = 0.0       # sin result cached each tick

        self._src_pal      = _PALETTES[OrbState.IDLE]
        self._dst_pal      = _PALETTES[OrbState.IDLE]
        self._pal_t        = 1.0

        self._particles: List[_Particle] = []
        self._rings = [
            _Ring(0.42, 0.0),
            _Ring(0.42, math.pi * 0.5),
            _Ring(0.42, math.pi),
            _Ring(0.42, math.pi * 1.5),
        ]

        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setStyleSheet("background: transparent;")

        self._timer = QTimer(self)
        self._timer.setInterval(16)        # ≈60 fps
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ── Public API ───────────────────────────────────────────────────────────

    def set_amplitude(self, value: float):
        self._target_amp = max(0.0, min(1.0, value))

    def set_state(self, state: str):
        try:
            new = OrbState(state)
        except ValueError:
            new = OrbState.IDLE
        if new != self._state:
            self._src_pal = self._current_palette()
            self._dst_pal = _PALETTES[new]
            self._pal_t   = 0.0
            self._state   = new

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _current_palette(self) -> dict:
        """Return interpolated palette at current _pal_t."""
        if self._pal_t >= 1.0:
            return dict(self._dst_pal)
        result = {}
        for k in self._dst_pal:
            result[k] = tuple(
                _lerp(self._src_pal[k][i], self._dst_pal[k][i], self._pal_t)
                for i in range(len(self._dst_pal[k]))
            )
        return result

    def _gc(self, key: str) -> QColor:
        """Get a color from the interpolated palette."""
        pal = self._current_palette()
        c = pal[key]
        if len(c) == 4:
            return QColor(int(c[0]), int(c[1]), int(c[2]), int(c[3]))
        return QColor(int(c[0]), int(c[1]), int(c[2]))

    def _tick(self):
        if self.width() < 4 or self.height() < 4:
            return

        self._t += 0.016
        self._pulse = math.sin(self._t * (math.tau / 3.0)) * 0.5 + 0.5

        # Smooth amplitude
        self._amp += (self._target_amp - self._amp) * 0.14

        # Advance palette blend
        if self._pal_t < 1.0:
            self._pal_t = min(1.0, self._pal_t + 0.032)

        cx = self.width()  / 2.0
        cy = self.height() / 2.0
        r  = self._effective_r()

        # Particle population
        want = max(18, int(self._MAX_PARTICLES * (0.38 + self._amp * 0.62)))
        self._particles = [p for p in self._particles if p.update(cx, cy, self._amp)]
        while len(self._particles) < want:
            self._particles.append(_Particle(cx, cy, r))

        self.update()

    def _effective_r(self) -> float:
        """Orb radius: clamp to widget size and add pulse/amp expansion."""
        cap = min(self.width(), self.height()) * 0.26
        r   = min(self._base_r, cap)
        return r * (1.0 + 0.038 * self._pulse + 0.13 * self._amp)

    # ── Paint ────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w  = float(self.width())
        h  = float(self.height())
        cx = w / 2.0
        cy = h / 2.0
        r  = self._effective_r()
        p  = self._pulse
        a  = self._amp

        self._draw_deep_glow(painter, cx, cy, r, p, a)
        self._draw_rings(painter, cx, cy, r, p, a)
        if a > 0.025:
            self._draw_waveform_ring(painter, cx, cy, r, a)
        self._draw_particles(painter)
        self._draw_corona(painter, cx, cy, r, p, a)
        self._draw_sphere(painter, cx, cy, r, a)
        self._draw_specular(painter, cx, cy, r, a)
        self._draw_scan_line(painter, cx, cy, r)

        painter.end()

    def _draw_deep_glow(self, painter, cx, cy, r, p, a):
        gr = r * (2.6 + 0.35 * p + a * 0.55)
        g  = QRadialGradient(cx, cy, gr)

        gc = self._gc("glow")
        base_a = gc.alpha()

        c0 = QColor(gc); c0.setAlpha(min(255, int(base_a * (0.65 + 0.35 * p + a * 0.35))))
        c1 = QColor(gc); c1.setAlpha(int(base_a * 0.28))
        c2 = QColor(gc); c2.setAlpha(0)

        g.setColorAt(0.0, c0)
        g.setColorAt(0.5, c1)
        g.setColorAt(1.0, c2)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(g))
        painter.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))

    def _draw_rings(self, painter, cx, cy, r, p, a):
        rc = self._gc("ring")
        for ring in self._rings:
            radius, alpha = ring.eval(self._t, r, a)
            if alpha < 0.01:
                continue
            c = QColor(rc)
            c.setAlphaF(alpha * (0.5 + p * 0.3))
            pen = QPen(c, 1.4)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))
        painter.setPen(Qt.PenStyle.NoPen)

    def _draw_waveform_ring(self, painter, cx, cy, r, a):
        base = r * 1.28
        scale = a * 32.0
        t = self._t

        rc = self._gc("ring")
        c  = QColor(rc)
        c.setAlphaF(0.38 + a * 0.44)
        pen = QPen(c, 1.0 + a * 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        n = self._WAVE_SEGS
        path = QPainterPath()
        for i in range(n + 1):
            angle = (i / n) * math.tau
            dist = (
                math.sin(6 * angle + t * 4.2) * 0.50 +
                math.sin(3 * angle - t * 2.6) * 0.32 +
                math.sin(13 * angle + t * 6.1) * 0.18
            ) * scale
            rr = base + dist
            x  = cx + math.cos(angle) * rr
            y  = cy + math.sin(angle) * rr
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
        painter.drawPath(path)

    def _draw_particles(self, painter):
        pc = self._gc("particle")
        painter.setPen(Qt.PenStyle.NoPen)
        a = self._amp
        for p in self._particles:
            alpha = p.life * (0.72 + a * 0.28)
            c = QColor(pc)
            c.setAlphaF(min(1.0, alpha))
            painter.setBrush(QBrush(c))
            sz = p.size * (0.75 + p.life * 0.45 + a * 0.55)
            painter.drawEllipse(QRectF(p.x - sz / 2, p.y - sz / 2, sz, sz))

    def _draw_corona(self, painter, cx, cy, r, p, a):
        cr = r * (1.18 + 0.055 * p)
        g  = QRadialGradient(cx, cy, cr)
        gc = self._gc("glow")
        rc = self._gc("ring")

        c_in  = QColor(gc); c_in.setAlpha(min(255, int(185 * (0.48 + 0.52 * p + a * 0.45))))
        c_mid = QColor(rc); c_mid.setAlpha(90)
        c_edg = QColor(rc); c_edg.setAlpha(min(255, int(135 * (0.42 + 0.58 * p + a * 0.55))))
        c_out = QColor(rc); c_out.setAlpha(0)

        g.setColorAt(0.00, c_in)
        g.setColorAt(0.65, c_mid)
        g.setColorAt(0.84, c_edg)
        g.setColorAt(1.00, c_out)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(g))
        painter.drawEllipse(QRectF(cx - cr, cy - cr, cr * 2, cr * 2))

    def _draw_sphere(self, painter, cx, cy, r, a):
        ox = cx - r * 0.32
        oy = cy - r * 0.32
        g  = QRadialGradient(ox, oy, r * 1.25)

        hi  = self._gc("core_hi")
        mid = self._gc("core_mid")
        lo  = self._gc("core_lo")

        def bright(c: QColor, f: float) -> QColor:
            return QColor(
                min(255, int(c.red()   * (1.0 + f))),
                min(255, int(c.green() * (1.0 + f))),
                min(255, int(c.blue()  * (1.0 + f))),
            )

        g.setColorAt(0.00, bright(hi,  a * 0.32))
        g.setColorAt(0.38, bright(mid, a * 0.22))
        g.setColorAt(0.72, mid)
        g.setColorAt(1.00, lo)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(g))
        painter.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

    def _draw_specular(self, painter, cx, cy, r, a):
        sx = cx - r * 0.30
        sy = cy - r * 0.30
        sr = r  * 0.46

        g = QRadialGradient(sx, sy, sr)
        g.setColorAt(0.0, QColor(255, 255, 255, min(255, int(68 + a * 45))))
        g.setColorAt(0.5, QColor(255, 255, 255, min(255, int(22 + a * 22))))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(g))
        painter.drawEllipse(QRectF(sx - sr, sy - sr, sr * 2, sr * 2))

    def _draw_scan_line(self, painter, cx, cy, r):
        """Horizontal scan line that sweeps through the sphere."""
        frac    = (self._t % 4.0) / 4.0
        y_off   = (frac * 2.0 - 1.0) * r
        y       = cy + y_off
        half_w  = math.sqrt(max(0.0, r * r - y_off * y_off))
        if half_w < 1.0:
            return

        alpha = int(42 * (1.0 - abs(y_off) / r))
        mid   = self._gc("core_mid")
        pen   = QPen(QColor(mid.red(), mid.green(), mid.blue(), alpha), 1)
        painter.setPen(pen)
        painter.drawLine(int(cx - half_w), int(y), int(cx + half_w), int(y))
