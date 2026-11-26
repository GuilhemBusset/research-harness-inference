"""The workers of the evaluation: the disposable containers a trial and its submitted code run in
(docs/design/agent-session-evaluation.md, "Course quality and the grader").

A student harness produces code this repository has never seen, so it runs on the driver's host
only inside a worker, and the code it submits is executed in a second, fresh one. The trusted
grader stays in the driver: it reads what a worker wrote and is never mounted into one.

**A worker** is one container of the prepared trial image (`evaluate.image.prepare`) with a
workspace of its own on this host. `Workers.start` makes it from a `Spec`:

- *Nothing of this host is mounted as it is.* The task's files and each assigned tool are copied
  into the workspace, and only those copies are bound into the container, read-only. A source
  that is a sealed folder (the grader's fixtures, the reference answers), holds one or lies in
  one is refused, and so is one that holds the checkout or the home folder, or that is, holds
  or lies in a hidden entry of the home folder (`.aws`, `.ssh`, `.config`, ...): the
  configuration and the credentials of this host, also where such an entry is a link to a
  folder kept elsewhere, and also where the source is a link kept in such an entry: what was
  chosen from the configuration stays outside, wherever it leads. A copy keeps links as links
  and refuses a socket or a device.
- *A fresh home.* The root file system is read-only. Home, the working folder, the temporary
  folder and the output folder are memory file systems of a fixed size, empty at the start, and
  the task's files are copied into the working folder. No variable of this machine is passed.
- *Resource limits.* Memory with no swap beyond it, processor time, a number of processes, the
  sizes above and a lifetime: the container's first process ends by itself once the task's wall
  time and `GRACE_SECONDS` have passed, and the container is removed with it, also when the
  driver is gone. Once the container is started, the limits the runtime reports for it are read
  back, the sizes of its writable folders among them, and a worker whose limits are not the ones
  asked for is a fault: nothing is run in it. The wall time is the worker's, not a command's: it
  runs from the moment the worker is ready, and each command is given what is left of it.
- *No network.* The container has a loopback interface and no other. A trial is given one way
  out, to its own forwarder: a relay of this host listens on a socket in the trial's workspace
  and leads to the forwarder's address, the socket's folder is bound into the container, and
  the container's first process relays `FORWARDER_URL` to that socket. The forwarder holds the
  key, outside the trial. A worker with no forwarder, which is how submitted code is executed,
  has no way out at all.
- *Bounded output.* What a command prints is kept up to the output cap (the transcript), and the
  output folder cannot hold more than the cap. `collect` reads the folder back as one archive,
  bounded again, and unpacks its files and folders into the workspace.

**The lifecycle** is `Workers`, a context manager that owns every worker of a run:

    with Workers(scratch, sealed=[fixtures]) as workers:
        trial = workers.start(Spec(image, task_files, limits, forwarder=address, tools=tools))
        ran = trial.run(command)                 # wall time enforced; a timeout stops the worker
        submitted = trial.collect()
        trial.stop()                             # the trial's whole process tree is gone, or it raises
        candidate = workers.start(Spec(image, submitted, limits))
        candidate.run(["python", "solve.py"])
        verdict = grade(candidate.collect())     # in the driver, with fixtures no worker saw
    # here every container, relay, workspace and transcript is gone

Each worker is recorded before anything of it is started, so whatever ends the block (the end of
grading, a fault half-way through a start, an interrupt) finds it. `stop` removes the container by
force, which kills every process in it, ends the relay, and then asks the runtime whether the
container is gone: a worker the runtime still lists is never reported as ended (`Undisposed`, also
from a command that reached its wall time), and it stays recorded for the disposal to try again.
Disposal removes the workspace with its transcript and collected output. A removal that fails
does not stop the others, and neither does an interrupt that arrives during the disposal: it is
raised again once every worker was tried. What is left is reported then (`Undisposed`, or a note
on the error already on its way). Every runtime call is bounded in time.

The programs of this module that run in a worker (its first process, the wait for the relay, the
reading of the output folder) run isolated from the worker's files (`python -I`): a file of the
task named like a module of the standard library is not imported by them.

The runtime is reached through one object, `Docker`, which the tests replace.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from types import TracebackType

REPO = Path(__file__).resolve().parents[2]

# Where a worker finds things. The image makes `/trial/home` and `/trial/work` (its Dockerfile).
HOME = "/trial/home"
WORK = "/trial/work"
TEMPORARY = "/tmp"
INPUTS = "/trial/inputs"
OUTPUT = "/trial/output"
TOOLS = "/trial/tools"
FORWARDER_FOLDER = "/trial/forwarder"
SOCKET = "socket"
# The user the image runs as (`nobody`), given again here so that a worker never depends on it.
USER = "65534:65534"

# The only address a trial can reach besides itself: its own forwarder, through the relay.
FORWARDER_PORT = 8080
FORWARDER_URL = f"http://127.0.0.1:{FORWARDER_PORT}"

# The labels of a worker's container: the run it belongs to, and itself.
RUN_LABEL = "rhi.evaluate.run"
WORKER_LABEL = "rhi.evaluate.worker"

# How long a container outlives its task's wall time: its start, and the reading of its output.
GRACE_SECONDS = 300
START_SECONDS = 120
CALL_SECONDS = 60
READY_SECONDS = 30
COLLECT_SECONDS = 120
# How often, and how far apart, the runtime is asked whether a removed container is gone.
REMOVAL_CHECKS = 10
REMOVAL_PAUSE = 0.5
# How often the disposal of one worker is started again after an interrupt cut it short.
DISPOSAL_ATTEMPTS = 3

# How this module runs its own programs, here and in a worker: isolated, so that nothing of the
# working folder (the task's files, the submitted ones) or of the environment is imported.
_PYTHON = ("python", "-I", "-c")

_MEBIBYTE = 1024 * 1024
_TOOL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
# How many links a path may lead through, as the system itself allows.
_LINKS = 40

# The first process of every worker, and the relay of this host: one program. It ends by itself
# after `lifetime` seconds. Given `listen` and `upstream`, it accepts connections on the first and
# carries their bytes to and from the second; an address with a slash is a socket file, any other
# is `host:port`. As the first process of a container it also lets the kernel reap what the
# trial leaves behind.
RELAY = """\
import os
import signal
import socket
import sys
import threading
import time


def address(text):
    if "/" in text:
        return socket.AF_UNIX, text
    host, _, port = text.rpartition(":")
    return socket.AF_INET, (host, int(port))


def pump(source, sink):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve(client, upstream):
    family, where = address(upstream)
    with client, socket.socket(family, socket.SOCK_STREAM) as server:
        try:
            server.connect(where)
        except OSError:
            return
        back = threading.Thread(target=pump, args=(server, client), daemon=True)
        back.start()
        pump(client, server)
        back.join()


def main(lifetime, listen=None, upstream=None):
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    deadline = time.monotonic() + float(lifetime)
    if listen is None:
        while time.monotonic() < deadline:
            time.sleep(1)
        return
    family, where = address(listen)
    listener = socket.socket(family, socket.SOCK_STREAM)
    if family == socket.AF_UNIX:
        # Bound by its name from its folder: a socket's path has a short maximum length.
        os.chdir(os.path.dirname(where))
        listener.bind(os.path.basename(where))
        os.chmod(os.path.basename(where), 0o666)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(where)
    listener.listen(64)
    listener.settimeout(1)
    print("ready", flush=True)
    while time.monotonic() < deadline:
        try:
            client, _ = listener.accept()
        except TimeoutError:
            continue
        threading.Thread(target=serve, args=(client, upstream), daemon=True).start()


main(*sys.argv[1:])
"""

# In a trial, before its command: wait until the relay of the container accepts connections.
_AWAIT_RELAY = f"""\
import socket
import sys
import time

deadline = time.monotonic() + {READY_SECONDS}
while True:
    try:
        socket.create_connection(("127.0.0.1", {FORWARDER_PORT}), timeout=2).close()
        break
    except OSError:
        if time.monotonic() > deadline:
            sys.exit("the relay to the forwarder does not answer")
        time.sleep(0.1)
"""

# The output folder as one archive on standard output.
_PACK = f"""\
import sys
import tarfile

with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    archive.add({OUTPUT!r}, arcname=".")
"""


class Refused(ValueError):
    """A specification no worker is started from: a limit that is not one, or a source that would
    bring into a worker what must stay outside."""


class WorkerFault(RuntimeError):
    """The runtime or a worker did not do what the driver asked. The fault is the driver's side's,
    not the attempt's: a trial that met one is invalid, not failed."""


class OverCap(RuntimeError):
    """What a worker left in its output folder does not fit the output cap. Nothing of it is kept."""


class Undisposed(WorkerFault):
    """Something a run started could not be removed; each of its notes names one such thing."""


@dataclass(frozen=True)
class Limits:
    """What one worker may use. `wall_seconds` is the task's published wall time, and
    `output_bytes` bounds the output folder and, separately, the transcript. `scratch_bytes` is
    the size of each of the home, working and temporary folders."""

    wall_seconds: int
    memory_bytes: int
    cpu_millis: int
    pids: int
    scratch_bytes: int
    output_bytes: int

    def faults(self) -> list[str]:
        """Why these are not limits a worker can be held to; empty when they are."""
        found = [
            f"{name} must be a whole number above zero"
            for name, value in vars(self).items()
            if type(value) is not int or value <= 0
        ]
        # The folders are memory, charged to the worker like its processes: together they must
        # leave room for the processes, or filling a folder ends the trial instead of failing a write.
        if not found and 3 * self.scratch_bytes + self.output_bytes > self.memory_bytes // 2:
            found.append("the writable folders together may take at most half of memory_bytes")
        return found


@dataclass(frozen=True)
class Spec:
    """One worker. `image` is the prepared trial image, `inputs` the folder copied in as the
    worker's files (the task's files for a trial, the submitted files for their execution).
    `forwarder` is the address, on this host, of the forwarder of this trial alone; without one
    the worker has no way out. `tools` are the local tools assigned to it, by name: each folder
    is copied and appears read-only under `TOOLS`."""

    image: str
    inputs: Path
    limits: Limits
    forwarder: tuple[str, int] | None = None
    tools: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class Streamed:
    """How a streamed command ended: its exit code, or None when it was still running at its
    deadline; and whether it wrote more than was kept."""

    exit_code: int | None
    truncated: bool


@dataclass(frozen=True)
class Ran:
    """A command run in a worker. `exit_code` is None when the wall time ended it. `transcript`
    holds what it printed, up to the output cap; `truncated` says that it printed more."""

    exit_code: int | None
    timed_out: bool
    truncated: bool
    transcript: Path


def stream(command: Sequence[str], *, sink: Path, limit: int, seconds: float) -> Streamed:
    """Runs a command to its end or to `seconds`, appending what it prints (both streams) to
    `sink` up to `limit` bytes; what it prints beyond is read and dropped, so the command is never
    held back by its output. A command still running when this returns or raises is killed."""
    deadline = time.monotonic() + seconds
    kept = 0
    truncated = False
    with (
        sink.open("ab", buffering=0) as file,
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ) as process,
    ):
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return Streamed(None, truncated)
                if not select.select([descriptor], [], [], min(remaining, 1))[0]:
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                file.write(chunk[: limit - kept])
                truncated = truncated or len(chunk) > limit - kept
                kept = min(limit, kept + len(chunk))
            try:
                return Streamed(process.wait(timeout=max(deadline - time.monotonic(), 1)), truncated)
            except subprocess.TimeoutExpired:
                return Streamed(None, truncated)
        finally:
            if process.poll() is None:
                process.kill()


class Runtime(Protocol):
    """The container runtime, as the workers use it."""

    def call(self, arguments: Sequence[str], *, seconds: float = CALL_SECONDS) -> str:
        """Runs one runtime command to its end and returns what it printed; WorkerFault when it fails."""
        ...

    def stream(self, arguments: Sequence[str], *, sink: Path, limit: int, seconds: float) -> Streamed:
        """Runs one runtime command as `stream` does."""
        ...


class Docker:
    """The runtime: the `docker` command of this host, with this host's own configuration."""

    def call(self, arguments: Sequence[str], *, seconds: float = CALL_SECONDS) -> str:
        try:
            done = subprocess.run(
                ["docker", *arguments],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkerFault(f"docker {arguments[0]} did not complete: {error}") from error
        if done.returncode != 0:
            raise WorkerFault(f"docker {arguments[0]} failed:\n{done.stdout}\n{done.stderr}")
        return done.stdout

    def stream(self, arguments: Sequence[str], *, sink: Path, limit: int, seconds: float) -> Streamed:
        return stream(["docker", *arguments], sink=sink, limit=limit, seconds=seconds)


def _home() -> tuple[Path, ...]:
    try:
        return (Path.home().resolve(),)
    except RuntimeError:  # a user with no home folder has none to keep outside
        return ()


def _within(path: Path, other: Path) -> bool:
    return path == other or other in path.parents


def host_configuration(homes: Sequence[Path]) -> tuple[Path, ...]:
    """Where the configuration and the credentials of this host's user are kept: each hidden
    entry of a home folder (`.aws`, `.ssh`, `.config`), resolved, so that an entry which is a
    link names the folder the configuration is really in. Refused: a home folder that cannot be
    read, since what it keeps cannot be told from a task's files."""
    kept: list[Path] = []
    for home in homes:
        try:
            names = [entry.name for entry in os.scandir(home) if entry.name.startswith(".")]
        except (FileNotFoundError, NotADirectoryError):  # a home folder that is not there keeps nothing
            continue
        except OSError as error:
            raise Refused("the home folder cannot be read, so this host's configuration cannot be kept out") from error
        kept.extend((home / name).resolve() for name in names)
    return tuple(kept)


def _passed(path: Path) -> Iterator[Path]:
    """Every entry met on the way to where a path leads, links followed one at a time: each is
    named by the real folder that holds it, and the last one is the resolved path. A path that
    is only reached through a folder (a link kept in it) names that folder here, which the
    resolved path alone no longer does."""
    at = Path(path.anchor or "/")
    rest = list(reversed(path.absolute().parts[1:]))
    links = 0
    while rest:
        part = rest.pop()
        if part == "..":
            at = at.parent
            continue
        step = at / part
        yield step
        if not step.is_symlink():
            at = step
            continue
        links += 1
        if links > _LINKS:
            raise Refused(f"{path} leads through too many links")
        target = Path(os.readlink(step))
        if target.is_absolute():
            at = Path(target.anchor)
        rest.extend(reversed(target.parts[1:] if target.is_absolute() else target.parts))


def _in_host_configuration(path: Path, homes: Sequence[Path], kept: Sequence[Path]) -> bool:
    """Whether a path is, lies in or is reached through the host's configuration: a hidden entry
    of a home folder by its name, or the place one of them leads to. The path is taken as it was
    given, so that a link kept in the configuration is still a choice from it."""

    def configuration(step: Path) -> bool:
        named = any(home in step.parents and step.relative_to(home).parts[0].startswith(".") for home in homes)
        return named or any(_within(step, entry) for entry in kept)

    return any(configuration(step) for step in _passed(path))


def check_source(source: Path, sealed: Sequence[Path], outside: Sequence[Path], homes: Sequence[Path] = ()) -> Path:
    """The folder a worker's files are copied from, resolved. Refused when it is not a folder,
    when it is, holds or lies in a sealed folder, when it holds a folder that stays outside, or
    when it is, holds, lies in or is reached through the host's configuration: a hidden entry
    of one of `homes`, by whatever path it is named and wherever a link has moved it, and also a
    link kept in such an entry, wherever that link leads."""
    resolved = source.resolve()
    if not resolved.is_dir():
        raise Refused(f"{source} is not a folder")
    configuration = host_configuration(homes)
    if _in_host_configuration(source, homes, configuration):
        raise Refused(f"{source} lies in the configuration of this host, which stays outside the workers")
    if any(_within(entry, resolved) for entry in configuration):
        raise Refused(f"{source} holds configuration of this host, which stays outside the workers")
    for kept in sealed:
        if _within(resolved, kept.resolve()) or _within(kept.resolve(), resolved):
            raise Refused(f"{source} is sealed from the workers")
    for kept in outside:
        if _within(kept.resolve(), resolved):
            raise Refused(f"{source} holds what stays outside the workers")
    return resolved


def copy_tree(source: Path, target: Path) -> None:
    """Copies a folder for a worker to read: files and folders, readable by the worker's user. A
    link is copied as a link, never followed. Refused: anything else (a socket, a device, a pipe)."""
    target.mkdir()
    target.chmod(0o755)
    for entry in os.scandir(source):
        copy = target / entry.name
        if entry.is_symlink():
            copy.symlink_to(os.readlink(entry.path))
        elif entry.is_dir(follow_symlinks=False):
            copy_tree(Path(entry.path), copy)
        elif entry.is_file(follow_symlinks=False):
            shutil.copyfile(entry.path, copy, follow_symlinks=False)
            copy.chmod(0o755 if entry.stat(follow_symlinks=False).st_mode & 0o100 else 0o644)
        else:
            raise Refused(f"{entry.path} is neither a file nor a folder")


def _tmpfs(folder: str, size: int, *, execute: bool) -> str:
    owner, _, group = USER.partition(":")
    run = "exec" if execute else "noexec"
    return f"--tmpfs={folder}:rw,{run},nosuid,nodev,size={size},uid={owner},gid={group},mode=0700"


def _bind(source: Path, target: str) -> str:
    return f"--mount=type=bind,source={source},target={target},readonly"


def run_arguments(spec: Spec, *, name: str, run: str, workspace: Path) -> list[str]:
    """The `docker run` of one worker: its limits, its file systems and the only folders of this
    host it sees, all of them inside its own workspace. No variable is passed, and no network."""
    limits = spec.limits
    arguments = [
        *("run", "--detach", "--rm", "--pull=never", f"--name={name}"),
        *(f"--label={RUN_LABEL}={run}", f"--label={WORKER_LABEL}={name}"),
        *("--network=none", "--read-only", f"--user={USER}", f"--workdir={WORK}"),
        *("--cap-drop=ALL", "--security-opt=no-new-privileges"),
        *(f"--memory={limits.memory_bytes}", f"--memory-swap={limits.memory_bytes}"),
        *(f"--cpus={limits.cpu_millis / 1000:.3f}", f"--pids-limit={limits.pids}"),
        _tmpfs(HOME, limits.scratch_bytes, execute=True),
        _tmpfs(WORK, limits.scratch_bytes, execute=True),
        _tmpfs(TEMPORARY, limits.scratch_bytes, execute=True),
        _tmpfs(OUTPUT, limits.output_bytes, execute=False),
        _bind(workspace / "inputs", INPUTS),
        *(_bind(workspace / "tools" / tool, f"{TOOLS}/{tool}") for tool in sorted(spec.tools)),
    ]
    lifetime = str(limits.wall_seconds + GRACE_SECONDS)
    if spec.forwarder is None:
        return [*arguments, spec.image, *_PYTHON, RELAY, lifetime]
    relay = (f"127.0.0.1:{FORWARDER_PORT}", f"{FORWARDER_FOLDER}/{SOCKET}")
    socket_folder = _bind(workspace / "forwarder", FORWARDER_FOLDER)
    return [*arguments, socket_folder, spec.image, *_PYTHON, RELAY, lifetime, *relay]


def _reported_sizes(reported: object) -> dict[str, str | None]:
    """The size the runtime reports for each memory file system of a container (`Tmpfs` of its
    `HostConfig`: the folder, and its options as one text); None for one with no size."""
    if not isinstance(reported, dict):
        return {}
    sizes: dict[str, str | None] = {}
    for folder, options in reported.items():
        given = [option.partition("=")[2] for option in str(options).split(",") if option.startswith("size=")]
        sizes[str(folder)] = given[0] if len(given) == 1 else None
    return sizes


def limit_faults(limits: Limits, reported: Mapping[str, object]) -> list[str]:
    """Where the limits the runtime reports for a container (its `HostConfig`) are not the ones
    asked for: a runtime that cannot enforce a limit reports it as absent. The writable folders
    are part of them: each has the size asked for, and there is no other."""
    expected = {
        "Memory": limits.memory_bytes,
        "MemorySwap": limits.memory_bytes,
        "NanoCpus": limits.cpu_millis * 1_000_000,
        "PidsLimit": limits.pids,
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Privileged": False,
    }
    faults = [
        f"{key} is {reported.get(key)!r}, not {value!r}"
        for key, value in expected.items()
        if reported.get(key) != value
    ]
    scratch = dict.fromkeys((HOME, WORK, TEMPORARY), str(limits.scratch_bytes))
    sizes = {**scratch, OUTPUT: str(limits.output_bytes)}
    if (found := _reported_sizes(reported.get("Tmpfs"))) != sizes:
        faults.append(f"the writable folders are {found!r}, not {sizes!r}")
    return faults


class Worker:
    """One container and its workspace. Made by `Workers.start`, which records it first."""

    def __init__(self, spec: Spec, *, name: str, workspace: Path, runtime: Runtime) -> None:
        self.spec = spec
        self.name = name
        self.workspace = workspace
        self.transcript = workspace / "transcript"
        self._runtime = runtime
        self._relay: subprocess.Popen[bytes] | None = None
        # When the wall time of this worker ends: set once it is ready.
        self._deadline = 0.0
        # Whether the runtime may hold a container of this name: set before it is asked for one.
        self._container = False

    def _start(self, run: str, sources: Mapping[str, Path]) -> None:
        self.workspace.mkdir(mode=0o700, parents=True)
        self.transcript.touch()
        copy_tree(sources[""], self.workspace / "inputs")
        (self.workspace / "tools").mkdir()
        for tool in self.spec.tools:
            copy_tree(sources[tool], self.workspace / "tools" / tool)
        if self.spec.forwarder is not None:
            self._start_relay(self.spec.forwarder)
        self._container = True
        arguments = run_arguments(self.spec, name=self.name, run=run, workspace=self.workspace)
        self._runtime.call(arguments, seconds=START_SECONDS)
        reported = json.loads(self._runtime.call(["inspect", "--format", "{{json .HostConfig}}", self.name]))
        if faults := limit_faults(self.spec.limits, reported):
            raise WorkerFault(f"the worker does not have its limits: {'; '.join(faults)}")
        self._runtime.call(["exec", self.name, "cp", "-R", f"{INPUTS}/.", WORK])
        if self.spec.forwarder is not None:
            self._runtime.call(["exec", self.name, *_PYTHON, _AWAIT_RELAY], seconds=READY_SECONDS + CALL_SECONDS)
        self._deadline = time.monotonic() + self.spec.limits.wall_seconds

    def _start_relay(self, forwarder: tuple[str, int]) -> None:
        """The relay of this host: from a socket only this worker is given, to its forwarder."""
        folder = self.workspace / "forwarder"
        folder.mkdir()
        folder.chmod(0o755)
        lifetime = str(self.spec.limits.wall_seconds + GRACE_SECONDS)
        command = [sys.executable, "-I", "-c", RELAY, lifetime, str(folder / SOCKET), f"{forwarder[0]}:{forwarder[1]}"]
        self._relay = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        assert self._relay.stdout is not None
        ready = select.select([self._relay.stdout], [], [], READY_SECONDS)[0]
        if not ready or not self._relay.stdout.readline().strip():
            raise WorkerFault("the relay to the forwarder did not start")

    def run(self, command: Sequence[str]) -> Ran:
        """Runs a command in the worker, as its user in its working folder, for at most what is
        left of the worker's wall time; with none left, the command is not run and has timed out.
        A command that reaches the wall time, or an interrupt, stops the worker: nothing of the
        command is left running, and a timeout is only returned once the runtime confirmed that
        (`Undisposed` otherwise)."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self.stop()
            return Ran(None, True, False, self.transcript)
        room = self.spec.limits.output_bytes - (self.transcript.stat().st_size if self.transcript.exists() else 0)
        try:
            done = self._runtime.stream(
                ["exec", self.name, *command], sink=self.transcript, limit=max(room, 0), seconds=remaining
            )
        except BaseException as error:
            _note(error, self._end())
            raise
        if done.exit_code is None:
            self.stop()
        return Ran(done.exit_code, done.exit_code is None, done.truncated, self.transcript)

    def collect(self) -> Path:
        """What the worker left in its output folder, unpacked into a new folder of the workspace:
        files and folders only. OverCap when it does not fit the output cap."""
        archive = self.workspace / "output.tar"
        folder = self.workspace / "output"
        archive.unlink(missing_ok=True)
        if folder.exists():
            shutil.rmtree(folder)
        # A file takes at least a block of the folder and a header of the archive: an archive of
        # a folder within its cap is well below twice the cap, unless it is made of empty entries.
        limit = 2 * self.spec.limits.output_bytes + _MEBIBYTE
        done = self._runtime.stream(
            ["exec", self.name, *_PYTHON, _PACK], sink=archive, limit=limit, seconds=COLLECT_SECONDS
        )
        try:
            if done.truncated:
                raise OverCap(f"the output of {self.name} does not fit its cap")
            if done.exit_code != 0:
                head = archive.read_bytes()[:2000].decode("utf-8", "replace")
                raise WorkerFault(f"the output of {self.name} could not be read ({done.exit_code}):\n{head}")
            folder.mkdir()
            try:
                with tarfile.open(archive) as packed:
                    members = [member for member in packed if member.isfile() or member.isdir()]
                    if sum(member.size for member in members) > self.spec.limits.output_bytes:
                        raise OverCap(f"the output of {self.name} does not fit its cap")
                    packed.extractall(folder, members=members, filter="data")
            except tarfile.TarError as error:
                raise WorkerFault(f"the output of {self.name} is not an archive") from error
        finally:
            archive.unlink(missing_ok=True)
        return folder

    def stop(self) -> None:
        """Ends the worker: its container is removed by force, which kills every process in it,
        and its relay is ended. The workspace stays. Undisposed when the runtime does not confirm
        that the container is gone: the worker stays recorded, and its disposal tries again.
        Ending a worker that is ended asks the runtime nothing."""
        if left := self._end():
            raise _note(Undisposed(f"the worker {self.name} could not be ended"), left)

    def _end(self) -> list[str]:
        """Ends the relay and the container; returns what could not be ended. An interrupt may
        cut it short at any point: what was not ended yet is still recorded as such."""
        if self._relay is not None:
            self._relay.kill()
            self._relay.wait()
            if self._relay.stdout is not None:
                self._relay.stdout.close()
            self._relay = None
        if not self._container:
            return []
        try:
            self._remove_container()
        except WorkerFault as error:
            return [f"the container {self.name}: {error}"]
        self._container = False
        return []

    def _remove_container(self) -> None:
        try:
            self._runtime.call(["rm", "--force", "--volumes", self.name])
        except WorkerFault:
            # Whatever the removal said (no such container, a removal already under way), what
            # counts is whether the runtime still holds the container: it is asked below.
            pass
        listed = ["ps", "--all", "--quiet", "--filter", f"label={WORKER_LABEL}={self.name}"]
        for _ in range(REMOVAL_CHECKS):
            if not self._runtime.call(listed).strip():
                return
            time.sleep(REMOVAL_PAUSE)
        raise WorkerFault("the runtime still lists it")

    def discard(self) -> list[str]:
        """Ends the worker and removes its workspace, with its transcript and its collected
        output. Returns what could not be removed."""
        left = self._end()
        try:
            if self.workspace.exists():
                shutil.rmtree(self.workspace)
        except OSError as error:
            left.append(f"the workspace of {self.name}: {error}")
        return left


class Workers:
    """Every worker of one run, and their disposal. `root` is the scratch folder the workspaces
    are made in, and `sealed` the folders no worker may be given anything of: the grader's
    fixtures and the reference answers."""

    def __init__(self, root: Path, *, sealed: Sequence[Path] = (), runtime: Runtime | None = None) -> None:
        if "," in str(root.resolve()):
            raise Refused("the scratch folder's path holds a comma, which a mount cannot name")
        self.root = root.resolve()
        self.run = uuid4().hex[:12]
        self._sealed = tuple(sealed)
        # What no worker's files may hold: the checkout, the home folder, and this scratch folder.
        self._homes = _home()
        self._outside = (REPO, self.root, *self._homes)
        # What a worker writes is given to the next one from here, so this is a source as well.
        if _in_host_configuration(root, self._homes, host_configuration(self._homes)):
            raise Refused("the scratch folder lies in the configuration of this host")
        self._runtime: Runtime = runtime if runtime is not None else Docker()
        self._workers: list[Worker] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        left = self.dispose()
        if error is not None:
            _note(error, left)
        elif left:
            raise _note(Undisposed("the run left something behind"), left)

    def start(self, spec: Spec) -> Worker:
        """A started worker of this specification, its limits checked and its files in place.
        Refused, before anything is made, for a specification that is not one. A start that fails
        or is interrupted leaves nothing of the worker."""
        if faults := spec.limits.faults():
            raise Refused("; ".join(faults))
        if unnamed := [tool for tool in spec.tools if not _TOOL_NAME.fullmatch(tool)]:
            raise Refused(f"not the name of a tool: {unnamed}")
        sources = {"": spec.inputs, **spec.tools}
        checked = {
            key: check_source(source, self._sealed, self._outside, self._homes) for key, source in sources.items()
        }
        name = f"rhi-evaluate-{self.run}-{len(self._workers)}-{uuid4().hex[:8]}"
        worker = Worker(spec, name=name, workspace=self.root / name, runtime=self._runtime)
        self._workers.append(worker)
        try:
            worker._start(self.run, checked)
        except BaseException as error:
            _note(error, worker.discard())
            raise
        return worker

    def dispose(self) -> list[str]:
        """Ends every worker and removes every workspace, the last started first, going on past
        what cannot be removed. Returns what is left; empty when nothing is. An interrupt during
        it does not end it: the worker it cut short is disposed of again, the others follow, and
        the interrupt is raised once all were tried, with what is left as its notes."""
        left: list[str] = []
        interrupted: BaseException | None = None
        for worker in reversed(self._workers):
            for _ in range(DISPOSAL_ATTEMPTS):
                try:
                    left.extend(worker.discard())
                except BaseException as error:  # noqa: BLE001 - raised below, once every worker was tried
                    interrupted = interrupted or error
                else:
                    break
            else:
                left.append(f"the worker {worker.name}: its disposal was interrupted")
        if interrupted is not None:
            raise _note(interrupted, left)
        return left


def _note[Error: BaseException](error: Error, left: Sequence[str]) -> Error:
    for thing in left:
        error.add_note(f"not removed: {thing}")
    return error
