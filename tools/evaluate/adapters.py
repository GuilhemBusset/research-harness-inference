"""The student harnesses, run in a trial's worker on their normal routes
(docs/design/agent-session-evaluation.md, "Course quality and the grader").

A trial gives one prompt of a task to one harness: Claude Code, Codex, OpenCode, Pi or the SDK
tool loop. An `Adapter` is that harness for one task: built from the task's
`contracts.HarnessAssignment`, it knows the files of the harness's profile, the command that gives
it a prompt with no one at the keyboard, and how to read what the run left. The commands are those
of spike S3 (`spikes/s3_harness_task.py`, `command_of`), and of its second session for the tools
that start a worker.

**The profile is the student's** (`Adapter.profile`), a constant of this module for the versions
the suite and the trial image pin:

- Claude Code and Codex get exactly what `rhi login` writes (`rhi_cli.harness`): Claude Code its
  settings file, with the endpoint and `rhi token` as its key helper; Codex its profile `rhi`, on
  the Responses route, with the pool's model and `rhi token` as its key command. Nothing sets a
  model, an effort, a thinking budget or a window for Claude Code: it asks for its own names;
- OpenCode and Pi get the provider entry a student writes in the file the student guide names,
  on the Chat Completions route: OpenCode's entry is the one of spike S3, which the pinned
  OpenCode reads, and takes its key from the variable the guide names (`KEY_VARIABLE`); Pi's is
  the guide's own, and runs `rhi token`;
- the SDK tool loop is `evaluate/sdk_tool_loop.py`, copied into the home: it asks `rhi token` for
  its key and reads the endpoint's address from the SDK's own variable.

**The only destination is the trial's forwarder.** Every address a profile or a variable holds is
`workers.FORWARDER_URL`, the one address a trial's worker can reach, and the only key is
`forwarding.PLACEHOLDER_KEY`: the forwarder holds the student's key outside the trial and writes
it itself. The worker has no `rhi` command, so the home is given one (`bin/rhi`) that answers
`rhi token` with the placeholder: the profiles stay as a student has them, and a harness that
fetches its key before its first request gets one.

**In the worker.** `Worker.run` passes a command and nothing else: no variable, no file. So the
command of a step (`Adapter.command`) is a small launcher, run isolated from the task's files as
the worker's own programs are (`python -I`): it writes the profile into the home, puts the home's
`bin` and the trial's local tools first on `PATH`, sets the harness's variables and becomes the
harness. Everything else of the environment is the container's own.

**What a task may need** (the features of its assignment), and what the adapter does for each:

- *a follow-up in the same conversation* (`follow_up`; Claude Code, Codex): a later prompt is a
  second command in the same worker and home, which continues the conversation the harness stored
  there. It is refused for an assignment without the feature. A follow-up counts as given only
  once its command asked the model for a turn (below, *invalid*);
- *local research tools* (`local_search`): each tool folder the worker mounts under
  `workers.TOOLS` is on the `PATH`, so the harness's shell tool calls a tool by its name;
- *delegation* (`delegation`; Claude Code): the tools that start a worker are among those Claude
  Code may use without asking, since no one is there to ask, and `delegation` reads from Claude
  Code's own output how many delegated workers it started and whether two of them were alive at
  once: from the events it prints when a worker starts and when it ends, and from the worker's
  own messages. Workers that are alive at once are also answered at once only when the trial's
  budget gives each answer an allowance (`forwarding.Budget`, `request_tokens`): a request with
  none holds every token the trial has left, and the forwarder keeps the next one waiting;
- *skills* (`skills`; Claude Code): the skill tool is allowed too. Reading a PDF needs nothing more
  than the file tools every Claude Code trial has.

An assignment that asks a harness for a feature it does not have here is refused (`Unsupported`).

**How a trial ended** (`Adapter.trial`, `Ending`) is told in the vocabulary of `contracts`:

- *completed*: every prompt was given and the harness ended each in success (for Claude Code,
  with a result that is no error);
- *failed*, `timeout`: the worker's wall time ended a step;
- *failed*, `malformed_tool_call`: the harness ended without completing because of tool calls it
  could not carry out. The SDK tool loop says so itself (its exit status). Claude Code's output
  shows it: the run did not complete, and the last tool result of its main loop is its own
  refusal of the call (`CLAUDE_REFUSALS`). For Codex, OpenCode and Pi the adapter reads no such
  event from what they print: there, such a trial shows as the limit it runs into or as an exit
  in failure, and the adapter does not name it;
- *invalid*, `driver_error`: the measurement broke, not the attempt. The harness could not be
  started (the launcher could not write its profile, or there was no such command), the runtime
  failed, what a step printed could not be written or read back, or the harness ended a prompt
  in failure without having asked the model for a single turn for it: it never worked on that
  prompt, whatever the task's folder holds. For the first prompt the forwarder's count of the
  trial's turns shows it. For a follow-up, which a harness that cannot load its conversation ends
  the same way, only a count taken when the follow-up was given does (`Adapter.trial`, `turns`);
  without one, nothing shows the follow-up reached the model, and the trial is invalid.

A harness that ended in failure for another reason, after it asked the model, has no reason of
the adapter's:
`Ending.evidence` gives its exit status to the forwarder, which names an endpoint failure when a
turn was left unanswered, and adds the adapter's reason where the forwarder saw none. What was
written is still graded.

It keeps no text of a run: an ending is labels and an exit status, the delegation two numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from evaluate import workers
from evaluate.contracts import FailureReason, Harness, HarnessFeature, InvalidReason, Route
from evaluate.forwarding import PLACEHOLDER_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from evaluate.contracts import HarnessAssignment
    from evaluate.forwarding import Evidence, Forwarder

# The route each harness sends its turns on: its normal one.
ROUTE_OF = {
    Harness.CLAUDE_CODE: Route.MESSAGES,
    Harness.CODEX: Route.RESPONSES,
    Harness.OPENCODE: Route.CHAT_COMPLETIONS,
    Harness.PI: Route.CHAT_COMPLETIONS,
    Harness.SDK_TOOL_LOOP: Route.CHAT_COMPLETIONS,
}
# The features an adapter provides, harness by harness: those of the suite's harness table.
SUPPORTED = {
    Harness.CLAUDE_CODE: frozenset(HarnessFeature),
    Harness.CODEX: frozenset({HarnessFeature.FOLLOW_UP}),
    Harness.OPENCODE: frozenset(),
    Harness.PI: frozenset(),
    Harness.SDK_TOOL_LOOP: frozenset(),
}

# The name `rhi login` gives the endpoint in a harness's settings, and the command that prints the key.
PROVIDER = "rhi"
KEY_COMMAND = ("rhi", "token")
# The variable OpenCode's entry names for the key (the student guide, section 3).
KEY_VARIABLE = "RHI_API_KEY"
# Where the home keeps its commands: the first folder of the trial's `PATH`.
BIN = f"{workers.HOME}/bin"
# The worker's `rhi`: `rhi token` prints the placeholder, and any other use fails.
RHI = f"""#!/bin/sh
[ "$1" = token ] && printf '%s\\n' {PLACEHOLDER_KEY}
"""

# The tools Claude Code may use without asking: it runs with no one to ask (spike S3). With them,
# for an assignment that needs it, the tools that start a worker, by their names in its versions
# (spike S3's second session), and the skill tool.
ALLOWED_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
AGENT_TOOLS = ("Agent", "Task")
SKILL_TOOL = "Skill"
# How Claude Code's own refusal of a tool call begins, as the pinned version writes its tool result:
# arguments that are not JSON or not the tool's, and a tool it does not have. A tool that ran and
# failed begins otherwise.
CLAUDE_REFUSALS = ("<tool_use_error>InputValidationError", "<tool_use_error>Error: No such tool available")
# The events the pinned Claude Code prints (`type` `system`) when something it runs in the
# background starts and when it has ended, and what a delegated worker is among those things.
TASK_STARTED, TASK_ENDED = "task_started", "task_notification"
LIFECYCLE = (TASK_STARTED, TASK_ENDED)
AGENT_TASK = "local_agent"

# The SDK tool loop, as the file a trial's home is given.
SDK_TOOL_LOOP = Path(__file__).with_name("sdk_tool_loop.py")
SDK_TOOL_LOOP_FILE = "sdk_tool_loop.py"
# Its exit status when calls it could not carry out ended it (`sdk_tool_loop.MALFORMED_EXIT`).
MALFORMED_EXIT = 3
# The exit statuses that say no harness ran: the launcher's own (`SETUP_FAILED` when it could not
# set the home up, `NO_COMMAND` when it could not become the harness), and the runtime's (it could
# not run the command, could not start it, did not find it).
SETUP_FAILED = 125
NO_COMMAND = 127
NOT_STARTED = frozenset({SETUP_FAILED, 126, NO_COMMAND})

# The launcher: the command of every step, given one JSON object. It writes the files, makes the
# commands among them executable, and becomes the harness with its variables, the folders of
# `path` first on the container's own `PATH`. Whatever stops it before the harness is one of the
# statuses of `NOT_STARTED`, never a status a harness that ran could have ended with for a task.
LAUNCHER = f"""\
import json
import os
import sys

try:
    asked = json.loads(sys.argv[1])
    for path, text in asked["files"].items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(text)
    for path in asked["commands"]:
        os.chmod(path, 0o755)
    environment = {{**os.environ, **asked["environment"]}}
    environment["PATH"] = os.pathsep.join([*asked["path"], os.environ.get("PATH", "")])
except Exception as error:
    print("the harness was not set up:", type(error).__name__, file=sys.stderr)
    sys.exit({SETUP_FAILED})
try:
    os.execvpe(asked["argv"][0], asked["argv"], environment)
except Exception as error:
    print("the harness was not started:", type(error).__name__, file=sys.stderr)
    sys.exit({NO_COMMAND})
"""


class Unsupported(ValueError):
    """An assignment or a step this harness has no adapter for: a feature it lacks, or a
    follow-up the task does not assign."""


@dataclass(frozen=True)
class Profile:
    """What a harness is set up with: the files of its home, by their path under it, and its variables."""

    files: Mapping[str, str]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class Step:
    """One prompt given to the harness: how its command ended, and what it printed, up to the
    worker's output cap."""

    ran: workers.Ran
    output: str


@dataclass(frozen=True)
class Delegation:
    """What Claude Code's output shows of the workers it delegated to."""

    workers: int
    """The delegated workers Claude Code started."""
    overlapped: bool
    """Two of them were alive at once: each had started before the other ended."""


@dataclass(frozen=True)
class Ending:
    """How a trial's harness ended. Completed, or failed for `failure`, or invalid for `invalid`;
    with none of the three, the harness ended in failure for a reason that is not the adapter's to name."""

    completed: bool
    failure: FailureReason | None = None
    invalid: InvalidReason | None = None
    exit_status: int | None = None
    turns_before: int | None = 0
    """The model turns the trial had been admitted when the prompt that ended it was given: none
    before the first prompt, and before a follow-up `None` unless they were counted then."""

    def evidence(self, forwarder: Forwarder) -> Evidence:
        """The trial's evidence, with this ending: the forwarder's own reading of the exit
        status, and this ending's failure where the forwarder saw none. A harness that did not
        complete, for no reason either of them names, and that is not shown to have asked the
        model for a turn since its last prompt, never worked on that prompt: the trial is invalid,
        the driver's error. The turns of the prompts before it show nothing of this one."""
        observed = forwarder.evidence(invalid=self.invalid, exit_status=self.exit_status)
        failure, invalid = observed.failure or self.failure, observed.invalid
        asked = self.turns_before is not None and observed.model_turns > self.turns_before
        if not self.completed and failure is None and invalid is None and not asked:
            invalid = InvalidReason.DRIVER_ERROR
        return replace(observed, failure=failure, invalid=invalid)


@dataclass(frozen=True)
class Trial:
    """A trial's steps, the first prompt's then each follow-up's that was given, and its ending.
    `delegation` is read for an assignment that delegates, and is `None` for any other, and for a
    trial whose output could not all be read: no count of workers comes from what was not read."""

    steps: tuple[Step, ...]
    ending: Ending
    delegation: Delegation | None


class Runs(Protocol):
    """What an adapter needs of a worker (`workers.Worker`)."""

    transcript: Path
    """Where what the worker's commands print is kept, one after the other."""

    def run(self, command: Sequence[str]) -> workers.Ran:
        """Runs a command in the worker, as `workers.Worker.run` does."""
        ...


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# Claude Code's output: one JSON object a line (`--output-format stream-json`).


def events(output: str) -> list[dict[str, Any]]:
    """The lines of a step's output that are JSON objects: Claude Code's events, in order."""
    found = []
    for line in output.splitlines():
        try:
            held = json.loads(line)
        except ValueError:
            continue
        if isinstance(held, dict):
            found.append(held)
    return found


def _blocks(event: Mapping[str, Any], kind: str) -> Iterator[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == kind:
            yield block


def _result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    return "\n".join(part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))


def delegation(output: str) -> Delegation:
    """The delegated workers one run of Claude Code started, as its output shows them, and whether
    two overlapped. The pinned Claude Code starts its workers in the background, so the result of
    the call that starts one says nothing of when it ended. What does:

    - the event it prints when a worker starts (`task_started`, of an agent) and the one it prints
      when that worker ended, however it did (`task_notification`); each names the tool call that
      started the worker;
    - the worker's own messages, its model's and its tools' results, which name that call too
      (`parent_tool_use_id`).

    A worker is alive from the first of these to the last, and one whose start was printed and
    whose end was not is alive to the end of the output. The events are in the order Claude Code
    printed them, which is the evidence: two workers overlapped when each had started before the
    other ended. A worker that sent a single answer is alive all the same, from its start to its
    end."""
    printed = events(output)
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    running: set[str] = set()
    for at, event in enumerate(printed):
        call, lifecycle = event.get("parent_tool_use_id"), None
        if event.get("type") == "system" and event.get("subtype") in LIFECYCLE:
            call, lifecycle = event.get("tool_use_id"), event["subtype"]
        if not isinstance(call, str):
            continue
        if lifecycle == TASK_STARTED:
            # Claude Code prints the same event for the other things it runs in the background.
            if event.get("task_type") != AGENT_TASK:
                continue
            running.add(call)
        elif lifecycle == TASK_ENDED:
            if call not in first:
                continue
            running.discard(call)
        first.setdefault(call, at)
        last[call] = at
    spans = [(first[call], len(printed) if call in running else last[call]) for call in first]
    overlapped = any(
        start < other_end and other_start < end
        for index, (start, end) in enumerate(spans)
        for other_start, other_end in spans[index + 1 :]
    )
    return Delegation(len(spans), overlapped)


def _claude_ended(output: str) -> tuple[bool | None, bool]:
    """What Claude Code's output says of its end: whether its result is no error (`None` when it
    printed no result), and whether the last tool result of its main loop is its own refusal of
    the call."""
    completed, refused = None, False
    for event in events(output):
        if event.get("type") == "result":
            completed = event.get("is_error") is False
        elif event.get("type") == "user" and event.get("parent_tool_use_id") is None:
            for block in _blocks(event, "tool_result"):
                refused = block.get("is_error") is True and _result_text(block).lstrip().startswith(CLAUDE_REFUSALS)
    return completed, refused


class Adapter:
    """One harness for one task. `assignment` is the task's, `model` the pool's name, which the
    profiles that name a model hold, and `tools` the names of the local tools the trial's worker
    mounts (`workers.Spec.tools`)."""

    def __init__(self, assignment: HarnessAssignment, *, model: str, tools: Sequence[str] = ()) -> None:
        lacking = sorted(set(assignment.features) - SUPPORTED[assignment.harness])
        if lacking:
            raise Unsupported(f"{assignment.harness.value} has no {', '.join(lacking)} here")
        self.harness = assignment.harness
        self.features = frozenset(assignment.features)
        self.route = ROUTE_OF[self.harness]
        self.model = model
        self.tools = tuple(sorted(tools))

    # The profile.

    def profile(self) -> Profile:
        """The harness's files and variables, every address the forwarder's."""
        endpoint = workers.FORWARDER_URL
        files = {"bin/rhi": RHI}
        environment = {"HOME": workers.HOME, "LANG": "C.UTF-8"}
        if self.harness is Harness.CLAUDE_CODE:
            settings = {"apiKeyHelper": " ".join(KEY_COMMAND), "env": {"ANTHROPIC_BASE_URL": endpoint}}
            files[".claude/settings.json"] = json.dumps(settings, indent=2) + "\n"
            environment |= {"CLAUDE_CONFIG_DIR": f"{workers.HOME}/.claude", "DISABLE_AUTOUPDATER": "1"}
        elif self.harness is Harness.CODEX:
            files[f".codex/{PROVIDER}.config.toml"] = "\n".join(
                [
                    f"model_provider = {json.dumps(PROVIDER)}",
                    f"model = {json.dumps(self.model)}",
                    "",
                    f"[model_providers.{PROVIDER}]",
                    f"name = {json.dumps(PROVIDER)}",
                    f"base_url = {json.dumps(endpoint + '/v1')}",
                    'wire_api = "responses"',
                    "",
                    f"[model_providers.{PROVIDER}.auth]",
                    f"command = {json.dumps(KEY_COMMAND[0])}",
                    f"args = {json.dumps(list(KEY_COMMAND[1:]))}",
                    "",
                ]
            )
            environment["CODEX_HOME"] = f"{workers.HOME}/.codex"
        elif self.harness is Harness.OPENCODE:
            entry = {
                "npm": "@ai-sdk/openai-compatible",
                "name": PROVIDER,
                "options": {"baseURL": f"{endpoint}/v1", "apiKey": f"{{env:{KEY_VARIABLE}}}"},
                "models": {self.model: {"name": self.model}},
            }
            config = {
                "$schema": "https://opencode.ai/config.json",
                "autoupdate": False,
                "share": "disabled",
                "model": f"{PROVIDER}/{self.model}",
                "small_model": f"{PROVIDER}/{self.model}",
                "provider": {PROVIDER: entry},
            }
            files[".config/opencode/opencode.json"] = json.dumps(config, indent=2) + "\n"
            environment[KEY_VARIABLE] = PLACEHOLDER_KEY
        elif self.harness is Harness.PI:
            entry = {
                "baseUrl": f"{endpoint}/v1",
                "api": "openai-completions",
                "apiKey": "!" + " ".join(KEY_COMMAND),
                "models": [{"id": self.model}],
            }
            files[".pi/agent/models.json"] = json.dumps({"providers": {PROVIDER: entry}}, indent=2) + "\n"
        else:
            files[SDK_TOOL_LOOP_FILE] = SDK_TOOL_LOOP.read_text(encoding="utf-8")
            environment["OPENAI_BASE_URL"] = f"{endpoint}/v1"
        return Profile(files, environment)

    # The commands.

    def client(self, prompt: str, *, follow_up: bool = False) -> list[str]:
        """The harness's own command for a prompt: the task's first, or, with `follow_up`, one
        that continues the conversation the home holds. `Unsupported` for a follow-up the
        assignment does not have."""
        if follow_up and HarnessFeature.FOLLOW_UP not in self.features:
            raise Unsupported(f"{self.harness.value} is assigned no follow-up for this task")
        if self.harness is Harness.CLAUDE_CODE:
            allowed = [*ALLOWED_TOOLS]
            if HarnessFeature.DELEGATION in self.features:
                allowed += AGENT_TOOLS
            if HarnessFeature.SKILLS in self.features:
                allowed.append(SKILL_TOOL)
            resumed = ["--continue"] if follow_up else []
            # Last: the option takes every word after it.
            output = ["--output-format", "stream-json", "--verbose"]
            return ["claude", "-p", prompt, *output, *resumed, "--allowedTools", ",".join(allowed)]
        if self.harness is Harness.CODEX:
            options = ["--profile", PROVIDER, "--skip-git-repo-check", "--sandbox", "danger-full-access"]
            return ["codex", "exec", *options, *(["resume", "--last"] if follow_up else []), prompt]
        if self.harness is Harness.OPENCODE:
            return ["opencode", "run", "--model", f"{PROVIDER}/{self.model}", "--auto", prompt]
        if self.harness is Harness.PI:
            return ["pi", "--print", "--offline", "--no-session", "--provider", PROVIDER, "--model", self.model, prompt]
        return ["python", "-I", f"{workers.HOME}/{SDK_TOOL_LOOP_FILE}", self.model, prompt]

    def command(self, prompt: str, *, follow_up: bool = False) -> list[str]:
        """The command of a step, for `Worker.run`: the launcher, which sets the home up and
        becomes the harness."""
        profile = self.profile()
        asked = {
            "files": {f"{workers.HOME}/{path}": text for path, text in profile.files.items()},
            "commands": [f"{BIN}/rhi"],
            "environment": dict(profile.environment),
            "path": [BIN, *(f"{workers.TOOLS}/{tool}" for tool in self.tools)],
            "argv": self.client(prompt, follow_up=follow_up),
        }
        return ["python", "-I", "-c", LAUNCHER, json.dumps(asked)]

    # Running.

    def run(self, worker: Runs, prompt: str, *, follow_up: bool = False) -> Step:
        """Gives the harness one prompt in the worker, and keeps what the step printed. OSError
        when that cannot be read: nothing stands in for it."""
        before = _size(worker.transcript)
        ran = worker.run(self.command(prompt, follow_up=follow_up))
        with ran.transcript.open("rb") as file:
            file.seek(before)
            printed = file.read()
        return Step(ran, printed.decode("utf-8", "replace"))

    def trial(self, worker: Runs, prompts: Sequence[str], *, turns: Callable[[], int] | None = None) -> Trial:
        """Gives the harness the task's prompt, then each follow-up while the one before
        completed, and says how the trial ended. `turns` counts the model turns the trial's
        forwarder has admitted so far (`forwarder.budget.model_turns`): with it, a follow-up that
        ended in failure is known to have asked the model or not (`Ending.evidence`). A runtime
        that fails, or a transcript that cannot be written or read, is the driver's error: the
        trial has its ending all the same, and no delegation is read from it."""
        steps: list[Step] = []
        ending = Ending(completed=True)
        broken = False
        try:
            for index, prompt in enumerate(prompts):
                before = 0 if index == 0 else turns() if turns is not None else None
                steps.append(self.run(worker, prompt, follow_up=index > 0))
                ending = replace(self.ending(steps[-1]), turns_before=before)
                if not ending.completed:
                    break
        except (workers.WorkerFault, OSError):
            broken, ending = True, Ending(completed=False, invalid=InvalidReason.DRIVER_ERROR)
        found = None
        if HarnessFeature.DELEGATION in self.features and not broken:
            # Each step is a run of Claude Code, with workers of its own.
            runs = [delegation(step.output) for step in steps]
            found = Delegation(sum(one.workers for one in runs), any(one.overlapped for one in runs))
        return Trial(tuple(steps), ending, found)

    def ending(self, step: Step) -> Ending:
        """How one step ended (the module's text)."""
        status = step.ran.exit_code
        if step.ran.timed_out or status is None:
            return Ending(completed=False, failure=FailureReason.TIMEOUT)
        if status in NOT_STARTED:
            return Ending(completed=False, invalid=InvalidReason.DRIVER_ERROR, exit_status=status)
        succeeded, refused = status == 0, False
        if self.harness is Harness.CLAUDE_CODE:
            completed, refused = _claude_ended(step.output)
            succeeded = succeeded and completed is not False
        elif self.harness is Harness.SDK_TOOL_LOOP:
            refused = status == MALFORMED_EXIT
        if succeeded:
            return Ending(completed=True, exit_status=status)
        failure = FailureReason.MALFORMED_TOOL_CALL if refused else None
        return Ending(completed=False, failure=failure, exit_status=status)
