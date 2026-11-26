"""The trusted verifier. It runs from the engine's own checkout, never from the worktree it
judges, and it trusts nothing an implementer could have changed.

- Each command check produces an `Evidence` record bound to the exact head SHA. A command
  that several criteria name runs once per verification, and its run is the record of each.
  Every pytest process the command starts, however it starts it, reports each test through the
  `loop_pytest` plugin: a failed, erroring, skipped, xfailed or xpassed test fails the check,
  and so does a pytest that ran no test or could not run (usage error, nothing collected,
  internal error, interrupted).
- The standing checks (`config.STANDING_CHECKS`: the public-repository hygiene scan) run on the
  exact head before the issue's own commands, whatever the issue says. A failure, or a scanner
  that is missing, is a verification problem like any other, returned with its output.
- Every command runs inside the sandbox (`sandbox`), with a scrubbed environment.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable
from dataclasses import asdict, replace
from pathlib import Path

from . import sandbox
from .model import CheckSpec, Evidence

OUTPUT_TAIL = 3000


# ------------------------------------------------------- tests that can never pass

def _test_functions(root: Path, files: list[str]):
    """(id, function node) for every `test*` function or method in the Python files, and
    (`path::<unparseable>`, None) for a file Python cannot parse.

    Read with Python's own parser, so two `test_contract` methods in different classes stay two
    tests, and a test defined under `if`, `try` or `with` is still found."""

    def blocks(stmt) -> list[list]:
        """Statement lists a definition can sit in: if/else, try/except/else/finally, with, for, while, match."""
        out = [getattr(stmt, name, None) for name in ("body", "orelse", "finalbody")]
        out += [h.body for h in getattr(stmt, "handlers", [])] + [c.body for c in getattr(stmt, "cases", [])]
        return [b for b in out if isinstance(b, list)]

    def walk(statements: list, rel: str, prefix: list[str]):
        for child in statements:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test"):
                    yield "::".join([rel, *prefix, child.name]), child
                # pytest never collects functions nested inside functions
            elif isinstance(child, ast.ClassDef):
                yield from walk(child.body, rel, prefix + [child.name])
            else:
                for block in blocks(child):
                    yield from walk(block, rel, prefix)

    for rel in files:
        p = root / rel
        if p.suffix == ".py" and p.is_file():
            try:
                tree = ast.parse(p.read_text(errors="replace"))
            except SyntaxError:
                yield f"{rel}::<unparseable>", None
                continue
            yield from walk(tree.body, rel, [])


_FAILING_RAISES = {"AssertionError", "Exception", "NotImplementedError"}
_JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}
_JS_TEST = re.compile(
    r"""(?<![\w$.])(?:test|it)(?:\.only)?\s*\(\s*"""
    r"""(?P<q>['"`])(?:\\.|(?!(?P=q)).)*(?P=q)\s*,\s*"""                  # the test name
    r"""(?:(?P<options>\{[^{}]*\})\s*,\s*)?"""                              # node:test options
    r"""(?:async\s+)?(?:function\b\s*\*?\s*[\w$]*\s*\([^)]*\)|\([^)]*\)|[\w$]+)\s*(?:=>\s*)?\{""",
    re.DOTALL)
_JS_LEADING = re.compile(r"(?:\s+|//[^\n]*|/\*.*?\*/)*", re.DOTALL)
_JS_FAILS = re.compile(r"assert\.fail\s*\(|throw\s+(?:new\s+)?Error\s*\(|assert(?:\.ok)?\s*\(\s*false\s*[,)]")
_SHELL_NOISE = re.compile(r"(?:set\s|echo\b|printf\b|:(?:\s|$))")
_SHELL_FAILS = re.compile(r"(?:exit\s+[1-9]\d*|(?:/usr)?(?:/bin/)?false)")


def _python_always_fails(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Assert):
        return isinstance(stmt.test, ast.Constant) and not stmt.test.value
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        func = stmt.value.func   # pytest.fail(...), self.fail(...)
        return (isinstance(func, ast.Attribute) and func.attr == "fail" and isinstance(func.value, ast.Name)
                and func.value.id in {"pytest", "self"})
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return isinstance(exc, ast.Name) and exc.id in _FAILING_RAISES
    return False


def _in_js_comment(text: str, pos: int) -> bool:
    line = text[text.rfind("\n", 0, pos) + 1:pos]
    return "//" in line or text.rfind("/*", 0, pos) > text.rfind("*/", 0, pos)


def unconditional_failures(root: Path, files: list[str]) -> list[str]:
    """Tests in these files that fail whatever the code under test does, so no implementation
    can make them pass: a Python test whose first statement is `assert False` (or another
    falsy constant), `pytest.fail(...)`, `self.fail(...)` or `raise AssertionError/Exception/
    NotImplementedError`; a JavaScript or TypeScript `test(...)`/`it(...)` whose callback starts
    with `assert.fail(...)`, `throw new Error(...)` or `assert.ok(false)`; a shell script whose
    only command is `exit 1` (any non-zero status) or `false`. Deliberately narrow: anything
    that depends on the code under test is left alone."""
    problems: list[str] = []
    for test_id, node in _test_functions(root, files):
        if node is None:
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]   # the docstring
        if body and _python_always_fails(body[0]):
            problems.append(f"{test_id} always fails: its first statement is `{ast.unparse(body[0])[:80]}`")
    for rel in files:
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        if p.suffix in _JS_SUFFIXES:
            for m in _JS_TEST.finditer(text):
                if _in_js_comment(text, m.start()) or re.search(r"\b(?:skip|todo)\b", m.group("options") or ""):
                    continue
                leading = _JS_LEADING.match(text, m.end())
                assert leading is not None   # every repetition is optional: it matches the empty string
                start = leading.end()
                first = _JS_FAILS.match(text, start)
                if first:
                    line = text.count("\n", 0, m.start()) + 1
                    problems.append(f"{rel}:{line}: the test always fails: its callback starts with `{first.group(0).strip()}`")
        elif p.suffix in {".sh", ".bash"} or (p.suffix != ".py" and re.match(r"#!.*\b(?:ba|da|z)?sh\b", text)):
            commands = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                commands += [c.strip() for c in line.split(";") if c.strip()]
            effective = [c for c in commands if not _SHELL_NOISE.match(c)]
            if len(effective) == 1 and _SHELL_FAILS.fullmatch(re.sub(r"\s+#.*$", "", effective[0])):
                problems.append(f"{rel}: the script always fails: its only command is `{effective[0]}`")
    return problems


# ------------------------------------------------------------------ running checks

PLUGIN = "implement_loop.loop_pytest"
ENGINE_DIR = Path(__file__).resolve().parent.parent   # what PYTHONPATH needs for PLUGIN
# The name of a copy of the plugin, a top-level module alone in a directory of its own
# (`run_check`'s `plugin_dir`), which the second run of a reproduction loads (`Engine._reproduction`):
# that directory exposes no `implement_loop` package, to the command or to a process it starts.
PLUGIN_COPY = "implement_loop_result_plugin"
# outcome -> what it means for a check; a worse outcome of the same test wins (as in loop_pytest)
OUTCOMES = {"passed": "passed", "xfailed": "skipped", "skipped": "skipped", "xpassed": "failed",
            "failed": "failed", "error": "failed"}
SEVERITY = {"passed": 0, "xfailed": 1, "skipped": 2, "xpassed": 3, "failed": 4, "error": 5}
# pytest exit statuses that mean it did not run the tests it was given (0 and 1 are results)
BAD_EXIT = {2: "interrupted", 3: "internal error", 4: "usage error", 5: "no tests collected"}


def _run(command: str, cwd: Path, timeout: int, env: dict[str, str],
         mounts: sandbox.Mounts | None = None) -> tuple[int | None, str, bool]:
    """(exit code, output, timed out). The command is agent-written: it runs inside the sandbox
    (`mounts` is what it may see beyond the tools; `SandboxUnavailable` when the sandbox cannot be
    built, and nothing runs), with no terminal. It runs in its own process group, so a timeout
    stops everything it started, not only bash: `pytest A && pytest B` would otherwise keep
    the output pipes open until B finished."""
    argv = sandbox.current().command(["bash", "-c", command], cwd, vendor=None,
                                     mounts=mounts if mounts is not None else sandbox.Mounts(writable=(Path(cwd),)))
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", env=env, start_new_session=True)

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "") + (err or ""), False
    except subprocess.TimeoutExpired:
        kill()
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:   # something left the group and holds the pipes
            out, err = "", ""
        return None, f"timed out after {timeout} s\n{out or ''}{err or ''}", True
    except BaseException:
        kill()
        proc.wait()
        raise


def _read_records(report_dir: Path) -> dict | None:
    """Every pytest process's record, merged: ids by outcome, what was collected, how many
    processes ran, and the exit statuses that are not results. None when no pytest ran."""
    records = sorted(report_dir.glob("run-*.json"))
    if not records:
        return None
    worst: dict[str, str] = {}
    collected: dict[str, None] = {}
    bad_exit: list[int | None] = []
    for path in records:
        try:
            data = json.loads(path.read_text())
            status = data["exitstatus"]
            cases = [(str(c["id"]), c["outcome"] if c["outcome"] in SEVERITY else "error") for c in data["cases"]]
            selected = [str(i) for i in data.get("collected") or []]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            bad_exit.append(None)   # unreadable: no evidence either way
            continue
        if status not in (0, 1):
            bad_exit.append(status)
        for test_id, outcome in cases:
            if test_id not in worst or SEVERITY[outcome] > SEVERITY[worst[test_id]]:
                worst[test_id] = outcome
        collected.update(dict.fromkeys(selected))
    out: dict = {"passed": [], "failed": [], "skipped": []}
    for test_id, outcome in sorted(worst.items()):
        out[OUTCOMES[outcome]].append(test_id)
    out.update(collected=list(collected), processes=len(records), bad_exit=bad_exit)
    return out


def _pytest_said(output: str, status: int | None) -> str:
    """pytest's own one-line account of why it stopped, if it printed one."""
    if status == 3:
        lines = re.findall(r"^INTERNALERROR> (.+)$", output, re.MULTILINE)
        return lines[-1].strip()[:200] if lines else ""
    for pattern in (r"^\S+: error: (.+)$", r"^ERROR: (.+)$", r"^Interrupted: (.+)$"):
        m = re.search(pattern, output, re.MULTILINE)
        if m:
            return m.group(1).strip()[:200]
    return ""


def _notes(cases: dict | None, output: str) -> list[str]:
    """Why the pytest runs of a command do not prove anything, in words."""
    if cases is None:
        if f'Error importing plugin "{PLUGIN}"' in output:
            return [("pytest could not load the verifier's result plugin; if the command sets PYTHONPATH, "
                     "extend it (PYTHONPATH=src:$PYTHONPATH) instead of replacing it")]
        return []
    notes = []
    if cases["failed"]:
        notes.append(f"failed tests: {cases['failed'][:5]}")
    if cases["skipped"]:
        notes.append(f"unexpected skip: {cases['skipped'][:5]}")
    nothing_ran = not (cases["passed"] or cases["failed"] or cases["skipped"])
    stopped = []
    for status in dict.fromkeys(cases["bad_exit"]):
        said = _pytest_said(output, status)
        if status is None:
            stopped.append("pytest stopped before running any test" + (f" ({said})" if said else ""))
        elif not (status == 5 and nothing_ran):   # nothing collected anywhere: "no tests ran" says it
            label = BAD_EXIT.get(status, "not a test result")
            stopped.append(f"pytest exited {status} ({label}{': ' + said if said else ''})")
    if nothing_ran and not stopped:
        notes.append("no tests ran")
    return notes + stopped


CREDENTIAL_VARS = sandbox.GITHUB_VARS


def scrubbed_env(env: dict[str, str], work_dir: Path) -> dict[str, str]:
    """Check commands are written by agents: they get the agents' own scrub (`sandbox.clean_env`:
    no cloud or GitHub credentials, and no vendor's API key either), so evidence the engine
    publishes cannot carry them."""
    return sandbox.clean_env(env, work_dir / "no-gh", vendor=None)


def run_check(root: Path, spec: CheckSpec, head_sha: str, work_dir: Path, default_timeout: int = 900,
              extra_env: dict[str, str] | None = None, mounts: sandbox.Mounts | None = None,
              plugin_dir: Path | None = None) -> tuple[Evidence, dict | None]:
    """Run one check command. Returns its evidence, and what its pytest processes reported:
    `{"passed", "failed", "skipped", "collected": [ids], "processes": n, "bad_exit": [status]}`
    (failed includes errors and xpasses, skipped includes xfails), or None when no pytest ran.

    The command runs in the sandbox. `mounts` is what the engine lets it see of the tree under
    judgement and its git (default: `root`, read-write); the verifier's plugin, the record
    directory and the run's package caches are added here.

    With `plugin_dir`, a directory that holds a copy of the plugin alone as `PLUGIN_COPY`, pytest
    loads the plugin by that name, and the directory comes after the engine's on PYTHONPATH and is
    added to what the command may read.

    The command passes when it exits 0, no test failed, erred, xpassed, xfailed or was skipped,
    and, if pytest ran at all, at least one test ran and every pytest ended with a test result
    (not interrupted, internal error, usage error or nothing collected)."""
    if spec.kind != "command" or not spec.command:
        ev = Evidence(criterion=spec.criterion, kind=spec.kind, command=None, cwd=spec.cwd, head_sha=head_sha,
                      exit_code=None, passed=False, duration_s=0.0, output_tail="", output_sha256="",
                      note="proven by an artifact review or a person, not by a command")
        return ev, None
    # A fresh record directory per check, and the plugin in every pytest the command starts,
    # through the environment: tests run from a wrapper (`bash run.sh`, `uv run pytest`,
    # `make test`) or several pytest runs chained in one command are all accounted for.
    report_dir = (work_dir / f"pytest-{re.sub(r'[^A-Za-z0-9._-]', '_', spec.criterion)}-{head_sha[:12]}").resolve()
    shutil.rmtree(report_dir, ignore_errors=True)
    report_dir.mkdir(parents=True)
    env = scrubbed_env(dict(os.environ) | dict(extra_env or {}), work_dir)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")   # never write caches into the tree being judged
    env.pop("LOOP_PYTEST_PID", None)   # a verification is never nested in an outer one's pytest
    env["LOOP_REPORT_DIR"] = str(report_dir)
    env["LOOP_REPO_ROOT"] = str(Path(root).resolve())
    addopts, plugin = env.get("PYTEST_ADDOPTS", ""), PLUGIN
    if plugin_dir is not None:
        # A verifier run under another's pytest (the engine's own tests, run as a check) inherits
        # its `-p PLUGIN`, which a command that replaces PYTHONPATH no longer reaches.
        addopts, plugin = re.sub(rf"(?<!\S)-p {re.escape(PLUGIN)}(?!\S)", "", addopts), PLUGIN_COPY
    env["PYTEST_ADDOPTS"] = (addopts + f" -p {plugin} -p no:cacheprovider").strip()
    plugin_dirs = [ENGINE_DIR, *([plugin_dir] if plugin_dir is not None else [])]
    path = [str(d) for d in plugin_dirs] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    env["PYTHONPATH"] = os.pathsep.join(path)
    cwd = root / spec.cwd
    timeout = spec.timeout_s or default_timeout
    start = time.monotonic()
    notes: list[str] = []
    try:   # an agent-written directory may not lead out of the tree under judgement
        outside = not cwd.resolve().is_relative_to(root.resolve()) or ".git" in Path(spec.cwd).parts
    except (OSError, RuntimeError):
        outside = True
    if outside:
        code, output = None, f"check directory leaves the tree under judgement: {spec.cwd}"
        notes.append(output)
    elif not cwd.is_dir():
        code, output = None, f"check directory does not exist: {spec.cwd}"
        notes.append(output)
    else:
        box = (mounts if mounts is not None else sandbox.Mounts(writable=(Path(root),))).plus(
            readable=plugin_dirs, writable=[report_dir, *sandbox.cache_dirs(extra_env)])
        code, output, timed_out = _run(spec.command, cwd, timeout, env, box)
        if timed_out:
            notes.append(f"timed out after {timeout} s")
    duration = round(time.monotonic() - start, 2)
    cases = _read_records(report_dir)
    notes += _notes(cases, output)
    ev = Evidence(criterion=spec.criterion, kind="command", command=spec.command, cwd=spec.cwd, head_sha=head_sha,
                  exit_code=code, passed=code == 0 and not notes, duration_s=duration,
                  output_tail=output[-OUTPUT_TAIL:], output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                  tests={k: len(cases[k]) for k in ("passed", "failed", "skipped")} if cases else None,
                  note="; ".join(notes))
    return ev, cases


STANDING = "standing:"   # the criterion prefix of a standing check's evidence record


def standing_checks(root: Path, checks: Iterable[tuple[str, str]], head_sha: str, work_dir: Path,
                    default_timeout: int = 900, extra_env: dict[str, str] | None = None,
                    mounts: sandbox.Mounts | None = None) -> tuple[list[dict], list[str]]:
    """Run the repository's standing checks (`(name, command)` pairs, each from the root of the
    tree under judgement). Returns (evidence records, problems). A check that does not exit 0
    is a problem that carries the tail of its output, so the builder can act on it: that covers a
    finding, a scanner the tree no longer has, and a scanner that cannot start. Nothing here
    raises for a failing check."""
    evidence: list[dict] = []
    problems: list[str] = []
    for name, command in checks:
        spec = CheckSpec(criterion=f"{STANDING}{name}", command=command, cwd=".", author="engine",
                         description=f"standing check: {name}")
        ev, _ = run_check(root, spec, head_sha, work_dir, default_timeout, extra_env, mounts)
        evidence.append(asdict(ev))
        if not ev.passed:
            problems.append(f"standing check `{name}` failed ({ev.note or f'exit {ev.exit_code}'}): every change must "
                            f"pass `{command}`. Its output:\n{ev.output_tail[-1500:].strip() or '(none)'}")
    return evidence, problems


def verify(root: Path, specs: list[CheckSpec], head_sha: str, work_dir: Path, default_timeout: int = 900,
           extra_env: dict[str, str] | None = None, standing: Iterable[tuple[str, str]] = (),
           mounts: sandbox.Mounts | None = None) -> tuple[bool, list[dict], list[str]]:
    """Run the `standing` checks, then every command check. Returns (all passed, evidence
    records, problems). A command that several checks give (the same command, directory and
    timeout, for several criteria) runs once, and its run is the evidence record of each of those
    criteria. `extra_env` reaches the check commands only; `standing` is `config.STANDING_CHECKS`
    when the engine verifies a head that may be examined or pushed; `mounts` is what the commands
    may see (`run_check`)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    evidence, problems = standing_checks(root, standing, head_sha, work_dir, default_timeout, extra_env, mounts)
    runs: dict[tuple[str | None, str, int | None], Evidence] = {}
    for spec in specs:
        if spec.kind != "command":
            continue
        # A command several criteria name runs once; its run counts for each of them.
        key = (spec.command, spec.cwd, spec.timeout_s)
        if key not in runs:
            runs[key] = run_check(root, spec, head_sha, work_dir, default_timeout, extra_env, mounts)[0]
        ev = replace(runs[key], criterion=spec.criterion)
        evidence.append(asdict(ev))
        if not ev.passed:
            problems.append(f"{spec.criterion} failed: {ev.note or f'exit {ev.exit_code}'}")
    return (not problems), evidence, problems
