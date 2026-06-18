"""
Step 7 — PyInstaller Packaging: validation tests.

These tests verify that every build artifact is syntactically correct and
structurally complete WITHOUT actually running PyInstaller (which would take
several minutes and require all dependencies installed).

Key design note
───────────────
atlas.spec uses PyInstaller-injected globals (Analysis, PYZ, EXE, COLLECT,
BUNDLE, SPECPATH) that are not defined in a normal Python import context.
Using compile() + exec() on the spec would either fail or require mocking
those globals.  Instead, we use ast.parse() — it checks syntax without
executing the code.
"""

import ast
import importlib
import platform
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# ── helpers ────────────────────────────────────────────────────────────────────

def _parse_source(path: Path) -> ast.Module:
    """Return the AST of *path*, raising AssertionError with a readable message on failure."""
    src = path.read_text(encoding='utf-8')
    try:
        return ast.parse(src, filename=str(path))
    except SyntaxError as e:
        raise AssertionError(f"{path.name} has a syntax error: {e}") from e


def _names_in_module(tree: ast.Module) -> set:
    """Return the set of top-level names defined (def / class / assign) in *tree*."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _hook_collects_data(path: Path) -> bool:
    """Return True if the hook assigns `datas` at module level."""
    tree = _parse_source(path)
    return 'datas' in _names_in_module(tree)


# ══════════════════════════════════════════════════════════════════════════════
# Test classes
# ══════════════════════════════════════════════════════════════════════════════

class TestSpecFileExists(unittest.TestCase):
    def test_spec_exists(self):
        self.assertTrue((ROOT / 'atlas.spec').is_file(), "atlas.spec not found")

    def test_build_py_exists(self):
        self.assertTrue((ROOT / 'build.py').is_file(), "build.py not found")

    def test_build_macos_sh_exists(self):
        self.assertTrue((ROOT / 'build_macos.sh').is_file(), "build_macos.sh not found")

    def test_build_windows_bat_exists(self):
        self.assertTrue((ROOT / 'build_windows.bat').is_file(), "build_windows.bat not found")

    def test_hooks_dir_exists(self):
        self.assertTrue((ROOT / 'pyinstaller_hooks').is_dir(), "pyinstaller_hooks/ not found")


class TestSpecSyntax(unittest.TestCase):
    """atlas.spec must parse as valid Python (ast.parse, not exec)."""

    def setUp(self):
        self.tree = _parse_source(ROOT / 'atlas.spec')

    def test_spec_parses(self):
        self.assertIsInstance(self.tree, ast.Module)

    def test_spec_uses_analysis(self):
        calls = [
            n.func.id for n in ast.walk(self.tree)
            if isinstance(n, ast.Call)
            and isinstance(getattr(n, 'func', None), ast.Name)
        ]
        self.assertIn('Analysis', calls, "atlas.spec should call Analysis()")

    def test_spec_uses_pyz(self):
        calls = [
            n.func.id for n in ast.walk(self.tree)
            if isinstance(n, ast.Call)
            and isinstance(getattr(n, 'func', None), ast.Name)
        ]
        self.assertIn('PYZ', calls, "atlas.spec should call PYZ()")

    def test_spec_uses_exe(self):
        calls = [
            n.func.id for n in ast.walk(self.tree)
            if isinstance(n, ast.Call)
            and isinstance(getattr(n, 'func', None), ast.Name)
        ]
        self.assertIn('EXE', calls, "atlas.spec should call EXE()")

    def test_spec_uses_collect(self):
        calls = [
            n.func.id for n in ast.walk(self.tree)
            if isinstance(n, ast.Call)
            and isinstance(getattr(n, 'func', None), ast.Name)
        ]
        self.assertIn('COLLECT', calls, "atlas.spec should call COLLECT()")

    def test_spec_has_bundle_for_mac(self):
        src = (ROOT / 'atlas.spec').read_text(encoding='utf-8')
        self.assertIn('BUNDLE', src, "atlas.spec should reference BUNDLE for macOS")


class TestSpecContent(unittest.TestCase):
    """Structural checks on atlas.spec content."""

    def setUp(self):
        self.src = (ROOT / 'atlas.spec').read_text(encoding='utf-8')

    def test_includes_config_yaml(self):
        self.assertIn('config.yaml', self.src)

    def test_includes_ui_directory(self):
        self.assertIn("'ui'", self.src)

    def test_includes_whisper_assets(self):
        self.assertIn('whisper', self.src.lower())

    def test_includes_sherpa_onnx(self):
        self.assertIn('sherpa_onnx', self.src)

    def test_includes_pyqt6(self):
        self.assertIn('PyQt6', self.src)

    def test_excludes_tkinter(self):
        self.assertIn('tkinter', self.src)   # in excludes list

    def test_excludes_test_files(self):
        self.assertIn('test_step2_voice', self.src)

    def test_macos_info_plist_microphone(self):
        self.assertIn('NSMicrophoneUsageDescription', self.src)

    def test_macos_info_plist_screen(self):
        self.assertIn('NSScreenCaptureUsageDescription', self.src)

    def test_macos_info_plist_automation(self):
        self.assertIn('NSAppleEventsUsageDescription', self.src)

    def test_bundle_identifier(self):
        self.assertIn('com.atlas.ai.assistant', self.src)

    def test_arch_detection(self):
        self.assertIn('arm64', self.src)

    def test_custom_hooks_path(self):
        self.assertIn('pyinstaller_hooks', self.src)

    def test_no_hardcoded_api_keys(self):
        for bad in ('sk-', 'AIza', 'gsk_'):
            self.assertNotIn(bad, self.src, f"Possible hardcoded API key ({bad!r}) in atlas.spec")


class TestHookSyntax(unittest.TestCase):
    """Both custom hooks must parse as valid Python."""

    def test_hook_whisper_parses(self):
        path = ROOT / 'pyinstaller_hooks' / 'hook-whisper.py'
        self.assertTrue(path.exists(), "hook-whisper.py not found")
        tree = _parse_source(path)
        self.assertIsInstance(tree, ast.Module)

    def test_hook_sherpa_onnx_parses(self):
        path = ROOT / 'pyinstaller_hooks' / 'hook-pvporcupine.py'
        self.assertTrue(path.exists(), "hook-pvporcupine.py (sherpa-onnx hook) not found")
        tree = _parse_source(path)
        self.assertIsInstance(tree, ast.Module)

    def test_hook_whisper_collects_datas(self):
        path = ROOT / 'pyinstaller_hooks' / 'hook-whisper.py'
        self.assertTrue(_hook_collects_data(path))

    def test_hook_sherpa_onnx_collects_datas(self):
        path = ROOT / 'pyinstaller_hooks' / 'hook-pvporcupine.py'
        self.assertTrue(_hook_collects_data(path))

    def test_hook_whisper_has_hiddenimports(self):
        tree = _parse_source(ROOT / 'pyinstaller_hooks' / 'hook-whisper.py')
        self.assertIn('hiddenimports', _names_in_module(tree))

    def test_hook_sherpa_onnx_has_hiddenimports(self):
        tree = _parse_source(ROOT / 'pyinstaller_hooks' / 'hook-pvporcupine.py')
        self.assertIn('hiddenimports', _names_in_module(tree))


class TestBuildPySyntax(unittest.TestCase):
    """build.py must parse and expose expected symbols."""

    def setUp(self):
        self.tree = _parse_source(ROOT / 'build.py')
        self.names = _names_in_module(self.tree)

    def test_parses(self):
        self.assertIsInstance(self.tree, ast.Module)

    def test_has_main_function(self):
        self.assertIn('main', self.names)

    def test_has_check_env(self):
        self.assertIn('_check_env', self.names)

    def test_has_build_function(self):
        self.assertIn('_build', self.names)

    def test_has_post_build(self):
        self.assertIn('_post_build', self.names)

    def test_has_codesign_mac(self):
        self.assertIn('_codesign_mac', self.names)

    def test_no_hardcoded_api_keys(self):
        src = (ROOT / 'build.py').read_text(encoding='utf-8')
        for bad in ('sk-', 'AIza', 'gsk_'):
            self.assertNotIn(bad, src, f"Possible hardcoded API key ({bad!r}) in build.py")


class TestBuildPyFlags(unittest.TestCase):
    """build.py CLI must advertise the required flags."""

    def test_clean_flag(self):
        src = (ROOT / 'build.py').read_text(encoding='utf-8')
        self.assertIn('--clean', src)

    def test_debug_flag(self):
        src = (ROOT / 'build.py').read_text(encoding='utf-8')
        self.assertIn('--debug', src)

    def test_check_flag(self):
        src = (ROOT / 'build.py').read_text(encoding='utf-8')
        self.assertIn('--check', src)

    def test_no_sign_flag(self):
        src = (ROOT / 'build.py').read_text(encoding='utf-8')
        self.assertIn('--no-sign', src)


class TestBuildScripts(unittest.TestCase):
    """Shell/batch wrappers must contain expected content."""

    def test_macos_sh_calls_build_py(self):
        src = (ROOT / 'build_macos.sh').read_text(encoding='utf-8')
        self.assertIn('build.py', src)

    def test_macos_sh_has_shebang(self):
        src = (ROOT / 'build_macos.sh').read_text(encoding='utf-8')
        self.assertTrue(src.startswith('#!/'), "build_macos.sh missing shebang")

    def test_macos_sh_mentions_permissions(self):
        src = (ROOT / 'build_macos.sh').read_text(encoding='utf-8')
        self.assertIn('Accessibility', src)

    def test_macos_sh_mentions_env_vars(self):
        src = (ROOT / 'build_macos.sh').read_text(encoding='utf-8')
        self.assertIn('GROQ_API_KEY', src)

    def test_windows_bat_calls_build_py(self):
        src = (ROOT / 'build_windows.bat').read_text(encoding='utf-8')
        self.assertIn('build.py', src)

    def test_windows_bat_mentions_env_vars(self):
        src = (ROOT / 'build_windows.bat').read_text(encoding='utf-8')
        self.assertIn('GROQ_API_KEY', src)

    def test_windows_bat_exits_with_status(self):
        src = (ROOT / 'build_windows.bat').read_text(encoding='utf-8')
        self.assertIn('ERRORLEVEL', src)


class TestRequirements(unittest.TestCase):
    """requirements.txt must reference all Step 7 deps."""

    def setUp(self):
        self.src = (ROOT / 'requirements.txt').read_text(encoding='utf-8')

    def test_pyinstaller_present(self):
        self.assertIn('pyinstaller', self.src.lower())

    def test_pyqt6_present(self):
        self.assertIn('PyQt6', self.src)


class TestPyInstallerInstalled(unittest.TestCase):
    """PyInstaller must be importable (installed in current env)."""

    def test_pyinstaller_importable(self):
        try:
            import PyInstaller
            version = tuple(int(x) for x in PyInstaller.__version__.split('.')[:2])
            self.assertGreaterEqual(version, (6, 0),
                f"PyInstaller {PyInstaller.__version__} found; need >=6.0.0")
        except ImportError:
            self.skipTest("PyInstaller not installed — run: pip install pyinstaller>=6.0.0")


class TestAssetsDirectory(unittest.TestCase):
    def test_assets_dir_exists(self):
        self.assertTrue((ROOT / 'assets').is_dir(), "assets/ directory not found")

    def test_hooks_dir_exists(self):
        self.assertTrue((ROOT / 'pyinstaller_hooks').is_dir())

    def test_hooks_dir_not_empty(self):
        hooks = list((ROOT / 'pyinstaller_hooks').glob('hook-*.py'))
        self.assertTrue(len(hooks) >= 2, f"Expected >=2 hook files, found {len(hooks)}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
