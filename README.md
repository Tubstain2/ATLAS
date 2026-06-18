# ATLAS — AI Assistant

> Voice-activated AI assistant with a real-time animated desktop interface.
> Powered by Gemini (reasoning) · Groq (fast voice) · Whisper (STT) · Piper (TTS).

---

## Quick Start

### macOS

```bash
# 1. Clone / download the project
cd ~/Desktop/atlas

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set API keys (add to ~/.zshrc for persistence)
export GEMINI_API_KEY="your-gemini-key"
export GROQ_API_KEY="your-groq-key"

# 5. Launch
python main.py
```

### Windows

```powershell
# 1. Open PowerShell in the project folder
cd C:\Users\YourName\Desktop\atlas

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set API keys (System → Environment Variables, or in this session)
$env:GEMINI_API_KEY = "your-gemini-key"
$env:GROQ_API_KEY   = "your-groq-key"

# 5. Launch
python main.py
```

---

## UI Controls

| Key / Action | Effect |
|---|---|
| `Esc` | Minimise to system tray |
| `F11` | Toggle full-screen |
| Tray double-click | Show / hide window |
| Tray menu → Mute | Toggle microphone |

---

## Architecture

```
atlas/
├── main.py           Entry point + demo loop
├── config.yaml       All user-tunable settings
├── core.py           Agent loop: Groq ↔ Gemini routing      [Step 3]
├── voice.py          Wake word + STT + TTS + amplitude feed  [Step 2]
├── web.py            DuckDuckGo + BeautifulSoup scraper      [Step 4]
├── control.py        Mouse / keyboard / OCR / shell          [Step 5]
├── self_editor.py    Self-modifying code engine              [Step 6]
└── ui/
    ├── main_window.py    Top-level QMainWindow
    ├── orb_widget.py     Animated orb (QPainter, 60 fps)
    ├── hud_widget.py     Transparent HUD overlay
    └── transcript_widget.py  Live transcript + response reveal
```

---

## Configuration (`config.yaml`)

| Key | Default | Description |
|---|---|---|
| `app.window.width` | 1280 | Initial window width |
| `app.window.fullscreen` | false | Start full-screen |
| `ui.orb_radius` | 170 | Orb radius in pixels |
| `voice.wake_word` | atlas | Wake-word trigger |
| `voice.whisper_model` | base | Whisper model size |
| `safety.confirm_destructive_commands` | true | Require typed confirmation |

---

## API Keys

All keys are loaded from environment variables — **never hardcoded**.

| Variable | Used for |
|---|---|
| `GEMINI_API_KEY` | Gemini 2.0 Flash — reasoning & research |
| `GROQ_API_KEY` | Llama 3 70B on Groq — fast voice responses |

---

## Build Status

| Module | Status |
|---|---|
| UI Shell | ✅ Complete |
| Voice (wake word + STT + TTS) | 🔲 Step 2 |
| Core agent loop | 🔲 Step 3 |
| Web module | 🔲 Step 4 |
| Laptop control | 🔲 Step 5 |
| Self-modifying engine | 🔲 Step 6 |
| PyInstaller packaging | 🔲 Step 7 |
