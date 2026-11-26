"""The evaluation's workers: what a container is started with, what it may read of this host, and
that whatever was started is removed however the run ends (tools/evaluate/workers.py).

No container is started here. The runtime is a double that records what it is asked and can fail
or be interrupted at a chosen step; the relay to a forwarder and the bounded reading of a command
are the real ones, over local sockets and local processes.
"""

from __future__ import annotations

import http.client
import http.server
import io
import json
import socket
import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from evaluate import workers
from evaluate.workers import Limits, Refused, Spec, Streamed, Undisposed, WorkerFault, Workers

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

MEBIBYTE = 1024 * 1024
LIMITS = Limits(
    wall_seconds=720,
    memory_bytes=2048 * MEBIBYTE,
    cpu_millis=1500,
    pids=256,
    scratch_bytes=128 * MEBIBYTE,
    output_bytes=8 * MEBIBYTE,
)
IMAGE = "sha256:" + "ab" * 32
# The steps of a start, in order; `await` is only taken by a worker that has a forwarder.
SETUP = ("run", "inspect", "copy", "await")


def folders(output: int = LIMITS.output_bytes, more: dict[str, str] | None = None) -> dict[str, object]:
    """What a runtime reports as the writable folders of a worker of `LIMITS`, or of another one."""
    scratch = dict.fromkeys((workers.HOME, workers.WORK, workers.TEMPORARY), f"rw,size={LIMITS.scratch_bytes}")
    return {"Tmpfs": {**scratch, workers.OUTPUT: f"rw,noexec,size={output}", **(more or {})}}


def option(arguments: Sequence[str], name: str) -> list[str]:
    """The values one option was given in a `docker run`, before the image."""
    before = arguments[: arguments.index(IMAGE)]
    return [argument.partition("=")[2] for argument in before if argument.split("=")[0] == f"--{name}"]


class Runtime:
    """A runtime that holds a container from its `run` to its `rm`, and reports for it the limits
    it was started with unless `reported` says otherwise. `breaks` raises at a step, after the step
    took effect, and `once` does so a single time; a name in `stuck` is a container that no removal
    removes. `during` is called while a streamed command runs."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.started: list[list[str]] = []
        self.alive: set[str] = set()
        self.breaks: dict[str, BaseException] = {}
        self.once: dict[str, BaseException] = {}
        self.during: Callable[[], object] = lambda: None
        self.reported: dict[str, object] = {}
        self.stuck: set[str] = set()
        self.ended: Streamed = Streamed(0, False)
        self.output: dict[str, bytes] = {}
        # The seconds each streamed command was allowed.
        self.allowed: list[float] = []

    def _step(self, arguments: Sequence[str]) -> str:
        if arguments[0] != "exec":
            return arguments[0]
        return {"cp": "copy", "python": "await"}[arguments[2]]

    def steps(self) -> list[str]:
        return [self._step(call) if call[0] != "stream" else "stream" for call in self.calls]

    def call(self, arguments: Sequence[str], *, seconds: float = 0) -> str:
        self.calls.append(list(arguments))
        step = self._step(arguments)
        result = ""
        if step == "run":
            self.started.append(list(arguments))
            self.alive.add(option(arguments, "name")[0])
        elif step == "inspect":
            run = next(run for run in self.started if option(run, "name") == [arguments[-1]])
            result = json.dumps(
                {
                    "Memory": int(option(run, "memory")[0]),
                    "MemorySwap": int(option(run, "memory-swap")[0]),
                    "NanoCpus": round(float(option(run, "cpus")[0]) * 1_000_000_000),
                    "PidsLimit": int(option(run, "pids-limit")[0]),
                    "NetworkMode": option(run, "network")[0],
                    "ReadonlyRootfs": "--read-only" in run,
                    "Privileged": "--privileged" in run,
                    "Tmpfs": dict(value.split(":", 1) for value in option(run, "tmpfs")),
                    **self.reported,
                }
            )
        elif step == "rm":
            self.alive -= {arguments[-1]} - self.stuck
        elif step == "ps":
            name = arguments[-1].rpartition("=")[2]
            result = name if name in self.alive else ""
        if step in self.once:
            raise self.once.pop(step)
        if step in self.breaks:
            raise self.breaks[step]
        return result

    def stream(self, arguments: Sequence[str], *, sink: Path, limit: int, seconds: float) -> Streamed:
        self.calls.append(["stream", *arguments])
        self.allowed.append(seconds)
        if "command" in self.breaks:
            raise self.breaks["command"]
        self.during()
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode="w") as archive:
            for name, data in self.output.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            link = tarfile.TarInfo("link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(link)
        sink.write_bytes(packed.getvalue() if arguments[2:5] == ["python", "-I", "-c"] else b"printed\n")
        return self.ended

    def removed(self) -> set[str]:
        return {call[-1] for call in self.calls if call[0] == "rm"}

    def names(self) -> set[str]:
        return {option(run, "name")[0] for run in self.started}


@pytest.fixture
def runtime() -> Runtime:
    return Runtime()


@pytest.fixture
def relays(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
    """Every process the workers start on this host: the relays."""
    started: list[subprocess.Popen[bytes]] = []
    unpatched: Any = subprocess.Popen

    def recorded(*arguments: Any, **options: Any) -> subprocess.Popen[bytes]:
        process: subprocess.Popen[bytes] = unpatched(*arguments, **options)
        started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", recorded)
    return started


@pytest.fixture
def forwarder() -> Iterator[tuple[str, int]]:
    """A stand-in for a trial's forwarder: it answers every request with the path it was asked."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = f"forwarded {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *arguments: object) -> None:
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield "127.0.0.1", server.server_address[1]
        finally:
            server.shutdown()


@pytest.fixture
def task(tmp_path: Path) -> Path:
    folder = tmp_path / "task"
    (folder / "data").mkdir(parents=True)
    (folder / "TASK.md").write_text("complete the model\n", encoding="utf-8")
    (folder / "data" / "demand.csv").write_text("week,units\n1,40\n", encoding="utf-8")
    (folder / "data" / "demand.csv").chmod(0o600)
    return folder


@pytest.fixture
def sealed(tmp_path: Path) -> Path:
    folder = tmp_path / "suite" / "fixtures"
    (folder / "references").mkdir(parents=True)
    (folder / "references" / "optimum.json").write_text('{"objective": 20}\n', encoding="utf-8")
    return folder


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


def left_in(scratch: Path) -> list[Path]:
    return list(scratch.iterdir()) if scratch.exists() else []


def test_a_worker_is_started_with_every_limit_of_its_specification(runtime: Runtime, scratch: Path, task: Path) -> None:
    with Workers(scratch, runtime=runtime) as started:
        started.start(Spec(IMAGE, task, LIMITS))
    run = runtime.started[0]

    assert option(run, "memory") == option(run, "memory-swap") == [str(LIMITS.memory_bytes)]
    assert float(option(run, "cpus")[0]) == pytest.approx(1.5)
    assert option(run, "pids-limit") == ["256"]
    assert option(run, "network") == ["none"]
    assert option(run, "cap-drop") == ["ALL"]
    assert option(run, "security-opt") == ["no-new-privileges"]
    assert option(run, "user")[0].split(":")[0] not in {"0", "root"}
    assert {"--read-only", "--rm"} <= set(run)
    # Every folder it can write is memory of a fixed size, and the output folder is the cap's.
    writable = {value.partition(":")[0]: value.partition(":")[2].split(",") for value in option(run, "tmpfs")}
    assert set(writable) == {workers.HOME, workers.WORK, workers.TEMPORARY, workers.OUTPUT}
    assert f"size={LIMITS.output_bytes}" in writable[workers.OUTPUT]
    assert all(
        f"size={LIMITS.scratch_bytes}" in options for folder, options in writable.items() if folder != workers.OUTPUT
    )
    # Its first process ends by itself, a bounded time after the task's wall time.
    assert run[run.index(IMAGE) + 1 :][-1] == str(LIMITS.wall_seconds + workers.GRACE_SECONDS)


def test_a_worker_reads_only_copies_of_its_own_files_tools_and_forwarder_socket(
    runtime: Runtime,
    relays: list[subprocess.Popen[bytes]],
    forwarder: tuple[str, int],
    scratch: Path,
    task: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "search").mkdir()
    (tmp_path / "search" / "index.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_API_KEY", "a credential of this host")

    with Workers(scratch, runtime=runtime) as started:
        trial = started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder, tools={"search": tmp_path / "search"}))
        candidate = started.start(Spec(IMAGE, task, LIMITS))
        mounts = {}
        for worker, run in zip((trial, candidate), runtime.started, strict=True):
            fields = [dict(pair.partition("=")[::2] for pair in value.split(",")) for value in option(run, "mount")]
            assert all(mount["type"] == "bind" and "readonly" in mount for mount in fields)
            assert all(worker.workspace in Path(mount["source"]).parents for mount in fields)
            mounts[worker] = {mount["target"]: Path(mount["source"]) for mount in fields}
            # No variable, no other volume, no device and no privilege is given.
            assert not {argument.split("=")[0] for argument in run[: run.index(IMAGE)]} & {
                *("--env", "-e", "--env-file", "--volume", "-v", "--volumes-from"),
                *("--device", "--privileged", "--cap-add", "--publish", "-p"),
            }
            assert "a credential of this host" not in " ".join(run)

        assert set(mounts[trial]) == {workers.INPUTS, f"{workers.TOOLS}/search", workers.FORWARDER_FOLDER}
        assert set(mounts[candidate]) == {workers.INPUTS}
        # What is mounted is a copy the worker's user can read, not the task's own folder.
        copied = mounts[trial][workers.INPUTS] / "data" / "demand.csv"
        assert copied.read_text(encoding="utf-8") == "week,units\n1,40\n"
        assert copied.stat().st_mode & 0o004
        assert (mounts[trial][f"{workers.TOOLS}/search"] / "index.json").exists()
        assert [entry.name for entry in mounts[trial][workers.FORWARDER_FOLDER].iterdir()] == [workers.SOCKET]
        assert not (candidate.workspace / "forwarder").exists()
        # The files are copied into the working folder inside the container, before any command.
        assert ["exec", trial.name, "cp", "-R", f"{workers.INPUTS}/.", workers.WORK] in runtime.calls


def test_the_socket_of_a_trial_leads_to_its_own_forwarder(
    runtime: Runtime, forwarder: tuple[str, int], scratch: Path, task: Path
) -> None:
    with Workers(scratch, runtime=runtime) as started, socket.socket() as free:
        trial = started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder))
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        # The container's side of the relay, as the worker's first process runs it.
        inside = subprocess.Popen(
            [sys.executable, "-c", workers.RELAY, "60", f"127.0.0.1:{port}", f"./{workers.SOCKET}"],
            cwd=trial.workspace / "forwarder",
            stdout=subprocess.PIPE,
        )
        try:
            assert inside.stdout is not None
            assert inside.stdout.readline().strip() == b"ready"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/v1/messages")
            assert connection.getresponse().read() == b"forwarded /v1/messages"
            connection.close()
        finally:
            inside.kill()
            inside.wait()
            assert inside.stdout is not None
            inside.stdout.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"memory_bytes": 0},
        {"pids": True},
        {"cpu_millis": 1.5},
        {"wall_seconds": -1},
        # The folders are memory: these alone could take all of the worker's.
        {"scratch_bytes": 700 * MEBIBYTE},
    ],
)
def test_limits_that_cannot_hold_a_worker_are_refused(
    changed: dict[str, int], runtime: Runtime, scratch: Path, task: Path
) -> None:
    limits = Limits(**{**vars(LIMITS), **changed})

    with Workers(scratch, runtime=runtime) as started, pytest.raises(Refused):
        started.start(Spec(IMAGE, task, limits))

    assert runtime.calls == []
    assert left_in(scratch) == []


@pytest.mark.parametrize("source", ["fixtures", "inside", "around", "checkout", "root", "missing"])
def test_a_source_that_would_bring_in_what_stays_outside_is_refused(
    source: str, runtime: Runtime, relays: list[subprocess.Popen[bytes]], scratch: Path, task: Path, sealed: Path
) -> None:
    folder = {
        "fixtures": sealed,
        "inside": sealed / "references",
        "around": sealed.parent,
        "checkout": workers.REPO,
        "root": Path("/"),
        "missing": task / "nothing",
    }[source]

    with Workers(scratch, sealed=[sealed], runtime=runtime) as started:
        for spec in (Spec(IMAGE, folder, LIMITS), Spec(IMAGE, task, LIMITS, tools={"search": folder})):
            with pytest.raises(Refused):
                started.start(spec)
        with pytest.raises(Refused):
            started.start(Spec(IMAGE, task, LIMITS, tools={"../search": task}))

    assert runtime.calls == []
    assert relays == []
    assert left_in(scratch) == []


def test_the_configuration_of_this_host_is_no_source_and_no_scratch_folder(
    runtime: Runtime, scratch: Path, task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(home))
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "config").write_text("# a stand-in for this host's configuration\n", encoding="utf-8")
    (home / ".config" / "tool").mkdir(parents=True)
    (home / "course" / "task").mkdir(parents=True)

    with Workers(scratch, runtime=runtime) as started:
        for folder in (home / ".aws", home / ".config" / "tool", home / ".config"):
            for spec in (Spec(IMAGE, folder, LIMITS), Spec(IMAGE, task, LIMITS, tools={"search": folder})):
                with pytest.raises(Refused):
                    started.start(spec)
        assert runtime.calls == []
        assert left_in(scratch) == []
        # What a worker wrote is the next one's source: the scratch folder is held to the same.
        with pytest.raises(Refused):
            Workers(home / ".cache" / "evaluate", runtime=runtime)
        # The rest of the home folder is not the host's configuration: a task may be kept there.
        started.start(Spec(IMAGE, home / "course" / "task", LIMITS))


def test_configuration_kept_elsewhere_through_a_link_is_no_source_either(
    runtime: Runtime, scratch: Path, task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    moved = tmp_path / "disk" / "settings"
    (moved / "tool").mkdir(parents=True)
    (home / ".config").symlink_to(moved, target_is_directory=True)
    (tmp_path / "disk" / "course").mkdir()

    with Workers(scratch, runtime=runtime) as started:
        # By the link, by the folder it leads to, by a folder in it and by the folder that holds it.
        for folder in (home / ".config", moved, home / ".config" / "tool", moved / "tool", tmp_path / "disk"):
            for spec in (Spec(IMAGE, folder, LIMITS), Spec(IMAGE, task, LIMITS, tools={"search": folder})):
                with pytest.raises(Refused):
                    started.start(spec)
        assert runtime.calls == []
        assert left_in(scratch) == []
        with pytest.raises(Refused):
            Workers(moved / "evaluate", runtime=runtime)
        # A folder next to the moved configuration is not configuration.
        started.start(Spec(IMAGE, tmp_path / "disk" / "course", LIMITS))


def test_a_link_kept_in_the_configuration_is_no_source_wherever_it_leads(
    runtime: Runtime, scratch: Path, task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "operator"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    settings = tmp_path / "settings"
    (settings / "profile").mkdir(parents=True)
    (home / ".config" / "tool").symlink_to(settings, target_is_directory=True)
    # A link from elsewhere that leads through the configuration.
    (tmp_path / "through").symlink_to(home / ".config" / "tool", target_is_directory=True)
    # The same link named from the doubled root, which Linux reads as the root.
    doubled = Path(f"/{home}") / ".config" / "tool"
    (tmp_path / "doubled").symlink_to(doubled, target_is_directory=True)
    (home / "course").mkdir()

    with Workers(scratch, runtime=runtime) as started:
        chosen = (
            home / ".config" / "tool",
            home / ".config" / "tool" / "profile",
            home / "course" / ".." / ".config" / "tool",
            tmp_path / "through",
            doubled,
            tmp_path / "doubled",
        )
        for folder in chosen:
            for spec in (Spec(IMAGE, folder, LIMITS), Spec(IMAGE, task, LIMITS, tools={"search": folder})):
                with pytest.raises(Refused):
                    started.start(spec)
        assert runtime.calls == []
        assert left_in(scratch) == []
        with pytest.raises(Refused):
            Workers(home / ".config" / "tool" / "evaluate", runtime=runtime)
        with pytest.raises(Refused):
            Workers(doubled / "evaluate", runtime=runtime)
        # The same folder by a path that passes no configuration is a task's folder like any other.
        started.start(Spec(IMAGE, settings, LIMITS))


def test_a_copy_follows_no_link_and_takes_no_socket(runtime: Runtime, scratch: Path, task: Path, sealed: Path) -> None:
    (task / "answer.json").symlink_to(sealed / "references" / "optimum.json")
    with Workers(scratch, sealed=[sealed], runtime=runtime) as started:
        copied = started.start(Spec(IMAGE, task, LIMITS)).workspace / "inputs" / "answer.json"
        assert copied.is_symlink()

        with socket.socket(socket.AF_UNIX) as listening:
            listening.bind(str(task / "agent.sock"))
            with pytest.raises(Refused):
                started.start(Spec(IMAGE, task, LIMITS))

    assert runtime.names() <= runtime.removed()
    assert left_in(scratch) == []


@pytest.mark.parametrize(
    "reported",
    [
        # A runtime that cannot enforce a limit reports it as absent.
        {"Memory": 0, "MemorySwap": -1},
        {"MemorySwap": -1},
        {"PidsLimit": None},
        {"NanoCpus": 0},
        {"NetworkMode": "bridge"},
        {"ReadonlyRootfs": False},
        # A writable folder larger than asked for, one with no size, one more, and none reported.
        folders(output=2 * LIMITS.output_bytes),
        folders(more={workers.HOME: "rw"}),
        folders(more={"/var": f"rw,size={LIMITS.scratch_bytes}"}),
        {"Tmpfs": None},
    ],
)
def test_a_worker_without_its_limits_runs_nothing(
    reported: dict[str, object], runtime: Runtime, scratch: Path, task: Path
) -> None:
    runtime.reported = reported

    with Workers(scratch, runtime=runtime) as started, pytest.raises(WorkerFault):
        started.start(Spec(IMAGE, task, LIMITS))

    assert "exec" not in [call[0] for call in runtime.calls]
    assert "stream" not in runtime.steps()
    assert runtime.alive == set()
    assert left_in(scratch) == []


@pytest.mark.parametrize("error", [WorkerFault("the runtime failed"), KeyboardInterrupt()], ids=["fault", "interrupt"])
@pytest.mark.parametrize("step", SETUP)
def test_a_start_that_stops_half_way_leaves_no_worker(
    step: str,
    error: BaseException,
    runtime: Runtime,
    relays: list[subprocess.Popen[bytes]],
    forwarder: tuple[str, int],
    scratch: Path,
    task: Path,
) -> None:
    with pytest.raises(type(error)) as raised, Workers(scratch, runtime=runtime) as started:
        started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder))
        started.start(Spec(IMAGE, task, LIMITS))
        runtime.breaks[step] = error
        started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder))

    assert raised.value is error
    # The two that were started and the one that was not: each was asked to be removed, none is left.
    assert len(runtime.names()) == 3
    assert runtime.names() <= runtime.removed()
    assert runtime.alive == set()
    assert len(relays) == 2
    assert all(relay.poll() is not None for relay in relays)
    assert left_in(scratch) == []


def test_an_interrupted_command_ends_its_worker_and_the_run(
    runtime: Runtime, relays: list[subprocess.Popen[bytes]], forwarder: tuple[str, int], scratch: Path, task: Path
) -> None:
    with pytest.raises(KeyboardInterrupt), Workers(scratch, runtime=runtime) as started:
        started.start(Spec(IMAGE, task, LIMITS))
        trial = started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder))
        runtime.breaks["command"] = KeyboardInterrupt()
        try:
            trial.run(["claude", "--print", "solve the task"])
        finally:
            # The trial's processes are gone at once, before the run is disposed of.
            assert trial.name not in runtime.alive
            assert relays[0].poll() is not None

    assert runtime.names() <= runtime.removed()
    assert runtime.alive == set()
    assert left_in(scratch) == []


def test_a_command_past_its_wall_time_ends_its_worker(runtime: Runtime, scratch: Path, task: Path) -> None:
    runtime.ended = Streamed(None, False)

    with Workers(scratch, runtime=runtime) as started:
        trial = started.start(Spec(IMAGE, task, LIMITS))
        ran = trial.run(["sleep", "infinity"])

        assert ran.timed_out
        assert ran.exit_code is None
        assert len(runtime.allowed) == 1
        assert 0 < runtime.allowed[0] <= LIMITS.wall_seconds
        assert runtime.alive == set()

    assert left_in(scratch) == []


def test_the_commands_of_a_worker_share_its_wall_time(
    runtime: Runtime, scratch: Path, task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(workers.time, "monotonic", lambda: clock[0])
    runtime.during = lambda: clock.__setitem__(0, clock[0] + 6)
    limits = Limits(**{**vars(LIMITS), "wall_seconds": 10})

    with Workers(scratch, runtime=runtime) as started:
        trial = started.start(Spec(IMAGE, task, limits))
        ran = [trial.run(["claude", "--print", "go on"]) for _ in range(3)]

        # Ten seconds, then the four that are left; with none left the third is not run at all,
        # and it has timed out: the worker is ended.
        assert runtime.allowed == [10, 4]
        assert [one.timed_out for one in ran] == [False, False, True]
        assert runtime.alive == set()


def test_a_timeout_is_not_returned_while_the_runtime_still_holds_the_worker(
    runtime: Runtime, scratch: Path, task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers, "REMOVAL_PAUSE", 0)
    runtime.ended = Streamed(None, False)

    with Workers(scratch, runtime=runtime) as started:
        trial = started.start(Spec(IMAGE, task, LIMITS))
        runtime.stuck = {trial.name}
        with pytest.raises(Undisposed):
            trial.run(["sleep", "infinity"])
        with pytest.raises(Undisposed):
            trial.stop()
        assert runtime.alive == {trial.name}
        # The worker stays recorded: the disposal of the run tries again, and now ends it.
        runtime.stuck = set()

    assert runtime.alive == set()
    assert left_in(scratch) == []


def test_transcripts_and_collected_output_are_gone_after_grading(runtime: Runtime, scratch: Path, task: Path) -> None:
    runtime.output = {"./plan.tex": b"\\begin{tabular}", "./model/solve.py": b"print(20)\n"}

    with Workers(scratch, runtime=runtime) as started:
        trial = started.start(Spec(IMAGE, task, LIMITS))
        ran = trial.run(["claude", "--print", "solve the task"])
        submitted = trial.collect()
        trial.stop()
        assert trial.name not in runtime.alive
        # Only files and folders come back: the link the worker left is not followed or kept.
        assert sorted(path.relative_to(submitted).as_posix() for path in submitted.rglob("*") if path.is_file()) == [
            "model/solve.py",
            "plan.tex",
        ]
        assert not (submitted / "link").exists()
        # What a trial wrote can be given to a fresh worker, and to nothing else.
        candidate = started.start(Spec(IMAGE, submitted, LIMITS))
        assert (candidate.workspace / "inputs" / "model" / "solve.py").read_bytes() == b"print(20)\n"
        assert ran.transcript.read_bytes() == b"printed\n"

        runtime.ended = Streamed(0, True)
        with pytest.raises(workers.OverCap):
            candidate.collect()

    assert not ran.transcript.exists()
    assert not submitted.exists()
    assert runtime.alive == set()
    assert left_in(scratch) == []


def test_the_files_of_a_task_are_not_imported_by_the_programs_that_serve_the_worker(
    runtime: Runtime, forwarder: tuple[str, int], scratch: Path, task: Path, tmp_path: Path
) -> None:
    for module in ("tarfile", "socket", "threading"):
        (task / f"{module}.py").write_text("raise SystemExit('a file of the task was imported')\n", encoding="utf-8")
    output = tmp_path / "written"
    output.mkdir()
    (output / "answer.txt").write_text("20", encoding="utf-8")

    def packed_here(arguments: Sequence[str], *, sink: Path, limit: int, seconds: float) -> Streamed:
        """The program that reads the output folder, run as it is given, in the task's folder."""
        program = [*arguments[3:-1], arguments[-1].replace(workers.OUTPUT, str(output))]
        with sink.open("wb") as file:
            done = subprocess.run([sys.executable, *program], cwd=task, stdout=file, timeout=60, check=False)
        return Streamed(done.returncode, False)

    with Workers(scratch, runtime=runtime) as started:
        trial = started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder))
        runtime.stream = packed_here
        assert (trial.collect() / "answer.txt").read_text(encoding="utf-8") == "20"

    # The same holds for the worker's first process and for the wait for its relay.
    run = runtime.started[0]
    programs = [run[run.index(IMAGE) + 1 :], *(call[2:] for call in runtime.calls if call[2:3] == ["python"])]
    assert len(programs) == 2
    assert all(program[:3] == ["python", "-I", "-c"] for program in programs)


@pytest.mark.parametrize("first", [True, False], ids=["first-removal", "second-removal"])
def test_an_interrupt_during_disposal_does_not_end_it(
    first: bool,
    runtime: Runtime,
    relays: list[subprocess.Popen[bytes]],
    forwarder: tuple[str, int],
    scratch: Path,
    task: Path,
) -> None:
    interrupt = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as raised, Workers(scratch, runtime=runtime) as started:
        started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder))
        late = started.start(Spec(IMAGE, task, LIMITS, forwarder=forwarder))
        if first:
            runtime.once["rm"] = interrupt
        else:
            # The second removal: the first worker to be disposed of is ended by hand.
            late.stop()
            runtime.once["rm"] = interrupt

    assert raised.value is interrupt
    assert runtime.alive == set()
    assert all(relay.poll() is not None for relay in relays)
    assert left_in(scratch) == []


def test_disposal_goes_on_past_a_worker_that_cannot_be_removed(
    runtime: Runtime, scratch: Path, task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers, "REMOVAL_PAUSE", 0)
    started = Workers(scratch, runtime=runtime)
    first, second, third = (started.start(Spec(IMAGE, task, LIMITS)) for _ in range(3))
    runtime.stuck = {second.name}

    # With nothing else wrong, what is left is the error; with an error on its way, a note on it.
    with pytest.raises(Undisposed) as undisposed, started:
        pass
    with pytest.raises(KeyboardInterrupt) as interrupted, started:
        raise KeyboardInterrupt

    for raised in (undisposed, interrupted):
        assert [second.name in note for note in raised.value.__notes__] == [True]
    assert runtime.alive == {second.name}
    assert {first.name, third.name} <= runtime.removed()
    assert left_in(scratch) == []


def test_what_a_command_prints_is_kept_up_to_its_limit_and_its_time(tmp_path: Path) -> None:
    printing = [sys.executable, "-c", "import sys; print('x' * 300000); print('y', file=sys.stderr); sys.exit(3)"]
    printed = workers.stream(printing, sink=tmp_path / "printed", limit=1000, seconds=60)

    assert printed == Streamed(3, True)
    assert (tmp_path / "printed").read_bytes() == b"x" * 1000

    waiting = [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(600)"]
    waited = workers.stream(waiting, sink=tmp_path / "waited", limit=1000, seconds=2)

    assert waited == Streamed(None, False)
    assert (tmp_path / "waited").read_bytes() == b"started\n"
