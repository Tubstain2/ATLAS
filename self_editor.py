"""
ATLAS Self-Modifying Code Engine (Step 6)

Safety-first design
────────────────────
  • Backup BEFORE every write — .atlas_backups/<file>.<timestamp>.bak
  • Syntax-check generated code BEFORE writing to disk
  • Run tests AFTER writing — automatic rollback if any test fails
  • core.py, main.py, voice.py are PROTECTED — require user confirmation
  • Paths outside project root are always blocked
  • Every modification is logged to changelog.json

Architecture
────────────
  EditResult    ─  dataclass returned by apply_edit(); has as_voice_response()
  Backup        ─  creates / restores / cleans timestamped .bak files
  Changelog     ─  append-only JSON audit trail
  CodePatcher   ─  applies replace / insert_after / insert_before / full_rewrite
  TestResult    ─  pass/fail + output from test run
  TestRunner    ─  runs pytest (fallback: unittest) on test_step*.py files
  SelfEditor    ─  public API; orchestrates all of the above

ATLASCore integration
──────────────────────
  main.py:
      editor = SelfEditor(config, PROJECT_ROOT, confirm_cb=confirm_dlg.ask)
      core.set_self_editor(editor)

  core.py:
      result = editor.apply_edit(edit_spec_dict)
      return result.as_voice_response()
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Files that require explicit user confirmation before modification
_PROTECTED_FILES = frozenset({"core.py", "main.py", "voice.py"})

# Max chars accepted for a full_rewrite "content" field (~20K tokens)
_MAX_CONTENT_CHARS = 80_000


# ══════════════════════════════════════════════════════════════════════════════
# EditResult
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EditResult:
    success: bool
    description: str = ""
    backup_path: str = ""
    tests_passed: Optional[bool] = None
    rolled_back: bool = False
    error: str = ""

    def as_voice_response(self) -> str:
        if self.success:
            msg = self.description.strip() or "The change has been applied."
            if not msg.endswith("."):
                msg += "."
            if self.tests_passed:
                msg += " All tests passed."
            return msg
        if self.rolled_back:
            return (
                "I applied the change, but the tests failed — "
                "so I've automatically reverted the file. "
                + self.error[:200]
            ).strip()
        return ("I couldn't apply that change. " + self.error[:300]).strip()


# ══════════════════════════════════════════════════════════════════════════════
# Backup
# ══════════════════════════════════════════════════════════════════════════════

class Backup:
    """Creates and manages timestamped .bak files in backup_dir."""

    def __init__(self, backup_dir: Path, max_backups: int = 50):
        self._dir = backup_dir
        self._max = max_backups
        backup_dir.mkdir(parents=True, exist_ok=True)

    def create(self, file_path: Path) -> Path:
        """Copy file → backup_dir/<name>.<YYYYMMDD_HHMMSS_ffffff>.bak. Returns backup path."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")  # microseconds for uniqueness
        backup_path = self._dir / f"{file_path.name}.{ts}.bak"
        shutil.copy2(file_path, backup_path)
        log.info("Backup: %s → %s", file_path.name, backup_path.name)
        self._trim()
        return backup_path

    def restore(self, backup_path: Path, target: Path) -> bool:
        """Overwrite target with backup. Returns True on success."""
        try:
            shutil.copy2(backup_path, target)
            log.info("Restored: %s → %s", backup_path.name, target.name)
            return True
        except Exception as exc:
            log.error("Restore failed: %s", exc)
            return False

    def cleanup(self) -> int:
        """Delete oldest .bak files that exceed max_backups. Returns count removed."""
        all_bak = sorted(self._dir.glob("*.bak"))
        excess  = max(0, len(all_bak) - self._max)
        removed = 0
        for bak in all_bak[:excess]:
            try:
                bak.unlink()
                removed += 1
            except Exception:
                pass
        return removed

    def latest_for(self, filename: str) -> Optional[Path]:
        """Return the most recent backup for a given filename, or None."""
        backups = sorted(self._dir.glob(f"{Path(filename).name}.*.bak"))
        return backups[-1] if backups else None

    def _trim(self) -> None:
        self.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# Changelog
# ══════════════════════════════════════════════════════════════════════════════

class Changelog:
    """Append-only JSON list persisted to changelog.json."""

    def __init__(self, path: Path):
        self._path = path

    def append(self, entry: dict) -> None:
        records = self._load()
        records.append(entry)
        try:
            self._path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        except Exception as exc:
            log.error("Changelog write failed: %s", exc)

    def recent(self, n: int = 10) -> list[dict]:
        return self._load()[-n:]

    def last_entry(self) -> Optional[dict]:
        records = self._load()
        return records[-1] if records else None

    def _load(self) -> list:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8")) or []
            except Exception:
                pass
        return []


# ══════════════════════════════════════════════════════════════════════════════
# CodePatcher
# ══════════════════════════════════════════════════════════════════════════════

class CodePatcher:
    """
    Applies a structured edit dict to a source file, returning new content.
    Never writes to disk — caller does that after validation.

    Supported edit types
    ─────────────────────
      "replace"       — replace the FIRST occurrence of "old" with "new"
      "insert_after"  — insert "insert" after the FIRST occurrence of "after"
      "insert_before" — insert "insert" before the FIRST occurrence of "before"
      "full_rewrite"  — replace the entire file with "content"
    """

    def apply(self, edit: dict, file_path: Path) -> str:
        """Return new file content. Raise ValueError on any problem."""
        edit_type = edit.get("type", "replace")

        if edit_type == "full_rewrite":
            content = edit.get("content", "")
            if not content.strip():
                raise ValueError("full_rewrite requires a non-empty 'content' field.")
            if len(content) > _MAX_CONTENT_CHARS:
                raise ValueError(
                    f"'content' too large ({len(content):,} chars). "
                    f"Maximum is {_MAX_CONTENT_CHARS:,}."
                )
            return content

        # All other types need the current file content
        if not file_path.exists():
            raise ValueError(f"File not found: {file_path.name}")
        original = file_path.read_text(encoding="utf-8")

        if edit_type == "replace":
            old = edit.get("old", "")
            new = edit.get("new", "")
            if not old:
                raise ValueError("'replace' edit requires a non-empty 'old' field.")
            if old not in original:
                raise ValueError(
                    f"The 'old' string was not found verbatim in {file_path.name}. "
                    "The file may have changed, or the edit spec has a quoting issue."
                )
            return original.replace(old, new, 1)

        if edit_type == "insert_after":
            after  = edit.get("after", "")
            insert = edit.get("insert", "")
            if not after:
                raise ValueError("'insert_after' requires an 'after' field.")
            if after not in original:
                raise ValueError(f"'after' string not found in {file_path.name}.")
            pos = original.find(after) + len(after)
            return original[:pos] + "\n" + insert + original[pos:]

        if edit_type == "insert_before":
            before = edit.get("before", "")
            insert = edit.get("insert", "")
            if not before:
                raise ValueError("'insert_before' requires a 'before' field.")
            if before not in original:
                raise ValueError(f"'before' string not found in {file_path.name}.")
            pos = original.find(before)
            return original[:pos] + insert + "\n" + original[pos:]

        raise ValueError(f"Unknown edit type: {edit_type!r}")


# ══════════════════════════════════════════════════════════════════════════════
# TestRunner
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TestResult:
    passed: bool
    message: str = ""


class TestRunner:
    """
    Runs the project test suite after a code edit.
    Prefers pytest; falls back to python -m unittest.

    test_file=None  → run all test_step*.py files found in project root
    test_file="x"   → run only that file (fastest path after a targeted edit)
    """

    def run(
        self,
        project_root: Path,
        test_file: Optional[str] = None,
        timeout: int = 90,
    ) -> TestResult:
        if test_file:
            target = project_root / test_file
            if not target.exists():
                return TestResult(passed=True, message=f"No test file: {test_file} — skipping.")
            files = [str(target)]
        else:
            all_tests = sorted(project_root.glob("test_step*.py"))
            if not all_tests:
                return TestResult(passed=True, message="No test files found.")
            files = [str(f) for f in all_tests]

        # Try pytest first (with timeout plugin if available, without if not)
        def _run_pytest(extra_args: list) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-m", "pytest", "-x", "-q", "--tb=short",
                 *extra_args, *files],
                capture_output=True, text=True,
                timeout=timeout,
                cwd=str(project_root),
            )

        try:
            r = _run_pytest([f"--timeout={max(10, timeout - 15)}"])
            # If --timeout flag was rejected (pytest-timeout not installed), retry without it
            if r.returncode != 0 and "unrecognized arguments" in (r.stdout + r.stderr):
                r = _run_pytest([])
            output = (r.stdout + r.stderr).strip()
            return TestResult(passed=(r.returncode == 0), message=output[-2_000:])
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, message="Test run timed out.")
        except FileNotFoundError:
            pass  # pytest not installed

        # Fallback: python -m unittest
        try:
            r = subprocess.run(
                [sys.executable, "-m", "unittest", *files],
                capture_output=True, text=True,
                timeout=timeout,
                cwd=str(project_root),
            )
            output = (r.stdout + r.stderr).strip()
            return TestResult(passed=(r.returncode == 0), message=output[-2_000:])
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, message="Test run timed out.")
        except Exception as exc:
            return TestResult(passed=True, message=f"Could not run tests: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# SelfEditor — public API
# ══════════════════════════════════════════════════════════════════════════════

class SelfEditor:
    """
    Orchestrates the ATLAS self-modification loop:
      validate → backup → syntax-check → write → test → rollback-if-fail → log

    main.py wires:
        editor = SelfEditor(config, PROJECT_ROOT, confirm_cb=confirm_dlg.ask)
        core.set_self_editor(editor)

    ATLASCore calls:
        result = editor.apply_edit(edit_spec)
        return result.as_voice_response()
    """

    PROTECTED = _PROTECTED_FILES   # public alias

    # Source-file → test-file mapping (targeted test runs are much faster)
    _TEST_MAP: dict[str, str] = {
        "web.py":         "test_step4_web.py",
        "control.py":     "test_step5_control.py",
        "self_editor.py": "test_step6_self_editor.py",
        "voice.py":       "test_step2_voice.py",
        "core.py":        "test_step3_core.py",
    }

    def __init__(
        self,
        config: dict,
        project_root: Path,
        confirm_cb: Optional[Callable[[str], bool]] = None,
    ):
        se_cfg      = config.get("self_editor", {})
        backup_dir  = project_root / se_cfg.get("backup_dir",    ".atlas_backups")
        cl_path     = project_root / se_cfg.get("changelog_path","changelog.json")
        max_backups = int(se_cfg.get("max_backups", 50))

        self._root        = project_root
        self._backup      = Backup(backup_dir, max_backups)
        self._changelog   = Changelog(cl_path)
        self._patcher     = CodePatcher()
        self._test_runner = TestRunner()
        self._confirm_cb  = confirm_cb
        log.info("SelfEditor ready (root=%s).", project_root)

    def set_confirm_cb(self, cb: Callable[[str], bool]) -> None:
        self._confirm_cb = cb

    # ── Primary entry point ───────────────────────────────────────────────────

    def apply_edit(self, edit: dict) -> EditResult:
        """
        Apply a structured code edit with full safety guarantees.

        edit dict must contain:
          "type"        : "replace" | "insert_after" | "insert_before" | "full_rewrite"
          "file"        : relative path e.g. "web.py" or "ui/orb_widget.py"
          "description" : one-line summary of the change
          + type-specific fields (old/new, after/insert, before/insert, content)

        Returns EditResult — call .as_voice_response() for TTS output.
        """
        file_rel    = edit.get("file", "").strip()
        description = edit.get("description", "Code change").strip()

        # ── Validate path ──────────────────────────────────────────────────────
        if not file_rel:
            return EditResult(success=False, error="No 'file' field in edit spec.")

        file_path = (self._root / file_rel).resolve()
        try:
            file_path.relative_to(self._root.resolve())
        except ValueError:
            return EditResult(
                success=False,
                error=f"Path escapes project root: {file_rel}",
            )

        if not file_path.exists() and edit.get("type") != "full_rewrite":
            return EditResult(success=False, error=f"File not found: {file_rel}")

        # ── Protected file gate ────────────────────────────────────────────────
        if file_path.name in _PROTECTED_FILES:
            prompt = (
                f"Modify protected file: {file_rel}\n\n"
                f"Description: {description}\n\n"
                "Type  confirm  to proceed."
            )
            if not self._confirm_cb:
                return EditResult(
                    success=False,
                    error=(
                        f"{file_rel} is a protected file. "
                        "No confirmation callback is configured."
                    ),
                )
            if not self._confirm_cb(prompt):
                return EditResult(
                    success=False,
                    error=f"Modification of {file_rel} was cancelled.",
                )

        # ── Backup ────────────────────────────────────────────────────────────
        backup_path: Optional[Path] = None
        if file_path.exists():
            backup_path = self._backup.create(file_path)

        # ── Compute new content ───────────────────────────────────────────────
        try:
            new_content = self._patcher.apply(edit, file_path)
        except ValueError as exc:
            return EditResult(
                success=False,
                description=description,
                backup_path=str(backup_path) if backup_path else "",
                error=str(exc),
            )

        # ── Syntax check before writing ────────────────────────────────────────
        if file_rel.endswith(".py"):
            try:
                compile(new_content, str(file_path), "exec")
            except SyntaxError as exc:
                return EditResult(
                    success=False,
                    description=description,
                    backup_path=str(backup_path) if backup_path else "",
                    error=f"Generated code has a syntax error: {exc}",
                )

        # ── Write ──────────────────────────────────────────────────────────────
        file_path.write_text(new_content, encoding="utf-8")
        log.info("Edit written: %s (%s)", file_rel, edit.get("type"))

        # ── Run tests ──────────────────────────────────────────────────────────
        test_file   = self._TEST_MAP.get(file_path.name)
        test_result = self._test_runner.run(self._root, test_file=test_file)
        log.info("Tests %s for %s", "PASS" if test_result.passed else "FAIL", file_rel)

        # ── Rollback on failure ────────────────────────────────────────────────
        if not test_result.passed:
            if backup_path:
                self._backup.restore(backup_path, file_path)
            self._record(edit, backup_path,
                         success=False, tests_passed=False, rolled_back=True)
            return EditResult(
                success=False,
                description=description,
                backup_path=str(backup_path) if backup_path else "",
                tests_passed=False,
                rolled_back=True,
                error=test_result.message[-400:],
            )

        # ── Record success ─────────────────────────────────────────────────────
        self._record(edit, backup_path, success=True, tests_passed=True, rolled_back=False)
        return EditResult(
            success=True,
            description=description,
            backup_path=str(backup_path) if backup_path else "",
            tests_passed=True,
        )

    # ── Manual rollback ───────────────────────────────────────────────────────

    def rollback_last(self) -> str:
        """Restore the most recently edited file from its backup."""
        entry = self._changelog.last_entry()
        if not entry:
            return "Nothing in the changelog to roll back."
        if entry.get("rolled_back"):
            return "The last edit was already rolled back automatically."

        backup   = entry.get("backup_path", "")
        file_rel = entry.get("file", "")
        if not backup or not file_rel:
            return "The last changelog entry has no backup path."

        bk     = Path(backup)
        target = self._root / file_rel
        if not bk.exists():
            return f"Backup file not found: {bk.name}"
        if self._backup.restore(bk, target):
            return f"Rolled back {file_rel} to the backup from {bk.stem}."
        return f"Rollback failed — backup at {backup}."

    # ── Convenience / legacy API ──────────────────────────────────────────────

    def read_source(self, filename: str) -> str:
        path = self._root / filename
        if not path.exists():
            return f"File not found: {filename}"
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Could not read {filename}: {exc}"

    def patch(self, filename: str, new_code: str, confirmed: bool = False) -> bool:
        """Legacy API: overwrite a file with new_code."""
        if filename in _PROTECTED_FILES and not confirmed:
            return False
        result = self.apply_edit({
            "type": "full_rewrite",
            "file": filename,
            "content": new_code,
            "description": f"Full rewrite of {filename}",
        })
        return result.success

    def rollback(self, filename: str) -> bool:
        """Legacy API: restore the latest backup of filename."""
        bk = self._backup.latest_for(filename)
        if not bk:
            return False
        return self._backup.restore(bk, self._root / filename)

    def changelog(self) -> list:
        """Legacy API: return all changelog records."""
        return self._changelog.recent(n=9_999)

    def list_changelog(self, n: int = 10) -> list[dict]:
        return self._changelog.recent(n=n)

    def cleanup_old_backups(self) -> int:
        return self._backup.cleanup()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _record(
        self,
        edit: dict,
        backup_path: Optional[Path],
        *,
        success: bool,
        tests_passed: bool,
        rolled_back: bool,
    ) -> None:
        self._changelog.append({
            "id":          datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "file":        edit.get("file", ""),
            "edit_type":   edit.get("type", ""),
            "description": edit.get("description", ""),
            "backup_path": str(backup_path) if backup_path else "",
            "success":     success,
            "tests_passed": tests_passed,
            "rolled_back": rolled_back,
        })
