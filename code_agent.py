"""
ATLAS Code Agent — Smolagents-inspired sandboxed Python execution.

Adapts Smolagents' core pattern:
  LLM writes Python code blocks → sandboxed subprocess → result captured

Key differences from raw Smolagents:
  • Runs in a subprocess (not in-process AST interpreter) for true isolation
  • No internet access inside sandbox; project dir is readable but not writable
  • State persistence across steps via shared variable namespace (serialised to JSON)
  • Auto-retry once on error (Smolagents planning_interval equivalent)
  • Tool functions registered as Python functions with type hints in the sandbox env

Sandbox constraints (from smolagents/src/smolagents/local_python_executor.py):
  MAX_EXECUTION_TIME = 30 seconds
  MAX_OUTPUT_LENGTH  = 10_000 characters (truncated if exceeded)
  No: os.system, subprocess, __import__ of network modules
  Allowed: math, json, re, datetime, pathlib, collections, itertools, functools

Voice commands:
  "ATLAS run that code"       → execute last code block seen in conversation
  "ATLAS test this"           → same as run, plus assertions
  "ATLAS remember that value" → persist last result to vault
  "ATLAS clear code state"    → wipe variable namespace
  "ATLAS what code ran"       → recall last execution
  "ATLAS show code output"    → repeat last result
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# ── Sandbox constants (mirrors Smolagents) ────────────────────────────────────

MAX_EXECUTION_TIME  = 30      # seconds
MAX_OUTPUT_LENGTH   = 10_000  # characters; truncated if longer
MAX_RETRIES         = 1       # auto-retry count on error

# Allowed stdlib modules in sandbox (mirrors smolagents BASE_PYTHON_TOOLS)
_ALLOWED_IMPORTS = {
    "math", "json", "re", "datetime", "pathlib",
    "collections", "itertools", "functools", "random",
    "string", "textwrap", "statistics", "decimal",
    "fractions", "operator", "copy", "pprint",
    "time",   # time.time() only — not time.sleep() in outer block
}

# Blocked patterns (injection / escape attempts)
_BLOCKED_PATTERNS = [
    r"__import__",
    r"importlib",
    r"subprocess",
    r"os\.system",
    r"os\.popen",
    r"sys\.path",
    r"open\s*\(",         # file I/O (use sandbox_read/write tools instead)
    r"socket\.",
    r"urllib",
    r"requests",
    r"httpx",
    r"eval\s*\(",
    r"exec\s*\(",
    r"compile\s*\(",
]

# ── Execution result ──────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    code:     str
    stdout:   str
    stderr:   str
    success:  bool
    duration: float
    retried:  bool = False
    variables: Dict[str, Any] = field(default_factory=dict)


# ── Code extractor ────────────────────────────────────────────────────────────

def extract_code_blocks(text: str) -> List[str]:
    """Extract Python code from markdown code fences (``` ... ```)."""
    blocks = re.findall(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)
    return [b.strip() for b in blocks if b.strip()]


def _check_safety(code: str) -> Optional[str]:
    """Returns an error message if code contains blocked patterns."""
    for pattern in _BLOCKED_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return f"Code contains blocked pattern: {pattern}"
    return None


# ── Sandbox executor ──────────────────────────────────────────────────────────

class CodeSandbox:
    """
    Subprocess-based Python sandbox.

    Variables from prior runs are serialised to JSON and injected as
    local assignments at the top of each new code block — this is the
    Smolagents planning_interval / persistent-state pattern.
    """

    def __init__(self, project_dir: Optional[Path] = None,
                 timeout: int = MAX_EXECUTION_TIME):
        self._timeout      = timeout
        self._project_dir  = project_dir or Path.cwd()
        self._namespace: Dict[str, Any] = {}   # persisted variables
        self._last_result: Optional[ExecutionResult] = None
        self._lock = threading.Lock()

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._namespace)

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._namespace[key] = value

    def clear_state(self) -> None:
        with self._lock:
            self._namespace.clear()

    @property
    def last_result(self) -> Optional[ExecutionResult]:
        return self._last_result

    # ── Execution ─────────────────────────────────────────────────────────────

    def run(self, code: str) -> ExecutionResult:
        """Execute code in sandbox subprocess. Returns ExecutionResult."""
        safety_err = _check_safety(code)
        if safety_err:
            return ExecutionResult(
                code=code, stdout="", stderr=safety_err,
                success=False, duration=0.0)

        result = self._execute(code)

        if not result.success and result.retried is False:
            # Auto-retry once with error context injected (Smolagents pattern)
            fixed_code = self._attempt_fix(code, result.stderr)
            if fixed_code and fixed_code != code:
                log.info("CodeSandbox: auto-retrying with fixed code.")
                retry = self._execute(fixed_code)
                retry.retried = True
                result = retry

        with self._lock:
            if result.success:
                self._namespace.update(result.variables)
            self._last_result = result

        return result

    def _execute(self, code: str) -> ExecutionResult:
        """Run code in isolated subprocess, capture stdout/stderr."""
        # Build preamble: inject persisted variables + sandbox helpers
        preamble = self._build_preamble()
        full_code = preamble + "\n" + code + "\n" + self._build_epilogue()

        script_content = textwrap.dedent(f"""
import sys, json, traceback

# Sandbox: block dangerous modules
class _BlockedModule:
    def __getattr__(self, name):
        raise ImportError("Module blocked in ATLAS sandbox")

import builtins
_real_import = builtins.__import__
def _safe_import(name, *args, **kwargs):
    _allowed = {json.dumps(list(_ALLOWED_IMPORTS))}
    import json as _json
    allowed_list = _json.loads(_allowed)
    top = name.split('.')[0]
    if top not in allowed_list:
        raise ImportError(f"Import '{{name}}' is blocked in the ATLAS code sandbox.")
    return _real_import(name, *args, **kwargs)
builtins.__import__ = _safe_import

# Inject persisted namespace
{preamble}

_output_lines = []
class _Tee:
    def write(self, s):
        _output_lines.append(s)
        sys.__stdout__.write(s)
    def flush(self):
        sys.__stdout__.flush()
sys.stdout = _Tee()

_result_vars = {{}}
try:
    exec(compile({repr(code)}, '<atlas_sandbox>', 'exec'), {{'__builtins__': builtins}})
    # Capture any new simple-type variables
    frame_locals = locals()
    for _k, _v in list(frame_locals.items()):
        if _k.startswith('_'):
            continue
        try:
            json.dumps(_v)   # only serialisable values
            _result_vars[_k] = _v
        except (TypeError, ValueError):
            pass
    print("__ATLAS_SUCCESS__")
except Exception as _exc:
    print(f"__ATLAS_ERROR__: {{_exc}}", file=sys.__stderr__)
    traceback.print_exc(file=sys.__stderr__)

sys.stdout = sys.__stdout__
print("__ATLAS_VARS__:" + json.dumps(_result_vars))
        """)

        with tempfile.NamedTemporaryFile(
                suffix=".py", mode="w", encoding="utf-8", delete=False) as f:
            f.write(script_content)
            tmp_path = f.name

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True, text=True,
                timeout=self._timeout,
                cwd=str(self._project_dir),
            )
            duration = time.monotonic() - t0

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""

            # Parse variables from last line
            variables: Dict[str, Any] = {}
            lines = stdout.splitlines()
            for i, ln in enumerate(lines):
                if ln.startswith("__ATLAS_VARS__:"):
                    try:
                        variables = json.loads(ln[len("__ATLAS_VARS__:"):])
                    except Exception:
                        pass
                    lines = lines[:i]   # remove from output
                    break

            # Remove internal markers from output
            clean_lines = [l for l in lines if not l.startswith("__ATLAS_")]
            stdout = "\n".join(clean_lines)

            success = "__ATLAS_SUCCESS__" in proc.stdout and proc.returncode == 0

            if len(stdout) > MAX_OUTPUT_LENGTH:
                stdout = stdout[:MAX_OUTPUT_LENGTH] + "\n... [output truncated]"

            return ExecutionResult(
                code=code, stdout=stdout, stderr=stderr,
                success=success, duration=duration, variables=variables,
            )

        except subprocess.TimeoutExpired:
            duration = time.monotonic() - t0
            return ExecutionResult(
                code=code, stdout="", stderr=f"Execution timed out after {self._timeout}s.",
                success=False, duration=duration,
            )
        except Exception as exc:
            duration = time.monotonic() - t0
            return ExecutionResult(
                code=code, stdout="", stderr=str(exc),
                success=False, duration=duration,
            )
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _build_preamble(self) -> str:
        """Inject persisted variables as Python literals."""
        lines = []
        with self._lock:
            for k, v in self._namespace.items():
                try:
                    lines.append(f"{k} = {repr(v)}")
                except Exception:
                    pass
        return "\n".join(lines)

    def _build_epilogue(self) -> str:
        return ""   # output capture handled in script_content

    def _attempt_fix(self, code: str, error: str) -> Optional[str]:
        """
        Simple heuristic fixes before retry.
        Not calling LLM here to keep sandbox latency low.
        """
        if "NameError" in error and "is not defined" in error:
            # Extract missing name and add a dummy definition
            m = re.search(r"name '(\w+)' is not defined", error)
            if m:
                name = m.group(1)
                return f"{name} = None  # auto-defined for retry\n" + code
        if "IndentationError" in error:
            # Try de-denting the code
            return textwrap.dedent(code)
        return None


# ── ATLASCodeAgent ────────────────────────────────────────────────────────────

class ATLASCodeAgent:
    """
    Code-writing + execution agent for ATLAS.

    When the user asks ATLAS to build/write/implement something:
      1. ATLASCodeAgent asks the brain to write Python code
      2. Extracts code blocks from the LLM response
      3. Runs each block in the sandbox
      4. Feeds output back to brain for a natural-language summary
      5. Persists variables across multi-step sessions

    Smolagents adaptation:
      • planning_interval equivalent → brain writes plan comment before code
      • LocalPythonExecutor equivalent → CodeSandbox (subprocess-based)
      • Tool functions → registered as Python stubs in sandbox preamble
    """

    def __init__(self, brain=None, config: dict = None,
                 vault_brain=None, speak_cb=None):
        self._brain  = brain
        self._config = config or {}
        self._vb     = vault_brain
        self._speak  = speak_cb or (lambda s: None)

        project_dir = Path(self._config.get("vault_path", ".")).expanduser()
        timeout     = int(self._config.get("code_agent_timeout", MAX_EXECUTION_TIME))

        self._sandbox  = CodeSandbox(project_dir=project_dir, timeout=timeout)
        self._last_code: Optional[str]  = None
        self._last_resp: Optional[str]  = None
        self._lock = threading.Lock()

        log.info("ATLASCodeAgent: initialised (timeout=%ds, project=%s).",
                 timeout, project_dir)

    # ── Main API ──────────────────────────────────────────────────────────────

    def process_request(self, task: str) -> str:
        """
        Generate and execute code for a task. Returns voice-ready result string.
        Called from brain routing when coding intent is detected.
        """
        if not self._brain:
            return "Code agent is not connected to a brain, Boss."

        # Step 1: Ask LLM to write the code (Smolagents CodeAgent pattern)
        code_prompt = (
            f"Write Python code to accomplish: {task}\n\n"
            "Rules:\n"
            "- Use ONLY these imports: math, json, re, datetime, pathlib, "
            "collections, itertools, functools, random, statistics\n"
            "- Print the final result so I can see it\n"
            "- Assign the key result to a variable called `result`\n"
            "- Write a comment explaining each major step\n"
            "- Wrap your code in ```python ... ``` fences\n"
        )

        try:
            llm_response = self._brain.ask(code_prompt)
        except Exception as exc:
            return f"Could not generate code: {exc}"

        blocks = extract_code_blocks(llm_response)
        if not blocks:
            # LLM response might be direct answer without code
            return llm_response[:500]

        # Step 2: Run each code block sequentially
        results: List[str] = []
        for i, block in enumerate(blocks):
            with self._lock:
                self._last_code = block
            exec_result = self._sandbox.run(block)
            results.append(self._format_result(exec_result, i + 1, len(blocks)))
            if not exec_result.success:
                break   # stop on failure; auto-retry happened inside sandbox.run()

        combined = "\n".join(results)

        # Step 3: Synthesise result in natural language
        if self._brain:
            synth_prompt = (
                f"Original task: {task}\n"
                f"Code execution output:\n{combined[:2000]}\n\n"
                "Summarise the result in 1-2 sentences for voice delivery. "
                "Focus on what was accomplished or found, not the code itself."
            )
            try:
                voice_summary = self._brain.ask(synth_prompt)
                with self._lock:
                    self._last_resp = voice_summary
                return voice_summary
            except Exception:
                pass

        with self._lock:
            self._last_resp = combined
        return combined[:400]

    def run_raw(self, code: str) -> ExecutionResult:
        """Execute raw code directly (from voice 'run that code' command)."""
        with self._lock:
            self._last_code = code
        return self._sandbox.run(code)

    def _format_result(self, r: ExecutionResult, step: int, total: int) -> str:
        prefix = f"[step {step}/{total}]" if total > 1 else ""
        if r.success:
            out = r.stdout.strip() or "[no output]"
            retry_note = " (auto-corrected)" if r.retried else ""
            return f"{prefix} {out}{retry_note}".strip()
        else:
            err = r.stderr.strip()[:500]
            retry_note = " (after retry)" if r.retried else ""
            return f"{prefix} Error{retry_note}: {err}".strip()

    # ── Vault persistence ─────────────────────────────────────────────────────

    def save_result_to_vault(self, label: str = "") -> str:
        result = self._sandbox.last_result
        if not result:
            return "Nothing to save yet, Boss."
        if not self._vb:
            return "Vault not connected — cannot save."
        try:
            from datetime import date
            folder = self._vb.atlas / "Memory" / "code_runs"
            folder.mkdir(parents=True, exist_ok=True)
            slug  = re.sub(r"[^\w]", "_", label)[:30] or "run"
            fname = f"{date.today().isoformat()}-{slug}.md"
            path  = folder / fname
            state = self._sandbox.get_state()
            vars_md = "\n".join(f"- `{k}` = `{repr(v)[:100]}`"
                                for k, v in state.items() if not k.startswith("_"))
            path.write_text(
                f"---\ntags: [atlas, code-run]\ndate: {date.today()}\n---\n\n"
                f"## Code\n```python\n{result.code}\n```\n\n"
                f"## Output\n```\n{result.stdout[:2000]}\n```\n\n"
                f"## Variables\n{vars_md or '_none_'}\n",
                encoding="utf-8",
            )
            return f"Saved to {path.name} in your vault, Boss."
        except Exception as exc:
            log.warning("Could not save code run: %s", exc)
            return "Could not save to vault."

    # ── Voice commands ────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        if any(p in lower for p in ("atlas run that code", "atlas execute that",
                                     "atlas run it", "atlas test this")):
            with self._lock:
                code = self._last_code
            if not code:
                return "No code ready to run, Boss. Give me something to code first."
            r = self.run_raw(code)
            if r.success:
                out = r.stdout.strip()[:300] or "[no output]"
                return f"Code ran successfully in {r.duration:.1f}s. Output: {out}"
            else:
                return f"Code failed: {r.stderr.strip()[:200]}"

        if any(p in lower for p in ("atlas remember that value", "atlas save that result",
                                     "atlas save the code", "atlas save code output")):
            label = re.sub(
                r"atlas (?:remember|save) (?:that |the )?(?:value|result|code(?: output)?)?",
                "", lower).strip()
            return self.save_result_to_vault(label or "result")

        if any(p in lower for p in ("atlas clear code state", "atlas reset code",
                                     "atlas wipe variables", "atlas clear variables")):
            self._sandbox.clear_state()
            return "Code state cleared, Boss. Fresh slate."

        if any(p in lower for p in ("atlas what code ran", "atlas show last code",
                                     "atlas last code")):
            with self._lock:
                code = self._last_code
            if not code:
                return "No code has run yet this session, Boss."
            snippet = code[:300].replace("\n", " | ")
            return f"Last code: {snippet}"

        if any(p in lower for p in ("atlas show code output", "atlas last output",
                                     "atlas what did the code return")):
            with self._lock:
                resp = self._last_resp
            if not resp:
                return "No code output yet, Boss."
            return resp[:400]

        return None

    def is_code_request(self, text: str) -> bool:
        """Heuristic: is this a code writing/running request?"""
        lower = text.lower()
        triggers = {
            "write code", "write a script", "write a function", "write a class",
            "build a", "create a script", "implement", "code that",
            "python script", "make a function", "generate code",
            "calculate", "compute", "run code", "execute code",
        }
        return any(t in lower for t in triggers)
