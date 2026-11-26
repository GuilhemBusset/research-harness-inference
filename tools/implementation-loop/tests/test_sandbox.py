"""The sandbox (ADR 0001, decision 3): the command line it builds, the one environment scrub,
that every place the engine starts agent-controlled code goes through it, that nothing runs
without it, and, with the real bubblewrap, what a process inside can and cannot reach.

The tests named `Real...` need a bubblewrap that may create namespaces on this machine. They are
the proof the doctor's canary rests on, so CI must provide one (a skip there fails the step)."""

import argparse
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import REPO_ROOT, SHIP, TempRepo, git, identify, issue_body
from implement_loop import cli, environment, live, sandbox, verify, workspace
from implement_loop.agents import FakeAgents, Script
from implement_loop.config import Caps, Config, is_safety_surface
from implement_loop.engine import Engine
from implement_loop.github import FakeGitHub
from implement_loop.graph import build_plan
from implement_loop.model import WAIT_ENVIRONMENT, CheckSpec, Phase
from implement_loop.state import RunStore
from implement_loop.supervisor import Outcome
from implement_loop.workspace import Workspace
from test_live import ANSWERS, FAKE, RecordingMeter, StubWorkspace
from test_temp_repo import traced


def _bubblewrap_works() -> bool:
    exe = shutil.which("bwrap")
    if exe is None:
        return False
    try:
        return subprocess.run([exe, "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                              capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


REAL_SANDBOX = _bubblewrap_works()
needs_bubblewrap = unittest.skipUnless(REAL_SANDBOX, "needs a bubblewrap that may create namespaces on this machine")


class Recording:
    """A launcher that passes every command through and remembers what it was asked for."""

    def __init__(self):
        self.launches: list[SimpleNamespace] = []

    def command(self, argv, cwd, vendor=None, mounts=None):
        self.launches.append(SimpleNamespace(argv=[str(a) for a in argv], cwd=Path(cwd), vendor=vendor,
                                             mounts=mounts or sandbox.Mounts()))
        return [str(a) for a in argv]


class Refusing:
    def command(self, argv, cwd, vendor=None, mounts=None):
        raise sandbox.SandboxUnavailable("bubblewrap is not installed")


class SandboxCase(unittest.TestCase):
    """A temporary directory, and the suite's pass-through launcher put back afterwards."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-sandbox-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(sandbox.use, sandbox.PassThrough())

    def fake_home(self) -> Path:
        """A home directory with everything a real one has that no agent may see."""
        home = self.tmp / "home"
        for name, content in ((".aws/credentials", "[default]\n"), (".config/gh/hosts.yml", "host\n"),
                              (".ssh/id_canary", "key\n"), (".claude/.credentials.json", "{}"),
                              (".claude/history.jsonl", "private\n"), (".codex/auth.json", "{}"),
                              (".codex/config.toml", ""), (".codex/packages/bin/tool", "x"), (".local/bin/tool", "x")):
            path = home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return home


# ---------------------------------------------------------------- the environment

SCRUB_BASE = {
    "PATH": "/usr/bin", "HOME": "/somewhere", "TOKENIZERS_PARALLELISM": "false", "UV_CACHE_DIR": "/cache",
    "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_SESSION_TOKEN": "x", "AWS_PROFILE": "x",
    "AWS_REGION": "x", "AMAZON_ANYTHING": "x", "CDK_DEFAULT_ACCOUNT": "x", "CDK_DEFAULT_REGION": "x",
    "GOOGLE_APPLICATION_CREDENTIALS": "x", "GCLOUD_PROJECT": "x", "AZURE_CLIENT_SECRET": "x",
    "GH_TOKEN": "x", "GITHUB_TOKEN": "x", "GH_ENTERPRISE_TOKEN": "x", "GITHUB_ENTERPRISE_TOKEN": "x",
    "SSH_AUTH_SOCK": "x", "GIT_ASKPASS": "x", "VSCODE_GIT_IPC_HANDLE": "x", "XDG_RUNTIME_DIR": "x",
    "WSL_INTEROP": "x", "DBUS_SESSION_BUS_ADDRESS": "x", "CLAUDE_CODE_MESSAGING_SOCKET": "x",
    "CLAUDE_CODE_MESSAGING_TOKEN": "x", "NPM_TOKEN": "x", "DB_PASSWORD": "x", "SOME_API_KEY": "x",
    "npm_config_authtoken": "x", "ANTHROPIC_API_KEY": "a", "CLAUDE_CODE_OAUTH_TOKEN": "a",
    "OPENAI_API_KEY": "o", "CODEX_API_KEY": "o",
}
SCRUB_KEPT = {"PATH", "HOME", "TOKENIZERS_PARALLELISM", "UV_CACHE_DIR"}
SCRUB_ADDED = {"AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": "/dev/null",
               "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""}


class EnvironmentScrubTests(SandboxCase):
    def test_a_check_command_gets_no_credential_of_any_kind(self):
        env = sandbox.clean_env(SCRUB_BASE, self.tmp / "no-gh")
        self.assertEqual(set(env), SCRUB_KEPT | set(SCRUB_ADDED) | {"GH_CONFIG_DIR"})
        self.assertEqual({k: env[k] for k in SCRUB_ADDED}, SCRUB_ADDED)
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.tmp / "no-gh"))
        self.assertEqual(list((self.tmp / "no-gh").iterdir()), [], "gh is pointed at a directory with no login")

    def test_a_vendors_keys_reach_that_vendors_call_only(self):
        claude = sandbox.clean_env(SCRUB_BASE, self.tmp / "no-gh", "claude")
        codex = sandbox.clean_env(SCRUB_BASE, self.tmp / "no-gh", "codex")
        own = {"claude": {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}, "codex": {"OPENAI_API_KEY", "CODEX_API_KEY"}}
        self.assertEqual(set(claude) - set(codex), own["claude"])
        self.assertEqual(set(codex) - set(claude), own["codex"])
        for env in (claude, codex):
            self.assertFalse([k for k in env if k.startswith(("AWS_", "GH_", "GITHUB_")) and k not in SCRUB_ADDED
                              and k != "GH_CONFIG_DIR"])

    def test_the_only_aws_variables_left_switch_credential_sources_off(self):
        for vendor in (None, "claude", "codex"):
            env = sandbox.clean_env(SCRUB_BASE | {"AWS_EC2_METADATA_DISABLED": "false", "AWS_CONFIG_FILE": "/real"},
                                    self.tmp / "no-gh", vendor)
            self.assertEqual({k: v for k, v in env.items() if k.startswith("AWS_")}, sandbox.NEUTRAL_AWS)

    def test_agents_and_checks_share_the_one_scrub(self):
        with mock.patch.dict(os.environ, SCRUB_BASE, clear=True):
            agent = live.agent_env(self.tmp / "a" / "no-gh", {"UV_LINK_MODE": "copy", "GH_TOKEN": "late"})
            check = verify.scrubbed_env(dict(os.environ) | {"UV_LINK_MODE": "copy", "GH_TOKEN": "late"}, self.tmp / "a")
        self.assertEqual(agent, check)
        self.assertEqual(agent["UV_LINK_MODE"], "copy")
        self.assertNotIn("GH_TOKEN", agent, "what the engine adds is scrubbed like the rest")
        self.assertEqual(live.CREDENTIAL_VARS, verify.CREDENTIAL_VARS)

    def test_the_claude_deny_list_names_aws_and_deploys(self):
        self.assertIn("Bash(aws *)", live.CLAUDE_DENY)
        self.assertIn("Bash(cdk deploy*)", live.CLAUDE_DENY)

    def kept(self, env: dict[str, str], vendor=None) -> set[str]:
        return set(sandbox.clean_env(env, self.tmp / "no-gh", vendor)) - set(SCRUB_ADDED) - {"GH_CONFIG_DIR"}

    def test_names_are_compared_without_regard_to_case(self):
        env = {"PATH": "/usr/bin", "aws_profile": "x", "Aws_Region": "x", "gh_token": "x", "anthropic_api_key": "x",
               "vscode_ipc": "x", "http_proxy": "http://proxy.example.com:3128", "npm_config_cache": "/cache"}
        self.assertEqual(self.kept(env), {"PATH", "http_proxy", "npm_config_cache"})
        self.assertEqual(self.kept(env, "claude"), {"PATH", "http_proxy", "npm_config_cache"},
                         "a vendor's key passes under the name its CLI reads, and under no other spelling")

    def test_a_value_shaped_like_a_credential_is_dropped_whatever_the_variable_is_called(self):
        # each value is put together here, so that no line of this file has the shape itself
        shaped = {"BACKUP": "AKIA" + "A1B2C3D4E5F6G7H8", "NOTE": "see ghp_" + "a1B2" * 9,
                  "FINE_GRAINED": "github_pat_" + "a1_B" * 10, "MODEL_A": "sk-ant-" + "ab12" * 8,
                  "MODEL_O": "sk-proj-" + "ab12" * 10, "DEPLOY": "-----BEGIN OPENSSH PRIVATE " + "KEY-----\nabc",
                  "DATABASE_URL": "postgres://app:" + "hunter2@db.example.com/app",
                  "HTTPS_PROXY": "http://me:" + "pw@proxy.example.com:3128"}
        plain = {"PATH": "/usr/bin", "HTTP_PROXY": "http://proxy.example.com:3128", "LOCAL": "http://localhost:8080/@scope/pkg",
                 "REGISTRY": "https://registry.example.com/simple", "REMOTE": "git@github.com:owner/repo.git",
                 "SUM": "sha256-" + "ab12" * 16, "TASK": "task-" + "ab12" * 10, "STAMP": "AKIA-is-a-prefix"}
        for vendor in (None, "claude", "codex"):
            with self.subTest(vendor=vendor):
                self.assertEqual(self.kept(shaped | plain, vendor), set(plain))

    def test_a_vendors_own_key_passes_with_the_shape_it_has(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-" + "ab12" * 8, "OPENAI_API_KEY": "sk-proj-" + "ab12" * 10}
        self.assertEqual(self.kept(env, "claude"), {"ANTHROPIC_API_KEY"})
        self.assertEqual(self.kept(env, "codex"), {"OPENAI_API_KEY"})
        self.assertEqual(self.kept(env), set())

    def test_session_keys_and_pointers_to_other_credentials_are_dropped(self):
        names = ("BW_SESSION", "OP_SESSION_work", "CLAUDE_CODE_SESSION_ID", "XDG_SESSION_TYPE", "COPILOT_DEBUG_NONCE",
                 "ERLANG_COOKIE", "KUBECONFIG", "DOCKER_HOST", "DOCKER_CONFIG", "NETRC", "BOTO_CONFIG", "PGPASSFILE")
        harmless = {"PATH": "/usr/bin", "TERM": "xterm", "SESSIONS_DIR": "/var/lib/sessions", "LANG": "C.UTF-8"}
        self.assertEqual(self.kept(dict.fromkeys(names, "x") | harmless), set(harmless))

    def test_what_the_scrub_cannot_recognise_passes(self):
        # the limit, stated: it is a list of what is removed, by name and by shape
        self.assertEqual(self.kept({"DEPLOY_HOOK": "https://hooks.example.com/services/T000/B000/abcdef"}), {"DEPLOY_HOOK"})


# ---------------------------------------------------------------- the command line

class CommandLineTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.home = self.fake_home()
        self.bwrap = self.tmp / "bwrap"
        self.bwrap.write_text("#!/bin/sh\n")
        self.bwrap.chmod(self.bwrap.stat().st_mode | stat.S_IEXEC)
        self.box = sandbox.Bubblewrap(sandbox.Policy(), bwrap=str(self.bwrap), home=self.home)
        self.worktree = self.tmp / "worktree"
        self.worktree.mkdir()

    def args(self, vendor=None, mounts=None) -> list[str]:
        return self.box.command(["tool", "--flag"], self.worktree, vendor=vendor,
                                mounts=mounts or sandbox.Mounts(writable=(self.worktree,)))

    @staticmethod
    def binds(args: list[str]) -> dict[str, str]:
        return {args[i + 1]: a for i, a in enumerate(args) if a in ("--bind", "--ro-bind") and args[i + 1] == args[i + 2]}

    @staticmethod
    def hidden(args: list[str]) -> list[str]:
        return [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]

    def test_the_shape_of_the_sandbox(self):
        args = self.args()
        self.assertEqual(args[0], str(self.bwrap))
        for flag in ("--die-with-parent", "--new-session", "--unshare-pid"):
            self.assertIn(flag, args)
        self.assertNotIn("--unshare-net", args, "the network is shared")
        self.assertEqual(args[args.index("--ro-bind"):args.index("--ro-bind") + 3], ["--ro-bind", "/", "/"])
        self.assertEqual(args[args.index("--proc") + 1], "/proc")
        self.assertEqual(args[args.index("--dev") + 1], "/dev")
        self.assertIn("/tmp", self.hidden(args), "a /tmp of its own")
        self.assertIn(str(self.home), self.hidden(args), "the home directory is an empty directory")
        self.assertEqual(args[-5:], ["--chdir", str(self.worktree), "--", "tool", "--flag"])

    def test_everything_hidden_comes_before_anything_bound_back(self):
        args = self.args("claude")
        last_hidden = max(i for i, a in enumerate(args) if a == "--tmpfs")
        first_bound = min(i for i, a in enumerate(args) if a in ("--bind", "--ro-bind") and args[i + 1] != "/")
        self.assertLess(last_hidden, first_bound)

    def test_the_home_directory_holds_only_the_tools(self):
        binds = self.binds(self.args())
        inside_home = {p: mode for p, mode in binds.items() if p.startswith(str(self.home))}
        self.assertEqual(inside_home, {str(self.home / ".local/bin"): "--ro-bind",
                                       str(self.home / ".codex/packages"): "--ro-bind"})
        for secret in (".aws", ".ssh", ".config", ".config/gh"):
            self.assertFalse([p for p in binds if p.startswith(str(self.home / secret))], secret)

    def test_a_vendor_gets_its_own_login_and_never_the_others(self):
        claude, codex, check = (self.binds(self.args(v)) for v in ("claude", "codex", None))
        login = {"claude": str(self.home / ".claude/.credentials.json"), "codex": str(self.home / ".codex/auth.json")}
        self.assertEqual(claude[login["claude"]], "--bind")
        self.assertNotIn(login["codex"], claude)
        self.assertEqual(codex[login["codex"]], "--bind")
        self.assertEqual(codex[str(self.home / ".codex/config.toml")], "--ro-bind", "settings are read-only")
        self.assertNotIn(login["claude"], codex)
        self.assertFalse(set(login.values()) & set(check), "a check command gets neither login")
        self.assertNotIn(str(self.home / ".claude/history.jsonl"), claude, "the login file, not the vendor's directory")
        self.assertNotIn(str(self.home / ".claude"), claude)

    def test_the_callers_paths_are_bound_as_asked_and_nothing_else_is_writable(self):
        ro = self.tmp / "engine"
        ro.mkdir()
        args = self.args(mounts=sandbox.Mounts(readable=(ro,), writable=(self.worktree,)))
        binds = self.binds(args)
        self.assertEqual((binds[str(ro)], binds[str(self.worktree)]), ("--ro-bind", "--bind"))
        self.assertEqual([p for p, mode in binds.items() if mode == "--bind"], [str(self.worktree)])

    def test_a_narrower_bind_lands_on_top_of_a_wider_one(self):
        inner = self.worktree / "report"
        inner.mkdir()
        outer_first = self.args(mounts=sandbox.Mounts(readable=(self.worktree,), writable=(inner,)))
        inner_first = self.args(mounts=sandbox.Mounts(writable=(inner,), readable=(self.worktree,)))
        for args in (outer_first, inner_first):
            self.assertLess(args.index(str(self.worktree)), args.index(str(inner)))
            self.assertEqual(self.binds(args)[str(inner)], "--bind")

    def test_a_missing_tool_directory_is_skipped_and_a_missing_caller_path_is_refused(self):
        self.assertNotIn(str(self.home / ".local/share/claude"), self.binds(self.args()))
        with self.assertRaises(sandbox.SandboxUnavailable):
            self.args(mounts=sandbox.Mounts(writable=(self.tmp / "no-such-worktree",)))

    def test_without_bubblewrap_there_is_no_command(self):
        box = sandbox.Bubblewrap(sandbox.Policy(), bwrap=str(self.tmp / "not-installed"), home=self.home)
        with self.assertRaises(sandbox.SandboxUnavailable) as raised:
            box.command(["true"], self.worktree)
        self.assertIn("not installed", str(raised.exception))

    def test_an_unusable_home_directory_is_refused(self):
        for home in (Path("/"), self.tmp / "no-such-home"):
            with self.subTest(home=home), self.assertRaises(sandbox.SandboxUnavailable):
                sandbox.Bubblewrap(sandbox.Policy(), bwrap=str(self.bwrap), home=home).command(["true"], self.worktree)

    def test_the_default_launcher_is_bubblewrap(self):
        sandbox.use(None)
        self.assertIsInstance(sandbox.current(), sandbox.Bubblewrap)
        sandbox.use(sandbox.PassThrough())
        self.assertIsInstance(sandbox.current(), sandbox.PassThrough)


class AccountHomeTests(SandboxCase):
    """`$HOME` is what the launching shell says. The account's own home directory is hidden too."""

    def test_the_password_database_names_it(self):
        self.assertEqual(sandbox.account_homes(), [Path(pwd.getpwuid(os.getuid()).pw_dir)])

    def test_it_is_hidden_when_home_points_elsewhere(self):
        real = self.tmp / "accounts" / "owner"
        (real / ".aws").mkdir(parents=True)
        bwrap = self.tmp / "bwrap"
        bwrap.write_text("#!/bin/sh\n")
        bwrap.chmod(0o755)
        worktree = self.tmp / "worktree"
        worktree.mkdir()
        box = sandbox.Bubblewrap(sandbox.Policy(), bwrap=str(bwrap), home=self.fake_home())
        with mock.patch.object(sandbox, "account_homes", return_value=[real]):
            args = box.command(["true"], worktree, mounts=sandbox.Mounts(writable=(worktree,)))
        hidden = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
        self.assertIn(str(real), hidden)
        self.assertIn(str(self.tmp / "home"), hidden, "and the one the environment names, as before")
        first_bound = min(i for i, a in enumerate(args) if a in ("--bind", "--ro-bind") and args[i + 1] != "/")
        self.assertLess(args.index(str(real)), first_bound)

    def test_the_canary_looks_for_credentials_there_too(self):
        paths = sandbox.secret_paths(Path("/somewhere"), {}, [Path("/accounts/owner"), Path("/somewhere")])
        self.assertEqual(len(paths), 6, "the home the environment names, once, and the other one")
        self.assertEqual((paths["~/.aws of the account's own home"], paths["~/.ssh of the account's own home"],
                          paths["the gh login of the account's own home"]),
                         ("/accounts/owner/.aws", "/accounts/owner/.ssh", "/accounts/owner/.config/gh"))
        home = self.fake_home()
        real = self.tmp / "accounts" / "owner"
        (real / ".aws").mkdir(parents=True)
        (real / ".aws" / "credentials").write_text("[default]\n")
        environ = dict(os.environ) | {"HOME": str(home), "XDG_CONFIG_HOME": "", "GH_CONFIG_DIR": ""}
        with mock.patch.object(sandbox, "account_homes", return_value=[real]):
            report = sandbox.canary(self.tmp / "scratch", launcher=sandbox.PassThrough(), environ=environ,
                                    policy=sandbox.Policy(), read_only=[])
        self.assertIn("check: ~/.aws of the account's own home is readable in the sandbox", "\n".join(report.problems))


class PolicyTests(SandboxCase):
    def test_loop_toml_states_the_defaults(self):
        policy = sandbox.load_policy()
        self.assertEqual(policy, sandbox.Policy(), "loop.toml [sandbox] and the code's defaults say the same")
        data = __import__("tomllib").loads(sandbox.CONFIG_FILE.read_text())["sandbox"]
        self.assertEqual(set(data), {"toolchain", "hide", "login", "settings"}, "every key is written down")

    def test_no_credential_directory_is_on_the_allow_list(self):
        policy = sandbox.load_policy()
        listed = [*policy.toolchain, *[p for paths in (*policy.login.values(), *policy.settings.values()) for p in paths]]
        for path in listed:
            for secret in ("~/.aws", "~/.ssh", "~/.config", "~/.gnupg"):
                self.assertFalse(path == secret or path.startswith(secret + "/") or secret.startswith(path + "/"),
                                 f"{path} would bring {secret} back")
        self.assertFalse({"~", "~/", "~/.claude", "~/.codex"} & set(listed), "never a vendor's whole directory")
        for must in ("/home", "/run", "/mnt"):
            self.assertIn(must, policy.hide)

    def test_a_malformed_table_is_refused(self):
        for text in ('[sandbox]\ntoolchain = "~/.local/bin"\n', "[sandbox]\nhide = [1]\n", "[sandbox]\nallow = []\n",
                     '[sandbox.login]\ngemini = ["~/x"]\n', "[sandbox\n"):
            path = self.tmp / "loop.toml"
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(sandbox.SandboxUnavailable):
                sandbox.load_policy(path)

    def test_a_table_narrows_or_extends_the_defaults(self):
        path = self.tmp / "loop.toml"
        path.write_text('[sandbox]\ntoolchain = ["~/tools"]\n[sandbox.login]\nclaude = ["~/elsewhere/login.json"]\n')
        policy = sandbox.load_policy(path)
        self.assertEqual(policy.toolchain, ("~/tools",))
        self.assertEqual(policy.login, {"claude": ("~/elsewhere/login.json",), "codex": sandbox.DEFAULT_LOGIN["codex"]})
        self.assertEqual(policy.hide, sandbox.DEFAULT_HIDE)

    def test_the_sandbox_is_on_the_safety_surface(self):
        self.assertTrue(is_safety_surface("tools/implementation-loop/implement_loop/sandbox.py"))
        self.assertTrue(is_safety_surface("tools/implementation-loop/loop.toml"), "its allow-list too")


# ---------------------------------------------------------------- what a worktree may see

class WorktreeMountTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.common = self.tmp / "repo" / ".git"
        (self.common / "objects").mkdir(parents=True)
        self.loop = self.tmp / "repo.loop" / "issue-1"
        self.private = self.loop / "agent-git" / "abc123"
        (self.private / "objects" / "info").mkdir(parents=True)
        self.worktree = self.loop / "issue-7"
        self.worktree.mkdir()
        self.ws = SimpleNamespace(dir=self.loop, common_dir=lambda: self.common)

    def on(self, gitdir: Path) -> None:
        (self.worktree / ".git").write_text(f"gitdir: {gitdir}\n")

    def test_the_marker_of_a_private_directory_is_the_workspaces(self):
        self.assertEqual(workspace.SAVED_GITFILE, "implement-loop-gitfile")

    def test_a_working_call_gets_its_worktree_and_private_git_directory(self):
        self.on(self.private)
        mounts = sandbox.worktree_mounts(self.worktree, True, self.ws)
        self.assertEqual(mounts.writable, (self.worktree, self.private))
        self.assertEqual(mounts.readable, (self.common / "objects",), "of the shared repository, the objects only")

    def test_a_read_only_call_gets_nothing_writable(self):
        self.on(self.private)
        mounts = sandbox.worktree_mounts(self.worktree, False, self.ws)
        self.assertEqual(mounts.writable, ())
        self.assertEqual(mounts.readable, (self.worktree, self.private, self.common / "objects"))
        (self.common / "worktrees" / "coord").mkdir(parents=True)
        self.on(self.common / "worktrees" / "coord")
        mounts = sandbox.worktree_mounts(self.worktree, False, self.ws)
        self.assertEqual((mounts.writable, mounts.readable), ((), (self.worktree, self.common)))

    def test_a_gitfile_an_agent_rewrote_binds_nothing_writable(self):
        elsewhere = self.worktree / "planted"   # looks like a private directory, with a marker and alternates
        (elsewhere / "objects" / "info").mkdir(parents=True)
        (elsewhere / workspace.SAVED_GITFILE).write_text("gitdir: x\n")
        (elsewhere / "objects" / "info" / "alternates").write_text(f"{Path.home() / '.aws'}\n")
        for target in (elsewhere, self.common, Path.home() / ".ssh", self.loop / "agent-git", self.private / "objects"):
            with self.subTest(target=target):
                self.on(target)
                mounts = sandbox.worktree_mounts(self.worktree, True, self.ws)
                self.assertEqual(mounts.writable, (self.worktree,), "only a directory directly under agent-git/ counts")
                self.assertEqual(mounts.readable, (self.common,))

    def test_what_an_agent_wrote_into_its_private_directory_is_never_read(self):
        self.on(self.private)
        (self.private / "objects" / "info" / "alternates").write_text(f"{Path.home() / '.aws'}\n{Path.home() / '.ssh'}\n")
        (self.private / "commondir").write_text(f"{Path.home()}\n")
        mounts = sandbox.worktree_mounts(self.worktree, True, self.ws)
        self.assertEqual(mounts.readable, (self.common / "objects",))
        self.assertEqual(mounts.writable, (self.worktree, self.private))

    def test_without_a_workspace_only_the_worktree_is_bound(self):
        self.on(self.private)
        for ws in (None, StubWorkspace(self.tmp)):
            self.assertEqual(sandbox.worktree_mounts(self.worktree, True, ws), sandbox.Mounts(writable=(self.worktree,)))
            self.assertEqual(sandbox.worktree_mounts(self.worktree, False, ws), sandbox.Mounts(readable=(self.worktree,)))

    def test_a_real_agent_call_is_recognised(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        ws = Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir)
        wt = ws.issue_worktree(5, "feat/five")
        before = sandbox.worktree_mounts(wt, True, ws)
        self.assertEqual((before.writable, before.readable), ((wt,), (ws.common_dir(),)))
        with ws.agent_call(wt):
            during = sandbox.worktree_mounts(wt, True, ws)
            private = during.writable[1]
            self.assertEqual(private.parent, (ws.dir / "agent-git").resolve())
            self.assertEqual(during.readable, (ws.common_dir() / "objects",))
        with ws.disposable(wt) as scratch:
            self.assertEqual(len(sandbox.worktree_mounts(scratch, True, ws).writable), 2, "a check's copy as well")


# ---------------------------------------------------------------- every launch goes through it

class LaunchSiteTests(SandboxCase):
    """The engine starts agent-controlled code in four places. Each asks the sandbox."""

    def setUp(self):
        super().setUp()
        bins = self.tmp / "bin"
        bins.mkdir()
        for name in ("claude", "codex"):
            path = bins / name
            path.write_text(FAKE.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.log, self.answer = self.tmp / "calls.jsonl", self.tmp / "answer.json"
        fake_env = {"FAKE_LOG": str(self.log), "FAKE_ANSWER": str(self.answer)}
        patcher = mock.patch.dict(os.environ, fake_env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = self.tmp / "cache"
        self.agents = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), claude_bin=str(bins / "claude"),
                                      codex_bin=str(bins / "codex"), sleep=lambda s: None, retries=0,
                                      extra_env={"UV_CACHE_DIR": str(self.cache / "uv")})
        self.agents.meter = self.meter = RecordingMeter()
        self.recorder = Recording()
        sandbox.use(self.recorder)

    def call(self, vendor: str, role: str) -> None:
        self.answer.write_text(json.dumps(ANSWERS[role]))
        self.agents.call(vendor, role, self.tmp, "prompt", 28)

    def test_a_claude_call_is_one_sandboxed_process(self):
        self.call("claude", "build")
        [launch] = self.recorder.launches
        self.assertEqual((launch.vendor, launch.argv[1], launch.cwd), ("claude", "-p", self.tmp))
        self.assertEqual(launch.mounts.writable, (self.tmp, self.cache / "uv"), "its worktree and the run's cache")
        self.assertTrue((self.cache / "uv").is_dir())

    def test_a_read_only_role_gets_nothing_writable(self):
        self.call("claude", "plan")
        [launch] = self.recorder.launches
        self.assertEqual((launch.mounts.writable, launch.mounts.readable), ((), (self.tmp,)))

    def test_a_codex_call_is_two_sandboxed_processes(self):
        self.call("codex", "build")
        listing, call = self.recorder.launches
        self.assertEqual((listing.vendor, listing.argv[1:3]), ("codex", ["mcp", "list"]))
        self.assertEqual(listing.mounts.writable, (), "listing its servers writes nothing")
        self.assertEqual((call.vendor, call.argv[1]), ("codex", "exec"))
        out = Path(call.argv[call.argv.index("-o") + 1])
        schema = Path(call.argv[call.argv.index("--output-schema") + 1])
        self.assertEqual(call.mounts.writable, (self.tmp, self.cache / "uv", out), "plus the file its answer goes to")
        self.assertEqual(call.mounts.readable, (schema,))
        self.assertTrue(out.is_file() and schema.is_file(), "both exist before the sandbox is built")

    def test_a_direct_spawn_is_sandboxed_too(self):
        code, out, _ = self.agents._spawn(["bash", "-c", "cat"], "through stdin", self.tmp, 30)
        self.assertEqual((code, out), (0, "through stdin"))
        [launch] = self.recorder.launches
        self.assertEqual((launch.vendor, launch.mounts), (None, sandbox.Mounts(readable=(self.tmp,))))

    def test_the_supervisors_reachability_call_is_sandboxed(self):
        ran = []

        def runner(cmd, **kwargs):
            ran.append(cmd)
            if cmd[1:3] == ["mcp", "list"]:
                return subprocess.CompletedProcess(cmd, 0, "[]", "")
            if cmd[0] == "codex":
                Path(cmd[cmd.index("-o") + 1]).write_text('{"notes": "ok"}')
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"structured_output": {"notes": "ok"}}), "")
        self.assertIs(environment.vendor_reachable("claude", runner), True)
        self.assertIs(environment.vendor_reachable("codex", runner), True)
        self.assertEqual([(l.vendor, l.argv[0]) for l in self.recorder.launches],
                         [("claude", "claude"), ("codex", "codex"), ("codex", "codex")])
        self.assertEqual(len(ran), 3, "nothing was started beside what the sandbox was asked for")
        for launch in self.recorder.launches:
            self.assertEqual(launch.mounts.readable[0], environment.REPO_ROOT)
            self.assertFalse([p for p in launch.mounts.writable if p == environment.REPO_ROOT], "the checkout is read-only")

    def test_a_check_command_is_sandboxed(self):
        root = self.tmp / "tree"
        root.mkdir()
        work = self.tmp / "work"
        ev, _ = verify.run_check(root, CheckSpec(criterion="D1", command="true"), "a" * 40, work,
                                 extra_env={"npm_config_cache": str(self.cache / "npm")})
        self.assertTrue(ev.passed)
        [launch] = self.recorder.launches
        self.assertEqual((launch.vendor, launch.argv, launch.cwd), (None, ["bash", "-c", "true"], root))
        report = (work / f"pytest-D1-{'a' * 12}").resolve()
        self.assertEqual(launch.mounts.writable, (root, report, self.cache / "npm"))
        self.assertEqual(launch.mounts.readable, (verify.ENGINE_DIR,), "the verifier's plugin, read-only")

    def test_the_standing_checks_are_sandboxed(self):
        root = self.tmp / "tree"
        root.mkdir()
        verify.verify(root, [], "a" * 40, self.tmp / "work", standing=(("hygiene", "true"),))
        self.assertEqual([(l.vendor, l.argv) for l in self.recorder.launches], [(None, ["bash", "-c", "true"])])

    def test_the_engines_checks_see_the_tree_its_private_git_directory_and_the_objects(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        ws = Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir)
        engine = Engine.__new__(Engine)
        engine.ws = ws
        with ws.disposable(ws.coord()) as scratch:
            mounts = engine._mounts(scratch)
            self.assertEqual(mounts.writable[0], scratch)
            self.assertEqual(mounts.writable[1].parent, (ws.dir / "agent-git").resolve())
            self.assertEqual(mounts.readable, (ws.common_dir() / "objects",))


# ---------------------------------------------------------------- nothing runs without it

class RefusalTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.ran = self.tmp / "ran"
        bins = self.tmp / "bin"
        bins.mkdir()
        for name in ("claude", "codex"):
            path = bins / name
            path.write_text(f"#!/bin/sh\ntouch {self.ran}\n")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.agents = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), claude_bin=str(bins / "claude"),
                                      codex_bin=str(bins / "codex"), sleep=lambda s: None)
        self.agents.meter = self.meter = RecordingMeter()
        sandbox.use(Refusing())

    def test_no_agent_starts(self):
        for vendor in ("claude", "codex"):
            with self.subTest(vendor), self.assertRaises(sandbox.SandboxUnavailable):
                self.agents.call(vendor, "explore", self.tmp, "prompt", 28)
        self.assertFalse(self.ran.exists(), "neither CLI was started")
        self.assertEqual(self.meter.outcomes(), ["unavailable", "unavailable"], "charged to nothing, and not left pending")

    def test_no_check_starts(self):
        root = self.tmp / "tree"
        root.mkdir()
        spec = CheckSpec(criterion="D1", command=f"touch {self.ran}")
        with self.assertRaises(sandbox.SandboxUnavailable):
            verify.run_check(root, spec, "a" * 40, self.tmp / "work")
        with self.assertRaises(sandbox.SandboxUnavailable):
            verify.verify(root, [spec], "a" * 40, self.tmp / "work")
        with self.assertRaises(sandbox.SandboxUnavailable):
            verify.standing_checks(root, (("hygiene", f"touch {self.ran}"),), "a" * 40, self.tmp / "work")
        self.assertFalse(self.ran.exists())

    def test_nobody_is_reachable(self):
        def runner(cmd, **kwargs):
            raise AssertionError(f"started {cmd}")
        self.assertIs(environment.vendor_reachable("claude", runner), False)
        self.assertIs(environment.vendor_reachable("codex", runner), False)

    def test_a_missing_bubblewrap_refuses_by_default(self):
        sandbox.use(None)
        with mock.patch.object(sandbox.shutil, "which", return_value=None):
            with self.assertRaises(sandbox.SandboxUnavailable):
                self.agents.call("claude", "explore", self.tmp, "prompt", 28)
            with self.assertRaises(sandbox.SandboxUnavailable):
                verify.run_check(self.tmp, CheckSpec(criterion="D1", command=f"touch {self.ran}"), "a" * 40, self.tmp / "w")
        self.assertFalse(self.ran.exists())


class EngineRefusalTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.addCleanup(sandbox.use, sandbox.PassThrough())
        self.gh = FakeGitHub(on_merge=self.repo.merge)

    def test_an_issue_waits_for_the_host_and_nothing_is_concluded(self):
        self.gh.add_issue(2, "Issue 2", issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["It passes."]),
                          labels=("size:S",))
        self.gh.add_issue(1, "Epic", "An epic.\n", subs=(2,))
        agents = FakeAgents({2: Script(files={"docs/a.txt": "alpha"})})
        store = RunStore.for_root(self.repo.work, 1)
        engine = Engine(build_plan(self.gh, 1, self.repo.work), store, self.gh,
                        Workspace(self.repo.work, 1, SHIP, base_dir=self.repo.loop_dir), agents,
                        Config(repo="t/t", caps=Caps(ci_poll_s=0, queue_poll_s=0)), repo_root=self.repo.work,
                        log=lambda m: None, sleep=lambda s: None, environment={"fingerprint": "host-1"})
        sandbox.use(Refusing())
        summary = engine.run()
        st = store.issue(2)
        self.assertEqual(st.phase, Phase.WAITING)
        wait = st.wait or {}
        self.assertEqual((wait.get("kind"), wait.get("resume"), wait.get("env")), (WAIT_ENVIRONMENT, "verify", "host-1"))
        self.assertIn("sandbox is unavailable", wait.get("detail", ""))
        self.assertIn("sandbox is unavailable", summary["waiting"][2])
        events = [e["kind"] for e in store.events()]
        self.assertNotIn("step_crashed", events, "not an engine fault")
        self.assertNotIn("verified", events, "no verdict was recorded")
        self.assertFalse([c for c in agents.calls if c[0] == "examine"])
        self.assertEqual(self.gh.prs, {}, "nothing was pushed")


# ---------------------------------------------------------------- the engine's own git

class EngineGitTests(SandboxCase):
    """The engine runs git outside the sandbox, in worktrees an agent wrote to. It must not run
    what that agent left there."""

    def setUp(self):
        super().setUp()
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        git("config", "core.hooksPath", ".githooks", cwd=self.repo.work)   # as setup/git-hooks.sh sets it
        self.ws = Workspace(self.repo.work, 1, SHIP, base_dir=self.repo.loop_dir)
        self.wt = self.ws.issue_worktree(7, "feat/seven")
        self.ran = self.tmp / "ran-outside"
        self.pinned = sandbox.engine_git_env({}, self.tmp / "no-hooks")

    def plant_hooks(self) -> None:
        """What a builder can leave in its worktree: its own copies of the repository's hooks."""
        hooks = self.wt / ".githooks"
        hooks.mkdir()
        for name in ("pre-commit", "post-commit", "commit-msg", "post-checkout", "pre-merge-commit", "post-merge"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\necho {name} >> {self.ran}\n")
            hook.chmod(0o755)
        (self.wt / "change.txt").write_text("x\n")

    def test_what_the_engine_adds_to_its_environment(self):
        self.assertEqual(self.pinned, {
            "GIT_NO_LAZY_FETCH": "1", "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(self.tmp / "no-hooks"),
            "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "protocol.ext.allow", "GIT_CONFIG_VALUE_2": "never",
            "GIT_CONFIG_KEY_3": "maintenance.auto", "GIT_CONFIG_VALUE_3": "false"})
        self.assertEqual(list((self.tmp / "no-hooks").iterdir()), [], "an empty directory of its own")
        kept = sandbox.engine_git_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name"}, self.tmp / "no-hooks")
        self.assertEqual((kept["GIT_CONFIG_COUNT"], kept["GIT_CONFIG_KEY_1"]), ("5", "core.hooksPath"))
        self.assertNotIn("GIT_CONFIG_KEY_0", kept, "what the launching shell configured stays")

    def test_without_it_git_runs_the_worktrees_own_hooks(self):
        # the hole: the builder's copy of a hook runs as the owner at the engine's next commit
        self.plant_hooks()
        self.ws.commit_all(self.wt, "Build #7", "implementation", 7)
        self.assertIn("pre-commit", self.ran.read_text())

    def test_the_engines_commits_merges_and_worktrees_run_no_hook_of_the_worktree(self):
        self.plant_hooks()
        with mock.patch.dict(os.environ, self.pinned):
            sha = self.ws.commit_all(self.wt, "Build #7", "implementation", 7)
            self.assertTrue(sha)
            with self.ws.disposable(self.wt) as scratch:   # a checkout of the builder's tree: post-checkout
                self.assertTrue((scratch / ".githooks" / "post-checkout").is_file())
            seed = self.repo.tmp / "seed"
            (seed / "other.txt").write_text("y\n")
            git("add", "other.txt", cwd=seed)
            git("commit", "-q", "-m", "main moves", cwd=seed)
            git("push", "-q", "origin", "HEAD:main", cwd=seed)
            self.assertEqual(self.ws.update(self.wt).status, "merged")
            self.ws.push(self.wt)
        self.assertFalse(self.ran.exists(), "nothing the builder left in .githooks/ ran")
        self.assertIn("feat/seven", git("ls-remote", "--heads", "origin", cwd=self.repo.work))

    def test_an_agents_own_git_keeps_the_repositorys_hooks(self):
        self.plant_hooks()
        env = sandbox.clean_env(os.environ | self.pinned, self.tmp / "no-gh", "claude")
        self.assertEqual((env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"]), ("1", "credential.helper"))
        subprocess.run(["git", "add", "-A"], cwd=self.wt, env=env, check=True)
        subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@example.com", "commit", "-qm", "inside"],
                       cwd=self.wt, env=env, check=True)
        self.assertIn("pre-commit", self.ran.read_text(), "inside the sandbox the hooks are the repository's business")

    def test_a_file_system_monitor_an_agent_configured_is_not_started(self):
        monitor = self.tmp / "monitor.sh"
        monitor.write_text(f"#!/bin/sh\necho monitor >> {self.ran}\nexit 1\n")
        monitor.chmod(0o755)
        with self.ws.agent_call(self.wt):   # the agent's private git directory: its config is the agent's to write
            git("config", "core.fsmonitor", str(monitor), cwd=self.wt)
            git("update-index", "--fsmonitor", cwd=self.wt)
            subprocess.run(["git", "status", "--short"], cwd=self.wt, capture_output=True, check=False)
            if not self.ran.exists():
                self.skipTest("this git does not start a monitor hook from core.fsmonitor")
            self.ran.unlink()
            with mock.patch.dict(os.environ, self.pinned):
                self.ws.index_entries(self.wt)   # what the engine reads when the call ends
                self.ws.uncommitted(self.wt)
            self.assertFalse(self.ran.exists(), "the engine's git did not start it")

    def test_the_engines_git_starts_no_background_maintenance(self):
        # The repository asks for it, as a clone does by default (the test repositories turn it
        # off, `helpers.NO_MAINTENANCE`). The engine's fetch, commit and merge start none: from git
        # 2.56 it prunes a worktree that a `git worktree add` right after a fetch is still creating.
        git("config", "maintenance.auto", "true", cwd=self.repo.work)
        seed = self.repo.tmp / "seed"
        (seed / "other.txt").write_text("y\n")
        git("add", "other.txt", cwd=seed)
        git("commit", "-q", "-m", "main moves", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        (self.wt / "change.txt").write_text("x\n")
        with mock.patch.dict(os.environ, self.pinned), traced(self.repo) as started:
            self.assertTrue(self.ws.commit_all(self.wt, "Build #7", "implementation", 7))
            self.assertEqual(self.ws.update(self.wt).status, "merged")   # a fetch, then a merge
            self.ws.issue_worktree(8, "feat/eight")                       # a fetch, then a worktree
        self.assertEqual(started, [])

        # Without the engine's setting a commit starts maintenance, and the trace shows it. The
        # control says so itself, whatever the launching shell's git configuration, and has git
        # wait for that maintenance rather than leave it running after the test.
        asked = {"GIT_CONFIG_COUNT": "3",
                 "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "true",
                 "GIT_CONFIG_KEY_1": "maintenance.autoDetach", "GIT_CONFIG_VALUE_1": "false",
                 "GIT_CONFIG_KEY_2": "gc.autoDetach", "GIT_CONFIG_VALUE_2": "false"}
        (self.wt / "more.txt").write_text("z\n")
        with mock.patch.dict(os.environ, asked), traced(self.repo) as started:
            self.assertTrue(self.ws.commit_all(self.wt, "Build #7", "implementation", 7))
        self.assertTrue(started)


# ---------------------------------------------------------------- the real thing

@needs_bubblewrap
class RealSandboxTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.home = self.fake_home()
        self.worktree = self.tmp / "worktree"
        self.worktree.mkdir()
        self.engine = self.tmp / "engine"
        self.engine.mkdir()
        (self.engine / "plugin.py").write_text("x = 1\n")
        self.box = sandbox.Bubblewrap(sandbox.Policy(), home=self.home)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "AWS_ACCESS_KEY_ID": "canary",
                                               "GH_TOKEN": "canary", "AWS_PROFILE": "canary"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The gh login is looked for where the launching environment says (secret_paths): a host
        # that names its own configuration directory, as GitHub's runners do with XDG_CONFIG_HOME,
        # would point the canary away from the fake home. patch.dict restores both on stop.
        for var in ("XDG_CONFIG_HOME", "GH_CONFIG_DIR"):
            os.environ.pop(var, None)

    def run_in(self, script: str, vendor=None, mounts=None) -> subprocess.CompletedProcess:
        mounts = mounts or sandbox.Mounts(readable=(self.engine,), writable=(self.worktree,))
        argv = self.box.command(["bash", "-c", script], self.worktree, vendor=vendor, mounts=mounts)
        return subprocess.run(argv, env=sandbox.clean_env(os.environ, self.tmp / "no-gh", vendor), cwd=self.worktree,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)

    def test_the_home_directory_is_empty_but_for_the_tools(self):
        proc = self.run_in('cd "$HOME" && find . -mindepth 1 -maxdepth 3 | sort')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["./.codex", "./.codex/packages", "./.codex/packages/bin",
                                               "./.local", "./.local/bin", "./.local/bin/tool"])

    def test_no_credential_can_be_listed_or_read(self):
        proc = self.run_in('for p in .aws .aws/credentials .ssh .config/gh .config .claude .codex/auth.json; do '
                           'ls "$HOME/$p" >/dev/null 2>&1 && echo "visible: $p"; done; env | grep -E "^(AWS|GH|GITHUB)_" | sort')
        # What the host itself sets under GH_ or GITHUB_ and is no credential (a CI runner's
        # GITHUB_ACTION, GITHUB_REPOSITORY, ...) passes the scrub by design; nothing else may.
        host = {f"{k}={v}" for k, v in os.environ.items()
                if k.startswith(("GH_", "GITHUB_")) and k != "GH_CONFIG_DIR" and not sandbox.dropped(k, None, v)}
        self.assertEqual(set(proc.stdout.splitlines()), {"AWS_CONFIG_FILE=/dev/null", "AWS_EC2_METADATA_DISABLED=true",
                                                         "AWS_SHARED_CREDENTIALS_FILE=/dev/null",
                                                         f"GH_CONFIG_DIR={self.tmp / 'no-gh'}"} | host)
        self.assertNotIn("visible:", proc.stdout)
        self.assertFalse([line for line in proc.stdout.splitlines()
                          if line.split("=", 1)[0] in ("GH_TOKEN", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_PROFILE")])

    def test_only_the_worktree_is_writable(self):
        proc = self.run_in(f'echo kept > made-inside; echo x > "{self.engine}/plugin.py" 2>/dev/null || echo engine-refused; '
                           f'echo x > /usr/canary 2>/dev/null || echo usr-refused; echo x > "$HOME/left-in-home"; '
                           f'echo x > /tmp/left-in-tmp; echo x > "{self.home}/.local/bin/tool" 2>/dev/null || echo tool-refused')
        self.assertEqual(proc.stdout.split(), ["engine-refused", "usr-refused", "tool-refused"], proc.stderr)
        self.assertEqual((self.worktree / "made-inside").read_text(), "kept\n")
        self.assertEqual((self.engine / "plugin.py").read_text(), "x = 1\n")
        self.assertFalse((self.home / "left-in-home").exists(), "the home directory inside is not the real one")
        self.assertEqual((self.home / ".local/bin/tool").read_text(), "x")

    def test_the_sandbox_has_its_own_tmp_and_its_own_processes(self):
        outside = self.tmp / "outside-file"
        outside.write_text("x")
        proc = self.run_in(f'ls "{outside}" 2>/dev/null || echo hidden; ls /proc | grep -c "^[0-9]"; echo /run:; ls /run')
        hidden, pids, marker, *run_entries = proc.stdout.split()
        self.assertEqual((hidden, marker), ("hidden", "/run:"))
        # /run is emptied, except for the path to resolv.conf when the host keeps it there (Ubuntu's
        # systemd-resolved links /etc/resolv.conf into /run/systemd/resolve): the sandbox binds that
        # one file back so that names resolve.
        resolv = Path("/etc/resolv.conf").resolve()
        expected = [resolv.relative_to("/run").parts[0]] if Path("/run") in resolv.parents else []
        self.assertEqual(run_entries, expected)
        self.assertLess(int(pids), 6, "only the sandbox's own processes are visible")

    def test_a_vendor_call_sees_its_login_and_nothing_else_of_its_directory(self):
        script = 'cd "$HOME" && ls -A .claude .codex 2>&1 | tr "\\n" " "'
        self.assertEqual(self.run_in(script, "claude").stdout.split(), [".claude:", ".credentials.json", ".codex:", "packages"])
        self.assertEqual(self.run_in(script, "codex").stdout.split()[-3:], ["auth.json", "config.toml", "packages"])
        self.assertIn("No such file", self.run_in(script).stdout, "a check command has neither")
        proc = self.run_in('echo refreshed > "$HOME/.claude/.credentials.json"; '
                           'echo x > "$HOME/.claude/settings.json"; echo done', "claude")
        self.assertEqual(proc.stdout.strip(), "done", proc.stderr)
        self.assertEqual((self.home / ".claude/.credentials.json").read_text(), "refreshed\n", "a refreshed login is kept")
        self.assertFalse((self.home / ".claude/settings.json").exists(), "a setting written inside stays inside")
        proc = self.run_in('echo x >> "$HOME/.codex/config.toml" || echo refused', "codex")
        self.assertEqual((proc.stdout.strip(), (self.home / ".codex/config.toml").read_text()), ("refused", ""))

    def test_a_redirected_home_does_not_expose_the_accounts_own(self):
        # an account whose home is under none of the directories this policy hides
        policy = sandbox.Policy(hide=tuple(h for h in sandbox.DEFAULT_HIDE if h not in ("/home", "/root", "/var/tmp")))
        real = Path(tempfile.mkdtemp(prefix="loop-account-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, real, ignore_errors=True)
        (real / ".aws").mkdir()
        (real / ".aws" / "credentials").write_text("[default]\n")
        self.box = sandbox.Bubblewrap(policy, home=self.home)
        look = f'ls "{real}/.aws" 2>&1 | head -1'
        self.assertEqual(self.run_in(look).stdout.strip(), "credentials", "hiding `$HOME` alone leaves it readable")
        with mock.patch.object(sandbox, "account_homes", return_value=[real]):
            self.assertIn("No such file or directory", self.run_in(look).stdout)
            report = sandbox.canary(self.tmp / "scratch", environ=os.environ, policy=policy, read_only=[self.engine])
        self.assertEqual(report.problems, [])
        self.assertIn("~/.aws of the account's own home", sandbox.describe(report))

    def test_the_canary_passes_and_says_what_it_proved(self):
        report = sandbox.canary(self.tmp / "scratch", self.tmp, home=self.home, environ=os.environ,
                                policy=sandbox.Policy(), read_only=[self.engine, self.home])
        self.assertEqual(report.problems, [])
        self.assertEqual(sorted(report.raw), ["check", "claude", "codex"])
        line = sandbox.describe(report)
        self.assertIn("hidden: ~/.aws, the gh login, ~/.ssh", line)
        self.assertIn("no credential variable", line)
        check = report.raw["check"]
        self.assertEqual((check["visible"], check["env"], check["after_umount"]), ({}, [], {}))
        self.assertTrue(all(result != "done" for result in check["direct_umount"].values()), check["direct_umount"])
        self.assertIn(str(self.home), check["direct_umount"])
        nested = check["nested"]
        if "attempts" in nested:   # a nested user namespace is possible here: every attempt in it failed
            self.assertEqual([a for a, result in nested["attempts"].items() if result == "done"], [])
            self.assertEqual(nested["visible"], {})
        self.assertEqual(list((self.tmp / "scratch").iterdir()), [], "its stand-in worktrees are removed")
        self.assertFalse(list(self.engine.glob(".implement-loop-canary-*")) + list(self.home.glob(".implement-loop-canary-*")))

    def test_the_canary_fails_on_an_allow_list_that_brings_credentials_back(self):
        loose = sandbox.Policy(toolchain=("~/.local/bin", "~/.config", "~/.aws"),
                               login={"claude": ("~/.claude/.credentials.json", "~/.codex/auth.json"), "codex": ()})
        report = sandbox.canary(self.tmp / "scratch", home=self.home, environ=os.environ, policy=loose,
                                read_only=[self.engine])
        text = "\n".join(report.problems)
        self.assertIn("check: ~/.aws is readable in the sandbox", text)
        self.assertIn("check: the gh login is readable in the sandbox", text)
        self.assertIn("claude: another vendor's login", text)
        self.assertNotIn("~/.ssh", text, "what stayed hidden is not reported")
        self.assertFalse(report.ok)

    def test_a_timeout_kills_everything_the_command_started(self):
        sandbox.use(self.box)
        tag = f"300.{uuid.uuid4().int % 100000}"
        started = time.monotonic()
        code, output, timed_out = verify._run(f"(setsid sleep {tag} &) ; sleep {tag} & sleep {tag}", self.worktree, 2,
                                              sandbox.clean_env(os.environ, self.tmp / "no-gh"),
                                              sandbox.Mounts(writable=(self.worktree,)))
        self.assertEqual((code, timed_out), (None, True), output)
        self.assertLess(time.monotonic() - started, 30)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            left = subprocess.run(["pgrep", "-f", f"sleep {tag}"], capture_output=True, text=True, check=False).stdout.split()
            if not left:
                break
            time.sleep(0.2)
        self.assertEqual(left, [], "even a process that left the group died with the sandbox")

    def test_stopping_the_run_kills_a_sandboxed_agent(self):
        sandbox.use(self.box)
        agents = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), sleep=lambda s: None)
        tag = f"300.{uuid.uuid4().int % 100000}"
        result: list = []
        thread = threading.Thread(target=lambda: result.append(self._spawn(agents, tag)))
        thread.start()
        deadline = time.monotonic() + 20
        while not (self.worktree / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        agents.terminate_all()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "the call returned")
        self.assertEqual(result, ["stopped"])
        time.sleep(0.5)
        left = subprocess.run(["pgrep", "-f", f"sleep {tag}"], capture_output=True, text=True, check=False).stdout.split()
        self.assertEqual(left, [])

    def _spawn(self, agents, tag: str) -> str:
        try:
            agents._spawn(["bash", "-c", f"touch started; sleep {tag}"], "", self.worktree, 120, None,
                          sandbox.Mounts(writable=(self.worktree,)))
        except live.AgentStopped:
            return "stopped"
        return "returned"

    def test_an_agent_reads_its_prompt_and_answers_through_the_sandbox(self):
        sandbox.use(self.box)
        agents = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), sleep=lambda s: None)
        code, out, err = agents._spawn(["bash", "-c", 'cat; ls "$HOME/.aws" 2>&1 | head -1; echo "[$AWS_ACCESS_KEY_ID]"'],
                                       "the prompt\n", self.worktree, 60, "claude", sandbox.Mounts(writable=(self.worktree,)))
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual((lines[0], lines[2]), ("the prompt", "[]"))
        self.assertIn("No such file or directory", lines[1])

    def test_a_check_and_its_pytest_records_work_inside(self):
        sandbox.use(self.box)
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        ws = Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir)
        with ws.disposable(ws.coord()) as scratch:
            mounts = sandbox.worktree_mounts(scratch, True, ws)
            spec = CheckSpec(criterion="D1", command='git status --short && git log -1 --format=%s && echo "$LOOP_REPORT_DIR" '
                                                     '&& touch "$LOOP_REPORT_DIR/x" && touch made-by-check '
                                                     f'&& python3 -c "import implement_loop" '
                                                     f'&& ! ls {repo.work}/README.md 2>/dev/null')
            ev, _ = verify.run_check(scratch, spec, ws.head(scratch), repo.tmp / "verifier", mounts=mounts)
            self.assertTrue(ev.passed, ev.output_tail)
            self.assertIn("seed", ev.output_tail, "git reads the shared objects through the private directory")
            self.assertTrue((scratch / "made-by-check").is_file())
            self.assertTrue((repo.work / "README.md").is_file(), "the main checkout exists, and was not visible inside")


@needs_bubblewrap
class RealCanaryOnThisHostTests(SandboxCase):
    def test_this_repositorys_sandbox_hides_this_hosts_credentials(self):
        report = sandbox.canary(self.tmp / "scratch", REPO_ROOT, read_only=[self.tmp])
        self.assertEqual(report.problems, [], "the doctor's sandbox line would FAIL on this host")


class CanaryWithoutASandboxTests(SandboxCase):
    def test_the_canary_reports_everything_a_missing_sandbox_exposes(self):
        home = self.fake_home()
        target = self.tmp / "engine"
        target.mkdir()
        environ = dict(os.environ) | {"HOME": str(home), "XDG_CONFIG_HOME": "", "GH_CONFIG_DIR": ""}
        report = sandbox.canary(self.tmp / "scratch", launcher=sandbox.PassThrough(), home=home, environ=environ,
                                policy=sandbox.Policy(), read_only=[target])
        text = "\n".join(report.problems)
        for expected in ("check: ~/.aws is readable in the sandbox", "check: the gh login is readable in the sandbox",
                         "check: ~/.ssh is readable in the sandbox", "claude: another vendor's login",
                         "codex: another vendor's login", f"a write from inside the sandbox reached {target}"):
            self.assertIn(expected, text)
        self.assertEqual(list(target.iterdir()), [], "the marker it managed to write is removed")
        self.assertIn("readable", sandbox.describe(report))

    def test_planted_variables_are_scrubbed_before_the_probe_runs(self):
        home = self.fake_home()
        seen = []

        def runner(argv, **kwargs):
            seen.append(kwargs["env"])
            return subprocess.CompletedProcess(argv, 1, "", "stopped here")
        report = sandbox.canary(self.tmp / "scratch", launcher=sandbox.PassThrough(), home=home, runner=runner,
                                environ={"HOME": str(home), "PATH": os.environ["PATH"]}, policy=sandbox.Policy(), read_only=[])
        self.assertEqual(len(seen), 3)
        for env in seen:
            self.assertFalse([k for k in env if k.startswith(("AWS_", "GH_TOKEN", "GITHUB_")) and k not in sandbox.NEUTRAL_AWS])
        self.assertIn("the probe did not run in the sandbox (check)", report.problems[0])

    def test_a_missing_bubblewrap_is_the_canarys_one_finding(self):
        with mock.patch.object(sandbox.shutil, "which", return_value=None):
            report = sandbox.canary(self.tmp / "scratch", home=self.fake_home(), environ={"PATH": ""}, policy=sandbox.Policy())
        self.assertEqual(len(report.problems), 1)
        self.assertIn("not installed", report.problems[0])

    def test_the_secret_paths_follow_the_launching_environment(self):
        home = Path("/somewhere")
        self.assertEqual(sandbox.secret_paths(home, {}),
                         {"~/.aws": "/somewhere/.aws", "the gh login": "/somewhere/.config/gh", "~/.ssh": "/somewhere/.ssh"})
        paths = sandbox.secret_paths(home, {"GH_CONFIG_DIR": "/elsewhere/gh", "AWS_SHARED_CREDENTIALS_FILE": "/etc/creds",
                                            "AWS_CONFIG_FILE": "/dev/null"})
        self.assertEqual((paths["the gh login"], paths["$AWS_SHARED_CREDENTIALS_FILE"]), ("/elsewhere/gh", "/etc/creds"))
        self.assertNotIn("$AWS_CONFIG_FILE", paths)
        self.assertEqual(sandbox.secret_paths(home, {"XDG_CONFIG_HOME": "/xdg"})["the gh login"], "/xdg/gh")


# ---------------------------------------------------------------- the doctor and the launch commands

class DoctorTests(SandboxCase):
    def test_the_sandbox_line_fails_without_bubblewrap(self):
        with mock.patch.object(sandbox, "version", return_value=None):
            ok, name, detail = cli.sandbox_check(self.tmp)
        self.assertEqual((ok, name), (False, "sandbox"))
        self.assertIn("bubblewrap is not installed", detail)

    def test_the_sandbox_line_fails_on_what_the_canary_saw(self):
        seen = sandbox.CanaryReport(problems=["check: ~/.aws is readable in the sandbox at /x/.aws (credentials)"])
        with mock.patch.object(sandbox, "version", return_value="bubblewrap 0.11.1"), \
             mock.patch.object(cli, "runs_dir", return_value=self.tmp), \
             mock.patch.object(sandbox, "canary", return_value=seen) as canary:
            ok, name, detail = cli.sandbox_check(self.tmp / "repo")
        self.assertEqual((ok, name), (False, "sandbox"))
        self.assertIn("~/.aws is readable", detail)
        self.assertEqual(canary.call_args.args, (self.tmp / "canary", self.tmp / "repo"))
        self.assertEqual(canary.call_args.kwargs, {}, "the real sandbox with the repository's policy: no launcher passed in")

    def test_the_sandbox_line_passes_with_the_canarys_facts(self):
        clean = sandbox.CanaryReport(facts=["hidden: ~/.aws", "no credential variable"])
        with mock.patch.object(sandbox, "version", return_value="bubblewrap 0.11.1"), \
             mock.patch.object(cli, "runs_dir", return_value=self.tmp), mock.patch.object(sandbox, "canary", return_value=clean):
            self.assertEqual(cli.sandbox_check(self.tmp), (True, "sandbox", "bubblewrap 0.11.1; hidden: ~/.aws; no credential variable"))

    def test_the_canary_alone_prints_everything_it_saw_and_its_verdict_is_the_exit_status(self):
        seen = {"check": {"visible": {}, "env": [], "direct_umount": {str(Path.home()): "EPERM"}}}
        for report, status in ((sandbox.CanaryReport(facts=["no credential variable"], raw=seen), 0),
                               (sandbox.CanaryReport(problems=["check: the variable AWS_PROFILE reaches the sandbox"], raw=seen), 1)):
            out = io.StringIO()
            with mock.patch.object(cli, "repo_root", return_value=self.tmp), mock.patch.object(cli, "runs_dir", return_value=self.tmp), \
                 mock.patch.object(sandbox, "canary", return_value=report) as canary, \
                 mock.patch.object(cli, "doctor_checks", side_effect=AssertionError("the other checks ran")), \
                 redirect_stdout(out):
                code = cli.cmd_doctor(argparse.Namespace(canary=True, smoke=False, repo=None))
            text = out.getvalue()
            self.assertEqual(code, status)
            self.assertEqual(canary.call_args.args, (self.tmp / "canary", self.tmp), "the real sandbox, this repository's policy")
            self.assertIn("sandbox canary: PASS" if status == 0 else "sandbox canary: FAIL", text)
            self.assertIn('"direct_umount"', text)
            self.assertIn('"~": "EPERM"', text, "the home directory is written `~`")
            self.assertNotIn(str(Path.home()), text)
            if status:
                self.assertIn("problem: check: the variable AWS_PROFILE reaches the sandbox", text)

    def doctor(self, **sandbox_line) -> dict[str, tuple[str, str]]:
        line = sandbox_line.get("line", (True, "sandbox", "bubblewrap 0.11.1; hidden"))
        with mock.patch.object(cli, "_out", lambda cmd: (0, sandbox_line.get("node", "v24.1.0") if cmd[0] == "node" else "ok 1.0")), \
             mock.patch.object(cli.environment, "probe", return_value=[]), \
             mock.patch.object(cli, "sandbox_check", return_value=line), \
             mock.patch.object(cli.shutil, "which", lambda name: sandbox_line.get("which", f"/usr/bin/{name}")):
            return {name: (status, detail) for status, name, detail in cli.doctor_checks(self.tmp, None)}

    def test_the_doctor_prints_a_sandbox_line_and_it_can_fail(self):
        self.assertEqual(self.doctor()["sandbox"], ("PASS", "bubblewrap 0.11.1; hidden"))
        self.assertEqual(self.doctor(line=(False, "sandbox", "~/.aws is readable"))["sandbox"], ("FAIL", "~/.aws is readable"))

    def test_node_outside_the_cdk_majors_is_a_warning_never_a_failure(self):
        self.assertEqual(self.doctor(node="v24.1.0")["node"], ("PASS", "v24.1.0"))
        self.assertEqual(self.doctor(node="v22.11.0")["node"][0], "PASS")
        for version in ("v26.4.0", "v20.18.0", "nonsense"):
            status, detail = self.doctor(node=version)["node"]
            self.assertEqual(status, "WARN", version)
            self.assertIn("22 or 24", detail)
        status, detail = self.doctor(which=None)["node"]
        self.assertEqual(status, "WARN")
        self.assertIn("not found on PATH", detail)

    def test_the_run_environment_keeps_private_caches_and_notices_a_lost_sandbox(self):
        with mock.patch.object(cli.environment, "heal", return_value=[]), mock.patch.object(cli.environment, "probe", return_value=[]):
            with mock.patch.object(sandbox, "version", return_value="bubblewrap 0.11.1"):
                env = cli.prepare_environment(self.tmp, log=lambda m: None, cache_dir=self.tmp / "loop" / "cache")
            with mock.patch.object(sandbox, "version", return_value=None):
                lost = cli.prepare_environment(self.tmp, log=lambda m: None, cache_dir=self.tmp / "loop" / "cache")
        cache = self.tmp / "loop" / "cache"
        self.assertEqual(env["check_env"], {"UV_CACHE_DIR": str(cache / "uv"), "npm_config_cache": str(cache / "npm"),
                                            "UV_LINK_MODE": "copy"})
        self.assertTrue((cache / "uv").is_dir() and (cache / "npm").is_dir())
        self.assertEqual(sandbox.cache_dirs(env["check_env"]), [cache / "uv", cache / "npm"])
        self.assertNotEqual(env["fingerprint"], lost["fingerprint"], "an issue waiting for the sandbox resumes when it is back")

    def test_a_cache_named_by_the_launching_shell_is_never_bound(self):
        with mock.patch.dict(os.environ, {"UV_CACHE_DIR": str(self.tmp / "owner-cache")}):
            self.assertEqual(sandbox.cache_dirs({}), [])
            self.assertEqual(sandbox.cache_dirs(None), [])
        self.assertFalse((self.tmp / "owner-cache").exists())


class SuperviseRefusalTests(SandboxCase):
    """`supervise` never ran the doctor: on a repository that is not ready it launched a run that
    refused, waited, and launched it again until its bounds. It refuses up front now."""

    def args(self, **changes) -> argparse.Namespace:
        values = {"issue": 7, "operator": "claude", "yes": True, "detach": False, "max_runs": 10, "max_hours": None,
                  "repo": "o/r"}
        return argparse.Namespace(**(values | changes))

    def supervise(self, checks, **changes):
        err, out = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.object(cli, "repo_root", return_value=self.tmp), \
             mock.patch.object(cli, "doctor_checks", return_value=checks) as doctor, \
             mock.patch.object(cli.subprocess, "Popen") as popen, \
             mock.patch("implement_loop.supervisor.Supervisor") as supervisor, \
             mock.patch.object(cli.RunStore, "for_root", return_value=SimpleNamespace(dir=self.tmp)), \
             redirect_stderr(err), redirect_stdout(out):
            supervisor.return_value.supervise.return_value = Outcome(finished=True, reason="done", runs=1)
            popen.return_value.pid = 4242
            try:
                code = cli.cmd_supervise(self.args(**changes))
            except SystemExit as e:
                code = e
        return code, doctor, popen, supervisor, err.getvalue()

    def test_supervise_refuses_while_the_doctor_fails(self):
        failing = [("PASS", "git", "git version 2"), ("WARN", "node", "v26"),
                   ("FAIL", "base branch", "origin has no `main` branch: nothing is pushed yet")]
        for detach in (False, True):
            with self.subTest(detach=detach):
                code, doctor, popen, supervisor, err = self.supervise(failing, detach=detach)
                self.assertIsInstance(code, SystemExit)
                self.assertIn("fix the failed checks", str(code))
                self.assertIn("FAIL base branch: origin has no `main` branch", err)
                self.assertNotIn("node", err, "a warning does not stop it")
                doctor.assert_called_once_with(self.tmp, "o/r")
                popen.assert_not_called()
                supervisor.assert_not_called()

    def test_supervise_refuses_on_a_failing_sandbox(self):
        code, _, popen, supervisor, err = self.supervise([("FAIL", "sandbox", "bubblewrap is not installed")])
        self.assertIsInstance(code, SystemExit)
        self.assertIn("FAIL sandbox", err)
        popen.assert_not_called()
        supervisor.assert_not_called()

    def test_supervise_starts_once_the_doctor_passes(self):
        passing = [("PASS", "git", "git version 2"), ("WARN", "node", "v26")]
        code, _, popen, supervisor, _ = self.supervise(passing)
        self.assertEqual(code, 0)
        supervisor.return_value.supervise.assert_called_once_with()
        code, _, popen, supervisor, _ = self.supervise(passing, detach=True)
        self.assertEqual(code, 0)
        popen.assert_called_once()
        supervisor.assert_not_called()

    def test_a_supervision_has_a_deadline_only_when_one_is_given(self):
        passing = [("PASS", "git", "git version 2")]
        for hours in (None, 6.0):
            with self.subTest(hours=hours):
                _, _, _, supervisor, _ = self.supervise(passing, max_hours=hours)
                self.assertEqual(supervisor.call_args.kwargs["max_hours"], hours)
                _, _, popen, _, _ = self.supervise(passing, detach=True, max_hours=hours)
                cmd = popen.call_args.args[0]
                self.assertEqual(cmd[cmd.index("--max-hours") + 1] if "--max-hours" in cmd else None,
                                 None if hours is None else "6.0", "the detached supervisor gets the same bound")

    def test_run_and_supervise_refuse_alike_on_a_repository_nothing_was_pushed_to(self):
        root = self.tmp / "work"
        git("init", "-q", "-b", "main", str(root), cwd=self.tmp)
        identify(root)
        git("init", "-q", "--bare", "-b", "main", str(self.tmp / "remote.git"), cwd=self.tmp)
        git("remote", "add", "origin", str(self.tmp / "remote.git"), cwd=root)
        real = cli._out
        for command in (cli.cmd_supervise, cli.cmd_run):
            err = io.StringIO()
            with self.subTest(command=command.__name__), \
                 mock.patch.object(cli, "repo_root", return_value=root), \
                 mock.patch.object(cli, "_out", lambda cmd: real(cmd) if cmd[0] == "git" else (0, "stub 1.0")), \
                 mock.patch.object(cli.environment, "probe", return_value=[]), \
                 mock.patch.object(cli, "sandbox_check", return_value=(True, "sandbox", "ok")), \
                 mock.patch.object(cli, "build_plan", side_effect=AssertionError("it read the issues")), \
                 mock.patch("implement_loop.supervisor.Supervisor", side_effect=AssertionError("it supervised")), \
                 redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
                command(self.args(fresh=False, repair=False))
            self.assertIn("fix the failed checks", str(stopped.exception))
            self.assertIn("FAIL base branch: origin has no `main` branch: nothing is pushed yet", err.getvalue())


if __name__ == "__main__":
    unittest.main()
