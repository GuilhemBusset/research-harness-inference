"""Spike S3: does each harness complete a task of the course's kind on the coursework model (system
design, sections 4.2, 6.1, 6.2 and 12)?

A route existing is not a harness working (section 6.1). This job gives the task of
`spikes/s3_task/` to each harness against a named pool, and records what it observed, with times.
**It judges nothing**: the pass condition is read later, from the first real run's data, by an
issue written then (section 12). It posts no measurement event; what it observed is its log.

**The harnesses** are the versions `spikes/harnesses.py` pins, installed into the job, and an SDK
script of this module (`sdk_script`), with the OpenAI SDK of the group `spikes`:

- Claude Code with its default settings: what `rhi login` writes (`rhi_cli.harness`), its key from
  `rhi token`, and nothing else of its own. No model, effort, thinking or window is set: it asks for
  its own `claude-` names. Two things are set that change no request it sends: the tools it may use
  without asking (`ALLOWED_TOOLS`), since it runs with no one to ask, and `DISABLE_AUTOUPDATER`, so
  that what ran is the version recorded;
- Codex with the profile `rhi` that `rhi login` writes, with no sandbox of its own (the job's
  machine is a throwaway build's, and Codex's sandbox needs what a build's container may not have);
- the SDK script: a loop of Chat Completions calls with four tools (list, read and write a file of
  the folder, run a Python file of it), until the model answers without a tool call.

Each runs in a home of its own, through a forwarder of its own (`s2_client_hold.Forwarder`), which
sends every request on to the gateway with the pool's name as its model, so that every request,
whatever name the harness asks for, reaches the named pool, and records each exchange.

**The task** is a copy of `spikes/s3_task/` for each harness. Once the harness has ended, the job
puts the task's own `check_task.py` and `instance.json` back where the harness may have changed
them, notes whether it had, and runs the check in the folder the harness left: its exit status is
the task's result.

**What is recorded**, one JSON object a line on standard output, which the spike jobs' log group
keeps (`Lines`). Every line has `at`, its UTC time, and `kind`:

| Kind | What it holds |
| --- | --- |
| `job` | The pool and its engine |
| `versions` | The version of each harness and SDK |
| `harness_run` | For each harness: when its run started and ended, its turns, its exit status, the check's exit status, its version, and the build the pool served as its lifecycle agents report it |
| `refused_request` | For each Claude Code run, each request the gateway or the engine refused, side queries and token counts included: its time, route and status, and its cause |
| `image_step` | Whether an image Claude Code read with a tool reached the pool inside a tool result, how that request was answered, and whether Claude Code named what it shows |
| `compaction` | Whether a Claude Code session driven past its window compacted, and whether it completed its next turn |
| `replay` | The replay of the recorded Claude Code session: its times and its requests |
| `engine_counters` | The engine's input and cached input token counters, at the replay's start and at its end |
| `step_failed` | A step that raised: its name and the type of the error |

**The turns.** Claude Code's are the turns its own result counts (`num_turns`): its main loop, with
its side queries and token counts left out. Codex and the SDK script send no side query, so theirs
are the requests on their inference route that carry the run's marker, the random word of the
prompt.

**A refused request** is one the forwarder saw answered with a status other than 200, or with a
stream that an error event ended (`s2_client_hold.refused`): the gateway answers a held stream 200
at once, and the engine may refuse it afterwards. A request that got no answer at all is not one:
nothing shows that either refused it. Its cause is what the error names, when that is a name.

**The image step.** In the folder Claude Code left, the job writes `figure.png`, an image filled
with one colour it picks at random, and asks Claude Code to open it with its tools and say which
colour fills it. The request that carries the image inside a tool result is the one recorded.

**Compaction.** In a folder of its own, the job writes `FILLER_FILES` files of text, more than
Claude Code's default window holds, and asks Claude Code to read them all, one at a time, then give
the last word of the last. Claude Code says in its own output that it compacted (an event
`compact_boundary`), and gives every turn of its main loop as an `assistant` event once whole: the
session completed its next turn when such an event came after the compaction.

**The replay.** The Claude Code session of the task, every request the forwarder saw answered,
side queries included, with the pauses between them, is replayed through the gateway with the load
generator (`loadgen.replay`) as a canary student that has sent nothing in the job, so its cache
starts empty. The engine's input and cached input token counters, by the names of the per-engine
table (`rhi_common.engine_metrics`), are read for the moment just before the replay and for a
scrape after its end, once the workspace holds that scrape, so that the counters hold the replay's
last answers. Each instance is named by its order of appearance in the job, never by its own name.

**The build** is the one the pool's lifecycle agents report (`rhi_lifecycle.serving.RELEASE_METRIC`)
over the run, read once the workspace holds the scrape after it (`s5_capacity.in_service_of`); it is
`null` when they reported none, or two.

**No key and no text from AWS.** Every line goes through `Lines`, which refuses one that holds a
key of the job. An error is recorded by the type's name, never its message, and a cause only when
it is a name. The answers of the harnesses are read for what they hold, never copied.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s3_harness_task`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, or an `https://` address |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL` | The pool, `coursework` unless given |

The pool's engine comes from the deployed configuration (`rhi.config.read_ssm`), the region from
CodeBuild's `AWS_REGION`. The canary students need four keys with the default budget. The build the
pool serves is the owner's to set (the 8-bit, then the 4-bit build) before starting the job.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import boto3
import openai
from openai.types.chat import ChatCompletionMessageFunctionToolCall

import common
import harnesses
import loadgen
import s2_client_hold as s2
import s4_weights
import s5_capacity as s5
from rhi import config
from rhi_cli import harness as login_files
from rhi_common.engine_metrics import Kind as MetricKind
from rhi_common.engine_metrics import Name, Quantity, metric_names

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

JOB = "s3_harness_task"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_VARIABLE = "RHI_SPIKE_POOL"
DEFAULT_POOL = "coursework"

# What the SDK script is given, besides the SDK's own variables.
PROMPT_VARIABLE = "RHI_S3_PROMPT"
MODEL_VARIABLE = "RHI_S3_MODEL"
SDK_FLAG = "--sdk"

HERE = Path(__file__).resolve().parent
TASK = HERE / "s3_task"
CHECK = "check_task.py"
# The task's files a harness is told not to change: the job puts them back before the check.
KEPT = (CHECK, "instance.json")

# The tools Claude Code may use without asking: it runs with no one to ask.
ALLOWED_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
# Seconds a harness may run before it is stopped, and the check.
HARNESS_LIMIT_SECONDS = 60 * 60
CHECK_LIMIT_SECONDS = 10 * 60
# The most model calls the SDK script makes, and the seconds one of its Python runs may take.
SDK_TURNS = 60
SDK_RUN_SECONDS = 5 * 60
SDK_TOOL_OUTPUT = 20_000

# The workspace scrapes every 15 seconds, and holds a scrape some time after it.
SCRAPE_SECONDS = s5.SCRAPE_SECONDS
SETTLE_SECONDS = s5.SETTLE_SECONDS
POLL_SECONDS = 5
READY_WAIT_SECONDS = 40 * 60
WAKE_EVERY_SECONDS = 60

# The image step: the file, its side in pixels, and the colours it may be filled with.
IMAGE_FILE = "figure.png"
IMAGE_SIDE = 64
COLOURS: dict[str, tuple[int, int, int]] = {
    "red": (220, 30, 30),
    "green": (30, 160, 60),
    "blue": (30, 60, 220),
    "yellow": (240, 220, 30),
}

# Compaction: files of text, each well inside what Claude Code reads at once, together more than
# its default window of 200,000 tokens.
FILLER_FILES = 24
FILLER_LINES = 800
FILLER_WORDS_A_LINE = 12
_FILLER = (
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her", "was",
    "one", "our", "out", "day", "get", "has", "him", "his", "how", "man", "new", "now", "old",
)  # fmt: skip

_CAUSE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")
MESSAGES_ROUTE = "/v1/messages"


class Line(StrEnum):
    """The kinds of line the job writes."""

    JOB = "job"
    VERSIONS = "versions"
    HARNESS_RUN = "harness_run"
    REFUSED_REQUEST = "refused_request"
    IMAGE_STEP = "image_step"
    COMPACTION = "compaction"
    REPLAY = "replay"
    ENGINE_COUNTERS = "engine_counters"
    STEP_FAILED = "step_failed"


class Harness(StrEnum):
    """The harnesses given the task."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    SDK_SCRIPT = "sdk_script"


class Step(StrEnum):
    """The steps of the job that run a harness."""

    TASK = "task"
    IMAGE = "image"
    COMPACTION = "compaction"


# The inference route of each harness.
ROUTE_OF = {
    Harness.CLAUDE_CODE: MESSAGES_ROUTE,
    Harness.CODEX: "/v1/responses",
    Harness.SDK_SCRIPT: "/v1/chat/completions",
}
# Where each harness's version is, in what `harnesses.versions` gives.
VERSION_OF = {
    Harness.CLAUDE_CODE: harnesses.CLAUDE_CODE.name,
    Harness.CODEX: harnesses.CODEX.name,
    Harness.SDK_SCRIPT: "openai_sdk",
}
# The home `rhi login` writes for each harness (`s2_client_hold.write_home`).
HOME_OF = {
    Harness.CLAUDE_CODE: s2.Client.CLAUDE_CODE,
    Harness.CODEX: s2.Client.CODEX_PROFILE,
    Harness.SDK_SCRIPT: s2.Client.OPENAI_SDK,
}


# The lines.


def utc(seconds: float) -> str:
    """A time in seconds since the epoch, as a UTC ISO time to the millisecond."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_or_none(seconds: float | None) -> str | None:
    return None if seconds is None else utc(seconds)


class Lines:
    """Writes each line as one JSON object with its UTC time and its kind, and refuses one that
    would hold any of `keys`."""

    def __init__(
        self, keys: Iterable[str], *, wall: Callable[[], float] = time.time, out: Callable[[str], None] = print
    ) -> None:
        self._keys = tuple(key for key in keys if key)
        self.wall, self.out = wall, out

    def write(self, kind: Line, **fields: Any) -> None:
        """Writes one line. ValueError, with nothing written, for one that would hold a key."""
        line = json.dumps({"at": utc(self.wall()), "kind": kind.value, **fields}, separators=(",", ":"))
        if any(key in line for key in self._keys):
            raise ValueError("the line would hold a key: it is not written")
        self.out(line)


def safe_cause(cause: str | None) -> str | None:
    """A refusal's cause when it is a name, as the gateway's and the engine's are; `other` otherwise."""
    if cause is None:
        return None
    return cause if _CAUSE.fullmatch(cause) else "other"


# The harnesses, as processes.


@dataclass(frozen=True)
class Command:
    """One run of a harness: its command, environment and folder, the forwarder's address it was
    given, and the seconds it may take."""

    harness: Harness
    argv: list[str]
    environment: dict[str, str]
    folder: Path
    endpoint: str
    limit: float


@dataclass
class Ran:
    """What a harness's run left: its exit status (`None` when it was stopped), and each line it
    printed with the time it arrived."""

    exit_code: int | None
    output: list[tuple[float, str]] = field(default_factory=list)
    stopped: bool = False


type Launch = Callable[[Command], Ran]


def launch_process(command: Command, wall: Callable[[], float] = time.time) -> Ran:
    """Runs the harness as a process, its output read line by line as it comes; stops it at its limit."""
    process = subprocess.Popen(
        command.argv,
        env=command.environment,
        cwd=command.folder,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    lines: list[tuple[float, str]] = []

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append((wall(), line.rstrip("\n")))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    stopped = False
    try:
        process.wait(timeout=command.limit)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        stopped = True
    reader.join(timeout=10)
    return Ran(None if stopped else process.returncode, list(lines), stopped)


def environment_of(home: Path) -> dict[str, str]:
    """What every harness runs with: its home, the `rhi` command and the job's Python first on its
    `PATH`, and the job's import path, so that `rhi token` and the task's check import what they need."""
    return {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(home / "bin"), str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry),
        "LANG": "C.UTF-8",
        # `rhi token` reads the key file the home holds, not a keychain of the job's machine.
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
    }


def command_of(
    harness: Harness,
    *,
    home: Path,
    folder: Path,
    endpoint: str,
    model: str,
    key: str,
    prompt: str,
    binaries: Mapping[str, Path],
) -> Command:
    """The command that gives `prompt` to `harness` in `folder`, through the forwarder at `endpoint`."""
    environment = environment_of(home)
    if harness is Harness.CLAUDE_CODE:
        environment |= {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "DISABLE_AUTOUPDATER": "1"}
        argv = [
            str(binaries[harnesses.CLAUDE_CODE.name]),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            ",".join(ALLOWED_TOOLS),
        ]
    elif harness is Harness.CODEX:
        environment["CODEX_HOME"] = str(home / ".codex")
        argv = [
            str(binaries[harnesses.CODEX.name]),
            "exec",
            "--profile",
            login_files.NAME,
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            prompt,
        ]
    else:
        environment |= {
            "OPENAI_API_KEY": key,
            "OPENAI_BASE_URL": f"{endpoint}/v1",
            PROMPT_VARIABLE: prompt,
            MODEL_VARIABLE: model,
        }
        argv = [sys.executable, str(Path(__file__).resolve()), SDK_FLAG]
    return Command(harness, argv, environment, folder, endpoint, HARNESS_LIMIT_SECONDS)


# What a run left.


@dataclass
class Session:
    """One run of a harness through its forwarder: when it started and ended, what the process left,
    and every exchange the forwarder saw."""

    harness: Harness
    step: Step
    started_at: float
    ended_at: float
    ran: Ran
    exchanges: list[s2.Exchange]

    def events(self) -> list[tuple[float, dict[str, Any]]]:
        """The lines of the output that are JSON objects, with the time each arrived: Claude Code's events."""
        found = []
        for at, line in self.ran.output:
            try:
                held = json.loads(line)
            except ValueError:
                continue
            if isinstance(held, dict):
                found.append((at, held))
        return found

    def result(self) -> dict[str, Any]:
        """Claude Code's last `result` event; empty when it gave none."""
        results = [event for _, event in self.events() if event.get("type") == "result"]
        return results[-1] if results else {}


def carries(exchange: s2.Exchange, marker: str) -> bool:
    return marker.encode() in exchange.body


def turns_of(session: Session, marker: str) -> int | None:
    """The turns the harness took: Claude Code's own count of its main loop's turns; for the others,
    their requests on their inference route that carry the run's marker."""
    if session.harness is Harness.CLAUDE_CODE:
        turns = session.result().get("num_turns")
        return turns if isinstance(turns, int) and not isinstance(turns, bool) else None
    route = ROUTE_OF[session.harness]
    return sum(1 for one in session.exchanges if one.method == "POST" and one.route == route and carries(one, marker))


def refusals_of(session: Session) -> list[dict[str, Any]]:
    """Each request of the run the gateway or the engine refused, whatever its kind, as its line's fields."""
    found = []
    for exchange in session.exchanges:
        if exchange.status is None or not s2.refused(exchange):
            continue
        body = exchange.json() or {}
        found.append(
            {
                "harness": session.harness.value,
                "step": session.step.value,
                "sent_at": utc(exchange.sent_at),
                "route": exchange.route if exchange.route.startswith("/v1/") else "other",
                "status": exchange.status,
                "cause": safe_cause(exchange.cause),
                "streamed": exchange.streams(),
                "declares_tools": bool(body.get("tools")),
            }
        )
    return found


def _blocks(content: object) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def image_in_tool_result(exchange: s2.Exchange) -> bool:
    """Whether a Messages request carries an image inside a tool result."""
    messages = (exchange.json() or {}).get("messages")
    for message in messages if isinstance(messages, list) else []:
        for block in _blocks(message.get("content") if isinstance(message, dict) else None):
            if block.get("type") == "tool_result" and any(
                inner.get("type") == "image" for inner in _blocks(block.get("content"))
            ):
                return True
    return False


def image_step_of(session: Session, colour: str) -> dict[str, Any]:
    """What became of the image: the first request that carried it inside a tool result, how it was
    answered, and whether Claude Code's answer names the colour."""
    carrying = [one for one in session.exchanges if one.route == MESSAGES_ROUTE and image_in_tool_result(one)]
    first = carrying[0] if carrying else None
    answer = session.result().get("result")
    return {
        "colour": colour,
        "image_in_tool_result": first is not None,
        "sent_at": None if first is None else utc(first.sent_at),
        "status": None if first is None else first.status,
        "cause": None if first is None else safe_cause(first.cause),
        "accepted": first is not None and first.completed and not s2.refused(first),
        "named": isinstance(answer, str) and colour in answer.lower(),
    }


def compaction_of(session: Session, word: str) -> dict[str, Any]:
    """Whether the session compacted, by Claude Code's own event, and whether a whole turn of its main
    loop followed it in the same session; and whether its answer was the word asked for."""
    events = session.events()
    index = next(
        (
            index
            for index, (_, event) in enumerate(events)
            if event.get("type") == "system" and event.get("subtype") == "compact_boundary"
        ),
        None,
    )
    boundary = after = None
    metadata: dict[str, Any] = {}
    if index is not None:
        boundary, event = events[index]
        held = event.get("compact_metadata")
        metadata = held if isinstance(held, dict) else {}
        after = next((at for at, later in events[index + 1 :] if later.get("type") == "assistant"), None)
    trigger = metadata.get("trigger")
    pre_tokens = metadata.get("pre_tokens")
    answer = session.result().get("result")
    return {
        "compacted": boundary is not None,
        "compacted_at": utc_or_none(boundary),
        "trigger": trigger if trigger in ("auto", "manual") else None,
        "tokens_before": pre_tokens if isinstance(pre_tokens, int) and not isinstance(pre_tokens, bool) else None,
        "next_turn_completed": after is not None,
        "next_turn_at": utc_or_none(after),
        "answered": isinstance(answer, str) and word in answer,
    }


def recorded_session(exchanges: Sequence[s2.Exchange]) -> loadgen.RecordedSession:
    """The Claude Code session as it was sent: every Messages request answered whole, side queries
    included, in order, each with the pause from the end of the one before."""
    kept = [
        one
        for one in exchanges
        if one.method == "POST" and one.route == MESSAGES_ROUTE and one.status == 200 and not s2.refused(one)
    ]
    found, before = [], None
    for one in kept:
        pause = 0.0 if before is None or before.ended_at is None else max(0.0, one.sent_at - before.ended_at)
        found.append(loadgen.Exchange(pause, loadgen.Request(one.method, one.path, dict(one.headers), one.body)))
        before = one
    return loadgen.RecordedSession(tuple(found))


# The files the job writes.


def png(colour: tuple[int, int, int], side: int = IMAGE_SIDE) -> bytes:
    """A square PNG image filled with one colour."""
    raw = b"".join(b"\x00" + bytes(colour) * side for _ in range(side))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def filler_name(number: int) -> str:
    return f"part-{number:02d}.txt"


def write_filler(folder: Path, word: str, rng: random.Random) -> None:
    """The files the compaction step reads: text of common words, the last file ending with `word`."""
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, FILLER_FILES + 1):
        lines = [" ".join(rng.choices(_FILLER, k=FILLER_WORDS_A_LINE)) for _ in range(FILLER_LINES)]
        if number == FILLER_FILES:
            lines.append(f"The last word is {word}")
        (folder / filler_name(number)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def task_prompt(marker: str) -> str:
    return f"Do the task that TASK.md in this folder describes. Run reference: {marker}"


def image_prompt(marker: str) -> str:
    return (
        f"The file {IMAGE_FILE} in this folder is an image. Open it with your Read tool and tell me which "
        f"colour fills it. Answer with the name of the colour only. Run reference: {marker}"
    )


def compaction_prompt(marker: str) -> str:
    return (
        f"Read the files {filler_name(1)} to {filler_name(FILLER_FILES)} in this folder with your Read tool, "
        "one at a time and in order, each one whole. When you have read them all, answer with the last "
        f"word of {filler_name(FILLER_FILES)} and nothing else. Run reference: {marker}"
    )


def run_check(folder: Path) -> int | None:
    """The task's check, run in `folder` with the job's Python: its exit status, `None` when it ran too long."""
    try:
        return subprocess.run(
            [sys.executable, CHECK], cwd=folder, capture_output=True, timeout=CHECK_LIMIT_SECONDS, check=False
        ).returncode
    except subprocess.TimeoutExpired:
        return None


def restore(folder: Path) -> list[str]:
    """Puts back the task's files a harness is told not to change; the names of those it had changed."""
    changed = []
    for name in KEPT:
        original, left = TASK / name, folder / name
        if not left.is_file() or left.read_bytes() != original.read_bytes():
            changed.append(name)
            shutil.copyfile(original, left)
    return changed


# The workspace.


class Reader(Protocol):
    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]: ...


class Replay(Protocol):
    def __call__(
        self, load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int
    ) -> Awaitable[list[loadgen.Record]]: ...


def counter_query(name: Name, pool: str) -> str:
    """The query of a counter of the per-engine table on the pool, summed by instance."""
    suffix = "_total" if name.kind is MetricKind.COUNTER else ""
    matchers = [f'pool="{pool}"', *(f'{label}="{value}"' for label, value in name.labels.items())]
    return f"sum by (instance) ({name}{suffix}{{{','.join(matchers)}}})"


def install_harnesses(folder: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """The pinned harnesses installed into `folder`, and the version of each harness and SDK."""
    binaries = harnesses.install(folder)
    return binaries, harnesses.versions(binaries)


# One run of the job.


class Job:
    """The job: the task given to each harness against the pool `pool` (served by `engine`), through
    the gateway at `endpoint`, as the canary students of `keys`; the pool woken through `gateway`,
    the workspace read through `workspace`; every line written to `lines`."""

    def __init__(
        self,
        *,
        pool: str,
        engine: str,
        endpoint: str,
        keys: Sequence[str],
        gateway: Any,
        workspace: Reader,
        lines: Lines,
        work: Path,
        install: Callable[[Path], tuple[Mapping[str, Path], Mapping[str, str]]] = install_harnesses,
        launch: Launch = launch_process,
        check: Callable[[Path], int | None] = run_check,
        replay: Replay = loadgen.replay,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if len(keys) < len(Harness) + 1:
            raise ValueError("spike S3 needs a canary student for each harness and one for the replay")
        self.names = metric_names(engine)
        self.pool, self.engine, self.endpoint, self.keys = pool, engine, endpoint, tuple(keys)
        self.gateway, self.workspace, self.lines, self.work = gateway, workspace, lines, work
        self.install, self.launch, self.check, self.replay = install, launch, check, replay
        self.wall, self.sleep = wall, sleep
        self.rng = rng or random.Random(secrets.randbits(64))
        self.binaries: Mapping[str, Path] = {}
        self.versions: Mapping[str, str] = {}
        # Each instance's name in the lines: its order of appearance, never its own.
        self._instances: dict[str, str] = {}
        self.failures = 0

    def key_of(self, harness: Harness) -> str:
        return self.keys[list(Harness).index(harness)]

    def _marker(self) -> str:
        return f"rhi-s3-{self.rng.getrandbits(32):08x}"

    def _wait_until(self, moment: float) -> None:
        while (left := moment - self.wall()) > 0:
            self.sleep(left)

    def ready(self) -> None:
        """Wakes the pool and waits until it is ready, so that no harness meets a cold start."""
        started = woke = self.wall()
        if self.gateway.state() == s5.READY:
            return
        self.gateway.wake()
        while self.gateway.state() != s5.READY:
            if self.wall() - started >= READY_WAIT_SECONDS:
                raise TimeoutError("the pool was not ready in time")
            if self.wall() - woke >= WAKE_EVERY_SECONDS:
                self.gateway.wake()
                woke = self.wall()
            self.sleep(POLL_SECONDS)

    def build_in(self, start: float, end: float) -> str | None:
        """The one build the pool's lifecycle agents reported from `start` to `end`, read once the
        workspace holds the scrape after it; `None` for none, or two."""
        after = end + SCRAPE_SECONDS
        self._wait_until(end + SETTLE_SECONDS)
        step = s5.fine_step(after - start)
        release = self.workspace.read_series(
            s5.release_query(self.pool), start=s5.grid_start(start, after, step), end=after, step=step
        )
        try:
            return s5.build_of(s5.in_service_of(release, start, end))
        except ValueError:
            return None

    def session(self, harness: Harness, step: Step, folder: Path, prompt: str) -> Session:
        """Runs `harness` once on `folder` with `prompt`, through a forwarder of its own."""
        home = Path(tempfile.mkdtemp(prefix=f"{harness}-{step}-", dir=self.work))
        key = self.key_of(harness)
        with s2.Forwarder(self.endpoint, self.pool, self.wall) as forwarder:
            s2.write_home(home, HOME_OF[harness], forwarder.url, self.pool, key)
            command = command_of(
                harness,
                home=home,
                folder=folder,
                endpoint=forwarder.url,
                model=self.pool,
                key=key,
                prompt=prompt,
                binaries=self.binaries,
            )
            started = self.wall()
            ran = self.launch(command)
            ended = self.wall()
            exchanges = forwarder.recorded()
        return Session(harness, step, started, ended, ran, exchanges)

    def _refusals(self, session: Session) -> None:
        for fields in refusals_of(session):
            self.lines.write(Line.REFUSED_REQUEST, **fields)

    def task(self, harness: Harness) -> Session:
        """The task given to `harness` in a copy of its folder, then its check on what it left."""
        folder = self.work / f"task-{harness}"
        shutil.copytree(TASK, folder)
        marker = self._marker()
        session = self.session(harness, Step.TASK, folder, task_prompt(marker))
        changed = restore(folder)
        checked = self.check(folder)
        build = self.build_in(session.started_at, session.ended_at)
        self.lines.write(
            Line.HARNESS_RUN,
            harness=harness.value,
            version=self.versions.get(VERSION_OF[harness]),
            build=build,
            started_at=utc(session.started_at),
            ended_at=utc(session.ended_at),
            seconds=round(session.ended_at - session.started_at, 1),
            turns=turns_of(session, marker),
            exit_status=session.ran.exit_code,
            stopped=session.ran.stopped,
            check_exit_status=checked,
            changed=changed,
            requests=len(session.exchanges),
        )
        if harness is Harness.CLAUDE_CODE:
            self._refusals(session)
        return session

    def image_step(self, folder: Path) -> None:
        """Claude Code asked what an image it opens with a tool shows, in the folder it left."""
        colour = self.rng.choice(sorted(COLOURS))
        (folder / IMAGE_FILE).write_bytes(png(COLOURS[colour]))
        session = self.session(Harness.CLAUDE_CODE, Step.IMAGE, folder, image_prompt(self._marker()))
        self.lines.write(
            Line.IMAGE_STEP,
            harness=Harness.CLAUDE_CODE.value,
            version=self.versions.get(VERSION_OF[Harness.CLAUDE_CODE]),
            started_at=utc(session.started_at),
            ended_at=utc(session.ended_at),
            exit_status=session.ran.exit_code,
            **image_step_of(session, colour),
        )
        self._refusals(session)

    def compaction(self) -> None:
        """Claude Code driven past its window, in a session of its own."""
        folder = self.work / "compaction"
        word = f"rhi{self.rng.getrandbits(24):06x}"
        write_filler(folder, word, self.rng)
        session = self.session(Harness.CLAUDE_CODE, Step.COMPACTION, folder, compaction_prompt(self._marker()))
        self.lines.write(
            Line.COMPACTION,
            harness=Harness.CLAUDE_CODE.value,
            version=self.versions.get(VERSION_OF[Harness.CLAUDE_CODE]),
            started_at=utc(session.started_at),
            ended_at=utc(session.ended_at),
            exit_status=session.ran.exit_code,
            stopped=session.ran.stopped,
            **compaction_of(session, word),
        )
        self._refusals(session)

    def counters(self, point: str, moment: float) -> None:
        """The engine's input and cached input token counters, per instance, as they stood at `moment`."""
        quantities = {"input_tokens": Quantity.INPUT_TOKENS, "cached_input_tokens": Quantity.CACHED_INPUT_TOKENS}
        values: dict[str, dict[str, float]] = {}
        for field_name, quantity in quantities.items():
            query = counter_query(self.names[quantity], self.pool)
            for series in self.workspace.read_series(query, start=moment, end=moment, step=SCRAPE_SECONDS):
                instance, held = series.labels.get("instance", ""), series.values()
                if not instance or not held:
                    continue
                label = self._instances.setdefault(instance, f"instance-{len(self._instances) + 1}")
                values.setdefault(label, {})[field_name] = held[-1]
        self.lines.write(Line.ENGINE_COUNTERS, point=point, counters_at=utc(moment), instances=values)

    def replayed(self, task: Session) -> None:
        """The task's Claude Code session replayed through the pool as a student that has sent nothing
        yet, with the engine's counters for the moment before it and for the scrape after its end."""
        session = recorded_session(task.exchanges)
        start = self.wall()
        self.counters("start", start)
        records = asyncio.run(self._send(session))
        end = self.wall()
        self.lines.write(
            Line.REPLAY,
            started_at=utc(start),
            ended_at=utc(end),
            requests=len(session.exchanges),
            sent=len(records),
            answered=sum(1 for record in records if record.status == 200),
            input_tokens=sum(record.input_tokens or 0 for record in records),
            cached_input_tokens=sum(record.cached_input_tokens or 0 for record in records),
        )
        # The scrape after the replay's end holds its last answers: read once the workspace holds it.
        self._wait_until(end + SETTLE_SECONDS)
        self.counters("end", end + SCRAPE_SECONDS)

    async def _send(self, session: loadgen.RecordedSession) -> list[loadgen.Record]:
        return await self.replay(loadgen.Load((session,)), [self.keys[len(Harness)]], self.endpoint, at_once=1)

    def _step[Result](self, name: str, action: Callable[[], Result]) -> Result | None:
        try:
            return action()
        except Exception as error:  # noqa: BLE001 - a step that fails is recorded, and the next runs
            self.failures += 1
            self.lines.write(Line.STEP_FAILED, step=name, error=type(error).__name__)
            return None

    def run(self) -> int:
        """Every step, in order; the number of steps that failed."""
        self.lines.write(Line.JOB, pool=self.pool, engine=self.engine)
        self.binaries, self.versions = self.install(self.work / "harnesses")
        self.lines.write(Line.VERSIONS, versions=dict(self.versions))
        self.ready()
        claude = self._step("task_claude_code", lambda: self.task(Harness.CLAUDE_CODE))
        for harness in (Harness.CODEX, Harness.SDK_SCRIPT):
            self._step(f"task_{harness}", lambda harness=harness: self.task(harness))
        self._step("image", lambda: self.image_step(self.work / f"task-{Harness.CLAUDE_CODE}"))
        self._step("compaction", self.compaction)
        if claude is not None:
            self._step("replay", lambda: self.replayed(claude))
        return self.failures


# The SDK script.

SDK_TOOLS: list[Any] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "The files of the working folder.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "The text of a file of the working folder.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Writes a file of the working folder, replacing its text.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Runs a Python file of the working folder; its exit status and what it printed.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
]


def use_tool(folder: Path, name: str, arguments: str) -> str:
    """What one of the SDK script's tools gives back, as text for the model."""
    try:
        given = json.loads(arguments or "{}")
        if name == "list_files":
            return "\n".join(sorted(one.relative_to(folder).as_posix() for one in folder.rglob("*") if one.is_file()))
        path = (folder / str(given["path"])).resolve()
        if not path.is_relative_to(folder.resolve()):
            return "refused: the path is outside the working folder"
        if name == "read_file":
            return path.read_text(encoding="utf-8")
        if name == "write_file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(given["content"]), encoding="utf-8")
            return "written"
        if name == "run_python":
            ran = subprocess.run(
                [sys.executable, str(path)],
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=SDK_RUN_SECONDS,
                check=False,
            )
            return f"exit status {ran.returncode}\n{(ran.stdout + ran.stderr)[-SDK_TOOL_OUTPUT:]}"
        return f"no tool is named {name}"
    except Exception as error:  # noqa: BLE001 - what went wrong is told to the model, as a tool's error
        return f"error: {type(error).__name__}"


def sdk_script(environ: Mapping[str, str], folder: Path) -> int:
    """The SDK script: Chat Completions calls with its tools, until the model answers without a tool
    call or `SDK_TURNS` calls are made. The SDK reads its key and address from its own variables."""
    client = openai.OpenAI()
    model = environ[MODEL_VARIABLE]
    messages: list[Any] = [{"role": "user", "content": environ[PROMPT_VARIABLE]}]
    try:
        for _ in range(SDK_TURNS):
            message = client.chat.completions.create(model=model, messages=messages, tools=SDK_TOOLS).choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            calls = [
                call for call in message.tool_calls or [] if isinstance(call, ChatCompletionMessageFunctionToolCall)
            ]
            if not calls:
                print(message.content or "")
                return 0
            for call in calls:
                result = use_tool(folder, call.function.name, call.function.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    except Exception as error:  # noqa: BLE001 - what the SDK raised is what a student's program shows
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 1


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Runs the job with its environment and credentials; exits 1 when a step failed."""
    endpoint = s4_weights.gateway_address(environ.get(ENDPOINT_VARIABLE, ""))
    name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
    pools = [pool for pool in config.read_ssm().pools if pool.name == name]
    if len(pools) != 1:
        raise ValueError("the deployed configuration has no pool of that name")
    (pool,) = pools
    keys = common.canary_keys()
    session = boto3.Session()
    workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
    lines = Lines((*keys.students, keys.spent))
    with tempfile.TemporaryDirectory(prefix="rhi-s3-") as work:
        failures = Job(
            pool=name,
            engine=pool.engine,
            endpoint=endpoint,
            keys=keys.students,
            gateway=s4_weights.Gateway(endpoint, name, keys.students[0]),
            workspace=workspace,
            lines=lines,
            work=Path(work),
        ).run()
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:2] == [SDK_FLAG]:
        sys.exit(sdk_script(os.environ, Path.cwd()))
    sys.exit(main())
