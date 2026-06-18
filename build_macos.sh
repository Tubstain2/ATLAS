#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ATLAS macOS build wrapper
#
# Usage
# ─────
#   bash build_macos.sh           # standard build
#   bash build_macos.sh --clean   # clean rebuild
#   bash build_macos.sh --debug   # keep console window + verbose output
#   bash build_macos.sh --check   # environment check only
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== ATLAS macOS Builder ==="
echo "Platform: $(uname -sm)"
echo ""

# Forward all CLI args to build.py
python3 build.py "$@"
STATUS=$?

if [ $STATUS -eq 0 ] && [ "${1:-}" != "--check" ]; then
    echo ""
    echo "=== Post-build notes ==="
    echo "• Grant permissions before first launch:"
    echo "    Accessibility  →  System Settings › Privacy & Security › Accessibility"
    echo "    Microphone     →  System Settings › Privacy & Security › Microphone"
    echo "    Screen Recording → System Settings › Privacy & Security › Screen Recording"
    echo "    Automation     →  System Settings › Privacy & Security › Automation"
    echo ""
    echo "• Runtime env vars (add to ~/.zshenv or ~/.zshrc):"
    echo "    export GROQ_API_KEY=your_key_here"
    echo ""
    echo "• Optional native tools:"
    echo "    brew install tesseract   # for OCR / screen reading"
    echo "    brew install ffmpeg      # for Whisper audio decoding"
fi

exit $STATUS
