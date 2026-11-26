"""A trial and the code it submits run in workers that see nothing of this host but their own
files and, for the trial, its own forwarder; and no worker outlives its run
(docs/design/agent-session-evaluation.md, "Course quality and the grader"; tools/evaluate/workers.py).

Real containers of the prepared trial image are started through `evaluate.workers`, and each is
asked from the inside what it can reach: a program run in it reports the answer of its forwarder,
what a connection out of the container fails with, its variables, its mounts, the sockets it can
see and whether a path of the grader's fixtures or of this checkout exists in it. The forwarders
are stand-ins in this process, each answering with a word of its own. The grader is this test: it
compares what the submitted program wrote, in a second worker, with a reference no worker was
given.

It needs a container runtime, so CI's job `container-tests` runs it and the local gate does not
(tests/container/README.md). Nothing here skips: without a runtime the fixture fails.
"""

from __future__ import annotations

import _thread
import http.server
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

# The evaluation tool is a package under `tools`, which only its own tests put on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from evaluate import image, workers
from evaluate.workers import Limits, Spec, Worker, Workers

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

MEBIBYTE = 1024 * 1024
LIMITS = Limits(
    wall_seconds=120,
    memory_bytes=512 * MEBIBYTE,
    cpu_millis=1000,
    pids=128,
    scratch_bytes=32 * MEBIBYTE,
    output_bytes=4 * MEBIBYTE,
)
# A variable that would carry a credential. The base image's own `GPG_KEY` is a public fingerprint.
CREDENTIAL = re.compile(r"API_KEY|TOKEN|SECRET|PASSWORD")
# Paths of this checkout, which no worker holds.
CHECKOUT = ("tools/evaluate/workers.py", "docs/design/agent-session-evaluation.md")
SOCKET = f"{workers.FORWARDER_FOLDER}/{workers.SOCKET}"

# What a worker can reach, reported from inside it as one line of JSON. Its argument names the
# paths to look for and the ports of this host's listeners to try.
PROBE = """\
import errno
import json
import os
import socket
import stat
import sys
import urllib.request

asked = json.loads(sys.argv[1])


def connect(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "connected"
    except OSError as error:
        return errno.errorcode.get(error.errno, repr(error))


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.read().decode()
    except OSError:
        return None


def fill(folder, most):
    written = 0
    path = os.path.join(folder, "fill")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        while written < most:
            written += os.write(descriptor, bytes(65536))
        return [written, None]
    except OSError as error:
        return [written, errno.errorcode.get(error.errno, repr(error))]
    finally:
        os.close(descriptor)
        os.remove(path)


def writable(folder):
    path = os.path.join(folder, "probe")
    try:
        with open(path, "w"):
            pass
    except OSError:
        return False
    os.remove(path)
    return True


found, sockets = [], []
for folder, folders, files in os.walk("/"):
    if folder == "/":
        folders[:] = [name for name in folders if name not in ("proc", "sys", "dev")]
    for name in folders + files:
        path = os.path.join(folder, name)
        if any(needle in path for needle in asked["needles"]):
            found.append(path)
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                sockets.append(path)
        except OSError:
            pass

with open("/proc/self/mounts") as table:
    mounts = {line.split()[1]: line.split()[3].split(",") for line in table}

print(json.dumps({
    "forwarder": fetch(asked["forwarder"] + "/v1/messages"),
    "outside": connect("192.0.2.1", 443),
    "listeners": [connect("127.0.0.1", port) for port in asked["ports"]],
    "environment": dict(os.environ),
    "found": found,
    "sockets": sockets,
    "mounts": mounts,
    "user": os.getuid(),
    "home": os.listdir(os.path.expanduser("~")),
    "work": sorted(os.listdir(".")),
    "writable": {folder: writable(folder) for folder in asked["folders"]},
    "filled": fill(asked["output"], 4 * asked["cap"]),
}))
"""

# A stand-in for a harness, given its forwarder's address and its output folder: it reads its
# task, asks its forwarder, keeps a note in its home and submits a program, which carries the
# task's numbers and the forwarder's word.
AGENT = """\
import json
import os
import sys
import urllib.request

url, output = sys.argv[1:]
with open("task.json") as file:
    task = json.load(file)
with urllib.request.urlopen(url + "/v1/messages", timeout=20) as response:
    word = response.read().decode()
with open(os.path.expanduser("~/notes"), "w") as file:
    file.write(word)
answer = output + "/answer.json"
program = [
    "import json",
    f"values, word = {task['values']!r}, {word!r}",
    f"with open({answer!r}, 'w') as file:",
    "    json.dump({'total': sum(values), 'word': word}, file)",
]
with open(output + "/solve.py", "w") as file:
    file.write("\\n".join(program) + "\\n")
"""

# A command that starts processes of its own and waits for them, far longer than any test.
TREE = ["sh", "-c", "sleep 600 & sleep 600 & echo spawned; wait"]


def docker(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run one docker command to its end, whatever its result."""
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False)


def containers(run: str) -> list[str]:
    """The containers of one run of workers the runtime holds, running or not."""
    listed = docker("ps", "--all", "--quiet", "--filter", f"label={workers.RUN_LABEL}={run}")
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.split()


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Iterator[image.Prepared]:
    """The trial image, prepared from the committed recipe under a name of this run alone."""
    name = f"rhi-evaluate-workers-test-{uuid4().hex}"
    try:
        yield image.prepare(name, record=tmp_path_factory.mktemp("image") / "inventory.json")
    finally:
        docker("image", "rm", "--force", image.reference(name))


@pytest.fixture
def forwarder() -> Iterator[Callable[[], tuple[tuple[str, int], str]]]:
    """Makes a stand-in forwarder on this host: its address, and the word only it answers with."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def make() -> tuple[tuple[str, int], str]:
        word = f"forwarded-{uuid4().hex}"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(word)))
                self.end_headers()
                self.wfile.write(word.encode())

            def log_message(self, format: str, *arguments: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return ("127.0.0.1", server.server_address[1]), word

    try:
        yield make
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.fixture
def task(tmp_path: Path) -> Path:
    folder = tmp_path / "task"
    folder.mkdir()
    (folder / "task.json").write_text(json.dumps({"values": [4, 7, 9]}), encoding="utf-8")
    return folder


@pytest.fixture
def reference(tmp_path: Path) -> Path:
    """The grader's reference answer, in a folder sealed from the workers. Both have a name of
    this test alone, so that a path of either inside a worker can only have come from here."""
    folder = tmp_path / f"fixtures-{uuid4().hex}"
    folder.mkdir()
    answer = folder / f"reference-{uuid4().hex}.json"
    answer.write_text(json.dumps({"total": 20}), encoding="utf-8")
    return answer


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    """A credential in this process's environment, as the driver's host would hold one."""
    value = f"secret-{uuid4().hex}"
    monkeypatch.setenv("EVALUATE_TEST_API_KEY", value)
    return value


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


def probe(worker: Worker, reference: Path, ports: list[int]) -> dict[str, Any]:
    asked = {
        "needles": [reference.name, reference.parent.name, "docker.sock", *CHECKOUT],
        "forwarder": workers.FORWARDER_URL,
        "ports": ports,
        "folders": [workers.INPUTS, workers.WORK, workers.HOME, workers.OUTPUT, "/", "/opt"],
        "output": workers.OUTPUT,
        "cap": worker.spec.limits.output_bytes,
    }
    ran = worker.run(["python", "-c", PROBE, json.dumps(asked)])
    printed = ran.transcript.read_text(encoding="utf-8")
    assert ran.exit_code == 0, f"the probe failed in {worker.name}:\n{printed}"
    return json.loads(printed.splitlines()[-1])


def assert_sealed(seen: dict[str, Any], secret: str) -> None:
    """What holds for every worker: no route out, nothing of this host's environment, fixtures or
    checkout, a read-only root and files, and an output folder that takes no more than its cap."""
    assert seen["outside"] == "ENETUNREACH"
    assert set(seen["listeners"]) == {"ECONNREFUSED"}
    variables = seen["environment"]
    assert not [name for name in variables if name.startswith("AWS_") or CREDENTIAL.search(name)]
    assert secret not in variables.values()
    assert seen["found"] == []
    assert seen["user"] != 0
    assert "ro" in seen["mounts"]["/"]
    assert "ro" in seen["mounts"][workers.INPUTS]
    assert seen["writable"] == {
        **dict.fromkeys((workers.WORK, workers.HOME, workers.OUTPUT), True),
        **dict.fromkeys((workers.INPUTS, "/", "/opt"), False),
    }
    written, refusal = seen["filled"]
    assert refusal == "ENOSPC"
    assert written <= LIMITS.output_bytes


def test_a_trial_reaches_its_own_forwarder_and_nothing_else_of_this_host(
    prepared: image.Prepared,
    forwarder: Callable[[], tuple[tuple[str, int], str]],
    scratch: Path,
    task: Path,
    reference: Path,
    secret: str,
    tmp_path: Path,
) -> None:
    (tmp_path / "search").mkdir()
    (tmp_path / "search" / "index.json").write_text("{}", encoding="utf-8")
    (one, first_word), (two, second_word) = forwarder(), forwarder()
    ports = [one[1], two[1]]

    with Workers(scratch, sealed=[reference.parent]) as started:
        first = started.start(
            Spec(prepared.image_id, task, LIMITS, forwarder=one, tools={"search": tmp_path / "search"})
        )
        second = started.start(Spec(prepared.image_id, task, LIMITS, forwarder=two))
        inspected = docker("inspect", "--format", "{{json .HostConfig}}", first.name)
        seen, other = probe(first, reference, ports), probe(second, reference, ports)

    # Each trial has one way out, and it leads to its own forwarder: not to the other's, and not
    # to the address either forwarder listens on.
    assert (seen["forwarder"], other["forwarder"]) == (first_word, second_word)
    for trial in (seen, other):
        assert_sealed(trial, secret)
        assert trial["sockets"] == [SOCKET]
        assert "ro" in trial["mounts"][workers.FORWARDER_FOLDER]
    # A fresh home, the task's files in the working folder, and the assigned tool, read-only.
    assert seen["home"] == []
    assert seen["work"] == ["task.json"]
    assert "ro" in seen["mounts"][f"{workers.TOOLS}/search"]
    assert f"{workers.TOOLS}/search" not in other["mounts"]
    # The limits the runtime holds the container to are the specification's.
    reported = json.loads(inspected.stdout)
    assert reported["Memory"] == reported["MemorySwap"] == LIMITS.memory_bytes
    assert reported["PidsLimit"] == LIMITS.pids
    assert reported["NanoCpus"] == LIMITS.cpu_millis * 1_000_000


def test_submitted_code_runs_in_a_fresh_worker_and_is_graded_outside_it(
    prepared: image.Prepared,
    forwarder: Callable[[], tuple[tuple[str, int], str]],
    scratch: Path,
    task: Path,
    reference: Path,
    secret: str,
) -> None:
    address, word = forwarder()

    with Workers(scratch, sealed=[reference.parent]) as started:
        trial = started.start(Spec(prepared.image_id, task, LIMITS, forwarder=address))
        wrote = trial.run(["python", "-c", AGENT, workers.FORWARDER_URL, workers.OUTPUT])
        assert wrote.exit_code == 0, wrote.transcript.read_text(encoding="utf-8")
        submitted = trial.collect()
        trial.stop()
        # The trial is gone, with its home and its forwarder, before what it submitted is run.
        assert containers(started.run) == []

        candidate = started.start(Spec(prepared.image_id, submitted, LIMITS))
        assert len(containers(started.run)) == 1
        ran = candidate.run(["python", "solve.py"])
        assert ran.exit_code == 0, ran.transcript.read_text(encoding="utf-8")
        answer = json.loads((candidate.collect() / "answer.json").read_text(encoding="utf-8"))
        seen = probe(candidate, reference, [address[1], workers.FORWARDER_PORT])

    # The grader, on this host: the reference was never in a worker.
    assert answer == {"total": json.loads(reference.read_text(encoding="utf-8"))["total"], "word": word}
    # The worker of the submitted code has no forwarder, no socket, and nothing the trial left.
    assert_sealed(seen, secret)
    assert seen["forwarder"] is None
    assert seen["sockets"] == []
    assert workers.FORWARDER_FOLDER not in seen["mounts"]
    assert seen["home"] == []
    assert seen["work"] == ["solve.py"]


def test_an_interrupted_trial_leaves_no_worker(
    prepared: image.Prepared,
    forwarder: Callable[[], tuple[tuple[str, int], str]],
    scratch: Path,
    task: Path,
    reference: Path,
) -> None:
    started = Workers(scratch, sealed=[reference.parent])
    returned = threading.Event()
    before: list[str] = []

    def interrupt(transcript: Path, name: str) -> None:
        """Interrupts the test once the trial's processes run, as a signal to the driver would."""
        deadline = time.monotonic() + 60
        while not returned.is_set() and time.monotonic() < deadline:
            if transcript.exists() and b"spawned" in transcript.read_bytes():
                break
            time.sleep(0.2)
        before.extend(docker("top", name).stdout.splitlines())
        if not returned.is_set():
            _thread.interrupt_main()

    with pytest.raises(KeyboardInterrupt), started:
        started.start(Spec(prepared.image_id, task, LIMITS))
        trial = started.start(Spec(prepared.image_id, task, LIMITS, forwarder=forwarder()[0]))
        assert len(containers(started.run)) == 2
        watcher = threading.Thread(target=interrupt, args=(trial.transcript, trial.name), daemon=True)
        watcher.start()
        try:
            trial.run(TREE)
        finally:
            returned.set()
    watcher.join(timeout=60)

    # The trial had a tree of processes; no container and no workspace of the run is left.
    assert len([line for line in before if "sleep 600" in line]) >= 2
    assert containers(started.run) == []
    assert list(scratch.iterdir()) == []


def test_a_trial_past_its_wall_time_is_ended_and_removed(
    prepared: image.Prepared, scratch: Path, task: Path, reference: Path
) -> None:
    limits = Limits(**{**vars(LIMITS), "wall_seconds": 5})

    with Workers(scratch, sealed=[reference.parent]) as started:
        trial = started.start(Spec(prepared.image_id, task, limits))
        ran = trial.run(TREE)

        assert ran.timed_out
        assert ran.transcript.read_bytes() == b"spawned\n"
        # Its processes ended with its container, before the run is disposed of.
        assert containers(started.run) == []

    assert list(scratch.iterdir()) == []
