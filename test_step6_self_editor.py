"""
Step 6 tests — Self-Modifying Code Engine

Run:  python3 test_step6_self_editor.py
All tests must PASS before Step 6 is considered complete.

What is tested
──────────────
  1.  EditResult.as_voice_response()      — success / failure / rollback messages
  2.  Backup.create() / restore()         — round-trip with a temp file
  3.  Backup.cleanup()                    — removes oldest files beyond max_backups
  4.  Changelog.append() / recent()       — JSON persistence
  5.  CodePatcher — replace              — first-occurrence replacement
  6.  CodePatcher — insert_after         — content inserted after anchor
  7.  CodePatcher — insert_before        — content inserted before anchor
  8.  CodePatcher — full_rewrite         — entire file replaced
  9.  CodePatcher errors                 — old-not-found, empty content, unknown type
 10.  SelfEditor.apply_edit() success    — full loop with mock test runner (pass)
 11.  SelfEditor.apply_edit() rollback   — automatic rollback when tests fail
 12.  SelfEditor.apply_edit() protected  — blocked without confirm_cb
 13.  SelfEditor.apply_edit() protected  — blocked when confirm_cb returns False
 14.  SelfEditor.apply_edit() path guard — path outside root → blocked
 15.  SelfEditor.apply_edit() syntax err — bad Python caught before write
 16.  SelfEditor.rollback_last()         — manually restores last edited file
 17.  SelfEditor.read_source()           — reads file content
 18.  SelfEditor.patch() / rollback()    — legacy API compatibility
 19.  _is_self_edit()                    — correct query classification
 20.  ATLASCore.set_self_editor()        — wiring check
"""

import sys
import os
import json
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ATLAS_ROOT", str(Path(__file__).parent))

from self_editor import (
    SelfEditor, EditResult, Backup, Changelog,
    CodePatcher, TestRunner, TestResult,
    _PROTECTED_FILES,
)
from core import _is_self_edit


# ── Helpers ───────────────────────────────────────────────────────────────────

_CONFIG = {
    "self_editor": {
        "changelog_path": "test_changelog_tmp.json",
        "backup_dir":     ".test_atlas_backups_tmp",
        "max_backups":    50,
    }
}

_SIMPLE_PY = "x = 1\ny = 2\nz = 3\n"


class _PassRunner:
    """Mock TestRunner that always reports passing tests."""
    def run(self, *args, **kwargs):
        return TestResult(passed=True, message="All tests passed (mock).")


class _FailRunner:
    """Mock TestRunner that always reports failing tests."""
    def run(self, *args, **kwargs):
        return TestResult(passed=False, message="AssertionError: mock failure.")


def _make_editor(tmpdir: Path, runner=None, confirm_cb=None) -> SelfEditor:
    cfg = {
        "self_editor": {
            "changelog_path": str(tmpdir / "changelog.json"),
            "backup_dir":     str(tmpdir / "backups"),
            "max_backups":    50,
        }
    }
    editor = SelfEditor(cfg, tmpdir, confirm_cb=confirm_cb)
    if runner is not None:
        editor._test_runner = runner
    return editor


# ══════════════════════════════════════════════════════════════════════════════
# 1. EditResult.as_voice_response()
# ══════════════════════════════════════════════════════════════════════════════

class TestEditResult(unittest.TestCase):

    def test_success_with_description(self):
        r = EditResult(success=True, description="Added new keyword", tests_passed=True)
        msg = r.as_voice_response()
        self.assertIn("Added new keyword", msg)
        self.assertIn("tests passed", msg.lower())

    def test_success_no_description(self):
        r = EditResult(success=True, tests_passed=True)
        msg = r.as_voice_response()
        self.assertIn("applied", msg.lower())

    def test_rollback_response(self):
        r = EditResult(success=False, rolled_back=True, error="SyntaxError at line 4.")
        msg = r.as_voice_response()
        self.assertIn("reverted", msg.lower())
        self.assertIn("SyntaxError", msg)

    def test_failure_response(self):
        r = EditResult(success=False, error="File not found: foo.py")
        msg = r.as_voice_response()
        self.assertIn("couldn't", msg.lower())
        self.assertIn("File not found", msg)

    def test_period_appended(self):
        r = EditResult(success=True, description="Added keyword", tests_passed=False)
        self.assertTrue(r.as_voice_response().endswith("."))


# ══════════════════════════════════════════════════════════════════════════════
# 2 & 3. Backup
# ══════════════════════════════════════════════════════════════════════════════

class TestBackup(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bdir = self.tmp / "backups"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_restore(self):
        src = self.tmp / "test.py"
        src.write_text("original content")
        bk = Backup(self.bdir)
        backup = bk.create(src)
        self.assertTrue(backup.exists())
        # Overwrite original
        src.write_text("modified content")
        self.assertEqual(src.read_text(), "modified content")
        # Restore
        ok = bk.restore(backup, src)
        self.assertTrue(ok)
        self.assertEqual(src.read_text(), "original content")

    def test_backup_filename_contains_timestamp(self):
        src = self.tmp / "file.py"
        src.write_text("x = 1")
        bk = Backup(self.bdir)
        backup = bk.create(src)
        self.assertIn("file.py.", backup.name)
        self.assertTrue(backup.name.endswith(".bak"))

    def test_cleanup_removes_excess(self):
        # Seed the backup dir directly (bypasses Backup.create auto-trim)
        # so we can test cleanup() in isolation with a known count.
        self.bdir.mkdir(parents=True, exist_ok=True)
        bk = Backup(self.bdir, max_backups=3)
        for i in range(5):
            (self.bdir / f"file.py.202601{i:02d}_120000_000000.bak").write_text(f"v{i}")
        removed  = bk.cleanup()
        remaining = list(self.bdir.glob("*.bak"))
        self.assertEqual(removed, 2)
        self.assertEqual(len(remaining), 3)

    def test_latest_for(self):
        src = self.tmp / "file.py"
        src.write_text("v1")
        bk = Backup(self.bdir)
        bk.create(src)
        src.write_text("v2")
        latest = bk.create(src)
        found = bk.latest_for("file.py")
        self.assertEqual(found, latest)

    def test_latest_for_missing(self):
        bk = Backup(self.bdir)
        self.assertIsNone(bk.latest_for("nonexistent.py"))


# ══════════════════════════════════════════════════════════════════════════════
# 4. Changelog
# ══════════════════════════════════════════════════════════════════════════════

class TestChangelog(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cl  = Changelog(self.tmp / "changelog.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_and_recent(self):
        self.cl.append({"file": "a.py", "success": True})
        self.cl.append({"file": "b.py", "success": False})
        records = self.cl.recent(n=10)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["file"], "a.py")
        self.assertEqual(records[1]["file"], "b.py")

    def test_recent_limit(self):
        for i in range(10):
            self.cl.append({"i": i})
        records = self.cl.recent(n=3)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[-1]["i"], 9)

    def test_last_entry(self):
        self.cl.append({"file": "first.py"})
        self.cl.append({"file": "last.py"})
        self.assertEqual(self.cl.last_entry()["file"], "last.py")

    def test_last_entry_empty(self):
        self.assertIsNone(self.cl.last_entry())

    def test_json_persists(self):
        self.cl.append({"file": "persistent.py"})
        # Create a new Changelog instance pointing to the same file
        cl2 = Changelog(self.tmp / "changelog.json")
        self.assertEqual(cl2.recent(1)[0]["file"], "persistent.py")


# ══════════════════════════════════════════════════════════════════════════════
# 5–9. CodePatcher
# ══════════════════════════════════════════════════════════════════════════════

class TestCodePatcher(unittest.TestCase):

    def setUp(self):
        self.tmp  = Path(tempfile.mkdtemp())
        self.src  = self.tmp / "sample.py"
        self.src.write_text(_SIMPLE_PY)
        self.cp = CodePatcher()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── replace ──────────────────────────────────────────────────────────────

    def test_replace_first_occurrence(self):
        result = self.cp.apply(
            {"type": "replace", "old": "y = 2", "new": "y = 99"},
            self.src,
        )
        self.assertIn("y = 99", result)
        self.assertNotIn("y = 2", result)
        self.assertIn("x = 1", result)   # unchanged

    def test_replace_only_first(self):
        self.src.write_text("a = 1\na = 1\n")
        result = self.cp.apply(
            {"type": "replace", "old": "a = 1", "new": "a = 9"},
            self.src,
        )
        self.assertEqual(result.count("a = 9"), 1)
        self.assertEqual(result.count("a = 1"), 1)

    def test_replace_old_not_found(self):
        with self.assertRaises(ValueError) as ctx:
            self.cp.apply({"type": "replace", "old": "not_in_file", "new": "x"}, self.src)
        self.assertIn("not found", str(ctx.exception))

    def test_replace_empty_old(self):
        with self.assertRaises(ValueError):
            self.cp.apply({"type": "replace", "old": "", "new": "x"}, self.src)

    # ── insert_after ─────────────────────────────────────────────────────────

    def test_insert_after(self):
        result = self.cp.apply(
            {"type": "insert_after", "after": "x = 1", "insert": "# comment"},
            self.src,
        )
        lines = result.splitlines()
        idx = lines.index("x = 1")
        self.assertEqual(lines[idx + 1].strip(), "# comment")

    def test_insert_after_not_found(self):
        with self.assertRaises(ValueError):
            self.cp.apply(
                {"type": "insert_after", "after": "not_here", "insert": "x"},
                self.src,
            )

    # ── insert_before ─────────────────────────────────────────────────────────

    def test_insert_before(self):
        result = self.cp.apply(
            {"type": "insert_before", "before": "z = 3", "insert": "# before z"},
            self.src,
        )
        lines = result.splitlines()
        idx = next(i for i, l in enumerate(lines) if "z = 3" in l)
        self.assertIn("before z", lines[idx - 1])

    # ── full_rewrite ─────────────────────────────────────────────────────────

    def test_full_rewrite(self):
        new_code = "a = 42\n"
        result   = self.cp.apply({"type": "full_rewrite", "content": new_code}, self.src)
        self.assertEqual(result, new_code)

    def test_full_rewrite_empty(self):
        with self.assertRaises(ValueError):
            self.cp.apply({"type": "full_rewrite", "content": ""}, self.src)

    # ── unknown type ──────────────────────────────────────────────────────────

    def test_unknown_type(self):
        with self.assertRaises(ValueError) as ctx:
            self.cp.apply({"type": "teleport"}, self.src)
        self.assertIn("Unknown", str(ctx.exception))


# ══════════════════════════════════════════════════════════════════════════════
# 10. SelfEditor.apply_edit() — success path
# ══════════════════════════════════════════════════════════════════════════════

class TestSelfEditorSuccess(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "sample.py"
        self.src.write_text(_SIMPLE_PY)
        self.editor = _make_editor(self.tmp, runner=_PassRunner())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replace_applies_and_logs(self):
        result = self.editor.apply_edit({
            "type":        "replace",
            "file":        "sample.py",
            "old":         "y = 2",
            "new":         "y = 200",
            "description": "Changed y to 200",
        })
        self.assertTrue(result.success)
        self.assertIn("y = 200", self.src.read_text())
        self.assertTrue(result.tests_passed)

    def test_backup_created(self):
        self.editor.apply_edit({
            "type": "replace", "file": "sample.py",
            "old": "x = 1", "new": "x = 99", "description": "x change",
        })
        bk_dir = self.tmp / "backups"
        backups = list(bk_dir.glob("sample.py.*.bak"))
        self.assertEqual(len(backups), 1)

    def test_changelog_entry_written(self):
        self.editor.apply_edit({
            "type": "replace", "file": "sample.py",
            "old": "x = 1", "new": "x = 7", "description": "x7",
        })
        entries = self.editor.list_changelog()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["file"], "sample.py")
        self.assertTrue(entries[0]["success"])


# ══════════════════════════════════════════════════════════════════════════════
# 11. SelfEditor.apply_edit() — automatic rollback when tests fail
# ══════════════════════════════════════════════════════════════════════════════

class TestSelfEditorRollback(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "sample.py"
        self.src.write_text(_SIMPLE_PY)
        self.editor = _make_editor(self.tmp, runner=_FailRunner())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_reverted_on_test_failure(self):
        original = self.src.read_text()
        result = self.editor.apply_edit({
            "type": "replace", "file": "sample.py",
            "old": "y = 2", "new": "y = BAD",
            "description": "Bad change",
        })
        self.assertFalse(result.success)
        self.assertTrue(result.rolled_back)
        self.assertFalse(result.tests_passed)
        # File must be restored
        self.assertEqual(self.src.read_text(), original)

    def test_rollback_response_mentions_revert(self):
        result = self.editor.apply_edit({
            "type": "replace", "file": "sample.py",
            "old": "x = 1", "new": "x = OOPS",
            "description": "Oops",
        })
        msg = result.as_voice_response()
        self.assertIn("reverted", msg.lower())


# ══════════════════════════════════════════════════════════════════════════════
# 12 & 13. Protected file gate
# ══════════════════════════════════════════════════════════════════════════════

class TestProtectedFiles(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in _PROTECTED_FILES:
            (self.tmp / name).write_text("# protected\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_protected_blocked_without_cb(self):
        editor = _make_editor(self.tmp, runner=_PassRunner(), confirm_cb=None)
        result = editor.apply_edit({
            "type": "replace", "file": "core.py",
            "old": "# protected", "new": "# hacked",
            "description": "hack core",
        })
        self.assertFalse(result.success)
        self.assertIn("protected", result.error.lower())

    def test_protected_blocked_when_cb_returns_false(self):
        editor = _make_editor(self.tmp, runner=_PassRunner(), confirm_cb=lambda _: False)
        result = editor.apply_edit({
            "type": "replace", "file": "core.py",
            "old": "# protected", "new": "# hacked",
            "description": "hack core",
        })
        self.assertFalse(result.success)
        self.assertIn("cancelled", result.error.lower())

    def test_protected_allowed_when_cb_returns_true(self):
        editor = _make_editor(self.tmp, runner=_PassRunner(), confirm_cb=lambda _: True)
        result = editor.apply_edit({
            "type": "replace", "file": "core.py",
            "old": "# protected", "new": "# confirmed change",
            "description": "authorized change to core.py",
        })
        self.assertTrue(result.success, result.error)
        self.assertIn("confirmed change", (self.tmp / "core.py").read_text())


# ══════════════════════════════════════════════════════════════════════════════
# 14. Path escape guard
# ══════════════════════════════════════════════════════════════════════════════

class TestPathGuard(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_outside_root_blocked(self):
        editor = _make_editor(self.tmp, runner=_PassRunner())
        result = editor.apply_edit({
            "type": "full_rewrite",
            "file": "../../etc/passwd",
            "content": "malicious",
            "description": "path traversal attempt",
        })
        self.assertFalse(result.success)
        self.assertIn("escapes", result.error.lower())

    def test_no_file_field(self):
        editor = _make_editor(self.tmp, runner=_PassRunner())
        result = editor.apply_edit({"type": "replace", "old": "x", "new": "y"})
        self.assertFalse(result.success)
        self.assertIn("file", result.error.lower())


# ══════════════════════════════════════════════════════════════════════════════
# 15. Syntax error caught before write
# ══════════════════════════════════════════════════════════════════════════════

class TestSyntaxCheck(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "good.py").write_text("x = 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_syntax_error_blocked(self):
        editor = _make_editor(self.tmp, runner=_PassRunner())
        result = editor.apply_edit({
            "type":    "replace",
            "file":    "good.py",
            "old":     "x = 1",
            "new":     "def broken(\n",   # invalid Python
            "description": "break it",
        })
        self.assertFalse(result.success)
        self.assertIn("syntax", result.error.lower())
        # File must remain unchanged
        self.assertEqual((self.tmp / "good.py").read_text(), "x = 1\n")


# ══════════════════════════════════════════════════════════════════════════════
# 16. rollback_last()
# ══════════════════════════════════════════════════════════════════════════════

class TestRollbackLast(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "target.py"
        self.src.write_text("original = True\n")
        self.editor = _make_editor(self.tmp, runner=_PassRunner())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rollback_restores_file(self):
        self.editor.apply_edit({
            "type": "replace", "file": "target.py",
            "old": "original = True", "new": "original = False",
            "description": "flip flag",
        })
        self.assertIn("False", self.src.read_text())
        msg = self.editor.rollback_last()
        self.assertIn("Rolled back", msg)
        self.assertIn("True", self.src.read_text())

    def test_rollback_empty_changelog(self):
        msg = self.editor.rollback_last()
        self.assertIn("Nothing", msg)


# ══════════════════════════════════════════════════════════════════════════════
# 17. read_source()
# ══════════════════════════════════════════════════════════════════════════════

class TestReadSource(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "hello.py").write_text("print('hello')\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_existing_file(self):
        editor = _make_editor(self.tmp)
        content = editor.read_source("hello.py")
        self.assertIn("hello", content)

    def test_missing_file_error(self):
        editor = _make_editor(self.tmp)
        content = editor.read_source("missing.py")
        self.assertIn("not found", content.lower())


# ══════════════════════════════════════════════════════════════════════════════
# 18. Legacy API: patch() / rollback()
# ══════════════════════════════════════════════════════════════════════════════

class TestLegacyAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "script.py").write_text("a = 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_patch_rewrites_file(self):
        editor = _make_editor(self.tmp, runner=_PassRunner())
        ok = editor.patch("script.py", "a = 2\n")
        self.assertTrue(ok)
        self.assertEqual((self.tmp / "script.py").read_text(), "a = 2\n")

    def test_rollback_legacy(self):
        editor = _make_editor(self.tmp, runner=_PassRunner())
        editor.patch("script.py", "a = 99\n")
        ok = editor.rollback("script.py")
        self.assertTrue(ok)
        self.assertEqual((self.tmp / "script.py").read_text(), "a = 1\n")

    def test_patch_protected_without_confirmed(self):
        for p in _PROTECTED_FILES:
            (self.tmp / p).write_text("# p\n")
            editor = _make_editor(self.tmp, runner=_PassRunner())
            ok = editor.patch(p, "# hacked\n", confirmed=False)
            self.assertFalse(ok, f"Should block unconfirmed write to {p}")

    def test_changelog_legacy(self):
        editor = _make_editor(self.tmp, runner=_PassRunner())
        editor.patch("script.py", "a = 77\n")
        records = editor.changelog()
        self.assertGreater(len(records), 0)


# ══════════════════════════════════════════════════════════════════════════════
# 19. _is_self_edit()
# ══════════════════════════════════════════════════════════════════════════════

class TestIsSelfEdit(unittest.TestCase):

    def _yes(self, text):
        self.assertTrue(_is_self_edit(text), f"Expected SELF-EDIT for: {text!r}")

    def _no(self, text):
        self.assertFalse(_is_self_edit(text), f"Expected NOT SELF-EDIT for: {text!r}")

    def test_modify_code(self):         self._yes("modify your code to add a feature")
    def test_edit_code(self):           self._yes("edit your code please")
    def test_update_code(self):         self._yes("update your code to fix the bug")
    def test_fix_code(self):            self._yes("fix your code so it works")
    def test_rewrite_code(self):        self._yes("rewrite your code for performance")
    def test_edit_web_py(self):         self._yes("edit web.py to add another keyword")
    def test_modify_control_py(self):   self._yes("modify control.py to support drag")
    def test_update_voice_py(self):     self._yes("update voice.py with faster VAD")
    def test_add_to_web_py(self):       self._yes("add to web.py a new search trigger")
    def test_self_modify(self):         self._yes("self modify to improve startup time")
    def test_code_change(self):         self._yes("change the code in web.py please")

    def test_not_chat(self):            self._no("what is machine learning")
    def test_not_weather(self):         self._no("what's the weather today")
    def test_not_open_app(self):        self._no("open Safari please")
    def test_not_search(self):          self._no("search the web for news")


# ══════════════════════════════════════════════════════════════════════════════
# 20. ATLASCore.set_self_editor() wiring
# ══════════════════════════════════════════════════════════════════════════════

class TestATLASCoreWiring(unittest.TestCase):

    def test_set_self_editor(self):
        from core import ATLASCore
        cfg  = {"core": {}, "api": {}}
        core = ATLASCore(cfg)
        self.assertIsNone(core._editor)

        tmp    = Path(tempfile.mkdtemp())
        editor = _make_editor(tmp)
        core.set_self_editor(editor)
        self.assertIs(core._editor, editor)
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.discover(".", pattern="test_step6_self_editor.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
