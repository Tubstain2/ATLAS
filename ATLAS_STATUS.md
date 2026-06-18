# ATLAS Build Status

**Session date:** 2026-06-17
**Current step:** 7 of 7 — PyInstaller Packaging ✅  (ALL STEPS COMPLETE)

---

## ✅ Steps 1–3
Working as shipped. See previous session notes.

---

## ✅ Step 4 — Web Module

### What works

**`DuckDuckGoSearch`**
- `text(query, max_results=5)` → `list[{title, href, body}]`
- `news(query, max_results=5)` → `list[{date, title, body, url, source}]`
- Throttle: 1.2 s minimum between calls
- Retry-with-backoff: up to 2 retries on 403/429 rate-limit responses (2.5 s gap)
- No API key required

**`PageFetcher`**
- `fetch(url, max_chars=5000)` → clean extracted text
- Strips: `<script>`, `<style>`, `<nav>`, `<footer>`, `<header>`, `<aside>`, `<form>`, `<iframe>`, and more
- Prefers `<article>` / `<main>` content blocks over whole-page text
- Falls back to full `<body>` text
- Returns `""` on any network/parse error (never throws)

**`WebModule`** (public API)
- `needs_web(text)` — 30-keyword trigger set; returns True for time-sensitive / current-event queries
- `search(query)` / `news(query)` — raw DDG results
- `fetch_page(url)` — extracted page text
- `build_context(query)` — search + optional deep-fetch of top result → formatted context block
- `answer(query, summarizer_fn)` — build context → call `summarizer(prompt)` → return answer
- `summarise_url(url, question, summarizer_fn)` — fetch + ask about specific page

**`ATLASCore` updates** (core.py)
- Added `_WEB_PROMPT` system instruction for Gemini web-augmented responses
- `set_web_module(web)` — injection point called by `main.py`
- `handle()` now: detects web-requiring queries → calls `web.build_context()` → builds augmented history (keeps main history clean) → Gemini receives context-enriched prompt
- Web context injected only when: provider=Gemini AND `web.needs_web()=True` AND Gemini available

**`main.py` update**
- `WebModule` constructed and wired: `core.set_web_module(web)`
- `window._web_module` kept alive

### Files changed
```
atlas/
├── web.py            ← complete (DuckDuckGoSearch, PageFetcher, WebModule)
├── core.py           ← added _WEB_PROMPT, set_web_module(), web-augmented _call()
├── main.py           ← WebModule constructed + wired to core
├── config.yaml       ← added web: block
└── requirements.txt  ← added duckduckgo-search, beautifulsoup4, requests, lxml
```

### How it flows (live usage)

```
User: "Hey Atlas, what's the latest news on AI?"
  → wake word detected → STT → "what's the latest news on AI"
  → core.handle() routes to Gemini
  → web.needs_web("what's the latest news on AI") = True
  → web.build_context() → DDG news search → top-result deep fetch
  → Gemini receives: [search results] + "User question: what's the latest..."
  → Gemini answers with citations
  → Piper TTS speaks the response
```

### Query classification samples

| Query | needs_web |
|---|---|
| "latest AI news today" | ✅ True |
| "weather in Tokyo" | ✅ True |
| "search the web for Python tutorials" | ✅ True |
| "who won the match last night" | ✅ True |
| "explain recursion" | ❌ False (parametric) |
| "hello atlas" | ❌ False |

---

## ✅ Step 5 — Laptop Control

### Architecture

```
control.py
├── ConfirmationDialog   Qt modal dialog (thread-safe) for destructive-command approval
├── MouseController      pyautogui: move, click (single/double/right), scroll, drag
├── KeyboardController   pyautogui: type_text, press, hotkey (with key aliases)
├── WindowManager        open_app / close_app / focus_app / minimize / maximize / list_windows
│   macOS  → open -a / osascript (AppleScript)
│   Windows → os.startfile / pygetwindow / taskkill
├── ScreenReader         pyautogui screenshot + pytesseract OCR
└── ControlModule        Public API — orchestrates all components

core.py additions
├── _CONTROL_PROMPT      Gemini system prompt for JSON action parsing
├── _parse_control_json  Extracts JSON dict from raw LLM response (with fallback)
├── ATLASCore.set_control_module(ctrl)
├── ATLASCore._call_control(text)  → Gemini parses → ControlModule.execute()
└── ATLASCore.handle()   → control routing runs BEFORE web/Groq/Gemini routing
```

### Control flow (live usage)

```
User: "Hey ATLAS, open Safari"
  → wake word detected → STT → "open Safari"
  → core.handle() detects control query (ControlModule.is_control_query())
  → _call_control("open Safari")
      → Gemini receives _CONTROL_PROMPT + "open Safari"
      → Gemini returns: {"action":"open_app","name":"Safari","response":"Opening Safari now."}
  → _parse_control_json() extracts dict
  → ControlModule.execute(action) → WindowManager.open_app("Safari")
      → subprocess.run(["open", "-a", "Safari"])   ← macOS
  → returns "Opening Safari now."
  → Piper TTS speaks it
```

### Safety guard

```
User: "run command rm -rf /tmp"
  → ShellExecutor.is_dangerous() → True
  → ConfirmationDialog.ask() → shows Qt modal on main thread, blocks voice thread
  → User must type "confirm" to proceed — otherwise command is blocked
  → Blocked: "Blocked by safety guard: 'rm -rf /tmp'"
```

- 20 compiled regex patterns + config.yaml `restricted_commands` list
- No confirm_cb set → dangerous commands always blocked
- `confirmed=True` bypass available for programmatic use only
- Shell commands run from `~` (never from ATLAS project root)

### OCR status

- `pytesseract` Python wrapper installed
- Tesseract binary: **not installed** — OCR will return a helpful install message
- Install: `brew install tesseract` (then restart ATLAS)
- Screenshot capture (without OCR) works independently via pyautogui

### macOS permissions required

| Feature | Permission |
|---|---|
| Mouse / keyboard | Accessibility (System Settings → Privacy & Security → Accessibility) |
| Screenshot / OCR | Screen Recording (System Settings → Privacy & Security → Screen Recording) |
| Window listing | Automation → System Events (System Settings → Privacy & Security → Automation) |

ATLAS gracefully handles missing permissions with clear error messages.

### Files changed

```
atlas/
├── control.py          ← full implementation (ConfirmationDialog, Mouse, Keyboard, Window, Screen, Shell, ControlModule)
├── core.py             ← added _CONTROL_PROMPT, _parse_control_json(), set_control_module(), _call_control(), handle() routing
├── main.py             ← ControlModule + ConfirmationDialog wired; CTRL badge shown in HUD
└── requirements.txt    ← Step 5 packages uncommented
```

### Tests (62 tests, all pass)

| Group | Tests |
|---|---|
| `TestIsControlQuery` | 18 — correct/incorrect control query detection |
| `TestIsDangerous` | 17 — dangerous/safe shell command classification |
| `TestShellSafeRun` | 3 — echo, python version, exit-code message |
| `TestShellDangerBlocked` | 2 — no-cb and false-cb both block |
| `TestShellDangerConfirmed` | 2 — confirmed=True executes |
| `TestWindowManagerMac` | 2 — list_windows (skipped if no Automation permission) |
| `TestControlModuleExecute` | 5 — none, unknown, run_command, blocked, list_windows |
| `TestParseControlJson` | 6 — clean, wrapped, fallback, empty, run_command, type_text |
| `TestATLASCoreControlWiring` | 2 — set_control_module, classmethod |

---

## ✅ Step 6 — Self-Modifying Code Engine

### Architecture

```
self_editor.py
├── EditResult        @dataclass — returned by apply_edit(); .as_voice_response() for TTS
├── Backup            create / restore / cleanup timestamped .bak files
│                     timestamp = YYYYMMDD_HHMMSS_ffffff (microsecond precision for uniqueness)
├── Changelog         append-only JSON list → changelog.json
├── CodePatcher       replace / insert_after / insert_before / full_rewrite
├── TestResult        @dataclass — pass/fail + output from test run
├── TestRunner        pytest (fallback: unittest) on test_step*.py files
└── SelfEditor        public API — orchestrates all of the above

core.py additions
├── _EDIT_INTENT_PROMPT  Pass 1: Gemini identifies file + one-line intent
├── _EDIT_SPEC_PROMPT    Pass 2: Gemini produces exact JSON edit spec
├── _EDIT_TRIGGERS       frozenset — routes voice queries into self-edit flow
├── _is_self_edit(text)  module-level classifier
├── ATLASCore.set_self_editor(editor)
├── ATLASCore._call_edit(text)    → two-pass Gemini → apply_edit → voice response
├── ATLASCore._list_atlas_files() → list of .py files for Gemini context
└── ATLASCore.handle()   self-edit routing runs BEFORE control routing
```

### Self-modification flow (live usage)

```
User: "Hey ATLAS, update web.py to add 'latest crypto news' to the search triggers"
  → wake word → STT → core.handle()
  → _is_self_edit() → True
  → _call_edit() — Pass 1: Gemini identifies file=web.py, intent="add 'latest crypto news' to _WEB_TRIGGERS"
  → web.py read (content passed to Gemini)
  → Pass 2: Gemini generates edit spec:
      {"type":"replace","file":"web.py","old":"\"bitcoin\",","new":"\"bitcoin\",\n        \"latest crypto news\",","description":"Add 'latest crypto news' to _WEB_TRIGGERS"}
  → SelfEditor.apply_edit(spec):
      1. Validate path (must be inside project root)
      2. Check protected-file gate (web.py is not protected)
      3. Backup: web.py.20260617_143022_123456.bak
      4. Compute new content (CodePatcher.replace)
      5. Syntax check (compile() — catches malformed edits before write)
      6. Write new web.py
      7. Run tests: python3 test_step4_web.py (targeted — fastest)
      8. Tests PASS → log to changelog.json
  → EditResult.as_voice_response() = "Add 'latest crypto news' to _WEB_TRIGGERS. All tests passed."
  → Piper TTS speaks it
```

### Safety guarantees

| Safety check | What it does |
|---|---|
| Path validation | Blocks `../../etc/passwd` style escapes |
| Protected file gate | `core.py`, `main.py`, `voice.py` require typed "confirm" in dialog |
| Syntax check | `compile()` catches invalid Python before any write |
| Test runner | Runs tests after write; rolls back on failure |
| Auto rollback | If tests fail, `Backup.restore()` is called immediately |
| Changelog audit | Every attempt (success or rollback) logged with timestamp + backup path |

### Edit types supported

| Type | Use case | Key fields |
|---|---|---|
| `replace` | Change a specific string | `old` (verbatim), `new` |
| `insert_after` | Add code after an anchor | `after` (verbatim), `insert` |
| `insert_before` | Add code before an anchor | `before` (verbatim), `insert` |
| `full_rewrite` | Major restructuring | `content` (full file, ≤80K chars) |

### Files changed

```
atlas/
├── self_editor.py     ← full implementation (EditResult, Backup, Changelog, CodePatcher, TestRunner, SelfEditor)
├── core.py            ← _EDIT_INTENT_PROMPT, _EDIT_SPEC_PROMPT, _EDIT_TRIGGERS, _is_self_edit(),
│                         set_self_editor(), _call_edit(), _list_atlas_files(), handle() routing
└── main.py            ← SelfEditor constructed and wired; EDIT badge shown in HUD
```

### Tests (60 tests, all pass)

| Group | Tests |
|---|---|
| `TestEditResult` | 5 — success/failure/rollback voice responses |
| `TestBackup` | 5 — create, restore, cleanup, latest_for |
| `TestChangelog` | 5 — append, recent, last_entry, persistence |
| `TestCodePatcher` | 10 — all 4 edit types + error cases |
| `TestSelfEditorSuccess` | 3 — replace applied, backup created, changelog written |
| `TestSelfEditorRollback` | 2 — file reverted when tests fail |
| `TestProtectedFiles` | 3 — blocked without cb, blocked with false cb, allowed with true cb |
| `TestPathGuard` | 2 — path traversal blocked, missing file field |
| `TestSyntaxCheck` | 1 — bad Python caught before write |
| `TestRollbackLast` | 2 — manual rollback restores file, empty changelog |
| `TestReadSource` | 2 — reads file, handles missing |
| `TestLegacyAPI` | 4 — patch, rollback, protected, changelog |
| `TestIsSelfEdit` | 11 — correct/incorrect query classification |
| `TestATLASCoreWiring` | 1 — set_self_editor() |

---

## ✅ Step 7 — PyInstaller Packaging

### Files created

```
atlas/
├── atlas.spec                     ← PyInstaller spec (macOS .app + Windows .exe)
├── build.py                       ← Cross-platform build script
├── build_macos.sh                 ← macOS convenience wrapper (bash)
├── build_windows.bat              ← Windows convenience wrapper
├── pyinstaller_hooks/
│   ├── hook-whisper.py            ← Collect whisper/assets/ + sub-modules
│   └── hook-pvporcupine.py        ← Collect pvporcupine native libs + resources
├── assets/                        ← Drop atlas.icns / atlas.ico here for icons
├── requirements.txt               ← pyinstaller>=6.0.0 uncommented
└── test_step7_build.py            ← 55 validation tests (all pass)
```

### How to build

```bash
# macOS (produces dist/ATLAS.app)
bash build_macos.sh

# Clean rebuild
bash build_macos.sh --clean

# Environment check without building
python3 build.py --check

# Windows (run in Command Prompt — produces dist\ATLAS\ATLAS.exe)
build_windows.bat
```

### What atlas.spec does

| Feature | Detail |
|---|---|
| Entry point | `main.py` |
| macOS output | `dist/ATLAS.app` with `BUNDLE` + Info.plist |
| Windows output | `dist/ATLAS/ATLAS.exe` via `COLLECT` |
| Architecture | Auto-detects arm64 vs x86_64; `target_arch=None` (native) |
| pvporcupine | dylib included per-arch from `pvporcupine/lib/mac/{arch}/` |
| onnxruntime | `.so`/`.dylib` from `onnxruntime/capi/` |
| whisper | `assets/` data files via `hook-whisper.py` |
| PyQt6 | Full hidden-imports list |
| Excluded | tkinter, matplotlib, scipy, pandas, jupyter, all test files |
| Icons | Reads `assets/atlas.icns` (macOS) / `assets/atlas.ico` (Windows) if present |
| UPX | Windows only (incompatible with macOS codesigning) |

### macOS Info.plist entries

| Key | Purpose |
|---|---|
| `NSMicrophoneUsageDescription` | Wake word + STT microphone access |
| `NSScreenCaptureUsageDescription` | Screen reading / OCR |
| `NSAppleEventsUsageDescription` | osascript automation (open/focus apps) |
| `LSMinimumSystemVersion` | macOS 13.0+ |
| `bundle_identifier` | `com.atlas.ai.assistant` |

### build.py flags

| Flag | Effect |
|---|---|
| `--clean` | Delete `build/` and `dist/` before building |
| `--debug` | Keep console window open; verbose PyInstaller logs |
| `--check` | Check env + packages; print report; don't build |
| `--no-sign` | Skip ad-hoc macOS codesign step |

### Not bundled (install separately)

```bash
brew install tesseract   # OCR / screen reading
brew install ffmpeg      # Whisper audio decoding
```

### Tests (55 tests, all pass)

| Group | Tests |
|---|---|
| `TestSpecFileExists` | 5 — all build artifacts exist |
| `TestSpecSyntax` | 6 — ast.parse + Analysis/PYZ/EXE/COLLECT/BUNDLE calls |
| `TestSpecContent` | 14 — config.yaml, ui/, whisper, pvporcupine, PyQt6, exclusions, Info.plist |
| `TestHookSyntax` | 6 — both hooks parse + define datas + hiddenimports |
| `TestBuildPySyntax` | 7 — parses + all expected functions defined + no hardcoded keys |
| `TestBuildPyFlags` | 4 — --clean/--debug/--check/--no-sign all present |
| `TestBuildScripts` | 7 — sh shebang, mentions permissions + env vars; bat exits with ERRORLEVEL |
| `TestRequirements` | 2 — pyinstaller and PyQt6 present |
| `TestPyInstallerInstalled` | 1 — importable at version >=6.0.0 |
| `TestAssetsDirectory` | 3 — assets/ exists; hooks/ exists and has >=2 hook files |

---

## Roadmap

| # | Module | Status |
|---|---|---|
| 1 | UI Shell | ✅ Done |
| 2 | Voice (wake word + STT + TTS) | ✅ Done |
| 3 | Core agent loop (Groq + Gemini) | ✅ Done |
| 4 | Web module | ✅ Done |
| 5 | Laptop control | ✅ Done |
| 6 | Self-modifying engine | ✅ Done |
| 7 | PyInstaller packaging | ✅ Done |
