#!/usr/bin/env python3
"""
Build ATLAS.app — macOS application bundle with custom holographic icon.
Run once:  python3 build_app.py
Then drag ATLAS.app from ~/Desktop to your Dock or Applications.
"""

import math
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
ATLAS_ROOT  = Path(__file__).resolve().parent
APP_NAME    = "ATLAS"
DESKTOP     = Path.home() / "Desktop"
APP_PATH    = DESKTOP / f"{APP_NAME}.app"
ICONSET_DIR = ATLAS_ROOT / "build" / "ATLAS.iconset"
ICNS_PATH   = ATLAS_ROOT / "build" / "ATLAS.icns"
PYTHON      = sys.executable   # whatever python3 ran this script

# ── Icon drawing ───────────────────────────────────────────────────────────────

def draw_icon(size: int):
    """
    Return a PIL Image of the ATLAS icon at `size` × `size` pixels.

    Design:
      • Deep space background with macOS rounded corners
      • Outer radial bloom halo
      • Three orbital rings at different tilts
      • Particle dots scattered on rings
      • Central holographic orb (sphere gradient + specular)
      • "ATLAS" wordmark at bottom
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    S  = size
    C  = S // 2                      # centre
    R  = int(S * 0.31)               # orb radius
    corner_r = int(S * 0.22)         # macOS icon corner radius

    # ── Background ────────────────────────────────────────────────────────────
    img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)

    # Rounded rectangle fill (dark navy)
    draw.rounded_rectangle([0, 0, S - 1, S - 1], radius=corner_r,
                           fill=(4, 6, 18, 255))

    # Subtle radial vignette — lighter in centre
    vignette = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vignette)
    steps = 24
    for i in range(steps, 0, -1):
        f   = i / steps
        r_v = int(C * 1.42 * f)
        col = int(12 * (1 - f))
        vd.ellipse([C - r_v, C - r_v, C + r_v, C + r_v],
                   fill=(col, col + 4, col + 14, 0))
    base = Image.alpha_composite(base, vignette)
    img  = Image.alpha_composite(img,  base)

    # ── Outer bloom halo ──────────────────────────────────────────────────────
    halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    hd   = ImageDraw.Draw(halo)
    halo_layers = [
        (int(R * 2.6), (0,  200, 255,  9)),
        (int(R * 2.1), (0,  210, 255, 14)),
        (int(R * 1.75),(20, 215, 255, 20)),
        (int(R * 1.45),(40, 200, 255, 28)),
    ]
    for r_h, col in halo_layers:
        hd.ellipse([C - r_h, C - r_h, C + r_h, C + r_h], fill=col)
    halo = halo.filter(ImageFilter.GaussianBlur(radius=S * 0.055))
    img  = Image.alpha_composite(img, halo)

    # ── Orbital rings ─────────────────────────────────────────────────────────
    ring_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring_layer)

    def draw_ring(rx, ry, angle_deg, color, width_px):
        """Draw an elliptical ring tilted by angle_deg (rotation around y-axis)."""
        steps = 360
        pts = []
        for i in range(steps + 1):
            a  = math.radians(i)
            x  = rx * math.cos(a)
            y  = ry * math.sin(a)
            # rotate around z-axis by angle_deg for tilt illusion
            rad = math.radians(angle_deg)
            xr  = x * math.cos(rad) - y * math.sin(rad)
            yr  = x * math.sin(rad) + y * math.cos(rad)
            pts.append((C + xr, C + yr))
        rd.line(pts, fill=color, width=max(1, width_px))

    ring_r_x = int(R * 1.52)
    ring_r_y = int(R * 0.44)
    lw = max(1, S // 256)

    draw_ring(ring_r_x, ring_r_y, 12,  (0, 190, 255, 55), lw)
    draw_ring(int(ring_r_x * 1.22), int(ring_r_y * 1.1), -28, (0, 150, 220, 38), lw)
    draw_ring(int(ring_r_x * 0.85), int(ring_r_y * 0.9),  55, (80, 200, 255, 30), lw)

    ring_layer = ring_layer.filter(ImageFilter.GaussianBlur(radius=max(1, S * 0.004)))
    img = Image.alpha_composite(img, ring_layer)

    # ── Particle dots on first ring ───────────────────────────────────────────
    part_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    pd = ImageDraw.Draw(part_layer)
    n_particles = max(6, S // 32)
    pr = max(1, S // 128)
    for i in range(n_particles):
        a   = math.radians(i / n_particles * 360)
        rad = math.radians(12)
        x   = ring_r_x * math.cos(a)
        y   = ring_r_y * math.sin(a)
        xr  = C + x * math.cos(rad) - y * math.sin(rad)
        yr  = C + x * math.sin(rad) + y * math.cos(rad)
        brightness = 160 + int(80 * abs(math.cos(a)))
        alpha      = 120 + int(120 * abs(math.sin(a * 2)))
        pd.ellipse([xr - pr, yr - pr, xr + pr, yr + pr],
                   fill=(brightness, 230, 255, alpha))
    part_layer = part_layer.filter(ImageFilter.GaussianBlur(radius=max(1, pr)))
    img = Image.alpha_composite(img, part_layer)

    # ── Core orb ──────────────────────────────────────────────────────────────
    orb_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    od = ImageDraw.Draw(orb_layer)

    # Layered radial gradient: dark core → mid cyan → edge glow
    gradient_steps = R + int(R * 0.3)
    for i in range(gradient_steps, 0, -1):
        f    = i / gradient_steps
        # Core colour: deep blue → electric cyan
        r_c  = int(0   + (0)   * (1 - f))
        g_c  = int(40  + (195 - 40)  * (1 - f ** 0.7))
        b_c  = int(120 + (255 - 120) * (1 - f ** 0.5))
        # Edge gets slightly greenish-cyan for depth
        if f < 0.25:
            g_c = min(255, g_c + 30)
        a_c  = min(255, int(255 * (1 - f ** 1.8)))
        od.ellipse([C - i, C - i, C + i, C + i], fill=(r_c, g_c, b_c, a_c))

    # Specular highlight (top-left)
    spec  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd    = ImageDraw.Draw(spec)
    sx, sy = C - int(R * 0.28), C - int(R * 0.28)
    sr     = int(R * 0.48)
    sd.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(200, 240, 255, 110))
    spec  = spec.filter(ImageFilter.GaussianBlur(radius=int(R * 0.22)))
    orb_layer = Image.alpha_composite(orb_layer, spec)

    # Inner rim glow
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rimd = ImageDraw.Draw(rim)
    for i in range(max(1, R // 6)):
        f   = i / max(1, R // 6)
        a_r = int(55 * (1 - f))
        rimd.arc([C - R + i, C - R + i, C + R - i, C + R - i],
                 start=0, end=360, fill=(130, 220, 255, a_r), width=1)
    orb_layer = Image.alpha_composite(orb_layer, rim)
    img = Image.alpha_composite(img, orb_layer)

    # ── Scanline overlay (subtle) ──────────────────────────────────────────────
    if size >= 64:
        scan = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sc   = ImageDraw.Draw(scan)
        step = max(2, S // 128)
        for y_s in range(0, S, step * 2):
            sc.rectangle([C - R, y_s, C + R, y_s + max(1, step // 3)],
                         fill=(0, 200, 255, 5))
        img = Image.alpha_composite(img, scan)

    # ── ATLAS wordmark ────────────────────────────────────────────────────────
    if size >= 64:
        text_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        td         = ImageDraw.Draw(text_layer)
        font_size  = max(8, int(S * 0.09))
        font       = None
        from PIL import ImageFont as IF
        for font_path in [
            "/System/Library/Fonts/SFNSMono.ttf",
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/SFCompact.ttf",
        ]:
            if Path(font_path).exists():
                try:
                    font = IF.truetype(font_path, font_size)
                    break
                except Exception:
                    pass
        if font is None:
            font = IF.load_default()

        label = "ATLAS"
        bbox  = td.textbbox((0, 0), label, font=font)
        tw    = bbox[2] - bbox[0]
        th    = bbox[3] - bbox[1]

        tx = C - tw // 2
        ty = C + int(R * 1.05)

        # Crisp text (drawn onto text_layer via td — before any reassignment)
        td.text((tx, ty), label, font=font, fill=(160, 235, 255, 240))

        # Glow behind text (separate layer, blurred)
        glow_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        gd.text((tx, ty), label, font=font, fill=(0, 200, 255, 200))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=max(2, S // 70)))

        # Composite: glow first, then crisp text on top
        final_text = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        final_text = Image.alpha_composite(final_text, glow_layer)
        final_text = Image.alpha_composite(final_text, glow_layer)  # double intensity
        final_text = Image.alpha_composite(final_text, text_layer)
        img = Image.alpha_composite(img, final_text)

    # Clip to rounded corners mask
    mask = Image.new("L", (S, S), 0)
    md   = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, S - 1, S - 1], radius=corner_r, fill=255)
    img.putalpha(mask)

    return img


# ── Iconset builder ────────────────────────────────────────────────────────────

ICON_SIZES = [16, 32, 64, 128, 256, 512, 1024]

def build_iconset():
    print("🎨  Drawing ATLAS icon…")
    ICONSET_DIR.mkdir(parents=True, exist_ok=True)

    # Draw at each native size
    for sz in ICON_SIZES:
        img  = draw_icon(sz)
        # 1× slot
        if sz <= 512:
            name = f"icon_{sz}x{sz}.png"
            img.save(ICONSET_DIR / name)
        # 2× slot (half the logical size)
        if sz >= 32:
            logical = sz // 2
            name2x  = f"icon_{logical}x{logical}@2x.png"
            img.save(ICONSET_DIR / name2x)

    print(f"   Saved {len(list(ICONSET_DIR.glob('*.png')))} PNG sizes to {ICONSET_DIR}")


def convert_to_icns():
    print("🔨  Converting to ICNS…")
    ICNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"   iconutil error: {result.stderr}")
        sys.exit(1)
    print(f"   ICNS ready: {ICNS_PATH}")


# ── App bundle builder ─────────────────────────────────────────────────────────

INFO_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>             <string>ATLAS</string>
    <key>CFBundleDisplayName</key>      <string>ATLAS</string>
    <key>CFBundleIdentifier</key>       <string>com.atlas.assistant</string>
    <key>CFBundleVersion</key>          <string>1.0.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>CFBundleSignature</key>        <string>ATLS</string>
    <key>CFBundleIconFile</key>         <string>ATLAS</string>
    <key>LSMinimumSystemVersion</key>   <string>12.0</string>
    <key>NSHighResolutionCapable</key>  <true/>
    <key>LSUIElement</key>              <false/>
    <key>NSMicrophoneUsageDescription</key>
        <string>ATLAS uses the microphone for voice commands.</string>
    <key>NSCameraUsageDescription</key>
        <string>ATLAS uses the camera for visual context awareness.</string>
    <key>NSAppleEventsUsageDescription</key>
        <string>ATLAS uses AppleScript to control apps on your Mac.</string>
</dict>
</plist>
"""

def build_app_bundle():
    print("📦  Building ATLAS.app…")

    # Remove previous build
    if APP_PATH.exists():
        shutil.rmtree(APP_PATH)

    # Directory structure
    macos_dir     = APP_PATH / "Contents" / "MacOS"
    resources_dir = APP_PATH / "Contents" / "Resources"
    macos_dir.mkdir(parents=True)
    resources_dir.mkdir(parents=True)

    # Info.plist
    (APP_PATH / "Contents" / "Info.plist").write_text(INFO_PLIST, encoding="utf-8")

    # Icon
    shutil.copy(ICNS_PATH, resources_dir / "ATLAS.icns")

    # Launcher script — silent, no Terminal window
    launcher = macos_dir / "ATLAS"
    launcher.write_text(f"""\
#!/bin/bash
# ATLAS launcher — runs silently, no Terminal window
ATLAS_ROOT="{ATLAS_ROOT}"
PYTHON="{PYTHON}"

# Use venv python if available
if [ -f "$ATLAS_ROOT/.venv/bin/python3" ]; then
    source "$ATLAS_ROOT/.venv/bin/activate"
    PYTHON="$ATLAS_ROOT/.venv/bin/python3"
fi

mkdir -p "$HOME/.atlas"

cd "$ATLAS_ROOT"
export PYTHONPATH="$ATLAS_ROOT"

# Run ATLAS directly — this process IS the app (no Terminal window)
exec "$PYTHON" main.py >> "$HOME/.atlas/atlas.log" 2>&1
""", encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # PkgInfo (optional but conventional)
    (APP_PATH / "Contents" / "PkgInfo").write_text("APPLEATLS", encoding="utf-8")

    print(f"   Bundle created: {APP_PATH}")


def refresh_icon_cache():
    """Tell macOS Finder + Dock to pick up the new icon immediately."""
    subprocess.run(["touch", str(APP_PATH)], check=False)
    subprocess.run(["killall", "Dock"], check=False, capture_output=True)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n── ATLAS App Builder ────────────────────────────────────────")
    build_iconset()
    convert_to_icns()
    build_app_bundle()
    refresh_icon_cache()
    print(f"\n✅  ATLAS.app is on your Desktop.")
    print("   Drag it to your Dock or /Applications to install permanently.\n")
