"""The sandbox every agent call and every check command runs in (ADR 0001, decision 3).

Agents, and the commands they write, are the only code in a run that the engine does not
control. Each of them is started through bubblewrap (`bwrap`), in a mount namespace built as an
allow-list:

- the root filesystem is read-only, and the places that hold other sessions' and other people's
  things are replaced by empty directories (`hide` in `loop.toml [sandbox]`: every home
  directory, `/run` with its sockets, `/mnt` and `/media` with the other filesystems);
- `/tmp` and the home directory are empty and private to the call;
- bound back in, read-only: the tools (`toolchain`), and what the caller names (the engine's
  directory for the verifier's plugin, the object store a worktree reads);
- bound back in, read-write: what the caller names (the issue worktree, the call's private git
  directory, the run's private package caches, the file an answer is written to);
- for an agent call, the running vendor's login file read-write and its settings read-only
  (`login` and `settings`). The other vendor's are absent, and a check command gets neither.

`~/.aws`, the `gh` login, `~/.ssh` and the rest of the home directory therefore do not exist in
there. The processes are in their own PID namespace, which dies with the engine's child process,
and in a session of their own, without the launching terminal. The network is shared.

`clean_env` is the one environment scrub, for agents and checks alike. `canary` proves the whole
on the host it runs on: it starts a probe inside the sandbox and reports whatever the probe could
see, change or bring back. The doctor fails on any of it.

What a caller may bind is decided by the engine, never read from a file an agent can write: see
`worktree_mounts`.

Tests drive fake agents and checks in temporary directories, without bubblewrap:
`tests/helpers.py` installs `PassThrough` with `use`. Nothing else may.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

ENGINE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = ENGINE_DIR / "loop.toml"
VENDORS = ("claude", "codex")

# The defaults are what loop.toml states: an absent file or table still gives the tight sandbox.
DEFAULT_TOOLCHAIN = (
    "~/.local/bin",                   # the claude, codex and uv launchers
    "~/.local/share/claude",          # the Claude Code binaries those launchers point to
    "~/.codex/packages",              # the Codex binary: it lives in Codex's own directory
    "~/.local/share/uv/python",       # the Pythons uv manages
    "~/.local/share/mise/installs",   # Node, installed by mise
)
DEFAULT_HIDE = ("/home", "/root", "/run", "/mnt", "/media", "/var/tmp")
DEFAULT_LOGIN = {"claude": ("~/.claude/.credentials.json",), "codex": ("~/.codex/auth.json",)}
DEFAULT_SETTINGS = {"claude": (), "codex": ("~/.codex/config.toml",)}

# ---------------------------------------------------------------- the environment

GITHUB_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK")
CLOUD_PREFIXES = ("AWS_", "AMAZON_", "CDK_DEFAULT_", "GOOGLE_", "GCLOUD_", "AZURE_")
# A vendor's own key variables reach that vendor's own call, and nothing else.
VENDOR_KEYS = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
               "codex": ("OPENAI_API_KEY", "CODEX_API_KEY")}
# Handles to the session that launched the engine: agents, helpers and sockets that live outside
# the sandbox (the directories they point into are hidden too).
SESSION_VARS = ("SSH_AGENT_PID", "GPG_AGENT_INFO", "GNOME_KEYRING_CONTROL", "DBUS_SESSION_BUS_ADDRESS",
                "XDG_RUNTIME_DIR", "XAUTHORITY", "DISPLAY", "WAYLAND_DISPLAY", "PULSE_SERVER", "WSL_INTEROP", "WSLENV",
                "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_SSE_PORT")
SESSION_PREFIXES = ("VSCODE_",)
# Pointers to credentials kept elsewhere: a file a tool would read, a daemon it would talk to.
POINTER_VARS = ("KUBECONFIG", "DOCKER_CONFIG", "DOCKER_HOST", "NETRC", "BOTO_CONFIG", "PGPASSFILE")
# Any other variable named like a secret is dropped as well: a token, a password or a key, and
# what stands in for one (the session key of a password manager, a cookie, a nonce).
SECRET_NAME = re.compile(r"(?:^|_)[A-Z0-9]*(?:TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL"
                         r"|CREDENTIALS|ASKPASS|SESSION|COOKIE|COOKIES|NONCE)(?:_|$)"
                         r"|(?:^|_)(?:API|ACCESS|PRIVATE|SECRET|AUTH)_?KEY(?:_|$)|_KEY$")
# And so is a variable whose value has the shape of a credential, whatever its name: an AWS access
# key ID, a GitHub, Anthropic or OpenAI token, a private key, a URL that carries a password
# (`DATABASE_URL`, a proxy, a package index). The scrub goes by names and shapes: a secret that
# has neither passes, so the engine is launched from a shell that holds none.
SECRET_VALUE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
                          r"|\bsk-ant-[A-Za-z0-9_-]{20,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"
                          r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
# The SDKs' other sources of credentials are switched off: the instance metadata service and the
# profile files (which do not exist in the sandbox anyway).
NEUTRAL_AWS = {"AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": "/dev/null",
               "AWS_SHARED_CREDENTIALS_FILE": "/dev/null"}
# Package caches the engine keeps for a run, named in the checks' environment (cli.py).
CACHE_VARS = ("UV_CACHE_DIR", "npm_config_cache")


def dropped(name: str, vendor: str | None = None, value: str = "") -> bool:
    """Whether `clean_env` removes the variable `name` (holding `value`, when given) from the
    environment of a call of `vendor` (None: a check command). Names are compared without regard
    to case; a vendor's own key variables, spelled as its CLI reads them, are the one exception."""
    if vendor is not None and name in VENDOR_KEYS.get(vendor, ()):
        return False
    upper = name.upper()
    return (upper in GITHUB_VARS or upper in SESSION_VARS or upper in POINTER_VARS
            or upper.startswith(CLOUD_PREFIXES + SESSION_PREFIXES)
            or any(upper in keys for keys in VENDOR_KEYS.values()) or bool(SECRET_NAME.search(upper))
            or bool(value and SECRET_VALUE.search(value)))


def clean_env(env: Mapping[str, str], no_gh_dir: Path, vendor: str | None = None) -> dict[str, str]:
    """The environment of an agent call (`vendor` is "claude" or "codex") or of a check command
    (None), from `env`: no cloud variable (`AWS_*`, `AMAZON_*`, `CDK_DEFAULT_*`, `GOOGLE_*`,
    `GCLOUD_*`, `AZURE_*`), no GitHub credential, no handle to the launching session, no variable
    named like a secret or holding a value shaped like one, and a vendor's API key only for that
    vendor's own call. Then what makes the tools refuse to look further: no AWS metadata service or
    profile file, a `gh` configuration directory with no login in it, no git prompt and no git
    credential helper.

    It is a list of what is removed, by name and by shape, not of what is kept: the tools read too
    many settings from their environment for that. What it cannot recognise passes."""
    out = {k: v for k, v in env.items() if not dropped(k, vendor, v)}
    out.update(NEUTRAL_AWS)
    no_gh_dir.mkdir(parents=True, exist_ok=True)
    out["GH_CONFIG_DIR"] = str(no_gh_dir)          # gh finds no stored login
    out["GIT_TERMINAL_PROMPT"] = "0"
    out["GIT_CONFIG_COUNT"] = "1"                 # an empty credential.helper clears every helper
    out["GIT_CONFIG_KEY_0"] = "credential.helper"
    out["GIT_CONFIG_VALUE_0"] = ""
    return out


def cache_dirs(env: Mapping[str, str] | None) -> list[Path]:
    """The private package caches named in `env` (the engine's own check environment, never the
    launching shell's), created if missing: a call may write to them."""
    named = env or {}
    dirs = [Path(named[name]) for name in CACHE_VARS if named.get(name)]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs


# What the engine's own git commands run with, whatever a repository's or a worktree's configuration
# says: no file-system monitor (a command git starts when it reads an index), no `ext::` transport.
ENGINE_GIT_CONFIG = (("core.fsmonitor", "false"), ("protocol.ext.allow", "never"))


def engine_git_env(env: Mapping[str, str], no_hooks_dir: Path) -> dict[str, str]:
    """What an engine run adds to its own environment before it starts any git process (its own
    commands and `ship.sh`): git then takes its hooks from `no_hooks_dir`, an empty directory the
    engine owns, starts no file-system monitor and fetches nothing lazily.

    The engine runs outside the sandbox, in worktrees and git directories an agent wrote to. With
    `core.hooksPath` set to `.githooks` in a clone, git would run the hooks of the worktree a
    command runs in: the builder's copy, as the owner, on the engine's next commit. Hooks are for
    people and interactive agents; the engine's gates are its own (the verifier's standing checks,
    `ship.sh` refusing `main`, the ruleset). Configuration given this way outranks every file, so
    an agent cannot switch it back. An agent's and a check's git get `clean_env` instead, and keep
    the repository's hooks, inside the sandbox."""
    no_hooks_dir.mkdir(parents=True, exist_ok=True)
    settings = (("core.hooksPath", str(no_hooks_dir)), *ENGINE_GIT_CONFIG)
    try:
        count = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        count = 0
    out = {"GIT_NO_LAZY_FETCH": "1", "GIT_CONFIG_COUNT": str(count + len(settings))}
    for i, (key, value) in enumerate(settings, start=count):
        out[f"GIT_CONFIG_KEY_{i}"] = key
        out[f"GIT_CONFIG_VALUE_{i}"] = value
    return out


# ---------------------------------------------------------------- what a call may see

class SandboxUnavailable(RuntimeError):
    """The sandbox cannot be built on this host. Nothing an agent controls runs without it."""


@dataclass(frozen=True)
class Mounts:
    """Paths bound into the sandbox as they are outside: `readable` read-only, `writable`
    read-write. Everything else under the hidden directories does not exist in there."""
    readable: tuple[Path, ...] = ()
    writable: tuple[Path, ...] = ()

    def plus(self, readable: Iterable[Path] = (), writable: Iterable[Path] = ()) -> Mounts:
        return Mounts(self.readable + tuple(readable), self.writable + tuple(writable))


def _gitfile_target(worktree: Path) -> Path | None:
    entry = worktree / ".git"
    try:
        if not entry.is_file():
            return None
        text = entry.read_text(errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir: "):
        return None
    target = Path(text.removeprefix("gitdir: "))
    return (target if target.is_absolute() else worktree / target).resolve()


def worktree_mounts(worktree: Path, write: bool, workspace=None) -> Mounts:
    """What a process working in `worktree` gets of it and of git: the worktree itself
    (read-write when `write`), the private git directory of the call in flight with the same
    access, and read-only the shared object store that directory borrows from. A worktree that is
    not on a private git directory gets the repository's git directory read-only: git can read
    there and write nothing.

    Which directories those are is the engine's own knowledge (`workspace.dir`,
    `workspace.common_dir()`), never the worktree's word: its `.git` file and the private
    directory's `alternates` are writable by the agent, so the first is believed only when it
    names a directory directly under this run's `agent-git/`, and the second is not read at all.
    Without a workspace (the doctor's smoke call in the main checkout) only the worktree is bound."""
    here = [Path(worktree)]
    if workspace is None:
        return Mounts(writable=tuple(here)) if write else Mounts(readable=tuple(here))
    try:
        common = Path(workspace.common_dir())
        private_root = (Path(workspace.dir) / "agent-git").resolve()
    except (AttributeError, OSError, RuntimeError):   # not a Workspace: nothing of git is known
        return Mounts(writable=tuple(here)) if write else Mounts(readable=tuple(here))
    target = _gitfile_target(Path(worktree))
    private = target if target is not None and target.parent == private_root and target.is_dir() else None
    if private is not None:
        here.append(private)
    shared = [common / "objects" if private is not None else common]
    return Mounts(readable=tuple(shared), writable=tuple(here)) if write else Mounts(readable=tuple(here + shared))


@dataclass(frozen=True)
class Policy:
    """`loop.toml [sandbox]`. `~` is the home directory; a path that does not exist is skipped."""
    toolchain: tuple[str, ...] = DEFAULT_TOOLCHAIN
    hide: tuple[str, ...] = DEFAULT_HIDE
    login: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_LOGIN))
    settings: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))


def _paths(value, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise SandboxUnavailable(f"loop.toml [sandbox] {what} must be a list of paths")
    return tuple(value)


def load_policy(path: Path = CONFIG_FILE) -> Policy:
    """The sandbox's allow-list. A table that is malformed is refused, never guessed around."""
    try:
        data = tomllib.loads(Path(path).read_text()).get("sandbox", {}) if Path(path).exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SandboxUnavailable(f"cannot read {path}: {e}") from None
    if not isinstance(data, dict):
        raise SandboxUnavailable("loop.toml [sandbox] must be a table")
    unknown = sorted(set(data) - {"toolchain", "hide", "login", "settings"})
    if unknown:
        raise SandboxUnavailable(f"loop.toml [sandbox] has unknown keys: {', '.join(unknown)}")
    policy = Policy()
    per_vendor = {}
    for key in ("login", "settings"):
        table = data.get(key, {})
        if not isinstance(table, dict) or set(table) - set(VENDORS):
            raise SandboxUnavailable(f"loop.toml [sandbox.{key}] takes one list of paths per vendor ({', '.join(VENDORS)})")
        per_vendor[key] = dict(getattr(policy, key)) | {v: _paths(p, f"{key}.{v}") for v, p in table.items()}
    return Policy(toolchain=_paths(data["toolchain"], "toolchain") if "toolchain" in data else policy.toolchain,
                  hide=_paths(data["hide"], "hide") if "hide" in data else policy.hide,
                  login=per_vendor["login"], settings=per_vendor["settings"])


class Launcher(Protocol):
    def command(self, argv: Sequence[str], cwd: Path, vendor: str | None = None,
                mounts: Mounts | None = None) -> list[str]: ...


class Bubblewrap:
    """Builds the `bwrap` command line that runs a command in the sandbox."""

    def __init__(self, policy: Policy | None = None, bwrap: str = "bwrap", home: Path | None = None):
        self.policy = policy
        self.bwrap = bwrap
        self._home = home

    def home(self) -> Path:
        home = Path(self._home or os.environ.get("HOME") or "")
        if not home.is_absolute() or home == Path("/") or not home.is_dir():
            raise SandboxUnavailable(f"the home directory to hide is not usable: {str(home)!r}")
        return home

    def expand(self, path: str) -> Path:
        home = self.home()
        expanded = home if path == "~" else home / path[2:] if path.startswith("~/") else Path(path)
        if not expanded.is_absolute():
            raise SandboxUnavailable(f"loop.toml [sandbox]: {path!r} is not an absolute path")
        return expanded

    def executable(self) -> str:
        exe = shutil.which(self.bwrap)
        if exe is None:
            raise SandboxUnavailable("bubblewrap is not installed (`bwrap` is not on PATH): agents and checks do not "
                                     "run without the sandbox")
        return exe

    def hidden(self) -> list[Path]:
        """The directories replaced by empty ones, parents first: the policy's, the home directory
        the launching environment names, and the one the account really has when `$HOME` points
        somewhere else."""
        policy = self.policy or load_policy()
        found = {self.expand(h) for h in policy.hide} | {self.home(), *account_homes()}
        return sorted((h for h in found if h.is_dir() and h != Path("/")), key=lambda p: (len(p.parts), str(p)))

    def command(self, argv: Sequence[str], cwd: Path, vendor: str | None = None,
                mounts: Mounts | None = None) -> list[str]:
        """`argv`, to be run in `cwd`, inside the sandbox: for an agent call of `vendor`, or for a
        check command (None). `mounts` is what the call may see beyond the tools; `cwd` must be
        inside it. Raises `SandboxUnavailable` when the sandbox cannot be built."""
        if vendor is not None and vendor not in VENDORS:
            raise ValueError(f"unknown vendor {vendor!r}")
        policy = self.policy or load_policy()
        mounts = mounts or Mounts()
        args = [self.executable(), "--die-with-parent", "--new-session",
                "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
                "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        hidden = self.hidden()
        for h in hidden:
            args += ["--tmpfs", str(h)]
        binds: dict[Path, bool] = {}   # path -> writable; a path asked for both ways is writable

        def bind(path: Path, writable: bool, required: bool) -> None:
            if not path.is_absolute():
                raise SandboxUnavailable(f"cannot bind {path}: not an absolute path")
            if not path.exists():
                if required:
                    raise SandboxUnavailable(f"cannot bind {path}: it does not exist")
                return
            binds[path] = binds.get(path, False) or writable

        try:   # name resolution: resolv.conf is often a link into a directory hidden above
            resolv = Path("/etc/resolv.conf").resolve(strict=True)
        except OSError:
            resolv = None
        if resolv is not None and any(h in resolv.parents for h in hidden):
            bind(resolv, False, False)
        for path in policy.toolchain:
            bind(self.expand(path), False, False)
        if vendor is not None:
            for path in policy.settings.get(vendor, ()):
                bind(self.expand(path), False, False)
            for path in policy.login.get(vendor, ()):
                bind(self.expand(path), True, False)
        for path in mounts.readable:
            bind(Path(path), False, True)
        for path in mounts.writable:
            bind(Path(path), True, True)
        # Parents before children, so a narrower bind lands on top of a wider one.
        for path, writable in sorted(binds.items(), key=lambda item: (len(item[0].parts), str(item[0]))):
            args += ["--bind" if writable else "--ro-bind", str(path), str(path)]
        return [*args, "--chdir", str(cwd), "--", *[str(a) for a in argv]]


def account_homes() -> list[Path]:
    """The home directory the password database gives the user the engine runs as. `$HOME` is
    only what the launching shell says."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return []
    return [home] if home.is_absolute() else []


class PassThrough:
    """No isolation: the command as it is. For tests only, which drive fake agents and checks in
    temporary directories; installed by `tests/helpers.py`."""

    def command(self, argv: Sequence[str], cwd: Path, vendor: str | None = None,
                mounts: Mounts | None = None) -> list[str]:
        return [str(a) for a in argv]


_installed: Launcher | None = None


def use(launcher: Launcher | None) -> None:
    """Replace the launcher every agent call and check command goes through (None: bubblewrap
    again). The engine never calls this."""
    global _installed   # one process-wide launcher, set once by the tests' helpers
    _installed = launcher


def current() -> Launcher:
    return _installed if _installed is not None else Bubblewrap()


# ---------------------------------------------------------------- the canary

# Runs inside the sandbox with the host's own python3; reads its orders as JSON (argv[1]) and
# prints what it found as JSON. Python 3.8 syntax at most, standard library only.
PROBE = r'''
import ctypes, errno, json, os, shutil, sys, tempfile

spec = json.loads(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
CLONE_NEWNS, CLONE_NEWUSER = 0x00020000, 0x10000000
MS_BIND, MS_REMOUNT, MNT_DETACH = 4096, 32, 2


def seen(path):
    """What can be read at `path`: a directory's entries, or proof that a file opens."""
    try:
        if os.path.isdir(path):
            return sorted(os.listdir(path))[:5] or None
        with open(path, "rb") as f:
            f.read(1)
        return ["(readable file)"]
    except OSError:
        return None


def same(path, ident):
    try:
        st = os.stat(path)
    except OSError:
        return False
    return ident is not None and [st.st_dev, st.st_ino] == ident


def mount_lines():
    try:
        with open("/proc/self/mountinfo") as f:
            return f.read().splitlines()
    except OSError:
        return []


def mount_points():
    points = []
    for line in mount_lines():
        parts = line.split()
        points.append((parts[3].replace("\\040", " "), parts[4].replace("\\040", " ")))
    return points


def visible(extra_roots=()):
    """Secret paths that can be read: at their own place, or through any other mount that shows
    the same files (told apart by device and inode, so a namesake is not a finding)."""
    found = {}
    for secret in spec["secrets"]:
        path, ident, fs_path = secret["path"], secret["ident"], secret["fs_path"]
        if seen(path):
            found[path] = seen(path)
            continue
        if ident is None:
            continue   # not on this host: nothing another mount could show
        candidates = ["/proc/1/root" + path, "/proc/self/root" + path] + [root + path for root in extra_roots]
        for fs_root, point in mount_points():
            for inner in (fs_path, path):
                root = fs_root.rstrip("/")
                if inner and (inner == root or inner.startswith(root + "/")):
                    candidates.append(point.rstrip("/") + inner[len(root):])
        for candidate in candidates:
            if candidate != path and same(candidate, ident) and seen(candidate):
                found[candidate] = seen(candidate)
    return found


def windows():
    """On a WSL host: Windows drives that can be read, and the socket through which a Linux
    process starts Windows programs as the Windows user."""
    out = []
    for line in mount_lines():
        head, _, tail = line.partition(" - ")
        if "drvfs" in tail and seen(head.split()[4].replace("\\040", " ")):
            out.append(head.split()[4])
    socket_path = os.environ.get("WSL_INTEROP")
    if socket_path and os.path.exists(socket_path):
        out.append(socket_path)
    if seen("/run/WSL"):
        out.append("/run/WSL")
    return out


def try_write(directory, name):
    target = os.path.join(directory, name)
    try:
        with open(target, "w") as f:
            f.write("canary\n")
        return True
    except OSError:
        return False


def nested():
    """From a user and mount namespace of its own, where this process is root: try to take the
    hidden directories' covers off, and to reach what is under them through a second bind."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid:
        os.close(write_end)
        with os.fdopen(read_end) as f:
            text = f.read()
        os.waitpid(pid, 0)
        try:
            return json.loads(text)
        except ValueError:
            return {"error": "the nested probe died"}
    os.close(read_end)
    out = {}
    try:
        uid, gid = os.getuid(), os.getgid()
        if libc.unshare(CLONE_NEWUSER | CLONE_NEWNS) != 0:
            out["unavailable"] = errno.errorcode.get(ctypes.get_errno(), "?")
        else:
            for name, text in (("setgroups", "deny"), ("uid_map", "0 %d 1" % uid), ("gid_map", "0 %d 1" % gid)):
                try:
                    with open("/proc/self/" + name, "w") as f:
                        f.write(text)
                except OSError:
                    pass
            attempts = {}
            for path in spec["covered"]:
                rc = libc.umount2(path.encode(), MNT_DETACH)
                attempts["umount " + path] = "done" if rc == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
            second = tempfile.mkdtemp(prefix="canary-bind-")
            rc = libc.mount(b"/", second.encode(), None, MS_BIND, None)
            attempts["bind / without its submounts"] = "done" if rc == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
            for path in spec["read_only"]:
                rc = libc.mount(None, path.encode(), None, MS_REMOUNT | MS_BIND, None)
                attempts["remount " + path + " read-write"] = "done" if rc == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
            out["attempts"] = attempts
            out["visible"] = visible([second])
            out["wrote"] = [d for d in spec["read_only"] if try_write(d, spec["marker"] + "-nested")]
    except Exception as e:   # the probe reports, it never raises
        out["error"] = repr(e)
    os.write(write_end, json.dumps(out).encode())
    os._exit(0)


report = {
    "visible": visible(),
    "env": sorted(k for k in os.environ if k in spec["planted"] or k.startswith("AWS_")
                  and os.environ[k] != spec["neutral"].get(k)),
    "wrote": [d for d in spec["read_only"] if try_write(d, spec["marker"])],
    "writable": {d: try_write(d, spec["marker"]) for d in spec["writable"]},
    "present": {p: os.path.exists(p) for p in spec["present"]},
    "absent": {p: not os.path.lexists(p) for p in spec["absent"]},
    "direct_umount": {},
    "windows": windows(),
    "tools": {t: shutil.which(t) is not None for t in spec["tools"]},
    "pids": len([p for p in os.listdir("/proc") if p.isdigit()]),
}
for path in spec["covered"]:
    rc = libc.umount2(path.encode(), MNT_DETACH)
    report["direct_umount"][path] = "done" if rc == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
report["after_umount"] = visible()
report["nested"] = nested()
print(json.dumps(report))
'''
# Stand-ins for what a launching shell may carry: the scrub has to remove every one of them.
PLANTED = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AMAZON_CANARY",
           "CDK_DEFAULT_ACCOUNT", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET", "GH_TOKEN", "GITHUB_TOKEN",
           "SSH_AUTH_SOCK", "CANARY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "aws_canary_lowercase",
           "CANARY_SESSION", "KUBECONFIG")
# And two whose names say nothing: only the shape of the value gives them away.
PLANTED_SHAPED = {"CANARY_ENDPOINT": "https://canary:" + "canary@example.com/",   # a password in a URL
                  "CANARY_ID": "AKIA" + "CANARY" + "0" * 10}                      # an access key ID
PROFILES = (None, "claude", "codex")


@dataclass
class CanaryReport:
    problems: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    raw: dict[str, dict] = field(default_factory=dict)   # profile -> what the probe printed

    @property
    def ok(self) -> bool:
        return not self.problems


def secret_paths(home: Path, environ: Mapping[str, str], others: Sequence[Path] = ()) -> dict[str, str]:
    """What no agent and no check may read, by name: the AWS profile directory, the `gh` login
    directory, the SSH directory, and any credentials file the launching environment names.
    `others` are further home directories to look in (the account's own, when `$HOME` is not it)."""
    gh = environ.get("GH_CONFIG_DIR") or (Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "gh")
    out = {"~/.aws": str(home / ".aws"), "the gh login": str(gh), "~/.ssh": str(home / ".ssh")}
    for other in others:
        if other != home:
            out.update({"~/.aws of the account's own home": str(other / ".aws"),
                        "the gh login of the account's own home": str(other / ".config" / "gh"),
                        "~/.ssh of the account's own home": str(other / ".ssh")})
    for var in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        if environ.get(var) and environ[var] != "/dev/null":
            out[f"${var}"] = environ[var]
    return out


def canary(scratch: Path, repo_root: Path | None = None, launcher: Launcher | None = None, home: Path | None = None,
           environ: Mapping[str, str] | None = None, policy: Policy | None = None,
           read_only: Sequence[Path] | None = None, runner=subprocess.run) -> CanaryReport:
    """Start a probe inside the sandbox, once as a check command and once as each vendor's call,
    and say what it could see or do that it should not: read a secret path (at its own place, or
    through any other mount of the same files), find a credential variable, write outside its
    worktree, bring a hidden directory back (unmounting it, from the sandbox or from a user
    namespace of its own, or binding the filesystem under it a second time), or reach the other
    vendor's login. `scratch` is where its stand-in worktree is made, and removed. `read_only` is
    where it tries to write and must not succeed (default: the engine, the repository, the home
    directory and `/usr`); a marker it manages to leave there is a finding, and is removed.

    `launcher` defaults to bubblewrap with this repository's policy, whatever `use` installed:
    the canary judges the real sandbox."""
    environ = dict(os.environ if environ is None else environ)
    report = CanaryReport()
    try:
        box = launcher or Bubblewrap(policy or load_policy(), home=home)
        policy = policy or getattr(box, "policy", None) or load_policy()
        home_dir = Path(home or environ.get("HOME") or "")
        expand = Bubblewrap(policy, home=home_dir).expand
        covered = [str(h) for h in Bubblewrap(policy, home=home_dir).hidden()]
    except SandboxUnavailable as e:
        report.problems.append(str(e))
        return report
    secrets = secret_paths(home_dir, environ, account_homes() if home is None else ())
    marker = f".implement-loop-canary-{uuid.uuid4().hex[:8]}"
    targets = [ENGINE_DIR, *([repo_root] if repo_root else []), home_dir, Path("/usr")] if read_only is None else read_only
    outside = [str(p) for p in dict.fromkeys(Path(t) for t in targets) if p.is_dir()]
    planted_vars = dict.fromkeys(PLANTED, "canary") | PLANTED_SHAPED
    planted = environ | planted_vars
    logins = {v: [str(expand(p)) for p in policy.login.get(v, ())] for v in VENDORS}
    scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="canary-", dir=scratch))
    try:
        for profile in PROFILES:
            name = profile or "check"
            worktree = work / name
            worktree.mkdir()
            # the other vendors' logins, wherever the policy or a default installation keeps them
            others = sorted({p for v in VENDORS if v != profile
                             for p in (*logins[v], *(str(expand(d)) for d in DEFAULT_LOGIN[v]))})
            spec = {"secrets": [{"path": path, "ident": _identity(path), "fs_path": _fs_path(path)}
                                for path in secrets.values()], "covered": covered,
                    "read_only": outside, "writable": [str(worktree)], "marker": marker,
                    "planted": [k for k, v in planted_vars.items() if dropped(k, profile, v)], "neutral": NEUTRAL_AWS,
                    "present": [p for p in logins.get(profile or "", []) if Path(p).exists()], "absent": others,
                    "tools": ["bash", "git", "python3", "uv", "node", *([profile] if profile else [])]}
            mounts = Mounts(readable=tuple(Path(p) for p in (ENGINE_DIR,) if p.is_dir()), writable=(worktree,))
            try:
                argv = box.command(["python3", "-c", PROBE, json.dumps(spec)], worktree, vendor=profile, mounts=mounts)
                proc = runner(argv, cwd=worktree, env=clean_env(planted, work / "no-gh", profile),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
                found = json.loads(proc.stdout) if proc.returncode == 0 else None
            except SandboxUnavailable as e:
                report.problems.append(str(e))
                return report
            except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                found, proc = None, subprocess.CompletedProcess([], 1, "", str(e))
            if not isinstance(found, dict):
                tail = " | ".join((proc.stderr or proc.stdout or "no output").strip().splitlines()[-2:])
                report.problems.append(f"the probe did not run in the sandbox ({name}): {tail[:300]}")
                continue
            report.raw[name] = found
            _judge(report, name, found, secrets, worktree, marker)
        if report.raw and not report.problems:
            first = next(iter(report.raw.values()))
            hidden = [label for label, path in secrets.items() if os.path.lexists(path)]
            missing = [label for label, path in secrets.items() if not os.path.lexists(path)]
            report.facts.append(f"hidden: {', '.join(hidden) or 'nothing to hide'}"
                                + (f" (not on this host: {', '.join(missing)})" if missing else ""))
            report.facts.append("no credential variable")
            report.facts.append("only the worktree is writable")
            nested = first.get("nested") or {}
            report.facts.append("unmount and second bind refused" if nested.get("attempts") else
                                f"unmount refused (no nested user namespace: {nested.get('unavailable') or nested.get('error')})")
            absent = sorted(t for t, ok in first.get("tools", {}).items() if not ok)
            if absent:
                report.facts.append(f"not found inside: {', '.join(absent)}")
    finally:
        # The probe tries to write a marker into each of these. Inside the sandbox that lands on an
        # empty stand-in directory or is refused; a marker on the real filesystem is a finding.
        for directory in outside:
            for leaked in Path(directory).glob(marker + "*"):
                report.problems.append(f"a write from inside the sandbox reached {leaked}")
                leaked.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    return report


def _identity(path: str) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_dev, st.st_ino]


def _fs_path(path: str) -> str | None:
    """`path` as its own filesystem names it: below the root of the mount that holds it. Another
    mount of that filesystem shows the same file at its mount point plus this."""
    best: tuple[str, str] | None = None
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        root, point = parts[3].replace("\\040", " "), parts[4].replace("\\040", " ")
        inside = path == point or path.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) >= len(best[1])):
            best = (root, point)
    if best is None:
        return None
    return best[0].rstrip("/") + path[len(best[1].rstrip("/")):]


def _judge(report: CanaryReport, name: str, found: dict, secrets: dict[str, str], worktree: Path, marker: str) -> None:
    labels = {path: label for label, path in secrets.items()}
    said: set[str] = set()

    def say(where: dict, how: str) -> None:
        for path, entries in sorted(where.items()):
            if path in said:
                continue   # already readable before the attempt: one finding, not three
            said.add(path)
            label = next((l for p, l in labels.items() if path.endswith(p)), path)
            report.problems.append(f"{name}: {label} is readable {how} at {path} ({', '.join(map(str, entries))[:80]})")

    nested = found.get("nested") or {}
    say(found.get("visible") or {}, "in the sandbox")
    say(found.get("after_umount") or {}, "after an unmount")
    say(nested.get("visible") or {}, "from a nested user namespace")
    for var in found.get("env") or []:
        report.problems.append(f"{name}: the variable {var} reaches the sandbox")
    for path, ok in (found.get("writable") or {}).items():
        if not ok or not (worktree / marker).is_file():
            report.problems.append(f"{name}: the worktree {path} is not writable from inside")
    for path, result in (found.get("direct_umount") or {}).items():
        if result == "done":
            report.problems.append(f"{name}: {path} could be unmounted from inside")
    for path, ok in (found.get("present") or {}).items():
        if not ok:
            report.problems.append(f"{name}: its own login {path} is missing inside")
    for path, ok in (found.get("absent") or {}).items():
        if not ok:
            report.problems.append(f"{name}: another vendor's login {path} exists inside")
    for path in found.get("windows") or []:
        report.problems.append(f"{name}: the Windows side of this WSL host is reachable at {path}")
    if nested.get("error"):
        report.problems.append(f"{name}: the nested probe failed: {nested['error']}")


def describe(report: CanaryReport) -> str:
    """One line for the doctor."""
    return "; ".join(report.problems[:4] if report.problems else report.facts)


def version(bwrap: str = "bwrap") -> str | None:
    """`bubblewrap <version>`, None when it is not installed."""
    exe = shutil.which(bwrap)
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None
