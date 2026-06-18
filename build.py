#!/usr/bin/env python3
"""
ATLAS cross-platform build script (Step 7).

Usage
─────
  python3 build.py              # build for current platform
  python3 build.py --clean      # delete build/ and dist/ first
  python3 build.py --debug      # keep console window open + verbose PyInstaller output
  python3 build.py --check      # verify environment without building
  python3 build.py --no-sign    # skip macOS ad-hoc codesigning step

Requirements
────────────
  pip install pyinstaller>=6.0.0
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT    = Path(__file__).resolve().parent
SPEC    = ROOT / 'atlas.spec'
DIST    = ROOT / 'dist'
BUILD   = ROOT / 'build'

IS_MAC  = sys.platform == 'darwin'
IS_WIN  = sys.platform == 'win32'
ARCH    = platform.machine()


# ── environment check ──────────────────────────────────────────────────────────

REQUIRED_PACKAGES = [
    'PyQt6', 'yaml', 'sounddevice', 'numpy',
    'groq', 'sherpa_onnx', 'pyautogui',
    'duckduckgo_search', 'bs4',
]

OPTIONAL_PACKAGES = {
    'whisper':      'openai-whisper  (STT — omit to skip transcription)',
    'piper':        'piper-tts       (TTS — omit to fall back to pyttsx3)',
    'onnxruntime':  'onnxruntime     (required by piper-tts)',
    'pytesseract':  'pytesseract     (OCR — omit to skip screen reading)',
}


def _check_env() -> bool:
    ok = True
    print("─── Environment check ───────────────────────────────")

    # PyInstaller
    try:
        import PyInstaller
        print(f"  ✓  PyInstaller {PyInstaller.__version__}")
    except ImportError:
        print("  ✗  PyInstaller not found — run: pip install pyinstaller>=6.0.0")
        ok = False

    # Required packages
    import importlib
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg.replace('-', '_'))
            print(f"  ✓  {pkg}")
        except ImportError:
            print(f"  ✗  {pkg}  (required)")
            ok = False

    # Optional packages
    for pkg, note in OPTIONAL_PACKAGES.items():
        try:
            importlib.import_module(pkg)
            print(f"  ✓  {pkg}")
        except ImportError:
            print(f"  ~  {pkg} not installed — {note}")

    # spec file
    if SPEC.exists():
        print(f"  ✓  {SPEC.name}")
    else:
        print(f"  ✗  {SPEC.name} not found")
        ok = False

    # API key hints (not mandatory at build time — only at runtime)
    for var in ('GROQ_API_KEY',):
        if os.environ.get(var):
            print(f"  ✓  {var} is set")
        else:
            print(f"  ~  {var} not set in this shell (must be set at runtime)")

    print("─────────────────────────────────────────────────────")
    return ok


# ── build ──────────────────────────────────────────────────────────────────────

def _build(args: argparse.Namespace) -> int:
    if args.clean:
        for d in (BUILD, DIST):
            if d.exists():
                print(f"Removing {d} …")
                shutil.rmtree(d)

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
    ]

    if args.clean:
        cmd.append('--clean')

    if args.debug:
        cmd += ['--debug', 'all']
    else:
        cmd.append('--log-level=WARN')

    cmd.append(str(SPEC))

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT)

    if result.returncode != 0:
        print(f"\nBuild FAILED (exit code {result.returncode})")
        return result.returncode

    _post_build(args)
    return 0


def _post_build(args: argparse.Namespace) -> None:
    """Report output paths and apply optional codesigning."""
    if IS_MAC:
        app_path = DIST / 'ATLAS.app'
        if app_path.exists():
            print(f"\nBuild succeeded  →  {app_path}")
            _print_size(app_path)

            if not args.no_sign:
                _codesign_mac(app_path)

            print("\nTo run:  open dist/ATLAS.app")
            print("Requires: GROQ_API_KEY in your environment.")
        else:
            print(f"\nBuild finished but {app_path} not found — check PyInstaller output.")

    elif IS_WIN:
        exe_dir = DIST / 'ATLAS'
        exe     = exe_dir / 'ATLAS.exe'
        if exe.exists():
            print(f"\nBuild succeeded  →  {exe}")
            _print_size(exe_dir)
        else:
            print(f"\nBuild finished but {exe} not found — check PyInstaller output.")

    else:
        app_dir = DIST / 'ATLAS'
        print(f"\nBuild succeeded  →  {app_dir}")
        _print_size(app_dir)


def _print_size(path: Path) -> None:
    try:
        if path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
        else:
            total = path.stat().st_size
        mb = total / (1024 * 1024)
        print(f"Bundle size: {mb:.0f} MB")
    except Exception:
        pass


def _codesign_mac(app_path: Path) -> None:
    """Ad-hoc codesign so macOS Gatekeeper allows the app to launch."""
    print("Applying ad-hoc codesign …")
    result = subprocess.run(
        ['codesign', '--force', '--deep', '--sign', '-', str(app_path)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  ✓  Codesigned (ad-hoc)")
    else:
        print(f"  ~  Codesign skipped: {result.stderr.strip()}")
        print("     To distribute, use a Developer ID: codesign --sign 'Developer ID Application: ...'")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description='Build ATLAS desktop application via PyInstaller.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--clean',    action='store_true', help='Delete build/ and dist/ before building')
    p.add_argument('--debug',    action='store_true', help='Keep console window; verbose PyInstaller output')
    p.add_argument('--check',    action='store_true', help='Check environment only, do not build')
    p.add_argument('--no-sign',  action='store_true', help='Skip macOS ad-hoc codesign step')
    args = p.parse_args()

    ok = _check_env()

    if args.check:
        return 0 if ok else 1

    if not ok:
        print("\nEnvironment check failed. Fix the issues above, then re-run.")
        return 1

    return _build(args)


if __name__ == '__main__':
    sys.exit(main())
