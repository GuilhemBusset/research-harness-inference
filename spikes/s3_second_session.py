"""Spike S3, second part: a Claude Code session shaped like the course's second session (system
design, sections 4.2, 6.1, 6.2 and 12).

The course's second session runs research skill packs inside the harness: they load skills and add
tools in mid-session, read PDFs, run several agents at once and search the web (section 4.2,
evidence E25). This job runs one Claude Code session of that shape against a named pool and records
what each step did, with times. **It judges nothing**: whether each step works or has a documented
limit and workaround is read later, from the first real run's data, by an issue written then
(section 12). It posts no measurement event; what it observed is its log.

**The harness** is the Claude Code `spikes/harnesses.py` pins, with its default settings, as spike
S3's first part runs it (`s3_harness_task`): what `rhi login` writes, its key from `rhi token`, no
model, effort or window of its own, through a forwarder that sends every request to the gateway
with the pool's name as its model and records each exchange. It runs with no one to ask, so the
tools of the steps may be used without asking (`ALLOWED_TOOLS`).

**The session** is one conversation in one folder, a turn (`claude -p`) for each step, each turn
after the first resuming it by the id Claude Code gave (`--resume`):

| Step | What the turn does | Its observable result (`result_present`) |
| --- | --- | --- |
| `open` | Opens the session: no skill in the folder, no MCP server | Claude Code answered and gave the session's id |
| `skill` | The job puts the skill of `s3_second_session/skill/` in the folder's `.claude/skills/`, then asks for what only the skill says how to do | The skill's output: `field-note.txt` holds the skill's word and the run's reference |
| `echo` | Resumes with an MCP configuration that adds the server of `s3_second_session/echo_server.py`, whose tool echo Claude Code then announces mid-conversation, and asks the model to call echo with a random text | echo was called and returned the text, and the answer quotes all it returned |
| `pdf` | Asks the model to read a PDF the job generated (`s3_second_session/make_pdf.py`) and give its code word | An answer after the PDF: one of a turn that sent it as a document block or in which a tool returned its text (its random word) |
| `agents` | Part 1 of the task: two agents in parallel, then a figure | The figure's file, `figure.svg` |
| `search` | Part 2 of the task: a web search | The search's answer: the reply gives an address that Claude Code's web search returned |

The skill and the tool both arrive in the middle of the session, as a pack's do. Every turn from
`echo` on keeps the MCP configuration, so the tool is not removed again. Each step's line holds,
besides its result, what shows how the result came about: whether the skill was called, whether a
request declared echo, whether Claude Code sent the PDF as a document block, whether a tool returned
its text and whether the answer names the PDF's word, how many agents it started and how many in one message, and whether the web
search was called, failed, and returned addresses. An agent asked for counts as started only once a
subagent's message names its call or its call returned no error. A reply that says the search could
not be made is no search answer: only an address the search itself returned counts, its path as
written.

**What is recorded**, one JSON object a line on standard output, which the spike jobs' log group
keeps (`s3_harness_task.Lines`). Every line has `at`, its UTC time, and `kind`:

| Kind | What it holds |
| --- | --- |
| `job` | The pool and its engine |
| `versions` | The version of each harness and SDK |
| `step` | For each step: when it started and ended, Claude Code's exit status (and the type of the error, when it could not be launched), whether its result is there, what shows how, its requests and refused requests, and the build the pool served as its lifecycle agents report it |
| `refused_request` | Each request the gateway or the engine refused during a step: the step, its time, route and status, and its cause |
| `step_failed` | A step that raised, a turn Claude Code could not be launched for (its `step` line still follows), or a reading of the workspace that failed: its name and the type of the error |
| `job_failed` | An error that stopped the job: the type of the error, and for a job the pool's session does not carry, the reason |

A refused request, and the build, are read as in the first part (`s3_harness_task`): a status other
than 200 or a stream an error event ended; the one build the lifecycle agents reported over the
step, `null` for none or two.

**No key and no text from AWS.** Every line goes through `Lines`, which refuses one that holds a key
of the job. An error is recorded by its type's name, a cause only when it is a published name
(`s3_harness_task.safe_cause`), a tool's name only when it is one of `TOOLS_NAMED`. The answers of
the model are read for what they hold, never copied.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s3_second_session`):
`RHI_SPIKE_ENDPOINT`, `RHI_SPIKE_WORKSPACE_URL` and `RHI_SPIKE_POOL` (`coursework` unless given),
as for the first part, and as for it `RHI_SPIKE_INSTANCES` (`1` unless given),
`RHI_SPIKE_RUN_MINUTES`, without which the job does not start, and `RHI_SPIKE_POOL_STATE_TABLE`:
the job runs in a session the owner has booked, checks it before it sends anything
(`common.require_session`), and otherwise writes `job_failed` with the reason and exits 1. It needs
one canary student's key. The build the pool serves is the owner's
to set (the 8-bit, then the 4-bit build) before starting the job.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import boto3

import common
import forwarder
import harnesses
import s3_harness_task as s3
import s5_capacity as s5
from rhi import config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

JOB = "s3_second_session"

HERE = Path(__file__).resolve().parent
FILES = HERE / "s3_second_session"
SKILL = FILES / "skill"
ECHO_SERVER = FILES / "echo_server.py"
MAKE_PDF = FILES / "make_pdf.py"
TASK = FILES / "TASK.md"

# The skill: its name, the file it asks for, and the word it says to write there.
SKILL_NAME = "field-note"
SKILL_OUTPUT = "field-note.txt"
SKILL_WORD = "lanternfish"
# The MCP server, as the configuration names it, and its tool as Claude Code names it.
ECHO_SERVER_NAME = "rhi_echo"
ECHO_TOOL = f"mcp__{ECHO_SERVER_NAME}__echo"
PDF_FILE = "notes.pdf"
FIGURE_FILE = "figure.svg"
# Claude Code's tools that start an agent, by their names in its versions.
AGENT_TOOLS = frozenset({"Task", "Agent"})
WEB_SEARCH = "WebSearch"
# The tools Claude Code may use without asking: the first part's, and those of the steps.
ALLOWED_TOOLS = (*s3.ALLOWED_TOOLS, "Skill", *sorted(AGENT_TOOLS), WEB_SEARCH, "WebFetch", ECHO_TOOL)
# The tools a line may name: any other is `other`.
TOOLS_NAMED = frozenset({*ALLOWED_TOOLS, "TodoWrite", "NotebookEdit", "BashOutput", "KillShell"})
# The states of an MCP server Claude Code reports that a line may name: any other is `other`.
SERVER_STATES = frozenset({"connected", "failed", "pending", "needs-auth", "disabled"})
_SESSION_ID = re.compile(r"[0-9A-Za-z-]{1,100}")
_ADDRESS = re.compile(r"https?://[^\s\"'<>`)\]}]+")

TURN_LIMIT_SECONDS = s3.HARNESS_LIMIT_SECONDS
PDF_LIMIT_SECONDS = 60


class Line(StrEnum):
    """The kinds of line the job writes."""

    JOB = "job"
    VERSIONS = "versions"
    STEP = "step"
    REFUSED_REQUEST = "refused_request"
    STEP_FAILED = "step_failed"
    JOB_FAILED = "job_failed"


class Step(StrEnum):
    """The steps of the session, in their order."""

    OPEN = "open"
    SKILL = "skill"
    ECHO = "echo"
    PDF = "pdf"
    AGENTS = "agents"
    SEARCH = "search"


class NoSession(RuntimeError):
    """Claude Code gave no session id to resume."""


# The prompts.


def open_prompt(marker: str) -> str:
    return f"List the files in this folder, in one line. Run reference: {marker}"


def skill_prompt(marker: str) -> str:
    return f"Use the skill {SKILL_NAME} to write the field note of this run. Run reference: {marker}"


def echo_prompt(text: str, marker: str) -> str:
    return (
        f"Call the tool echo of the MCP server {ECHO_SERVER_NAME} with the text {text}, then answer with "
        f"exactly what it returned. Run reference: {marker}"
    )


def pdf_prompt(marker: str) -> str:
    return (
        f"Read the file {PDF_FILE} in this folder and answer with the code word it gives, and nothing else. "
        f"Run reference: {marker}"
    )


def task_prompt(part: int, marker: str) -> str:
    return f"Do part {part} of TASK.md in this folder, and only that part. Run reference: {marker}"


# What a turn left.


@dataclass
class Turn:
    """One turn of the session: its step, when it started and ended, what Claude Code left, the
    exchanges the forwarder saw during it, and the type of the error that stopped Claude Code from
    being launched, if one did."""

    step: Step
    started_at: float
    ended_at: float
    ran: s3.Ran
    exchanges: list[forwarder.Exchange]
    launch_error: str | None = None

    def events(self) -> list[dict[str, Any]]:
        """Claude Code's events: the lines of its output that are JSON objects."""
        found = []
        for _, line in self.ran.output:
            try:
                held = json.loads(line)
            except ValueError:
                continue
            if isinstance(held, dict):
                found.append(held)
        return found

    def result(self) -> dict[str, Any]:
        """Claude Code's last `result` event; empty when it gave none."""
        results = [event for event in self.events() if event.get("type") == "result"]
        return results[-1] if results else {}

    def answer(self) -> str | None:
        """The turn's answer: the text of a result that is no error, `None` without one."""
        result = self.result()
        text = result.get("result")
        return text if result.get("is_error") is False and isinstance(text, str) and text.strip() else None

    def session_id(self) -> str | None:
        """The session's id, as Claude Code last gave it."""
        found = [event.get("session_id") for event in self.events()]
        ids = [one for one in found if isinstance(one, str) and _SESSION_ID.fullmatch(one)]
        return ids[-1] if ids else None

    def tool_uses(self) -> list[tuple[str | None, dict[str, Any]]]:
        """Each tool call of the session's own loop, a subagent's left out, with the id of the message
        that made it."""
        found = []
        for event in self.events():
            message = event.get("message")
            if event.get("type") != "assistant" or event.get("parent_tool_use_id") is not None:
                continue
            if isinstance(message, dict):
                found.extend((message.get("id"), block) for block in _blocks(message.get("content")))
        return [(message, block) for message, block in found if block.get("type") == "tool_use"]

    def calls(self, name: str) -> list[dict[str, Any]]:
        return [block for _, block in self.tool_uses() if block.get("name") == name]

    def tool_results(self) -> dict[str, tuple[bool, str]]:
        """Each tool's result, by the id of its call: whether it is an error, and its text."""
        found = {}
        for event in self.events():
            message = event.get("message")
            if event.get("type") != "user" or not isinstance(message, dict):
                continue
            for block in _blocks(message.get("content")):
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    found[block["tool_use_id"]] = (block.get("is_error") is True, _text(block.get("content")))
        return found

    def results_of(self, name: str) -> list[tuple[bool, str]]:
        """The results of the calls of the tool `name`."""
        results = self.tool_results()
        return [results[call["id"]] for call in self.calls(name) if call.get("id") in results]

    def children(self) -> set[str]:
        """The ids of the tool calls a subagent's messages name as their parent: the agents that ran."""
        found = {event.get("parent_tool_use_id") for event in self.events()}
        return {one for one in found if isinstance(one, str)}


def _blocks(content: object) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in _blocks(content) if isinstance(block.get("text"), str))


def addresses(text: str | None) -> set[str]:
    """The web addresses `text` holds, without trailing punctuation or slash; their scheme and host in
    lower case, which name the same thing in any case, and the rest as written, which may not."""
    found = set()
    for one in _ADDRESS.findall(text or ""):
        try:
            parts = urlsplit(one.rstrip(".,;:!?'\"/"))
        except ValueError:  # not an address urlsplit can read, such as a cut bracketed host: none
            continue
        found.add(urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower())))
    return found


def refused_requests(turn: Turn) -> list[dict[str, Any]]:
    """Each request of the turn the gateway or the engine refused, as its line's fields."""
    return [
        {
            "step": turn.step.value,
            "sent_at": s3.utc(one.sent_at),
            "route": one.route if one.route.startswith("/v1/") else "other",
            "status": one.status,
            "cause": s3.safe_cause(one.cause),
        }
        for one in turn.exchanges
        if one.status is not None and forwarder.refused(one)
    ]


def declares(exchange: forwarder.Exchange, tool: str) -> bool:
    tools = (exchange.json() or {}).get("tools")
    held = tools if isinstance(tools, list) else []
    return any(isinstance(one, dict) and one.get("name") == tool for one in held)


def carries_document(exchange: forwarder.Exchange) -> bool:
    """Whether a Messages request carries a document block, which is how Claude Code sends a PDF."""
    messages = (exchange.json() or {}).get("messages")
    for message in messages if isinstance(messages, list) else []:
        for block in _blocks(message.get("content") if isinstance(message, dict) else None):
            inner = _blocks(block.get("content")) if block.get("type") == "tool_result" else []
            if any(one.get("type") == "document" for one in (block, *inner)):
                return True
    return False


def tools_used(turn: Turn) -> list[str]:
    """The tools the session's own loop called in the turn, each named once, by `TOOLS_NAMED`."""
    names = {block.get("name") for _, block in turn.tool_uses()}
    return sorted({name if name in TOOLS_NAMED else "other" for name in names if isinstance(name, str)})


# What each step observed: its result, and what shows how it came about.


def open_of(turn: Turn) -> dict[str, Any]:
    return {"result_present": turn.answer() is not None and turn.session_id() is not None}


def skill_of(turn: Turn, output: Path, marker: str) -> dict[str, Any]:
    held = output.read_text(encoding="utf-8", errors="replace").split() if output.is_file() else []
    called = any(SKILL_NAME in json.dumps(call.get("input")) for call in turn.calls("Skill"))
    return {"result_present": SKILL_WORD in held and marker in held, "skill_called": called}


def echo_of(turn: Turn, text: str, servers: Mapping[str, str]) -> dict[str, Any]:
    """Echo's result is there when a call of echo returned the text it was asked for, and the answer
    quotes all that call returned."""
    returns = [held.strip() for error, held in turn.results_of(ECHO_TOOL) if not error and text in held]
    returned = bool(returns)
    quoted = any(held in (turn.answer() or "") for held in returns)
    state = servers.get(ECHO_SERVER_NAME)
    return {
        "result_present": returned and quoted,
        "server_status": None if state is None else state if state in SERVER_STATES else "other",
        "echo_declared": any(declares(one, ECHO_TOOL) for one in turn.exchanges),
        "echo_called": bool(turn.calls(ECHO_TOOL)),
        "echo_returned": returned,
        "answer_quotes": quoted,
    }


def pdf_of(turn: Turn, word: str) -> dict[str, Any]:
    """An answer after the PDF is an answer of a turn in which the PDF reached the model: Claude Code
    sent it as a document block (the endpoint's line in its place is that step's limit), or a tool
    returned its text, which the random word only the PDF holds shows, whatever tool or command
    converted it. A call that only names the file (a listing, a note about it) reads none of it: a
    reply after one, or to the prompt alone, is recorded as `answered`, and is none."""
    answer = turn.answer()
    document = any(carries_document(one) for one in turn.exchanges)
    text = any(word in held for error, held in turn.tool_results().values() if not error)
    read_call = any(
        PDF_FILE in str((block.get("input") or {}).get("file_path", "")) for block in turn.calls("Read")
    )
    return {
        "result_present": answer is not None and (document or text),
        "answered": answer is not None,
        "pdf_read_call": read_call,
        "pdf_text_returned": text,
        "document_block_sent": document,
        "names_word": answer is not None and word in answer,
    }


def agents_of(turn: Turn, figure: Path) -> dict[str, Any]:
    """An agent the model asked for is started when a subagent's message names its call as parent or
    its call returned a result that is no error; one the harness refused is only asked for."""
    results, children = turn.tool_results(), turn.children()
    per_message: dict[object, int] = {}
    asked = started = 0
    for message, block in turn.tool_uses():
        if block.get("name") not in AGENT_TOOLS:
            continue
        asked += 1
        call = block.get("id")
        if call in children or (isinstance(call, str) and call in results and not results[call][0]):
            started += 1
            per_message[message] = per_message.get(message, 0) + 1
    return {
        "result_present": figure.is_file() and figure.stat().st_size > 0,
        "agents_asked": asked,
        "agents_started": started,
        "agents_in_one_message": max(per_message.values(), default=0),
    }


def search_of(turn: Turn) -> dict[str, Any]:
    """The search's answer is there when the reply gives an address that the web search itself
    returned: a reply that says the search could not be made, or an address from elsewhere, is none."""
    results = turn.results_of(WEB_SEARCH)
    returned = set().union(*(addresses(held) for error, held in results if not error))
    return {
        "result_present": bool(returned & addresses(turn.answer())),
        "search_called": bool(turn.calls(WEB_SEARCH)),
        "search_errors": sum(1 for error, _ in results if error),
        "addresses_returned": len(returned),
        "answered": turn.answer() is not None,
    }


def mcp_servers(turn: Turn) -> dict[str, str]:
    """The MCP servers Claude Code reported at the turn's start, with their states."""
    found: dict[str, str] = {}
    for event in turn.events():
        if event.get("type") == "system" and event.get("subtype") == "init":
            for server in _blocks(event.get("mcp_servers")):
                if isinstance(server.get("name"), str) and isinstance(server.get("status"), str):
                    found[server["name"]] = server["status"]
    return found


# The files the job writes.


def mcp_configuration(path: Path) -> None:
    """The MCP configuration that adds the echo server, started with the job's Python."""
    server = {"type": "stdio", "command": sys.executable, "args": [str(ECHO_SERVER)], "env": {}}
    path.write_text(json.dumps({"mcpServers": {ECHO_SERVER_NAME: server}}, indent=2), encoding="utf-8")


def write_pdf(path: Path, word: str) -> None:
    """The PDF of the step, by the program of the run's files."""
    subprocess.run(
        [sys.executable, str(MAKE_PDF), str(path), word], check=True, capture_output=True, timeout=PDF_LIMIT_SECONDS
    )


# One run of the job.


class Job:
    """The session against the pool `pool` (served by `engine`), through the gateway at `endpoint`,
    as the canary student of `key`; the pool's state read through `gateway`, the workspace read through
    `workspace`; every line written to `lines`."""

    def __init__(
        self,
        *,
        pool: str,
        engine: str,
        endpoint: str,
        key: str,
        gateway: Any,
        workspace: s3.Reader,
        lines: s3.Lines,
        work: Path,
        install: Callable[[Path], tuple[Mapping[str, Path], Mapping[str, str]]] = s3.install_harnesses,
        launch: s3.Launch = s3.launch_process,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.pool, self.engine, self.endpoint, self.key = pool, engine, endpoint, key
        self.gateway, self.workspace, self.lines, self.work = gateway, workspace, lines, work
        self.install, self.launch, self.wall, self.sleep = install, launch, wall, sleep
        self.rng = rng or random.Random(secrets.randbits(64))
        self.folder = work / "session"
        self.home = work / "home"
        self.mcp = work / "mcp.json"
        self.binaries: Mapping[str, Path] = {}
        self.versions: Mapping[str, str] = {}
        self.session: str | None = None
        self.forwarder: forwarder.Forwarder | None = None
        self.failures = 0

    def _marker(self) -> str:
        return f"rhi-s3s-{self.rng.getrandbits(32):08x}"

    def _wait_until(self, moment: float) -> None:
        while (left := moment - self.wall()) > 0:
            self.sleep(left)

    def ready(self) -> None:
        """Waits until the status reads the pool ready, so that no step meets one that does not serve."""
        started = self.wall()
        while self.gateway.state() != s5.READY:
            if self.wall() - started >= s3.READY_WAIT_SECONDS:
                raise TimeoutError("the pool was not ready in time")
            self.sleep(s3.POLL_SECONDS)

    def build_in(self, start: float, end: float) -> str | None:
        """The one build the pool's lifecycle agents reported from `start` to `end`, read once the
        workspace holds the scrape after it; `None` for none, or two."""
        after = end + s3.SCRAPE_SECONDS
        self._wait_until(end + s3.SETTLE_SECONDS)
        step = s5.fine_step(after - start)
        release = self.workspace.read_series(
            s5.release_query(self.pool), start=s5.grid_start(start, after, step), end=after, step=step
        )
        try:
            return s5.build_of(s5.in_service_of(release, start, end))
        except ValueError:
            return None

    def command(self, prompt: str, *, echo: bool) -> s3.Command:
        """The turn's command: Claude Code with `prompt`, resuming the session once there is one, with
        the echo server in its MCP configuration when `echo`."""
        assert self.forwarder is not None
        argv = [
            str(self.binaries[harnesses.CLAUDE_CODE.name]),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.session is not None:
            argv += ["--resume", self.session]
        if echo:
            argv += ["--mcp-config", str(self.mcp)]
        # Last: the option takes every word after it.
        argv += ["--allowedTools", ",".join(ALLOWED_TOOLS)]
        environment = s3.environment_of(self.home) | {
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "DISABLE_AUTOUPDATER": "1",
        }
        return s3.Command(
            s3.Harness.CLAUDE_CODE, argv, environment, self.folder, self.forwarder.url, TURN_LIMIT_SECONDS
        )

    def turn(self, step: Step, prompt: str, *, echo: bool = False) -> Turn:
        """One turn of the session, through the session's forwarder."""
        assert self.forwarder is not None
        if step is not Step.OPEN and self.session is None:
            raise NoSession("Claude Code gave no session id to resume")
        before = len(self.forwarder.recorded())
        command = self.command(prompt, echo=echo)
        started = self.wall()
        error = None
        try:
            ran = self.launch(command)
        except Exception as raised:  # noqa: BLE001 - the turn is still recorded, with what it left
            ran, error = s3.Ran(None), type(raised).__name__
        ended = self.wall()
        turn = Turn(step, started, ended, ran, self.forwarder.recorded()[before:], error)
        self.session = turn.session_id() or self.session
        return turn

    def record(self, turn: Turn, observed: Mapping[str, Any]) -> None:
        """The step's line, after its refused requests' lines."""
        refused = refused_requests(turn)
        for fields in refused:
            self.lines.write(Line.REFUSED_REQUEST, **fields)
        if turn.launch_error is not None:
            self.failures += 1
            self.lines.write(Line.STEP_FAILED, step=turn.step.value, error=turn.launch_error)
        try:
            build = self.build_in(turn.started_at, turn.ended_at)
        except Exception as error:  # noqa: BLE001 - the step's observations are kept, the failure recorded
            build = None
            self.failures += 1
            self.lines.write(Line.STEP_FAILED, step=f"build_{turn.step}", error=type(error).__name__)
        self.lines.write(
            Line.STEP,
            step=turn.step.value,
            version=self.versions.get(harnesses.CLAUDE_CODE.name),
            build=build,
            started_at=s3.utc(turn.started_at),
            ended_at=s3.utc(turn.ended_at),
            seconds=round(turn.ended_at - turn.started_at, 1),
            exit_status=turn.ran.exit_code,
            stopped=turn.ran.stopped,
            launch_error=turn.launch_error,
            **observed,
            tools_used=tools_used(turn),
            requests=len(turn.exchanges),
            refused_requests=len(refused),
            refused_statuses=[fields["status"] for fields in refused],
        )

    def open(self) -> None:
        shutil.copyfile(TASK, self.folder / TASK.name)
        turn = self.turn(Step.OPEN, open_prompt(self._marker()))
        self.record(turn, open_of(turn))

    def skill(self) -> None:
        # The skill arrives in the middle of the session, as a pack's does.
        shutil.copytree(SKILL, self.folder / ".claude" / "skills", dirs_exist_ok=True)
        marker = self._marker()
        turn = self.turn(Step.SKILL, skill_prompt(marker))
        self.record(turn, skill_of(turn, self.folder / SKILL_OUTPUT, marker))

    def echo(self) -> None:
        mcp_configuration(self.mcp)
        text = f"echo-{self.rng.getrandbits(48):012x}"
        turn = self.turn(Step.ECHO, echo_prompt(text, self._marker()), echo=True)
        self.record(turn, echo_of(turn, text, mcp_servers(turn)))

    def pdf(self) -> None:
        word = f"rhi{self.rng.getrandbits(24):06x}"
        write_pdf(self.folder / PDF_FILE, word)
        turn = self.turn(Step.PDF, pdf_prompt(self._marker()), echo=True)
        self.record(turn, pdf_of(turn, word))

    def agents(self) -> None:
        turn = self.turn(Step.AGENTS, task_prompt(1, self._marker()), echo=True)
        self.record(turn, agents_of(turn, self.folder / FIGURE_FILE))

    def search(self) -> None:
        turn = self.turn(Step.SEARCH, task_prompt(2, self._marker()), echo=True)
        self.record(turn, search_of(turn))

    def _step(self, step: Step, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as error:  # noqa: BLE001 - a step that fails is recorded, and the next runs
            self.failures += 1
            self.lines.write(Line.STEP_FAILED, step=step.value, error=type(error).__name__)

    def run(self) -> int:
        """Every step, in order, in one session; the number of steps that failed."""
        self.lines.write(Line.JOB, pool=self.pool, engine=self.engine)
        self.binaries, self.versions = self.install(self.work / "harnesses")
        self.lines.write(Line.VERSIONS, versions=dict(self.versions))
        self.ready()
        self.folder.mkdir(parents=True, exist_ok=True)
        actions = {
            Step.OPEN: self.open,
            Step.SKILL: self.skill,
            Step.ECHO: self.echo,
            Step.PDF: self.pdf,
            Step.AGENTS: self.agents,
            Step.SEARCH: self.search,
        }
        with forwarder.Forwarder(self.endpoint, self.pool, self.wall) as own:
            self.forwarder = own
            forwarder.write_home(self.home, forwarder.Client.CLAUDE_CODE, own.url, self.pool, self.key)
            for step in Step:
                self._step(step, actions[step])
        return self.failures


def main(environ: Mapping[str, str] = os.environ, out: Callable[[str], None] = print) -> int:
    """Runs the job with its environment and credentials; exits 1 when a step failed, or when an
    error stopped the job: that error is a line `job_failed` with its type's name, never its text,
    which may be what AWS returned. A job the pool's session does not carry is that line with the
    reason, one of `common.Refusal`."""
    try:
        return run_job(environ, out)
    except common.SessionRefused as refused:
        s3.Lines((), out=out).write(Line.JOB_FAILED, error=type(refused).__name__, reason=refused.reason.value)
        return 1
    except Exception as error:  # noqa: BLE001 - the job's boundary: no traceback reaches the log
        s3.Lines((), out=out).write(Line.JOB_FAILED, error=type(error).__name__)
        return 1


def run_job(environ: Mapping[str, str], out: Callable[[str], None]) -> int:
    """The job, from its environment and credentials; 1 when a step failed. `common.SessionRefused`,
    with nothing sent to the gateway, when the pool's session does not carry the run."""
    needs = common.needs_of(environ)
    endpoint = common.gateway_address(environ.get(s3.ENDPOINT_VARIABLE, ""))
    name = environ.get(s3.POOL_VARIABLE, s3.DEFAULT_POOL)
    settings = config.read_ssm()
    pools = [pool for pool in settings.pools if pool.name == name]
    if len(pools) != 1:
        raise ValueError("the deployed configuration has no pool of that name")
    (pool,) = pools
    common.require_session(
        common.pool_state_table(environ),
        name,
        instances=needs.instances,
        run_minutes=needs.run_minutes,
        session_max_hours=settings.session_max_hours,
        now=time.time,
    )
    keys = common.canary_keys()
    key = keys.students[0]
    workspace = common.Workspace(
        environ[s3.WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], boto3.Session().get_credentials()
    )
    lines = s3.Lines(keys.students, out=out)
    with tempfile.TemporaryDirectory(prefix="rhi-s3s-") as work:
        failures = Job(
            pool=name,
            engine=pool.engine,
            endpoint=endpoint,
            key=key,
            gateway=common.Gateway(endpoint, name, key),
            workspace=workspace,
            lines=lines,
            work=Path(work),
        ).run()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
