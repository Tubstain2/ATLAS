# ATLAS — AI Desktop Assistant

> Voice-controlled AI desktop assistant for macOS. Always-on, always listening, fully local-first.
> Powered by Whisper (STT) · Piper (TTS) · MLX / Groq / OpenRouter (AI) · PyQt6 (UI).

---

## Features

### Core
- **Wake word** — say "ATLAS" to activate (offline, no API key required)
- **Speech-to-text** — local Whisper transcription, auto-selects model by chip (M1/M2/M3/Intel)
- **Text-to-speech** — Piper TTS running locally, JARVIS-style voice
- **AI routing** — local MLX for fast replies, Groq Llama 3 70B for cloud fallback, OpenRouter for reasoning and vision
- **Offline mode** — automatically switches to a local Ollama model when internet is unavailable
- **VAD** — WebRTC VAD for instant speech-end detection (<50 ms latency)

### UI
- **Orb** — animated 60fps particle orb that reacts to listening / thinking / speaking states
- **Smart Card** — floating glassmorphism overlay that auto-detects and renders product info, stocks, weather, news, recipes, debate results, and research papers
- **Feed panel** — live dashboard with stocks, crypto, weather, news, tasks, and Obsidian notes
- **Cursor overlay** — minimal always-on-top status indicator

### Capability Modules

| Module | Voice commands (examples) |
|--------|--------------------------|
| **Obsidian** | `ATLAS take a note` · `ATLAS add a task` · `ATLAS open obsidian graph` |
| **Research** | `ATLAS research X` · `ATLAS find papers on X` · `ATLAS cite that paper` |
| **Debate** | `ATLAS debate whether I should X` · `ATLAS debate X vs Y` · `ATLAS steelman the for side` |
| **Tutor** | `ATLAS teach me X` · `ATLAS give me a hint` · `ATLAS harder please` |
| **Coach** | `ATLAS coach me on X` · `ATLAS check in on X` · `ATLAS how am I doing with X` |
| **Recorder** | `ATLAS record my screen` · `ATLAS new chapter intro` · `ATLAS stop recording` |
| **Markets** | `ATLAS what is AAPL stock` · `ATLAS crypto update` · `ATLAS market summary` |
| **Chrome** | `ATLAS open youtube.com` · `ATLAS search Google for X` · `ATLAS click login` |
| **Vision** | `ATLAS what do you see` · `ATLAS describe my screen` |
| **Spotify** | `ATLAS play X` · `ATLAS skip` · `ATLAS what song is this` |
| **Images** | `ATLAS generate image of X` |
| **Tasks** | `ATLAS run X` (multi-step agentic planner) |

---

## Installation

### Prerequisites (macOS)

```bash
brew install ffmpeg tesseract portaudio
```

### Setup

```bash
git clone https://github.com/Tubstain2/ATLAS.git
cd ATLAS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### API Keys

All keys are optional — ATLAS works fully offline without them.

```bash
export GROQ_API_KEY="..."           # Groq Llama 3 70B (fast cloud responses)
export OPENROUTER_API_KEY="..."     # GPT-class models, vision, free models
export GEMINI_API_KEY="..."         # Gemini 2.0 Flash
export FINNHUB_API_KEY="..."        # Live stock prices and news
```

Add to `~/.zshrc` to persist across sessions.

### Configure

Edit `config.yaml` to set your Obsidian vault path and preferences:

```yaml
obsidian:
  vault_path: "/path/to/your/obsidian/vault"

voice:
  wake_word: "atlas"
  whisper_model: "auto"    # auto-selects based on chip

smart_card_auto_dismiss: false    # cards stay open until closed manually
```

### Run

```bash
python3 main.py
```

---

## Architecture

```
atlas/
├── main.py                 Entry point + module wiring
├── config.yaml             All user settings
├── core.py                 Agent loop + system prompt
├── voice.py                Wake word → STT → TTS pipeline
├── brain.py                AI router (MLX / Groq / OpenRouter)
├── web.py                  DuckDuckGo search + page scraper
├── control.py              Mouse / keyboard / OCR / shell
├── obsidian.py             Obsidian vault read/write + graph view
├── smart_card.py           Auto-detecting floating card widget
├── market.py               Stocks, crypto, Finnhub news
├── chrome_control.py       Playwright CDP browser control
├── recorder.py             Screen recorder + AI commentary
├── coach.py                30-day coaching goals + check-ins
├── debate.py               Parallel FOR/AGAINST debate engine
├── tutor.py                Socratic tutoring sessions
├── research.py             Academic paper search (arXiv / S2 / CrossRef)
├── vision.py               Screenshot + webcam analysis
├── memory.py               Episodic + working memory (encrypted)
├── planner.py              Multi-step agentic task planner
├── code_agent.py           Sandboxed code execution agent
├── pipeline.py             Interruption-aware TTS pipeline
├── scheduler.py            Cron jobs (briefing, check-ins, review)
├── soul.py                 Personality layer from SOUL.md in vault
├── playbook.py             Pattern memory — learns from interactions
├── offline.py              Connectivity monitor + local model fallback
├── context7.py             Live library docs injection for coding queries
├── shazam.py               Song identification
├── spotify.py              Spotify playback control
├── imagegen.py             Local Stable Diffusion image generation
└── ui/
    ├── main_window.py      Top-level QMainWindow
    └── smart_card.html     Smart card renderer (D3 / CSS glassmorphism)
```

---

## Obsidian Vault Structure

ATLAS writes to your vault under an `ATLAS/` folder — nothing outside it is touched.

```
ATLAS/
├── Daily/          ← daily notes + morning briefings
├── Notes/          ← voice notes
├── Inbox/          ← quick captures
├── Tasks/          ← task list
├── Coaching/       ← goal plans + daily progress logs
│   └── Learning/   ← tutoring session notes
├── Research/
│   ├── Academic/   ← paper search results
│   └── Debates/    ← debate transcripts
├── Recordings/     ← screen recording summaries
├── Memory/         ← episodic memory
└── Playbook/       ← pattern memory
```

---

## macOS Permissions

ATLAS will request these on first use:

| Permission | Used for |
|-----------|----------|
| Microphone | Wake word detection + Whisper STT |
| Accessibility | Keyboard/mouse control, Chrome AppleScript (graph view) |
| Screen Recording | Vision module + screen recorder |
| Automation | Controlling Obsidian and Chrome via AppleScript |

---

## UI Controls

| Action | Effect |
|--------|--------|
| `Esc` | Minimise to system tray |
| `F11` | Toggle full-screen |
| Tray double-click | Show / hide window |
| Tray → Mute | Toggle microphone |
