"""The environment probe, what the host lacks, and the smoke call, with a fake host: no network."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import REPO_ROOT, git, identify
from implement_loop import environment, schemas
from implement_loop.environment import Probe


def done(cmd, code=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, code, out, err)


class FakeHost:
    """Plays uv: `uv` says whether it is installed, `pytest` is what `pytest --version` prints."""

    def __init__(self, *, uv=True, pytest="pytest 8.3.5"):
        self.uv, self.pytest = uv, pytest
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[0] == "uv":
            if not self.uv:
                raise FileNotFoundError("uv")
            return done(cmd, out=self.pytest + "\n")
        raise AssertionError(f"unexpected command {cmd}")

    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls]


class EnvironmentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-env-"))
        self.root = self.tmp / "repo"
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def host(self, **kw) -> FakeHost:
        return FakeHost(**kw)


class ProbeTests(EnvironmentTestCase):
    def test_a_healthy_host_passes_each_kind(self):
        probes = environment.probe(self.root, self.host())
        self.assertEqual([(p.name, p.kind, p.ok) for p in probes], [("pytest", "python-tests", True)])
        self.assertEqual(probes[0].detail, "pytest 8.3.5 via uv run --no-project")

    def test_the_pinned_runner_is_the_one_ci_uses(self):
        host = self.host()
        environment.probe(self.root, host)
        uv = next(c for c in host.commands() if c[0] == "uv")
        self.assertEqual(uv, ["uv", "run", "--no-project", "--with", "pytest==8.3.5", "python", "-m", "pytest", "--version"])
        # the repository's pin (the dev group) and the gate script CI runs, which names it for the engine tests
        for pinned in ("pyproject.toml", "scripts/check.sh"):
            with self.subTest(pinned):
                self.assertIn(environment.PYTEST_PIN, (REPO_ROOT / pinned).read_text())

    def test_failures_are_parsed_per_probe(self):
        [pytest] = environment.probe(self.root, self.host(uv=False))
        self.assertFalse(pytest.ok)
        self.assertEqual(pytest.detail, "uv not found on PATH")
        [pytest] = environment.probe(self.root, self.host(pytest="pytest 9.0.0"))
        self.assertFalse(pytest.ok)


class HealTests(EnvironmentTestCase):
    def test_a_healthy_host_needs_nothing(self):
        host = self.host()
        self.assertEqual(environment.heal(self.root, host), [])
        self.assertTrue(all(cmd[0] == "uv" for cmd in host.commands()), "it only probes")

    def test_a_missing_uv_is_reported(self):
        host = self.host(uv=False)
        actions = environment.heal(self.root, host)
        self.assertEqual(actions, ["no recipe installs uv; Python test checks stay unavailable"])
        for cmd in host.commands():
            self.assertNotIn("sudo", cmd)


class FingerprintTests(unittest.TestCase):
    def test_it_depends_only_on_which_probes_pass(self):
        a = [Probe("pytest", True, "pytest 8.3.5", "python-tests"), Probe("synth", False, "x", "infrastructure")]
        b = [Probe("synth", False, "other detail", "infrastructure"), Probe("pytest", True, "pytest 8.3.6", "python-tests")]
        self.assertEqual(environment.fingerprint(a), environment.fingerprint(b))
        self.assertEqual(len(environment.fingerprint(a)), 12)
        healed = [Probe("pytest", True, "", "python-tests"), Probe("synth", True, "", "infrastructure")]
        self.assertNotEqual(environment.fingerprint(a), environment.fingerprint(healed))


KEYS = {"ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-x"}
NATIVE = "Linux version 6.8.0-45-generic (buildd@lcy02-amd64-075) (gcc 13.2.0) #45-Ubuntu SMP"
WSL2 = "Linux version 6.18.33.2-microsoft-standard-WSL2 (root@buildhost) (gcc 13.2.0) #1 SMP"
WSL1 = ("Linux version 4.4.0-19041-Microsoft "
        "(Microsoft@Microsoft.com) "  # hygiene: allow email (the WSL1 kernel banner names its builder, not a mailbox)
        "(gcc version 5.4.0 (GCC) ) #1237-Microsoft")


class HostLacksTests(unittest.TestCase):
    def test_a_native_host_with_both_keys_lacks_only_what_is_configured(self):
        self.assertEqual(environment.host_lacks([], KEYS, NATIVE), [])
        self.assertEqual(environment.host_lacks(["platform:windows", "hardware:laptop"], KEYS, NATIVE),
                         ["hardware:laptop", "platform:windows"])

    def test_the_result_is_sorted_and_each_capability_named_once(self):
        configured = ["human:rehearsal", "account:non-owner", "platform:macos", "human:rehearsal",
                      "credentials:OPENAI_API_KEY", "platform:native-linux", " ", ""]
        self.assertEqual(environment.host_lacks(configured, {}, WSL2),
                         ["account:non-owner", "credentials:ANTHROPIC_API_KEY", "credentials:OPENAI_API_KEY",
                          "human:rehearsal", "platform:macos", "platform:native-linux"])

    def test_a_missing_or_empty_key_is_lacked(self):
        for env, lacked in (({"OPENAI_API_KEY": "sk-x"}, ["credentials:ANTHROPIC_API_KEY"]),
                            ({"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "sk-x"}, ["credentials:ANTHROPIC_API_KEY"]),
                            ({"ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "  "}, ["credentials:OPENAI_API_KEY"]),
                            ({}, ["credentials:ANTHROPIC_API_KEY", "credentials:OPENAI_API_KEY"])):
            with self.subTest(env=env):
                self.assertEqual(environment.host_lacks([], env, NATIVE), lacked)

    def test_wsl_is_not_native_linux(self):
        for text in (WSL2, WSL1, "something wsl"):
            with self.subTest(text=text):
                self.assertEqual(environment.host_lacks([], KEYS, text), ["platform:native-linux"])
        self.assertEqual(environment.host_lacks([], KEYS, ""), [], "no /proc/version (macOS, Windows): no claim")

    def test_the_defaults_read_this_process_and_proc_version(self):
        with mock.patch.dict(os.environ, KEYS), \
             mock.patch.object(environment, "PROC_VERSION", Path(tempfile.gettempdir()) / "no-such-proc-version"):
            self.assertEqual(environment.host_lacks(["hardware:laptop"]), ["hardware:laptop"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "version"
            proc.write_text(WSL2)
            with mock.patch.object(environment, "PROC_VERSION", proc):
                lacks = environment.host_lacks([])
        self.assertIn("credentials:ANTHROPIC_API_KEY", lacks)
        self.assertIn("platform:native-linux", lacks)


class FakeVendor:
    """Plays `claude -p`, `codex exec` and `codex mcp list` for the smoke call. `answer` is "ok",
    "fails" (exits 1), "off-schema", "hangs" (times out) or "missing" (not installed); `mcp` is
    the server listing, None for a failing `codex mcp list`."""

    def __init__(self, answer="ok", mcp="[]"):
        self.answer, self.mcp = answer, mcp
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.answer == "missing":
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        if cmd[1:3] == ["mcp", "list"]:
            return done(cmd, 1, "", "boom") if self.mcp is None else done(cmd, 0, self.mcp)
        if self.answer == "hangs":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        if self.answer == "fails":
            return done(cmd, 1, "", "API Error: 529 Overloaded")
        value = {"notes": "ok"} if self.answer == "ok" else {"other": 1}
        if cmd[0] == "claude":
            return done(cmd, 0, json.dumps({"structured_output": value, "total_cost_usd": 0.001}))
        Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(value))
        return done(cmd, 0, '{"type": "turn.completed", "usage": {}}\n')

    def smoke_calls(self) -> list[tuple[list[str], dict]]:
        return [(cmd, kw) for cmd, kw in self.calls if cmd[1:3] != ["mcp", "list"]]


class VendorReachableTests(unittest.TestCase):
    def test_a_vendor_that_answers_is_reachable(self):
        for vendor in ("claude", "codex"):
            with self.subTest(vendor), mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_secret"}):
                fake = FakeVendor()
                self.assertIs(environment.vendor_reachable(vendor, fake), True)
                [(cmd, kw)] = fake.smoke_calls()
                self.assertEqual(cmd[0], vendor)
                self.assertEqual(kw["input"], environment.SMOKE_PROMPT)
                self.assertEqual((kw["cwd"], kw["timeout"]), (environment.REPO_ROOT, 120))
                self.assertNotIn("GH_TOKEN", kw["env"], "the smoke call runs with the agents' environment")

    def test_it_is_the_doctors_explore_call(self):
        fake = FakeVendor()
        environment.vendor_reachable("claude", fake)
        [(cmd, _)] = fake.smoke_calls()
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), schemas.BY_ROLE["explore"])
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        fake = FakeVendor(mcp='[{"name": "browser"}]')
        environment.vendor_reachable("codex", fake)
        [(cmd, _)] = fake.smoke_calls()
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("mcp_servers.browser.enabled=false", cmd, "MCP servers are disabled as in a run")

    def test_a_failing_or_off_schema_answer_is_unreachable_after_one_attempt(self):
        for vendor in ("claude", "codex"):
            for answer in ("fails", "off-schema", "hangs"):
                with self.subTest(vendor, answer=answer):
                    fake = FakeVendor(answer)
                    self.assertIs(environment.vendor_reachable(vendor, fake), False)
                    self.assertEqual(len(fake.smoke_calls()), 1, "no retries")

    def test_codex_without_its_mcp_listing_is_unreachable(self):
        fake = FakeVendor(mcp=None)
        self.assertIs(environment.vendor_reachable("codex", fake), False)
        self.assertEqual(fake.smoke_calls(), [], "it fails closed, as in a run")

    def test_a_cli_that_is_not_installed_is_none(self):
        for vendor in ("claude", "codex"):
            with self.subTest(vendor):
                self.assertIsNone(environment.vendor_reachable(vendor, FakeVendor("missing")))

    def test_the_timeout_bounds_each_process(self):
        fake = FakeVendor()
        environment.vendor_reachable("codex", fake, timeout_s=30)
        self.assertEqual([kw["timeout"] for _, kw in fake.calls], [30, 30])

    def test_an_unknown_vendor_is_refused(self):
        with self.assertRaises(ValueError):
            environment.vendor_reachable("gemini", FakeVendor())


class DoctorSmokeTests(unittest.TestCase):
    def test_the_doctor_makes_the_shared_smoke_call_and_reports_as_before(self):
        import argparse

        from implement_loop import cli
        calls = []

        class Agents:
            def __init__(self, *args, **kwargs):
                pass

            def call(self, vendor, role, cwd, prompt):
                calls.append((vendor, role, cwd, prompt))
                if vendor == "codex":
                    raise RuntimeError("codex exited 1: boom")
                return {"notes": "ok"}

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "repo_root", return_value=Path(tmp)), \
             mock.patch.object(cli, "runs_dir", return_value=Path(tmp)), \
             mock.patch.object(cli.environment, "heal", return_value=[]), \
             mock.patch.object(cli, "_out", return_value=(0, "o/r")), \
             mock.patch.object(cli, "doctor_checks", return_value=[("PASS", "python", "3.12")]), \
             mock.patch("implement_loop.live.LiveAgents", Agents), \
             mock.patch("implement_loop.workspace.Workspace", lambda *args, **kwargs: None), \
             redirect_stdout(out):
            code = cli.cmd_doctor(argparse.Namespace(repo=None, smoke=True))
            root = Path(tmp)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().splitlines(), [
            "PASS  python          3.12",
            "PASS  smoke claude    a structured call worked",
            "FAIL  smoke codex     codex exited 1: boom",
        ])
        self.assertEqual(calls, [(v, "explore", root, 'Do not use any tool. Return {"notes": "ok"}.')
                                 for v in ("claude", "codex")])


class DoctorRepositoryTests(unittest.TestCase):
    """Before the green light the doctor fails, and says which step is missing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-doctor-"))
        self.root = self.tmp / "work"
        git("init", "-q", "-b", "main", str(self.root), cwd=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_remote(self, pushed: bool) -> None:
        """A bare `origin` beside the checkout, empty or with one commit on `main`."""
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(remote), cwd=self.tmp)
        git("remote", "add", "origin", str(remote), cwd=self.root)
        if pushed:
            identify(self.root)
            (self.root / "README.md").write_text("x\n")
            git("add", "README.md", cwd=self.root)
            git("commit", "-q", "-m", "first", cwd=self.root)
            git("push", "-q", "origin", "HEAD:main", cwd=self.root)

    def lines(self, slug) -> dict[str, tuple[str, str]]:
        from implement_loop import cli
        real = cli._out

        def out(cmd):   # git is real, every other tool answers
            return real(cmd) if cmd[0] == "git" else (0, "stub 1.0")
        with mock.patch.object(cli, "_out", out), mock.patch.object(cli.environment, "probe", return_value=[]):
            checks = cli.doctor_checks(self.root, slug)
        return {name: (status, detail) for status, name, detail in checks}

    def test_a_repository_without_a_remote_fails_and_says_so(self):
        lines = self.lines(None)
        status, detail = lines["repository"]
        self.assertEqual(status, "FAIL")
        self.assertIn("no `origin` remote", detail)
        self.assertIn("docs/factory/green-light.md", detail)
        self.assertNotIn("base branch", lines, "nothing to look for without a repository")

    def test_with_a_remote_the_failure_is_left_to_gh(self):
        self.add_remote(pushed=False)
        self.assertEqual(self.lines(None)["repository"], ("FAIL", "gh repo view failed"))

    def test_a_remote_nothing_was_pushed_to_fails_on_the_base_branch(self):
        self.add_remote(pushed=False)
        lines = self.lines("o/r")
        self.assertEqual(lines["repository"], ("PASS", "o/r"))
        status, detail = lines["base branch"]
        self.assertEqual(status, "FAIL")
        self.assertIn("origin has no `main` branch: nothing is pushed yet", detail)
        self.assertIn("docs/factory/green-light.md", detail)

    def test_a_pushed_main_passes(self):
        self.add_remote(pushed=True)
        lines = self.lines("o/r")
        self.assertEqual((lines["repository"], lines["base branch"]), (("PASS", "o/r"), ("PASS", "origin/main")))

    def test_a_remote_that_cannot_be_read_fails_with_gits_message(self):
        git("remote", "add", "origin", str(self.tmp / "no-such-remote.git"), cwd=self.root)
        status, detail = self.lines("o/r")["base branch"]
        self.assertEqual(status, "FAIL")
        self.assertIn("could not read origin/main", detail)


if __name__ == "__main__":
    unittest.main()
