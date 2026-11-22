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
| `step_failed` | A step that raised, or a reading of the workspace that failed: its name and the type of the error |
| `job_failed` | An error that stopped the job: the type of the error |

**The turns.** Claude Code's are the turns its own result counts (`num_turns`): its main loop, with
its side queries and token counts left out; when it gave no result, because it was stopped, the
turns of its main loop it printed. Codex and the SDK script send no side query, so theirs are the
requests on their inference route that carry the run's marker, the random word of the prompt, a
request sent again with the same body (a retry) counted once.

**A refused request** is one the forwarder saw answered with a status other than 200, or with a
stream that an error event ended (`s2_client_hold.refused`): the gateway answers a held stream 200
at once, and the engine may refuse it afterwards. A request that got no answer at all is not one:
nothing shows that either refused it. Its cause is what the error names, when that is a name.

**The image step.** In the folder Claude Code left, the job writes `figure.png`, an image filled
with one colour it picks at random, and asks Claude Code to open it with its tools and say which
colour fills it. The request that carries the image inside a tool result is the one recorded: the
first such request that was accepted (a retry of a refused one among them), or the first sent when
none was. Claude Code named the colour when its answer names it, as a word, and no other colour.

**Compaction.** In a folder of its own, the job writes `FILLER_FILES` files of text, a quarter more
than the window the pinned Claude Code reports it works with (`CLIENT_WINDOW`), and asks Claude
Code to read them all, one at a time, then give the last word of the last. Claude Code says in its
own output that it compacted (an event `compact_boundary`), and gives every turn of its main loop
as an `assistant` event: the session completed its next turn when the first such event after the
compaction is no error, and the session went on after it or did not end in an error. The line also
holds what shows whether the window was reached at all: the window Claude Code reported, the
largest input it reported, the turns of its main loop the gateway or the engine refused (a token
count or a side query is in the per-request lines, not here), and among them those refused with a
cause that names the model's context once the session had grown (`window_refusals_of`). When it
neither compacted nor had such a turn refused, the window was not shown to be reached: a refusal of
another kind, a budget's on the first request among them, shows nothing of the window. The gateway
answers `request_refused` for a body above its limits and for any other body its filter refuses,
`engine_error` both for an engine that refused a prompt too long and for one it lost, and a body
too large (413) counts bytes, not tokens, so a grown turn refused so is counted apart
(`undetermined_refusals`), with the status of every refused turn.

**The replay.** The Claude Code session of the task, every request the forwarder saw answered,
side queries and token counts included, with the pauses between them, is replayed through the gateway with the load
generator (`loadgen.replay`) as a canary student that has sent nothing in the job, so its cache
starts empty. The engine's input and cached input token counters, by the names of the per-engine
table (`rhi_common.engine_metrics`), are read at the replay's own start, which comes a whole
scrape interval after the job's earlier traffic, so that the last scrape before it holds all of
that traffic and none of the replay; the end is read for a scrape after the replay's end. Both are
read once the workspace holds that scrape, so that the counters hold the replay's last answers. Each instance is named by its order of appearance in the job, never by its own name.

**The build** is the one the pool's lifecycle agents report (`rhi_lifecycle.serving.RELEASE_METRIC`)
over the run, read once the workspace holds the scrape after it (`s5_capacity.in_service_of`); it is
`null` when they reported none, or two, or when the workspace could not be read: the run's line is
written all the same, and a line `step_failed` names the reading that failed.

**No key and no text from AWS.** Every line goes through `Lines`, which refuses one that holds a
key of the job. An error is recorded by the type's name, never its message, and a cause only when
it is one of the names the gateway and the engines publish (`KNOWN_CAUSES`). An error that stops
the job, before or between its steps, is a line `job_failed` with its type's name, and the job
exits 1: no traceback reaches the log. The task's check, which runs the model a harness wrote,
runs with none of the job's credentials, nor the variables that say where CodeBuild keeps them. The answers of the harnesses are read for what they hold, never copied.

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
import contextlib
import json
import math
import os
import random
import re
import secrets
import shutil
import signal
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
from rhi_common.causes import Cause
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
# Seconds the job waits for what a harness left running to be gone, once it is killed.
GROUP_STOP_SECONDS = 5
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

# Compaction: files of text, each well inside what Claude Code reads at once, together a quarter
# more than the window the pinned Claude Code works with by default against the gateway, which it
# reports in its result (`modelUsage`, `contextWindow`). A line of 12 words is some 16 tokens, at
# most. The engine's own window is smaller: a session that does not compact first meets the
# engine's refusal as it grows, which is recorded as well.
CLIENT_WINDOW = 1_000_000
FILLER_LINES = 800
FILLER_WORDS_A_LINE = 12
FILLER_FILES = math.ceil(1.25 * CLIENT_WINDOW / (FILLER_LINES * (FILLER_WORDS_A_LINE + 4)))
_FILLER = (
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her", "was",
    "one", "our", "out", "day", "get", "has", "him", "his", "how", "man", "new", "now", "old",
)  # fmt: skip

# The causes a refusal may name in a line, as they are published: the gateway's own (section 6.2,
# step 8), and the error types of the Messages and OpenAI formats an engine answers with. Any other
# is `other`: what an error names beyond these may be text the service made.
KNOWN_CAUSES = frozenset(
    {
        *(cause.value for cause in Cause),
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "request_too_large",
        "rate_limit_error",
        "api_error",
        "overloaded_error",
        "timeout_error",
        "BadRequestError",
        "NotFoundError",
        "InternalServerError",
        "ServiceUnavailableError",
        "context_length_exceeded",
        "rate_limit_exceeded",
        "server_error",
    }
)
# The causes that name the model's context and nothing else, should the engine's error pass through.
LENGTH_CAUSES = frozenset({"context_length_exceeded"})
# A refusal whose cause a length may explain, among other things, so that it leaves undetermined
# whether the window was reached: the gateway answers `request_refused` (400) for a body above its
# limits and for every other body its filter refuses (a media reference that is not inline data, a
# field outside its allow-list); `engine_error` (502) both for an engine that refused a prompt
# longer than its context and for an engine it lost the connection to; the formats' own validation
# errors name no more than a request that was not valid; and a body too large to take (413, or
# `request_too_large`) counts bytes on the wire, which an image or a field can fill as well as
# tokens can.
UNDETERMINED_CAUSES = frozenset(
    {
        Cause.REQUEST_REFUSED.value,
        Cause.ENGINE_ERROR.value,
        "invalid_request_error",
        "BadRequestError",
        "request_too_large",
    }
)
UNDETERMINED_STATUSES = frozenset({400, 413, 502})
MESSAGES_ROUTE = "/v1/messages"
# The Messages routes a recorded session replays: the turns and the token counts.
REPLAYED_ROUTES = (MESSAGES_ROUTE, "/v1/messages/count_tokens")
# The variables the task's check runs with: none of the job's credentials, nor where to find them.
CHECK_VARIABLES = ("LANG", "LC_ALL", "TZ")


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
    JOB_FAILED = "job_failed"


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

    def write(self, kind: StrEnum, **fields: Any) -> None:
        """Writes one line. ValueError, with nothing written, for one that would hold a key."""
        line = json.dumps({"at": utc(self.wall()), "kind": kind.value, **fields}, separators=(",", ":"))
        if any(key in line for key in self._keys):
            raise ValueError("the line would hold a key: it is not written")
        self.out(line)


def safe_cause(cause: str | None) -> str | None:
    """A refusal's cause when it is one of `KNOWN_CAUSES`; `other` for any other."""
    if cause is None:
        return None
    return cause if cause in KNOWN_CAUSES else "other"


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


def stop_group(process: subprocess.Popen[str]) -> None:
    """Kills every process left in the process group `process` leads, reaps `process`, and waits, a
    while, until no other is left."""
    group = process.pid
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)
    process.wait()
    deadline = time.monotonic() + GROUP_STOP_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)


def launch_process(command: Command, wall: Callable[[], float] = time.time) -> Ran:
    """Runs the harness as a process, its output read line by line as it comes; stops it at its limit.
    The harness runs in a process group of its own, and whatever it left running in it, the tools
    it started among them, is stopped once it has ended or been stopped: nothing changes the
    folder after the run's end."""
    process = subprocess.Popen(
        command.argv,
        env=command.environment,
        cwd=command.folder,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
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
        stopped = True
    stop_group(process)
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


def _same(body: dict[str, Any] | None, raw: bytes) -> object:
    # What differs between two sends of one request is not the request: an SDK's metadata.
    if body is None:
        return raw
    return json.dumps({key: value for key, value in body.items() if key != "metadata"}, sort_keys=True)


def main_turns(session: Session) -> int:
    """The turns of Claude Code's main loop it printed whole: its `assistant` events outside a
    subagent, one a message, an error left out."""
    seen = set()
    for _, event in session.events():
        message = event.get("message")
        if (
            event.get("type") != "assistant"
            or event.get("parent_tool_use_id") is not None
            or event.get("error")
            or not isinstance(message, dict)
        ):
            continue
        seen.add(message.get("id") or len(seen))
    return len(seen)


def turns_of(session: Session, marker: str) -> int | None:
    """The turns the harness took: Claude Code's own count of its main loop's turns, or, when it
    gave no result (it was stopped), the turns of its main loop it printed whole; for the others,
    their requests on their inference route that carry the run's marker, a request sent again
    (a retry) counted once."""
    if session.harness is Harness.CLAUDE_CODE:
        turns = session.result().get("num_turns")
        if isinstance(turns, int) and not isinstance(turns, bool):
            return turns
        return main_turns(session)
    route = ROUTE_OF[session.harness]
    return len(
        {
            _same(one.json(), one.body)
            for one in session.exchanges
            if one.method == "POST" and one.route == route and carries(one, marker)
        }
    )


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


def accepted(exchange: s2.Exchange) -> bool:
    """Whether the request was answered whole, with no refusal."""
    return exchange.status == 200 and exchange.completed and not s2.refused(exchange)


def colours_named(answer: object) -> set[str]:
    """The colours of `COLOURS` an answer names, each as a word of its own."""
    if not isinstance(answer, str):
        return set()
    words = set(re.findall(r"[a-z]+", answer.lower()))
    return {colour for colour in COLOURS if colour in words}


def image_step_of(session: Session, colour: str) -> dict[str, Any]:
    """What became of the image: the request that carried it inside a tool result (the first that
    was accepted, a retry among them; the first sent when none was), how it was answered, and
    whether Claude Code's answer names the colour, and no other."""
    carrying = [one for one in session.exchanges if one.route == MESSAGES_ROUTE and image_in_tool_result(one)]
    chosen = next((one for one in carrying if accepted(one)), carrying[0] if carrying else None)
    return {
        "colour": colour,
        "image_in_tool_result": chosen is not None,
        "requests_carrying_image": len(carrying),
        "sent_at": None if chosen is None else utc(chosen.sent_at),
        "status": None if chosen is None else chosen.status,
        "cause": None if chosen is None else safe_cause(chosen.cause),
        "accepted": chosen is not None and accepted(chosen),
        "named": colours_named(session.result().get("result")) == {colour},
    }


def _main_assistant(event: dict[str, Any]) -> bool:
    return event.get("type") == "assistant" and event.get("parent_tool_use_id") is None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def client_window(session: Session) -> int | None:
    """The window Claude Code says it worked with, in its result: the largest of its models'."""
    usage = session.result().get("modelUsage")
    windows = [
        window
        for model in (usage.values() if isinstance(usage, dict) else [])
        if isinstance(model, dict) and (window := _count(model.get("contextWindow"))) is not None
    ]
    return max(windows, default=None)


def largest_input(session: Session) -> int | None:
    """The largest input a turn of Claude Code's main loop reported, cached or not."""
    found = []
    for _, event in session.events():
        message = event.get("message")
        usage = message.get("usage") if _main_assistant(event) and isinstance(message, dict) else None
        if isinstance(usage, dict):
            parts = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            found.append(sum(_count(usage.get(part)) or 0 for part in parts))
    return max(found, default=None)


def next_turn_completed(events: Sequence[tuple[float, dict[str, Any]]]) -> float | None:
    """When the first turn of the main loop among `events` completed; `None` when it did not: there
    was none, it was an error, or the session ended in an error with nothing after that turn."""
    turn = next((index for index, (_, event) in enumerate(events) if _main_assistant(event)), None)
    if turn is None or events[turn][1].get("error"):
        return None
    rest = [event for _, event in events[turn + 1 :]]
    continued = any(event.get("type") == "user" or _main_assistant(event) for event in rest)
    failed = any(event.get("type") == "result" and event.get("is_error") is not False for event in rest)
    return events[turn][0] if continued or not failed else None


def compaction_of(session: Session, word: str) -> dict[str, Any]:
    """Whether the session compacted, by Claude Code's own event, and whether the turn of its main
    loop after it completed; whether its answer was the word asked for; and what shows whether the
    window was reached at all: the window Claude Code worked with, the largest input it reported,
    and the requests of its main loop the gateway or the engine refused."""
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
        after = next_turn_completed(events[index + 1 :])
    trigger = metadata.get("trigger")
    answer = session.result().get("result")
    refused = [one for one in session.exchanges if main_turn(one) and s2.refused(one)]
    window_refusals = len(window_refusals_of(session))
    undetermined = len(window_refusals_of(session, _refused_undetermined))
    return {
        "compacted": boundary is not None,
        "compacted_at": utc_or_none(boundary),
        "trigger": trigger if trigger in ("auto", "manual") else None,
        "tokens_before": _count(metadata.get("pre_tokens")),
        "next_turn_completed": after is not None,
        "next_turn_at": utc_or_none(after),
        "answered": isinstance(answer, str) and word in re.findall(r"[a-z0-9]+", answer.lower()),
        "client_window": client_window(session),
        "largest_input_tokens": largest_input(session),
        "refused_turns": len(refused),
        "refused_turn_statuses": [one.status for one in refused],
        "window_refusals": window_refusals,
        # Grown turns refused with a cause that may be the window or something else: a body the
        # gateway's filter refused, a request the engine found invalid, a failure of the engine.
        "undetermined_refusals": undetermined,
        # Whether the session met its window: it compacted, or a turn was refused with a cause that
        # names a length once the session had grown. A refusal of another kind (a budget, a limit, a
        # cold pool, a generic refusal that may be a validation error or a lost engine) shows neither.
        "window_exercised": boundary is not None or window_refusals > 0,
    }


def window_refusals_of(
    session: Session, kind: Callable[[s2.Exchange], bool] | None = None
) -> list[s2.Exchange]:
    """The turns of the main loop refused as longer than the model's context (or, given `kind`, refused
    as it decides), each larger than every turn accepted before it: what a session refused at its
    window looks like. A refusal on the first turn, or of a kind no length causes (a budget, a limit,
    a cold pool, an engine error), is not one."""
    found, largest = [], None
    for one in session.exchanges:
        if not main_turn(one):
            continue
        if not s2.refused(one):
            largest = max(largest or 0, len(one.body))
        elif largest is not None and len(one.body) > largest and (kind or _refused_as_too_long)(one):
            found.append(one)
    return found


def main_turn(exchange: s2.Exchange) -> bool:
    """Whether a request is a turn of the main loop: a Messages request that declares tools, never
    a token count or a side query."""
    return exchange.route == MESSAGES_ROUTE and bool((exchange.json() or {}).get("tools"))


def _refused_as_too_long(exchange: s2.Exchange) -> bool:
    return exchange.cause in LENGTH_CAUSES


def _refused_undetermined(exchange: s2.Exchange) -> bool:
    if exchange.cause is not None:
        return exchange.cause in UNDETERMINED_CAUSES
    return exchange.status in UNDETERMINED_STATUSES


def recorded_session(exchanges: Sequence[s2.Exchange]) -> loadgen.RecordedSession:
    """The Claude Code session as it was sent: every Messages request answered, side queries and token
    counts included, in order, each with the pause from the end of the one before."""
    kept = [
        one
        for one in exchanges
        if one.method == "POST" and one.route in REPLAYED_ROUTES and one.status == 200 and not s2.refused(one)
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
    return f"part-{number:03d}.txt"


def write_filler(folder: Path, word: str, rng: random.Random) -> None:
    """The files the compaction step reads: text of common words, the last file ending with `word`."""
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, FILLER_FILES + 1):
        lines: list[str] = [" ".join(rng.choices(_FILLER, k=FILLER_WORDS_A_LINE)) for _ in range(FILLER_LINES)]
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


def check_environment(folder: Path) -> dict[str, str]:
    """What the task's check runs with: the job's Python first on its `PATH`, the folder as its home,
    and nothing of the job's own environment beyond `CHECK_VARIABLES`: no credential of the build's
    role, nor where to find one."""
    environment = {name: os.environ[name] for name in CHECK_VARIABLES if name in os.environ}
    return environment | {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "HOME": str(folder),
    }


def run_check(folder: Path) -> int | None:
    """The task's check, run in `folder` with the job's Python: its exit status, `None` when it ran too long."""
    try:
        return subprocess.run(
            [sys.executable, CHECK],
            cwd=folder,
            env=check_environment(folder),
            capture_output=True,
            timeout=CHECK_LIMIT_SECONDS,
            check=False,
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
        if harness is Harness.CLAUDE_CODE:
            self._refusals(session)
        try:
            build = self.build_in(session.started_at, session.ended_at)
        except Exception as error:  # noqa: BLE001 - the run's observations are kept, the failure recorded
            build = None
            self.failures += 1
            self.lines.write(Line.STEP_FAILED, step=f"build_{harness}", error=type(error).__name__)
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
        yet, with the engine's counters as they stood at its start and at the scrape after its end."""
        session = recorded_session(task.exchanges)
        # A whole scrape interval first, so that a scrape falls between all the job sent before and
        # the replay's start: the counters at the start hold the one and nothing of the other.
        self._wait_until(self.wall() + SCRAPE_SECONDS)
        start = self.wall()
        failure: Exception | None = None
        try:
            records = asyncio.run(self._send(session))
        except Exception as error:  # noqa: BLE001 - the counters of what ran are read, then it is raised
            failure, records = error, None
        end = self.wall()
        self.lines.write(
            Line.REPLAY,
            started_at=utc(start),
            ended_at=utc(end),
            requests=len(session.exchanges),
            # A replay the load generator gave up returns no records: what it sent is then unknown.
            sent=None if records is None else len(records),
            answered=None if records is None else sum(1 for record in records if record.status == 200),
            input_tokens=None if records is None else sum(record.input_tokens or 0 for record in records),
            cached_input_tokens=None
            if records is None
            else sum(record.cached_input_tokens or 0 for record in records),
            error=None if failure is None else type(failure).__name__,
        )
        # Both read once the workspace holds the scrape after the replay's end, which holds its last
        # answers; the start is the last scrape before the replay's first request. A replay that
        # failed part of the way still has both, over the interval that ran.
        self._wait_until(end + SETTLE_SECONDS)
        self.counters("start", start)
        self.counters("end", end + SCRAPE_SECONDS)
        if failure is not None:
            raise failure

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


def main(environ: Mapping[str, str] = os.environ, out: Callable[[str], None] = print) -> int:
    """Runs the job with its environment and credentials; exits 1 when a step failed, or when an
    error stopped the job: that error is a line `job_failed` with its type's name, never its text,
    which may be what AWS returned."""
    try:
        return run_job(environ, out)
    except Exception as error:  # noqa: BLE001 - the job's boundary: no traceback reaches the log
        Lines((), out=out).write(Line.JOB_FAILED, error=type(error).__name__)
        return 1


def run_job(environ: Mapping[str, str], out: Callable[[str], None]) -> int:
    """The job, from its environment and credentials; 1 when a step failed."""
    endpoint = s4_weights.gateway_address(environ.get(ENDPOINT_VARIABLE, ""))
    name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
    pools = [pool for pool in config.read_ssm().pools if pool.name == name]
    if len(pools) != 1:
        raise ValueError("the deployed configuration has no pool of that name")
    (pool,) = pools
    keys = common.canary_keys()
    session = boto3.Session()
    workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
    lines = Lines((*keys.students, keys.spent), out=out)
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
