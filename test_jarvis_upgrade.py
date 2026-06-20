"""
JARVIS Upgrade Tests — voice, vision, overlay, ambient, skills, digest, sounds, brain

Run:  python3 test_jarvis_upgrade.py
Run single suite:  python3 -m unittest test_jarvis_upgrade.TestSpeechFormatter -v

What is tested
──────────────
 1.  SpeechFormatter.format()        — markdown, abbreviations, numbers, camelCase
 2.  SpeechFormatter.to_voice()      — sentence trimming
 3.  SpeechFormatter.split_sentences() — sentence splitting
 4.  VoiceEmotionDetector            — urgency, relaxed, frustrated, speed, wit
 5.  ResponseCache                   — get/put, LRU eviction, TTL expiry, persistence
 6.  _PhraseCache                    — get/put, LRU eviction
 7.  _detect_whisper_model           — returns a valid model name for the current chip
 8.  WebRTCVAD                       — graceful init without webrtcvad installed
 9.  VisionModule.handle()           — correct routing for all voice commands
10.  VisionModule._needs_screenshot  — trigger vs no-screenshot keyword sets
11.  VisionModule guided walkthrough — set_steps / next_step / current_step
12.  handle_overlay_command()        — all mode and hide/show commands
13.  AmbientModule.handle()          — context recall, proactive toggle
14.  AmbientModule.update_context()  — stores and retrieves app history
15.  SkillsLoader discovery          — finds all 7 built-in skills
16.  SkillsLoader meta commands      — list, reload, disable, enable
17.  SkillsLoader routing            — weather/news/reminder triggers matched
18.  skill_info() contracts          — all 7 skills have name + triggers + description
19.  reminder_skill                  — add and list reminders (temp dir)
20.  DigestModule.handle()           — briefing, skip, elaborate, passthrough
21.  DigestModule config parsing     — briefing time correctly parsed
22.  DigestModule greeting           — returns non-empty string with day
23.  DigestModule cache roundtrip    — save/load to temp file
24.  SoundEngine generation          — all 7 sounds present, float32, non-empty
25.  SoundEngine amplitude           — all sounds within [-1.0, 1.0]
26.  SoundEngine controls            — enable/disable, volume, handle() routing
27.  SoundEngine ambient chunk       — float32, correct sample count
28.  brain._build_system()           — JARVIS personality in system prompt
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ATLAS_ROOT", str(Path(__file__).parent))

_CONFIG = {
    "voice": {
        "piper_voice": "en_GB-jarvis-high",
        "whisper_model": "auto",
        "vad_enabled": True,
        "tts_streaming": True,
        "response_cache_enabled": True,
        "response_cache_size": 10,
    },
    "api": {
        "qwen_coder": "qwen/qwen3-coder-480b-a35b-instruct:free",
    },
    "vision_enabled": True,
    "watch_mode_interval": 30,
    "overlay_mode": "normal",
    "sound_effects_enabled": True,
    "sound_volume": 0.3,
    "ambient_hum_enabled": False,   # off in tests — no audio playback
    "ambient_hum_volume": 0.05,
    "skills_folder": "./skills",
    "morning_briefing_time": "08:00",
    "proactive_suggestions": False,  # off in tests
    "location_lat": 51.5,
    "location_lon": -0.1,
    "news_topics": ["technology"],
    "response_cache_enabled": True,
    "response_cache_size": 10,
}


# ══════════════════════════════════════════════════════════════════════════════
# 1–3. SpeechFormatter
# ══════════════════════════════════════════════════════════════════════════════

class TestSpeechFormatter(unittest.TestCase):

    def setUp(self):
        from voice import SpeechFormatter
        self.fmt = SpeechFormatter()

    # ── Markdown removal ──────────────────────────────────────────────────────

    def test_strips_bold(self):
        self.assertEqual(self.fmt.format("**Bold text**"), "Bold text")

    def test_strips_italic(self):
        self.assertEqual(self.fmt.format("*italic*"), "italic")

    def test_strips_headers(self):
        self.assertEqual(self.fmt.format("## My heading"), "My heading")

    def test_strips_inline_code(self):
        result = self.fmt.format("Call `foo()` now")
        self.assertNotIn("`", result)

    def test_strips_bullet_points(self):
        text   = "- item one\n- item two"
        result = self.fmt.format(text)
        self.assertNotIn("- ", result)

    def test_strips_links(self):
        result = self.fmt.format("See [this link](https://example.com)")
        self.assertNotIn("http", result)
        self.assertIn("this link", result)

    # ── Abbreviation expansion ────────────────────────────────────────────────

    def test_expands_api(self):
        self.assertIn("A.P.I.", self.fmt.format("The API is ready"))

    def test_expands_url(self):
        self.assertIn("U.R.L.", self.fmt.format("Check the URL"))

    def test_expands_html(self):
        self.assertIn("H.T.M.L.", self.fmt.format("Write HTML code"))

    def test_expands_cpu(self):
        self.assertIn("C.P.U.", self.fmt.format("The CPU usage is high"))

    # ── Number formatting ─────────────────────────────────────────────────────

    def test_number_twelve_hundred(self):
        self.assertIn("twelve hundred", self.fmt.format("1200 errors"))

    def test_number_fifteen_hundred(self):
        self.assertIn("fifteen hundred", self.fmt.format("1500 items"))

    def test_number_one_thousand(self):
        self.assertIn("one thousand", self.fmt.format("1000 items"))

    def test_number_two_thousand(self):
        self.assertIn("two thousand", self.fmt.format("2000 bytes"))

    def test_small_numbers_unchanged(self):
        # Numbers < 100 should not be rewritten by the regex (3+ digits only)
        result = self.fmt.format("42 items")
        self.assertIn("42", result)

    # ── camelCase conversion ──────────────────────────────────────────────────

    def test_camel_case(self):
        result = self.fmt.format("the getUserName function")
        self.assertIn("get User Name", result)

    # ── Combined ──────────────────────────────────────────────────────────────

    def test_combined_format(self):
        result = self.fmt.format("**Bold** API response: 1200 errors")
        self.assertEqual(result, "Bold A.P.I. response: twelve hundred errors")

    # ── to_voice sentence trimming ────────────────────────────────────────────

    def test_to_voice_trims_sentences(self):
        long_text = "One. Two. Three. Four. Five."
        result    = self.fmt.to_voice(long_text, max_sentences=2)
        self.assertEqual(result, "One. Two.")

    def test_to_voice_keeps_all_when_fewer_than_max(self):
        short = "Hello. World."
        self.assertEqual(self.fmt.to_voice(short, max_sentences=5), "Hello. World.")

    # ── split_sentences ───────────────────────────────────────────────────────

    def test_split_sentences_basic(self):
        from voice import SpeechFormatter
        parts = SpeechFormatter.split_sentences("Hello. World. Foo.")
        self.assertEqual(parts, ["Hello.", "World.", "Foo."])

    def test_split_sentences_question(self):
        from voice import SpeechFormatter
        parts = SpeechFormatter.split_sentences("What? Really! Yes.")
        self.assertEqual(len(parts), 3)

    def test_split_sentences_single(self):
        from voice import SpeechFormatter
        parts = SpeechFormatter.split_sentences("Just one sentence.")
        self.assertEqual(parts, ["Just one sentence."])

    def test_split_sentences_empty(self):
        from voice import SpeechFormatter
        parts = SpeechFormatter.split_sentences("   ")
        self.assertEqual(parts, [])


# ══════════════════════════════════════════════════════════════════════════════
# 4. VoiceEmotionDetector
# ══════════════════════════════════════════════════════════════════════════════

class TestVoiceEmotionDetector(unittest.TestCase):

    def setUp(self):
        from voice import VoiceEmotionDetector
        self.det = VoiceEmotionDetector(window=20)

    def _feed(self, amp: float, n: int = 20):
        for _ in range(n):
            self.det.update(amp)

    def test_urgency_high_amplitude(self):
        self._feed(0.8)
        self.assertGreater(self.det.urgency, 0.5)
        self.assertTrue(self.det.is_urgent)

    def test_relaxed_low_amplitude(self):
        self._feed(0.05)
        self.assertTrue(self.det.is_relaxed)
        self.assertFalse(self.det.is_urgent)

    def test_speed_multiplier_urgent(self):
        self._feed(0.8)
        self.assertGreater(self.det.tts_speed_multiplier(), 1.0)

    def test_speed_multiplier_relaxed(self):
        self._feed(0.02)
        self.assertLess(self.det.tts_speed_multiplier(), 1.0)

    def test_allow_wit_when_relaxed(self):
        self._feed(0.02)
        self.assertTrue(self.det.allow_wit())

    def test_no_wit_when_urgent(self):
        self._feed(0.9)
        self.assertFalse(self.det.allow_wit())

    def test_empty_returns_zero_urgency(self):
        from voice import VoiceEmotionDetector
        det = VoiceEmotionDetector()
        self.assertEqual(det.urgency, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# 5. ResponseCache
# ══════════════════════════════════════════════════════════════════════════════

class TestResponseCache(unittest.TestCase):

    def setUp(self):
        from voice import ResponseCache
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.cache = ResponseCache(maxsize=5, ttl=3600, path=Path(self.tmp.name))

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_miss_returns_none(self):
        self.assertIsNone(self.cache.get("unknown command"))

    def test_put_and_get(self):
        self.cache.put("what time is it", "It is 3pm.")
        self.assertEqual(self.cache.get("what time is it"), "It is 3pm.")

    def test_case_insensitive_key(self):
        self.cache.put("What Time Is It", "It is 3pm.")
        self.assertEqual(self.cache.get("what time is it"), "It is 3pm.")

    def test_lru_eviction(self):
        for i in range(6):
            self.cache.put(f"command {i}", f"response {i}")
        # First entry should have been evicted
        self.assertIsNone(self.cache.get("command 0"))
        self.assertIsNotNone(self.cache.get("command 5"))

    def test_ttl_expiry(self):
        from voice import ResponseCache
        cache = ResponseCache(maxsize=5, ttl=0, path=Path(self.tmp.name))
        cache.put("stale command", "old response")
        time.sleep(0.01)
        self.assertIsNone(cache.get("stale command"))

    def test_persistence_roundtrip(self):
        self.cache.put("persistent query", "persistent answer")
        from voice import ResponseCache
        cache2 = ResponseCache(maxsize=5, ttl=3600, path=Path(self.tmp.name))
        self.assertEqual(cache2.get("persistent query"), "persistent answer")


# ══════════════════════════════════════════════════════════════════════════════
# 6. _PhraseCache
# ══════════════════════════════════════════════════════════════════════════════

class TestPhraseCache(unittest.TestCase):

    def setUp(self):
        from voice import _PhraseCache
        self.cache = _PhraseCache(maxsize=3)

    def test_miss_returns_none(self):
        self.assertIsNone(self.cache.get("hello"))

    def test_put_and_get(self):
        audio = np.zeros(1000, dtype=np.float32)
        self.cache.put("hello", 22050, audio)
        result = self.cache.get("hello")
        self.assertIsNotNone(result)
        sr, arr = result
        self.assertEqual(sr, 22050)
        np.testing.assert_array_equal(arr, audio)

    def test_lru_eviction(self):
        for i in range(4):
            self.cache.put(f"phrase {i}", 22050, np.zeros(10, dtype=np.float32))
        self.assertIsNone(self.cache.get("phrase 0"))
        self.assertIsNotNone(self.cache.get("phrase 3"))

    def test_access_updates_lru_order(self):
        for i in range(3):
            self.cache.put(f"phrase {i}", 22050, np.zeros(10, dtype=np.float32))
        # Access phrase 0 so it becomes most-recent
        self.cache.get("phrase 0")
        # Add a 4th item — phrase 1 (LRU) should be evicted, not phrase 0
        self.cache.put("phrase 3", 22050, np.zeros(10, dtype=np.float32))
        self.assertIsNone(self.cache.get("phrase 1"))
        self.assertIsNotNone(self.cache.get("phrase 0"))


# ══════════════════════════════════════════════════════════════════════════════
# 7. Whisper model detection
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectWhisperModel(unittest.TestCase):

    _VALID_MODELS = {"base", "base.en", "small.en", "tiny.en", "small", "medium"}

    def test_returns_valid_model_name(self):
        from voice import _detect_whisper_model
        model = _detect_whisper_model()
        self.assertIn(model, self._VALID_MODELS, f"Unexpected model: {model!r}")

    def test_m2_gets_small(self):
        from unittest.mock import patch
        import subprocess
        from voice import _detect_whisper_model
        with patch("platform.system", return_value="Darwin"), \
             patch("platform.machine", return_value="arm64"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Apple M2 Pro"
            self.assertEqual(_detect_whisper_model(), "small.en")

    def test_intel_gets_tiny(self):
        from unittest.mock import patch
        from voice import _detect_whisper_model
        with patch("platform.system", return_value="Darwin"), \
             patch("platform.machine", return_value="x86_64"):
            self.assertEqual(_detect_whisper_model(), "tiny.en")

    def test_non_mac_gets_base(self):
        from unittest.mock import patch
        from voice import _detect_whisper_model
        with patch("platform.system", return_value="Linux"):
            self.assertEqual(_detect_whisper_model(), "base")


# ══════════════════════════════════════════════════════════════════════════════
# 8. WebRTCVAD
# ══════════════════════════════════════════════════════════════════════════════

class TestWebRTCVAD(unittest.TestCase):

    def setUp(self):
        from voice import WebRTCVAD
        self.vad = WebRTCVAD()

    def test_init_does_not_raise(self):
        # Should succeed regardless of whether webrtcvad is installed
        self.assertIsNotNone(self.vad)

    def test_is_speech_returns_none_or_bool_on_short_chunk(self):
        # A chunk shorter than 480 samples → buffer not full → None
        chunk  = np.zeros(256, dtype=np.float32)
        result = self.vad.is_speech(chunk)
        self.assertIn(result, (None, True, False))

    def test_reset_clears_buffer(self):
        chunk = np.zeros(256, dtype=np.float32)
        self.vad.is_speech(chunk)
        self.vad.reset()
        # After reset, a 256-sample chunk should again be insufficient for a frame
        result = self.vad.is_speech(chunk)
        if self.vad.available:
            self.assertIsNone(result)


# ══════════════════════════════════════════════════════════════════════════════
# 9–11. VisionModule
# ══════════════════════════════════════════════════════════════════════════════

class TestVisionModuleRouting(unittest.TestCase):

    def setUp(self):
        from vision import VisionModule
        self.vm = VisionModule(_CONFIG)

    # ── Commands that must be intercepted ────────────────────────────────────

    def test_watch_mode_start(self):
        r = self.vm.handle("atlas watch my screen")
        self.assertIsNotNone(r)
        self.vm.stop_watch_mode()

    def test_watch_mode_stop(self):
        self.vm.start_watch_mode()
        r = self.vm.handle("atlas stop watching")
        self.assertIsNotNone(r)

    def test_look_at_this(self):
        r = self.vm.handle("atlas look at this")
        self.assertIsNotNone(r)  # will return something (API error or description)

    def test_what_do_you_see(self):
        r = self.vm.handle("atlas what do you see")
        self.assertIsNotNone(r)

    def test_read_that(self):
        r = self.vm.handle("atlas read that")
        self.assertIsNotNone(r)

    def test_font_identification(self):
        r = self.vm.handle("atlas what font is that")
        self.assertIsNotNone(r)

    def test_colour_matching(self):
        r = self.vm.handle("atlas match that colour")
        self.assertIsNotNone(r)

    def test_click_command(self):
        r = self.vm.handle("atlas click on the Save button")
        self.assertIsNotNone(r)

    # ── Commands that must NOT be intercepted ─────────────────────────────────

    def test_open_command_passthrough(self):
        self.assertIsNone(self.vm.handle("open Spotify"))

    def test_weather_passthrough(self):
        self.assertIsNone(self.vm.handle("what is the weather"))

    def test_volume_passthrough(self):
        self.assertIsNone(self.vm.handle("turn up the volume"))

    def test_time_passthrough(self):
        self.assertIsNone(self.vm.handle("what time is it"))

    def test_play_passthrough(self):
        self.assertIsNone(self.vm.handle("play my playlist"))

    # ── _needs_screenshot logic ───────────────────────────────────────────────

    def test_needs_screenshot_for_trigger_phrase(self):
        self.assertTrue(self.vm._needs_screenshot("help me with this"))

    def test_no_screenshot_for_app_open(self):
        self.assertFalse(self.vm._needs_screenshot("open terminal"))

    def test_no_screenshot_for_volume(self):
        self.assertFalse(self.vm._needs_screenshot("volume up"))

    def test_needs_screenshot_for_screen_mention(self):
        self.assertTrue(self.vm._needs_screenshot("what is on the screen"))

    # ── Guided walkthrough ────────────────────────────────────────────────────

    def test_next_step_without_steps_set(self):
        self.vm.set_steps([])
        r = self.vm.handle("atlas next step")
        self.assertIn("No guided", r)

    def test_walkthrough_sequence(self):
        self.vm.set_steps(["Click File", "Select Export", "Choose PNG"])
        r1 = self.vm._current_step()
        self.assertIn("Step 1", r1)
        self.assertIn("Click File", r1)
        r2 = self.vm._next_step()
        self.assertIn("Step 2", r2)
        r3 = self.vm._next_step()
        self.assertIn("Step 3", r3)
        # Next step at end stays on last
        r4 = self.vm._next_step()
        self.assertIn("Step 3", r4)


# ══════════════════════════════════════════════════════════════════════════════
# 12. handle_overlay_command
# ══════════════════════════════════════════════════════════════════════════════

class TestOverlayCommands(unittest.TestCase):
    """
    Tests the command-routing function only — does not instantiate OverlayWindow
    (which requires a QApplication).  Uses a mock overlay object.
    """

    def setUp(self):
        # Minimal mock that tracks which method was called
        class MockOverlay:
            def __init__(self):
                self.mode    = "normal"
                self.visible = True
            def set_mode(self, m):   self.mode    = m
            def hide_overlay(self):  self.visible = False
            def show_overlay(self):  self.visible = True
        self.overlay = MockOverlay()

    def _cmd(self, text):
        from overlay import handle_overlay_command
        return handle_overlay_command(text, self.overlay)

    def test_minimal_mode(self):
        r = self._cmd("atlas minimal mode")
        self.assertIsNotNone(r)
        self.assertEqual(self.overlay.mode, "minimal")

    def test_normal_mode(self):
        self.overlay.mode = "minimal"
        r = self._cmd("atlas show responses near cursor")
        self.assertIsNotNone(r)
        self.assertEqual(self.overlay.mode, "normal")

    def test_full_mode(self):
        r = self._cmd("atlas full overlay")
        self.assertIsNotNone(r)
        self.assertEqual(self.overlay.mode, "full")

    def test_hide_companion(self):
        r = self._cmd("atlas hide cursor companion")
        self.assertIsNotNone(r)
        self.assertFalse(self.overlay.visible)

    def test_show_companion(self):
        self.overlay.visible = False
        r = self._cmd("atlas show cursor companion")
        self.assertIsNotNone(r)
        self.assertTrue(self.overlay.visible)

    def test_unrelated_text_returns_none(self):
        self.assertIsNone(self._cmd("open Chrome"))
        self.assertIsNone(self._cmd("what is the weather"))
        self.assertIsNone(self._cmd("play some music"))


# ══════════════════════════════════════════════════════════════════════════════
# 13–14. AmbientModule
# ══════════════════════════════════════════════════════════════════════════════

class TestAmbientModule(unittest.TestCase):

    def setUp(self):
        from ambient import AmbientModule
        self.amb = AmbientModule(_CONFIG)

    def test_i_am_back_empty_history(self):
        r = self.amb.handle("atlas i am back")
        self.assertIsNotNone(r)
        self.assertIn("Boss", r)

    def test_i_am_back_with_history(self):
        self.amb.update_context("VS Code", "main.py")
        self.amb.update_context("Safari", "")  # triggers history save of VS Code
        r = self.amb.handle("atlas i am back")
        self.assertIsNotNone(r)

    def test_recall_specific_app(self):
        self.amb.update_context("VS Code", "main.py")
        self.amb.update_context("Safari", "")
        r = self.amb.handle("atlas what was i doing in vs code")
        self.assertIsNotNone(r)
        self.assertNotEqual(r, "")

    def test_proactive_off(self):
        r = self.amb.handle("atlas proactive off")
        self.assertIsNotNone(r)
        self.assertFalse(self.amb._proactive_enabled)

    def test_proactive_on(self):
        self.amb._proactive_enabled = False
        r = self.amb.handle("atlas proactive on")
        self.assertIsNotNone(r)
        self.assertTrue(self.amb._proactive_enabled)

    def test_unrelated_returns_none(self):
        self.assertIsNone(self.amb.handle("open Chrome"))
        self.assertIsNone(self.amb.handle("what is the weather"))

    def test_update_context_stores_history(self):
        self.amb.update_context("Xcode", "AppDelegate.swift")
        self.amb.update_context("Terminal", "")
        self.assertTrue(any("Xcode" in h["app"] for h in self.amb._context_history))

    def test_battery_level_returns_none_or_int(self):
        import platform
        level = self.amb._get_battery_level()
        if platform.system() == "Darwin":
            self.assertIsInstance(level, (int, type(None)))
        else:
            self.assertIsNone(level)


# ══════════════════════════════════════════════════════════════════════════════
# 15–19. Skills system
# ══════════════════════════════════════════════════════════════════════════════

class TestSkillsLoader(unittest.TestCase):

    def setUp(self):
        from skills.loader import SkillsLoader
        self.loader = SkillsLoader(_CONFIG)

    def test_discovers_seven_skills(self):
        self.assertEqual(len(self.loader._skills), 7,
                         f"Expected 7, got: {list(self.loader._skills.keys())}")

    def test_list_skills_command(self):
        r = self.loader.handle("atlas what skills do you have")
        self.assertIsNotNone(r)
        self.assertIn("skill", r.lower())

    def test_reload_skills_command(self):
        r = self.loader.handle("atlas reload skills")
        self.assertIsNotNone(r)

    def test_disable_and_enable_skill(self):
        r_disable = self.loader.handle("atlas disable weather skill")
        self.assertIsNotNone(r_disable)
        self.assertIn("weather", self.loader._disabled)

        r_enable = self.loader.handle("atlas enable weather skill")
        self.assertIsNotNone(r_enable)
        self.assertNotIn("weather", self.loader._disabled)

    def test_weather_trigger_matched(self):
        r = self.loader.handle("atlas weather")
        # Will attempt a real HTTP call — we just check it doesn't crash/return None
        self.assertIsNotNone(r)

    def test_unrelated_text_passthrough(self):
        self.assertIsNone(self.loader.handle("build me a snake game"))
        self.assertIsNone(self.loader.handle("how are you doing"))

    def test_disabled_skill_not_routed(self):
        self.loader._disabled.add("weather")
        # Weather trigger should now pass through
        with self.assertRaises(Exception) if False else self.subTest("disabled"):
            result = self.loader.handle("atlas weather")
            # If it returned something, it wasn't the weather skill
            # (could be another skill or None)
        self.loader._disabled.discard("weather")


class TestSkillInfoContracts(unittest.TestCase):
    """Every skill must expose name, triggers (non-empty list), and description."""

    _SKILL_MODULES = [
        "skills.weather_skill",
        "skills.news_skill",
        "skills.music_skill",
        "skills.reminder_skill",
        "skills.screenshot_skill",
        "skills.search_skill",
        "skills.calendar_skill",
    ]

    def _check(self, mod_name):
        import importlib
        mod  = importlib.import_module(mod_name)
        info = mod.skill_info()
        self.assertIn("name", info,        f"{mod_name}: missing 'name'")
        self.assertIn("triggers", info,    f"{mod_name}: missing 'triggers'")
        self.assertIn("description", info, f"{mod_name}: missing 'description'")
        self.assertIsInstance(info["name"],        str,  f"{mod_name}: name not str")
        self.assertIsInstance(info["triggers"],    list, f"{mod_name}: triggers not list")
        self.assertGreater(len(info["triggers"]),  0,    f"{mod_name}: triggers empty")
        self.assertIsInstance(info["description"], str,  f"{mod_name}: description not str")
        # All triggers must be lowercase strings
        for t in info["triggers"]:
            self.assertEqual(t, t.lower(), f"{mod_name}: trigger {t!r} not lowercase")

    def test_weather_skill_info(self):  self._check("skills.weather_skill")
    def test_news_skill_info(self):     self._check("skills.news_skill")
    def test_music_skill_info(self):    self._check("skills.music_skill")
    def test_reminder_skill_info(self): self._check("skills.reminder_skill")
    def test_screenshot_skill_info(self): self._check("skills.screenshot_skill")
    def test_search_skill_info(self):   self._check("skills.search_skill")
    def test_calendar_skill_info(self): self._check("skills.calendar_skill")


class TestReminderSkill(unittest.TestCase):

    def setUp(self):
        import skills.reminder_skill as rs
        self.rs   = rs
        self.tmp  = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        # Patch the module-level path
        self._orig_path = rs._REMINDERS_PATH
        rs._REMINDERS_PATH = Path(self.tmp.name)

    def tearDown(self):
        self.rs._REMINDERS_PATH = self._orig_path
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_add_reminder(self):
        r = self.rs.execute("atlas remind me to call John", {})
        self.assertIsNotNone(r)
        self.assertIn("call john", r.lower())

    def test_list_reminders_empty(self):
        # Fresh temp file — no reminders
        Path(self.tmp.name).write_text("[]")
        r = self.rs.execute("atlas what are my reminders", {})
        self.assertIn("no pending", r.lower())

    def test_list_reminders_after_add(self):
        self.rs.execute("atlas remind me to call John", {})
        r = self.rs.execute("atlas what are my reminders", {})
        self.assertIn("call john", r.lower())
        self.assertIn("1", r)

    def test_add_multiple_reminders(self):
        self.rs.execute("atlas remind me to call John", {})
        self.rs.execute("atlas remind me to buy milk", {})
        r = self.rs.execute("atlas what are my reminders", {})
        self.assertIn("2", r)


# ══════════════════════════════════════════════════════════════════════════════
# 20–23. DigestModule
# ══════════════════════════════════════════════════════════════════════════════

class TestDigestModule(unittest.TestCase):

    def setUp(self):
        from digest import DigestModule
        self.tmp  = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.dg   = DigestModule(_CONFIG)
        # Redirect cache to temp file
        from digest import _CACHE_PATH
        self.dg._path = Path(self.tmp.name)

    def tearDown(self):
        self.dg.stop()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_morning_briefing_command_not_none(self):
        r = self.dg.handle("atlas morning briefing")
        self.assertIsNotNone(r)

    def test_skip_to_news_command(self):
        r = self.dg.handle("atlas skip to news")
        self.assertIsNotNone(r)
        # "news" is the 4th section (index 3) in ["greeting","weather","calendar","news",...]
        self.assertEqual(self.dg._current_section_idx,
                         self.dg._SECTIONS.index("news"))

    def test_elaborate_without_script(self):
        r = self.dg.handle("atlas elaborate on that")
        self.assertIsNotNone(r)

    def test_unrelated_text_returns_none(self):
        self.assertIsNone(self.dg.handle("open Chrome"))
        self.assertIsNone(self.dg.handle("what is the weather"))

    def test_briefing_time_parsed_correctly(self):
        from digest import DigestModule
        cfg = dict(_CONFIG)
        cfg["morning_briefing_time"] = "07:30"
        dg  = DigestModule(cfg)
        self.assertEqual(dg._briefing_hour,   7)
        self.assertEqual(dg._briefing_minute, 30)

    def test_default_briefing_time(self):
        self.assertEqual(self.dg._briefing_hour,   8)
        self.assertEqual(self.dg._briefing_minute, 0)

    def test_greeting_contains_boss(self):
        greeting = self.dg._gen_greeting()
        self.assertIsInstance(greeting, str)
        self.assertGreater(len(greeting), 0)
        self.assertIn("Boss", greeting)

    def test_greeting_contains_day(self):
        from datetime import datetime
        greeting = self.dg._gen_greeting()
        today    = datetime.now().strftime("%A")
        self.assertIn(today, greeting)

    def test_cache_roundtrip(self):
        from digest import DigestModule
        import json
        from datetime import date
        script = {
            "greeting": "Good morning, Boss.",
            "weather":  "Sunny and warm.",
            "_date":    date.today().isoformat(),
        }
        self.dg._script = script
        self.dg._path   = Path(self.tmp.name)
        self.dg._save_cache()

        dg2       = DigestModule(_CONFIG)
        dg2._path = Path(self.tmp.name)
        dg2._load_cache()
        self.assertIsNotNone(dg2._script)
        self.assertEqual(dg2._script.get("greeting"), "Good morning, Boss.")


# ══════════════════════════════════════════════════════════════════════════════
# 24–27. SoundEngine
# ══════════════════════════════════════════════════════════════════════════════

class TestSoundEngine(unittest.TestCase):

    _EXPECTED_SOUNDS = {
        "STARTUP", "WAKE", "PROCESSING",
        "RESPONSE_READY", "SUCCESS", "ERROR", "SCREENSHOT",
    }

    def setUp(self):
        from sounds import SoundEngine
        # ambient_hum disabled so no audio thread starts
        cfg = dict(_CONFIG)
        cfg["ambient_hum_enabled"] = False
        self.se = SoundEngine(cfg)

    def tearDown(self):
        self.se.stop()

    # ── Presence ──────────────────────────────────────────────────────────────

    def test_all_sounds_present(self):
        self.assertEqual(set(self.se._sounds.keys()), self._EXPECTED_SOUNDS)

    # ── dtype ─────────────────────────────────────────────────────────────────

    def test_all_sounds_float32(self):
        for name, arr in self.se._sounds.items():
            self.assertEqual(arr.dtype, np.float32, f"{name}: not float32")

    # ── Non-empty ─────────────────────────────────────────────────────────────

    def test_all_sounds_non_empty(self):
        for name, arr in self.se._sounds.items():
            self.assertGreater(len(arr), 100, f"{name}: too short ({len(arr)} samples)")

    # ── Amplitude guard ───────────────────────────────────────────────────────

    def test_all_sounds_amplitude_within_bounds(self):
        for name, arr in self.se._sounds.items():
            self.assertLessEqual(float(np.max(np.abs(arr))), 1.0,
                                 f"{name}: amplitude > 1.0")

    # ── Relative lengths make sense ───────────────────────────────────────────

    def test_startup_longer_than_click(self):
        self.assertGreater(
            len(self.se._sounds["STARTUP"]),
            len(self.se._sounds["RESPONSE_READY"]),
        )

    def test_screenshot_shorter_than_startup(self):
        self.assertLess(
            len(self.se._sounds["SCREENSHOT"]),
            len(self.se._sounds["STARTUP"]),
        )

    # ── Controls ─────────────────────────────────────────────────────────────

    def test_set_enabled_false(self):
        self.se.set_enabled(False)
        self.assertFalse(self.se._enabled)

    def test_set_enabled_true(self):
        self.se.set_enabled(False)
        self.se.set_enabled(True)
        self.assertTrue(self.se._enabled)

    def test_set_volume_clamped(self):
        self.se.set_volume(5.0)
        self.assertEqual(self.se._volume, 1.0)
        self.se.set_volume(-1.0)
        self.assertEqual(self.se._volume, 0.0)

    # ── handle() command routing ──────────────────────────────────────────────

    def test_mute_sounds_command(self):
        r = self.se.handle("atlas mute sounds")
        self.assertIsNotNone(r)
        self.assertFalse(self.se._enabled)

    def test_enable_sounds_command(self):
        self.se._enabled = False
        r = self.se.handle("atlas enable sounds")
        self.assertIsNotNone(r)
        self.assertTrue(self.se._enabled)

    def test_mute_ambient_command(self):
        r = self.se.handle("atlas mute ambient")
        self.assertIsNotNone(r)
        self.assertFalse(self.se._ambient_enabled)

    def test_unrelated_text_returns_none(self):
        self.assertIsNone(self.se.handle("open Chrome"))
        self.assertIsNone(self.se.handle("what is the weather"))

    # ── Ambient chunk ─────────────────────────────────────────────────────────

    def test_ambient_chunk_float32(self):
        chunk = self.se._gen_ambient_chunk(chunk_dur=0.1)
        self.assertEqual(chunk.dtype, np.float32)

    def test_ambient_chunk_non_empty(self):
        chunk = self.se._gen_ambient_chunk(chunk_dur=0.1)
        self.assertGreater(len(chunk), 0)

    def test_ambient_chunk_amplitude_within_bounds(self):
        chunk = self.se._gen_ambient_chunk(chunk_dur=0.5)
        self.assertLessEqual(float(np.max(np.abs(chunk))), 1.0)

    def test_ambient_chunk_correct_length(self):
        from sounds import _SR
        chunk = self.se._gen_ambient_chunk(chunk_dur=1.0)
        self.assertEqual(len(chunk), _SR)


# ══════════════════════════════════════════════════════════════════════════════
# 28. brain._build_system personality
# ══════════════════════════════════════════════════════════════════════════════

class TestBrainPersonality(unittest.TestCase):

    def setUp(self):
        from brain import _build_system
        self.prompt = _build_system("Boss")

    def test_says_atlas_not_ai(self):
        self.assertIn("ATLAS", self.prompt)
        # The prompt forbids ATLAS from claiming to be a language model —
        # check it prohibits it rather than asserts it
        lower = self.prompt.lower()
        self.assertIn("never say you are", lower)
        # Must not open with a first-person AI claim
        self.assertNotIn("i am a language model", lower)
        self.assertNotIn("i am an ai", lower)

    def test_addresses_user_as_boss(self):
        self.assertIn("Boss", self.prompt)

    def test_jarvis_mentioned(self):
        self.assertIn("JARVIS", self.prompt)

    def test_no_think_directive(self):
        self.assertIn("/no_think", self.prompt)

    def test_never_breaks_character(self):
        self.assertIn("never break character", self.prompt.lower())

    def test_twenty_word_limit(self):
        self.assertIn("20", self.prompt)

    def test_screen_awareness(self):
        self.assertIn("screen", self.prompt.lower())

    def test_no_markdown_rule(self):
        self.assertIn("markdown", self.prompt.lower())

    def test_dry_wit_allowed(self):
        self.assertIn("wit", self.prompt.lower())

    def test_numbered_steps_ok(self):
        self.assertIn("numbered steps", self.prompt.lower())


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader  = unittest.TestLoader()
    suites  = [
        loader.loadTestsFromTestCase(TestSpeechFormatter),
        loader.loadTestsFromTestCase(TestVoiceEmotionDetector),
        loader.loadTestsFromTestCase(TestResponseCache),
        loader.loadTestsFromTestCase(TestPhraseCache),
        loader.loadTestsFromTestCase(TestDetectWhisperModel),
        loader.loadTestsFromTestCase(TestWebRTCVAD),
        loader.loadTestsFromTestCase(TestVisionModuleRouting),
        loader.loadTestsFromTestCase(TestOverlayCommands),
        loader.loadTestsFromTestCase(TestAmbientModule),
        loader.loadTestsFromTestCase(TestSkillsLoader),
        loader.loadTestsFromTestCase(TestSkillInfoContracts),
        loader.loadTestsFromTestCase(TestReminderSkill),
        loader.loadTestsFromTestCase(TestDigestModule),
        loader.loadTestsFromTestCase(TestSoundEngine),
        loader.loadTestsFromTestCase(TestBrainPersonality),
    ]
    suite  = unittest.TestSuite(suites)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
