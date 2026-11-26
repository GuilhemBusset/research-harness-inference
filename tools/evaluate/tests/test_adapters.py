"""What the harness adapters do for a trial (docs/design/agent-session-evaluation.md, "Course
quality and the grader"; tools/evaluate/adapters.py): each of the five harnesses is started with
its student's profile and reaches only the forwarder; a scripted tool exchange completes on each of
the three wire formats, every action made from the result the client really sent back; a follow-up
continues the conversation its home holds; the delegated workers and a trial's ending are read from
what the run left.

Nothing here starts a container or a model. The worker is a double that runs a step's command as a
local process, the trial's folders and the forwarder's address mapped to a folder and a port of
this test. The forwarder is the real one, and behind it the scripted pool (`evaluate.scripted`).
The SDK tool loop is the real one. Claude Code, Codex, OpenCode and Pi are stand-ins on the
worker's `PATH`: each reads the endpoint and the key the way its harness does, from the files and
variables the adapter gave it, and runs a tool loop on its harness's route with the pinned SDK
(Messages, Responses, Chat Completions as a stream). The pinned clients themselves are run by
tests/container/test_evaluate_adapters.py. Every key, prompt and word here is made up.
"""

from __future__ import annotations

import contextlib
import errno
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from openai.types.chat import ChatCompletion

from evaluate import adapters, plan, scripted, sdk_tool_loop, workers
from evaluate.adapters import Adapter, Delegation, Ending, Step, Unsupported
from evaluate.contracts import (
    FailureReason,
    Grade,
    Harness,
    HarnessAssignment,
    HarnessFeature,
    InvalidReason,
    Limits,
    Route,
    TrialOutcome,
)
from evaluate.forwarding import PLACEHOLDER_KEY, Budget, Forwarder, run_clock
from evaluate.scripted import Conversation, Delegate, Phase, Pool, Raw, Read, Say, Shell, Write
from rhi_cli import harness as login

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

PLAN = Limits(trial_wall_time_ms=720_000, trial_model_turns=32, trial_generated_tokens=32_000, drain_ms=60_000)
POOL = "pool-under-test"
CANARY_KEY = "made-up-canary-key"
STEP_SECONDS = 60
FOLLOW_UP, DELEGATION = HarnessFeature.FOLLOW_UP, HarnessFeature.DELEGATION
SEED = re.compile(r"[0-9a-f]{32}")

# The stand-in for the four harnesses that are commands: `client.py NAME ARGUMENTS`. It finds the
# endpoint and the key as that harness does, notes what it was started with, and runs a tool loop
# with one shell tool on the harness's route. A conversation is kept in the home, and continued
# when the command asks for it.
STAND_IN = """\
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import anthropic
import openai

name, arguments = sys.argv[1], sys.argv[2:]
home = Path(os.environ["HOME"])


def printed(line):
    return subprocess.run(line, shell=True, capture_output=True, text=True, check=True).stdout.strip()


resumed = False
if name == "claude":
    settings = json.loads((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").read_text())
    address, key = settings["env"]["ANTHROPIC_BASE_URL"], printed(settings["apiKeyHelper"])
    prompt, resumed = arguments[arguments.index("-p") + 1], "--continue" in arguments
elif name == "codex":
    chosen = arguments[arguments.index("--profile") + 1]
    profile = tomllib.loads((Path(os.environ["CODEX_HOME"]) / f"{chosen}.config.toml").read_text())
    provider = profile["model_providers"][profile["model_provider"]]
    address = provider["base_url"]
    key = printed(" ".join([provider["auth"]["command"], *provider["auth"]["args"]]))
    prompt, resumed = arguments[-1], "resume" in arguments
elif name == "opencode":
    (entry,) = json.loads((home / ".config/opencode/opencode.json").read_text())["provider"].values()
    address, key = entry["options"]["baseURL"], os.environ[entry["options"]["apiKey"][len("{env:") : -1]]
    prompt = arguments[-1]
else:
    (entry,) = json.loads((home / ".pi/agent/models.json").read_text())["providers"].values()
    address, key = entry["baseUrl"], printed(entry["apiKey"].removeprefix("!"))
    prompt = arguments[-1]
Path(f"started-{name}.json").write_text(json.dumps({"argv": arguments, "address": address, "key": key}))

session = home / f"{name}-session.json"
history = json.loads(session.read_text()) if resumed else []
SHELL = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}


def shell(line):
    ran = subprocess.run(line, shell=True, capture_output=True, text=True)
    return ran.stdout + ran.stderr


if name == "claude":
    client = anthropic.Anthropic(base_url=address, api_key=key)
    history.append({"role": "user", "content": prompt})
    while True:
        asked = {"model": "claude-made-up", "max_tokens": 1024, "messages": history}
        with client.messages.stream(**asked, tools=[{"name": "Bash", "input_schema": SHELL}]) as stream:
            message = stream.get_final_message()
        said = [block.model_dump(exclude_none=True) for block in message.content]
        history.append({"role": "assistant", "content": said})
        calls = [block for block in message.content if block.type == "tool_use"]
        if not calls:
            break
        results = [
            {"type": "tool_result", "tool_use_id": call.id, "content": shell(call.input["command"])}
            for call in calls
        ]
        history.append({"role": "user", "content": results})
elif name == "codex":
    client = openai.OpenAI(base_url=address, api_key=key)
    history.append({"role": "user", "content": prompt})
    parameters = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}
    tool = {"type": "function", "name": "exec_command", "parameters": parameters}
    while True:
        with client.responses.stream(model="made-up", input=history, tools=[tool]) as stream:
            response = stream.get_final_response()
        calls = [item for item in response.output if item.type == "function_call"]
        for item in response.output:
            history.append(item.model_dump(exclude_none=True))
        if not calls:
            break
        for call in calls:
            output = shell(json.loads(call.arguments)["cmd"])
            history.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
else:
    client = openai.OpenAI(base_url=address, api_key=key)
    history.append({"role": "user", "content": prompt})
    tool = {"type": "function", "function": {"name": "bash", "parameters": SHELL}}
    while True:
        with client.chat.completions.stream(model="made-up", messages=history, tools=[tool]) as stream:
            message = stream.get_final_completion().choices[0].message
        calls = message.tool_calls or []
        said = {"role": "assistant", "content": message.content}
        if calls:
            said["tool_calls"] = [
                {"id": call.id, "type": "function", "function": call.function.model_dump(exclude_none=True)}
                for call in calls
            ]
        history.append(said)
        if not calls:
            break
        for call in calls:
            output = shell(json.loads(call.function.arguments)["command"])
            history.append({"role": "tool", "tool_call_id": call.id, "content": output})
session.write_text(json.dumps(history))
"""
COMMANDS = {Harness.CLAUDE_CODE: "claude", Harness.CODEX: "codex", Harness.OPENCODE: "opencode", Harness.PI: "pi"}


class LocalWorker:
    """A worker's double: a step's command runs as a local process, in folders of this test.
    `/trial/...` is `root/...` and the forwarder's address in a worker is the real forwarder's,
    in every word of the command; nothing of this process's environment is passed but a `PATH`
    that holds the stand-ins and this Python."""

    def __init__(self, root: Path, forwarder: str) -> None:
        self.root, self._forwarder = root, forwarder
        for folder in ("home", "work", "bin", "tools"):
            (root / folder).mkdir(parents=True)
        (root / "client.py").write_text(STAND_IN, encoding="utf-8")
        scripts = {command: f'python "{root}/client.py" {command}' for command in COMMANDS.values()}
        # `python` is this test's own, by a script: a link to it would lose its environment.
        for command, line in {**scripts, "python": f'"{sys.executable}"'}.items():
            script = root / "bin" / command
            script.write_text(f'#!/bin/sh\nexec {line} "$@"\n', encoding="utf-8")
            script.chmod(0o755)
        self.transcript = root / "transcript"
        self.work = root / "work"

    def run(self, command: Sequence[str]) -> workers.Ran:
        mapped = [
            word.replace("/trial/", f"{self.root}/").replace(workers.FORWARDER_URL, self._forwarder) for word in command
        ]
        environment = {"PATH": f"{self.root / 'bin'}:/usr/bin:/bin", "HOME": str(self.root / "home")}
        with self.transcript.open("ab") as sink:
            try:
                done = subprocess.run(
                    mapped,
                    cwd=self.work,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    timeout=STEP_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return workers.Ran(None, True, False, self.transcript)
        return workers.Ran(done.returncode, False, False, self.transcript)

    def started(self, harness: Harness) -> dict:
        """What the harness's stand-in was last started with."""
        return json.loads((self.work / f"started-{COMMANDS[harness]}.json").read_text(encoding="utf-8"))


@dataclass
class Bench:
    """A trial's surroundings: the scripted pool, the forwarder in front of it, and a worker."""

    pool: Pool
    forwarder: Forwarder
    worker: LocalWorker


@contextlib.contextmanager
def bench(root: Path, harness: Harness, conversations: Sequence[Conversation]) -> Generator[Bench]:
    budget = Budget(PLAN, run_clock())
    with (
        Pool(conversations, model=POOL) as pool,
        Forwarder(pool.url, pool=POOL, key=CANARY_KEY, harness=harness, budget=budget) as forwarder,
    ):
        yield Bench(pool, forwarder, LocalWorker(root / uuid4().hex, forwarder.url))


def adapter(harness: Harness, *features: HarnessFeature, tools: Sequence[str] = ()) -> Adapter:
    assignment = HarnessAssignment(harness=harness, features=tuple(sorted(features)))
    return Adapter(assignment, model=POOL, tools=tools)


def word() -> str:
    """A marker no other prompt holds."""
    return f"marker-{uuid4().hex}"


def chained(marker: str, source: str, target: str) -> Phase:
    """A prompt answered with two tool actions and a word: read `source`, then write to `target`
    the 32 hexadecimal digits the result of the read holds, reversed. Only a client that sent the
    file's text back can be told what to write."""

    def reverse(results: Sequence[str]) -> Write:
        found = SEED.search(results[0])
        assert found is not None
        return Write(target, found.group(0)[::-1])

    return Phase(marker, [lambda _: Read(source), reverse, lambda _: Say("done")])


def seeded(worker: LocalWorker, name: str) -> str:
    """A file of the working folder holding digits only this test knows; the digits."""
    digits = uuid4().hex
    (worker.work / name).write_text(digits + "\n", encoding="utf-8")
    return digits


def event(kind: str, parent: str | None = None, **more: object) -> str:
    """One line of Claude Code's output, in the shape the pinned version prints."""
    return json.dumps({"type": kind, "parent_tool_use_id": parent, **more})


def lifecycle(subtype: str, call: str, **more: object) -> str:
    """What the pinned Claude Code prints when the worker a tool call started starts, or has ended."""
    return json.dumps({"type": "system", "subtype": subtype, "tool_use_id": call, "task_id": f"task-{call}", **more})


def worker_started(call: str, kind: str = "local_agent") -> str:
    return lifecycle("task_started", call, task_type=kind)


def worker_ended(call: str) -> str:
    return lifecycle("task_notification", call, status="completed")


def tool_result(text: str, *, failed: bool) -> str:
    block = {"type": "tool_result", "tool_use_id": "made-up", "is_error": failed, "content": text}
    return event("user", message={"role": "user", "content": [block]})


def step(output: str = "", *, exit_code: int | None = 0, tmp_path: Path) -> Step:
    return Step(workers.Ran(exit_code, exit_code is None, False, tmp_path / "transcript"), output)


# The commands and the profiles.


@pytest.mark.parametrize("harness", list(Harness))
def test_a_harness_is_started_with_its_profile_and_only_the_forwarder_to_reach(harness: Harness) -> None:
    built = adapter(harness)
    profile = built.profile()
    held = [*profile.files.values(), *profile.environment.values(), *built.client("made-up prompt")]

    # Every address the harness is given is the trial's forwarder, as itself or with `/v1`.
    addresses = {found for text in held for found in re.findall(r"https?://[^\s\"'}]+", text)}
    assert {address.removesuffix("/v1") for address in addresses} - {"https://opencode.ai/config.json"} == {
        workers.FORWARDER_URL
    }
    # No variable holds a key but the placeholder, and the home's `rhi token` prints nothing else.
    keys = {value for name, value in profile.environment.items() if "KEY" in name}
    assert keys <= {PLACEHOLDER_KEY}
    assert PLACEHOLDER_KEY in profile.files["bin/rhi"]


def test_claude_code_and_codex_are_given_what_a_login_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    settings = login.configure_claude_code(workers.FORWARDER_URL)
    profile = login.configure_codex(workers.FORWARDER_URL, POOL)
    assert settings is not None
    assert profile is not None

    claude, codex = adapter(Harness.CLAUDE_CODE).profile(), adapter(Harness.CODEX).profile()

    assert claude.files[".claude/settings.json"] == settings.read_text(encoding="utf-8")
    assert codex.files[f".codex/{login.NAME}.config.toml"] == profile.read_text(encoding="utf-8")
    # Each harness looks for them where the adapter wrote them.
    assert claude.environment["CLAUDE_CONFIG_DIR"] == f"{workers.HOME}/.claude"
    assert codex.environment["CODEX_HOME"] == f"{workers.HOME}/.codex"


def test_each_harness_gets_its_headless_command() -> None:
    prompt = "made-up prompt"
    claude = adapter(Harness.CLAUDE_CODE).client(prompt)
    codex = adapter(Harness.CODEX).client(prompt)
    opencode = adapter(Harness.OPENCODE).client(prompt)
    pi = adapter(Harness.PI).client(prompt)
    loop = adapter(Harness.SDK_TOOL_LOOP).client(prompt)

    assert claude[:3] == ["claude", "-p", prompt]
    assert claude[claude.index("--output-format") + 1] == "stream-json"
    assert codex[:4] == ["codex", "exec", "--profile", login.NAME]
    assert codex[-1] == prompt
    assert opencode[:4] == ["opencode", "run", "--model", f"{adapters.PROVIDER}/{POOL}"]
    assert opencode[-1] == prompt
    assert pi[0] == "pi"
    assert "--print" in pi
    assert pi[pi.index("--model") + 1 :] == [POOL, prompt]
    assert loop[-3:] == [f"{workers.HOME}/{adapters.SDK_TOOL_LOOP_FILE}", POOL, prompt]


def test_claude_code_may_start_workers_only_for_a_task_that_delegates() -> None:
    def allowed(built: Adapter) -> set[str]:
        command = built.client("made-up prompt")
        return set(command[command.index("--allowedTools") + 1].split(","))

    alone, delegating = allowed(adapter(Harness.CLAUDE_CODE)), allowed(adapter(Harness.CLAUDE_CODE, DELEGATION))

    assert not alone & set(adapters.AGENT_TOOLS)
    assert delegating - alone == set(adapters.AGENT_TOOLS)
    assert {"Bash", "Read", "Write"} <= alone


def test_every_assignment_of_the_course_suite_has_an_adapter() -> None:
    suite = plan.load_suite().suite
    assigned = [assignment for task in suite.tasks for assignment in task.harnesses]

    assert {assignment.harness for assignment in assigned} == set(Harness)
    for assignment in assigned:
        assert Adapter(assignment, model=POOL).route is adapters.ROUTE_OF[assignment.harness]


def test_a_feature_a_harness_lacks_is_refused() -> None:
    with pytest.raises(Unsupported):
        adapter(Harness.CODEX, DELEGATION)
    with pytest.raises(Unsupported):
        adapter(Harness.PI, FOLLOW_UP)
    with pytest.raises(Unsupported):
        adapter(Harness.OPENCODE).client("made-up prompt", follow_up=True)


# The three wire formats.


@pytest.mark.parametrize("harness", list(Harness))
def test_a_tool_exchange_completes_on_the_harness_route_from_the_results_it_sent_back(
    harness: Harness, tmp_path: Path
) -> None:
    marker = word()
    with bench(tmp_path, harness, [Conversation([chained(marker, "seed.txt", "answer.txt")])]) as made:
        digits = seeded(made.worker, "seed.txt")
        trial = adapter(harness).trial(made.worker, [f"Do the task {marker}."])
        evidence = trial.ending.evidence(made.forwarder)
        generating = [one for one in made.pool.seen if one.route is not None]

    assert trial.ending == Ending(completed=True, exit_status=0), trial.steps[-1].output
    # The second action was written from the first one's result, and each was asked for in turn.
    assert (made.worker.work / "answer.txt").read_text(encoding="utf-8") == digits[::-1]
    assert made.pool.turns() == [(0, 0), (0, 1), (0, 2)]
    assert made.pool.faults == []
    # On the harness's route alone, to the pool, with the key the forwarder wrote: the harness
    # itself held the placeholder, from the profile it was given, and the forwarder's address.
    assert {one.route for one in generating} == {adapters.ROUTE_OF[harness]}
    assert {(one.model, one.authorization) for one in generating} == {(POOL, f"Bearer {CANARY_KEY}")}
    if harness is not Harness.SDK_TOOL_LOOP:
        started = made.worker.started(harness)
        assert (started["key"], started["address"].removesuffix("/v1")) == (PLACEHOLDER_KEY, made.forwarder.url)
    assert (evidence.model_turns, evidence.failure, evidence.invalid) == (3, None, None)


def test_the_three_wire_formats_are_the_routes_of_the_five_harnesses() -> None:
    assert set(adapters.ROUTE_OF) == set(Harness)
    assert set(adapters.ROUTE_OF.values()) == set(Route)


def test_a_local_tool_is_called_by_its_name(tmp_path: Path) -> None:
    marker = word()
    steps = [
        lambda _: Shell("lookup alpha"),
        lambda results: Write("answer.txt", results[0].strip()),
        lambda _: Say("done"),
    ]
    with bench(tmp_path, Harness.CLAUDE_CODE, [Conversation([Phase(marker, steps)])]) as made:
        tool = made.worker.root / "tools" / "search" / "lookup"
        tool.parent.mkdir()
        tool.write_text('#!/bin/sh\necho "found $1 in the packet"\n', encoding="utf-8")
        tool.chmod(0o755)
        built = adapter(Harness.CLAUDE_CODE, HarnessFeature.LOCAL_SEARCH, tools=["search"])
        trial = built.trial(made.worker, [f"Look it up, {marker}."])

    assert trial.ending.completed, trial.steps[-1].output
    assert (made.worker.work / "answer.txt").read_text(encoding="utf-8") == "found alpha in the packet"


# A follow-up in the same conversation.


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX])
def test_a_follow_up_continues_the_conversation_the_home_holds(harness: Harness, tmp_path: Path) -> None:
    first, second = word(), word()
    conversation = Conversation(
        [chained(first, "seed.txt", "answer.txt"), chained(second, "changed.txt", "revised.txt")]
    )
    with bench(tmp_path, harness, [conversation]) as made:
        digits, changed = seeded(made.worker, "seed.txt"), seeded(made.worker, "changed.txt")
        trial = adapter(harness, FOLLOW_UP).trial(made.worker, [f"Do the task {first}.", f"Now also {second}."])

    assert trial.ending.completed, trial.steps[-1].output
    assert len(trial.steps) == 2
    # The follow-up was served: its requests carried the first prompt and the results of its calls.
    assert made.pool.faults == []
    assert made.pool.turns() == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    assert (made.worker.work / "answer.txt").read_text(encoding="utf-8") == digits[::-1]
    assert (made.worker.work / "revised.txt").read_text(encoding="utf-8") == changed[::-1]
    # Each step's output is its own part of the worker's transcript.
    assert "".join(one.output for one in trial.steps) == made.worker.transcript.read_text(encoding="utf-8")


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX])
def test_a_follow_up_without_the_first_conversation_is_not_answered(harness: Harness, tmp_path: Path) -> None:
    first, second = word(), word()
    conversation = Conversation(
        [chained(first, "seed.txt", "answer.txt"), chained(second, "changed.txt", "revised.txt")]
    )
    with bench(tmp_path, harness, [conversation]) as made:
        built = adapter(harness, FOLLOW_UP)
        seeded(made.worker, "seed.txt")
        assert built.trial(made.worker, [f"Do the task {first}."]).ending.completed
        # The same prompt as a follow-up would be, given as a first prompt in another home.
        other = LocalWorker(tmp_path / "other", made.forwarder.url)
        seeded(other, "changed.txt")
        alone = built.trial(other, [f"Now also {second}."])

    assert made.pool.faults == [scripted.CONTEXT_MISSING]
    assert not alone.ending.completed
    assert not (other.work / "revised.txt").exists()


@pytest.mark.parametrize("counted", [True, False], ids=["turns-counted", "turns-not-counted"])
def test_a_follow_up_whose_conversation_is_lost_cannot_pass_on_the_first_prompts_work(
    counted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = word(), word()
    conversation = Conversation(
        [chained(first, "seed.txt", "answer.txt"), chained(second, "changed.txt", "revised.txt")]
    )
    accepted = Grade(properties=(True,), within_limits=True)
    with bench(tmp_path, Harness.CODEX, [conversation]) as made:
        seeded(made.worker, "seed.txt")
        seeded(made.worker, "changed.txt")
        run = made.worker.run

        def forgetful(command: Sequence[str]) -> workers.Ran:
            (made.worker.root / "home" / "codex-session.json").unlink(missing_ok=True)
            return run(command)

        monkeypatch.setattr(made.worker, "run", forgetful)
        turns = (lambda: made.forwarder.budget.model_turns) if counted else None
        trial = adapter(Harness.CODEX, FOLLOW_UP).trial(
            made.worker, [f"Do the task {first}.", f"Now also {second}."], turns=turns
        )
        evidence = trial.ending.evidence(made.forwarder)

    # The first prompt was worked on, and the follow-up ended before it asked the model anything.
    assert made.pool.turns() == [(0, 0), (0, 1), (0, 2)]
    assert (made.worker.work / "answer.txt").exists()
    assert not (made.worker.work / "revised.txt").exists()
    assert not trial.ending.completed
    record = evidence.trial_record(execution=0, trial=0, student=0, grade=accepted)
    assert (record.outcome, record.invalid) == (TrialOutcome.INVALID, InvalidReason.DRIVER_ERROR)


def test_a_follow_up_that_failed_after_it_asked_the_model_is_the_graders_when_its_turns_were_counted(
    tmp_path: Path,
) -> None:
    first, second = word(), word()
    # The follow-up is answered with a call the stand-in ends in failure on.
    broken = Phase(second, [lambda _: Raw("exec_command", "{}"), lambda _: Say("done")])
    with bench(tmp_path, Harness.CODEX, [Conversation([chained(first, "seed.txt", "answer.txt"), broken])]) as made:
        seeded(made.worker, "seed.txt")
        prompts = [f"Do the task {first}.", f"Now also {second}."]
        trial = adapter(Harness.CODEX, FOLLOW_UP).trial(
            made.worker, prompts, turns=lambda: made.forwarder.budget.model_turns
        )
        evidence = trial.ending.evidence(made.forwarder)

    assert made.pool.turns() == [(0, 0), (0, 1), (0, 2), (1, 0)]
    assert (trial.ending.completed, trial.ending.turns_before) == (False, 3)
    assert (evidence.model_turns, evidence.failure, evidence.invalid) == (4, None, None)


# The delegated workers.


def test_two_workers_that_ran_at_once_are_overlap_evidence() -> None:
    lines = [
        event("assistant"),
        event("assistant", "worker-one"),
        event("assistant", "worker-two"),
        event("user", "worker-two"),
        event("user", "worker-one"),
        event("result", is_error=False),
    ]
    assert adapters.delegation("\n".join(lines)) == Delegation(workers=2, overlapped=True)


def test_workers_that_ran_one_after_the_other_did_not_overlap() -> None:
    lines = [
        event("assistant"),
        event("assistant", "worker-one"),
        event("user", "worker-one"),
        event("user"),
        event("assistant", "worker-two"),
        event("user", "worker-two"),
        "not an event of Claude Code",
    ]
    assert adapters.delegation("\n".join(lines)) == Delegation(workers=2, overlapped=False)


def test_two_workers_alive_at_once_overlap_though_each_sent_a_single_answer() -> None:
    # As the pinned Claude Code prints it: both workers started, then each answered once and ended.
    lines = [
        worker_started("worker-one"),
        worker_started("worker-two"),
        event("assistant", "worker-one"),
        worker_ended("worker-one"),
        event("assistant", "worker-two"),
        worker_ended("worker-two"),
        event("result", is_error=False),
    ]
    assert adapters.delegation("\n".join(lines)) == Delegation(workers=2, overlapped=True)


def test_a_worker_that_ended_before_the_next_started_did_not_overlap_it() -> None:
    lines = [
        worker_started("worker-one"),
        event("assistant", "worker-one"),
        worker_ended("worker-one"),
        worker_started("worker-two"),
        event("assistant", "worker-two"),
        worker_ended("worker-two"),
    ]
    assert adapters.delegation("\n".join(lines)) == Delegation(workers=2, overlapped=False)


def test_a_worker_whose_end_was_not_printed_is_alive_to_the_end_of_the_output() -> None:
    lines = [worker_started("worker-one"), event("assistant", "worker-one"), worker_started("worker-two"), worker_ended("worker-two")]
    assert adapters.delegation("\n".join(lines)) == Delegation(workers=2, overlapped=True)


def test_what_else_runs_in_the_background_is_no_delegated_worker() -> None:
    lines = [worker_started("worker-one"), worker_started("a-shell", "local_bash"), worker_ended("a-shell"), worker_ended("worker-one")]
    assert adapters.delegation("\n".join(lines)) == Delegation(workers=1, overlapped=False)


def test_a_run_that_delegated_nothing_has_no_worker() -> None:
    assert adapters.delegation("\n".join([event("assistant"), event("result")])) == Delegation(0, False)


def test_a_trial_reports_delegation_only_for_a_task_that_delegates(tmp_path: Path) -> None:
    marker, part = word(), word()
    # The stand-in has no tool that starts a worker, as a harness that cannot delegate has none.
    steps = [lambda _: Delegate(f"A part of it, {part}."), lambda _: Say("done")]
    with bench(tmp_path, Harness.CLAUDE_CODE, [Conversation([Phase(marker, steps)])]) as made:
        delegating = adapter(Harness.CLAUDE_CODE, DELEGATION).trial(made.worker, [f"Split the task {marker}."])
        alone = adapter(Harness.CLAUDE_CODE).trial(made.worker, [f"Split the task {marker}."])

    assert made.pool.faults == [scripted.NO_TOOL]
    assert (delegating.delegation, alone.delegation) == (Delegation(0, False), None)


def test_a_transcript_that_cannot_be_read_gives_no_count_of_workers_and_an_invalid_trial(tmp_path: Path) -> None:
    class Unread:
        """A worker whose command ends in success, and whose transcript is not there to read."""

        transcript = tmp_path / "transcript"

        def run(self, command: Sequence[str]) -> workers.Ran:
            return workers.Ran(0, False, False, self.transcript)

    trial = adapter(Harness.CLAUDE_CODE, DELEGATION).trial(Unread(), ["made-up prompt"])

    # What was not read shows no worker, and none that there was no worker.
    assert trial.delegation is None
    assert trial.ending == Ending(completed=False, invalid=InvalidReason.DRIVER_ERROR)


# Tools that fail, and how a trial ended.


def test_a_tool_that_fails_is_the_models_to_handle_and_the_trial_completes(tmp_path: Path) -> None:
    marker = word()
    steps = [lambda _: Read("missing.txt"), lambda results: Write("answer.txt", results[0]), lambda _: Say("done")]
    with bench(tmp_path, Harness.SDK_TOOL_LOOP, [Conversation([Phase(marker, steps)])]) as made:
        trial = adapter(Harness.SDK_TOOL_LOOP).trial(made.worker, [f"Read it, {marker}."])
        evidence = trial.ending.evidence(made.forwarder)

    assert trial.ending.completed, trial.steps[-1].output
    assert evidence.may_pass
    # The model was told of the failure, as the tool's result, and went on from it.
    assert (made.worker.work / "answer.txt").read_text(encoding="utf-8") == "error: FileNotFoundError"


def test_calls_the_loop_cannot_carry_out_end_the_trial_as_malformed(tmp_path: Path) -> None:
    marker = word()
    broken = [
        lambda _: Raw("read_file", '{"path": '),
        lambda _: Raw("no_such_tool", "{}"),
        lambda _: Raw("read_file", "[]"),
    ]
    with bench(
        tmp_path, Harness.SDK_TOOL_LOOP, [Conversation([Phase(marker, [*broken, lambda _: Say("done")])])]
    ) as made:
        trial = adapter(Harness.SDK_TOOL_LOOP).trial(made.worker, [f"Read it, {marker}.", "never given"])
        evidence = trial.ending.evidence(made.forwarder)

    # Each refusal went back to the model, which wrote another call that is none: the loop stopped.
    assert made.pool.turns() == [(0, 0), (0, 1), (0, 2)]
    assert len(trial.steps) == 1
    assert (trial.ending.completed, trial.ending.failure) == (False, FailureReason.MALFORMED_TOOL_CALL)
    assert (evidence.failure, evidence.may_pass) == (FailureReason.MALFORMED_TOOL_CALL, False)


@pytest.mark.parametrize(
    "call",
    [
        {"type": "custom", "custom": {"name": "no_such_tool", "input": "made-up"}},
        {"type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        {"type": "function", "function": {"name": "write_file", "arguments": '{"path": "a.txt", "content": 1}'}},
    ],
    ids=["no-function", "required-argument-missing", "required-argument-no-text"],
)
def test_a_call_the_loop_has_no_way_to_run_is_refused_to_the_model_and_ends_as_malformed(
    call: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[list[object]] = []

    class Client:
        """The SDK's client, as far as the loop uses it: every answer is that call, as the SDK reads it."""

        def __init__(self) -> None:
            self.chat = self.completions = self

        def create(self, *, model: str, messages: Sequence[object], tools: Sequence[object]) -> ChatCompletion:
            asked.append(list(messages))
            message = {"role": "assistant", "tool_calls": [{"id": f"call-{len(asked)}", **call}]}
            choice = {"index": 0, "message": message, "finish_reason": "tool_calls"}
            answer = {"id": "made-up", "object": "chat.completion", "created": 0, "model": model, "choices": [choice]}
            return ChatCompletion.model_validate(answer)

    status = sdk_tool_loop.loop(Client(), POOL, "made-up prompt", tmp_path)

    # It is no answer of the model's and no failure of a tool: the loop stopped for the calls,
    assert status == sdk_tool_loop.MALFORMED_EXIT
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "a.txt").exists()
    # after it told the model of each, as the result of that call.
    assert len(asked) == sdk_tool_loop.MALFORMED_CALLS
    assert asked[-1][-1] == {"role": "tool", "tool_call_id": f"call-{len(asked) - 1}", "content": sdk_tool_loop.REFUSAL}


def test_a_model_that_corrects_a_malformed_call_completes(tmp_path: Path) -> None:
    marker = word()
    steps = [lambda _: Raw("read_file", '{"path": '), lambda _: Read("seed.txt"), lambda _: Say("done")]
    with bench(tmp_path, Harness.SDK_TOOL_LOOP, [Conversation([Phase(marker, steps)])]) as made:
        seeded(made.worker, "seed.txt")
        trial = adapter(Harness.SDK_TOOL_LOOP).trial(made.worker, [f"Read it, {marker}."])

    assert trial.ending == Ending(completed=True, exit_status=0), trial.steps[-1].output
    assert made.pool.turns() == [(0, 0), (0, 1), (0, 2)]


def test_claude_code_that_ends_on_its_own_refusal_of_a_call_failed_for_the_call(tmp_path: Path) -> None:
    built = adapter(Harness.CLAUDE_CODE)
    refusal = "<tool_use_error>InputValidationError: Bash was called with input that could not be parsed as JSON."
    refused = tool_result(refusal, failed=True)
    failed = tool_result("Exit code 1\ncat: missing.txt: No such file or directory", failed=True)
    error, success = event("result", is_error=True), event("result", is_error=False)

    def ended(*lines: str, exit_code: int = 1) -> Ending:
        return built.ending(step("\n".join(lines), exit_code=exit_code, tmp_path=tmp_path))

    assert ended(refused, error).failure is FailureReason.MALFORMED_TOOL_CALL
    # A tool that ran and failed is no refusal, and a refusal the model recovered from ends nothing.
    assert ended(failed, error) == Ending(completed=False, exit_status=1)
    assert ended(refused, failed, success, exit_code=0) == Ending(completed=True, exit_status=0)
    # A result that is an error is no completion, whatever the exit status.
    assert not ended(error, exit_code=0).completed


def test_a_step_the_wall_time_ended_is_a_timeout_and_a_harness_that_did_not_start_is_the_drivers(
    tmp_path: Path,
) -> None:
    built = adapter(Harness.CODEX, FOLLOW_UP)

    assert built.ending(step(exit_code=None, tmp_path=tmp_path)) == Ending(
        completed=False, failure=FailureReason.TIMEOUT
    )
    not_started = built.ending(step(exit_code=127, tmp_path=tmp_path))
    assert (not_started.completed, not_started.invalid) == (False, InvalidReason.DRIVER_ERROR)
    # An exit in failure with no reason of the adapter's: the forwarder and the grader decide.
    assert built.ending(step(exit_code=1, tmp_path=tmp_path)) == Ending(completed=False, exit_status=1)


@pytest.mark.parametrize(
    "fault",
    [workers.WorkerFault("made-up fault"), OSError(errno.ENOSPC, "made-up fault of the transcript")],
    ids=["runtime", "transcript"],
)
def test_a_runtime_or_a_transcript_that_fails_makes_the_trial_invalid(fault: Exception, tmp_path: Path) -> None:
    class Broken:
        transcript = tmp_path / "transcript"

        def run(self, command: Sequence[str]) -> workers.Ran:
            raise fault

    trial = adapter(Harness.PI).trial(Broken(), ["made-up prompt"])

    assert trial.steps == ()
    assert trial.ending == Ending(completed=False, invalid=InvalidReason.DRIVER_ERROR)


def test_a_harness_that_could_not_be_set_up_is_an_invalid_trial_whatever_the_grade(tmp_path: Path) -> None:
    accepted = Grade(properties=(True,), within_limits=True)
    with bench(tmp_path, Harness.CODEX, []) as made:
        # The profile's folder cannot be made: the launcher ends before there is a Codex.
        (made.worker.root / "home" / ".codex").write_text("occupied", encoding="utf-8")
        trial = adapter(Harness.CODEX).trial(made.worker, ["made-up prompt"])
        evidence = trial.ending.evidence(made.forwarder)

    assert not (made.worker.work / "started-codex.json").exists()
    assert (trial.ending.completed, trial.ending.invalid) == (False, InvalidReason.DRIVER_ERROR)
    assert trial.ending.exit_status in adapters.NOT_STARTED
    record = evidence.trial_record(execution=0, trial=0, student=0, grade=accepted)
    assert (record.outcome, record.invalid) == (TrialOutcome.INVALID, InvalidReason.DRIVER_ERROR)


def test_a_harness_that_failed_before_any_model_turn_cannot_pass(tmp_path: Path) -> None:
    marker = word()
    failed = Ending(completed=False, exit_status=1)
    with bench(tmp_path, Harness.PI, [Conversation([chained(marker, "seed.txt", "answer.txt")])]) as made:
        before = failed.evidence(made.forwarder)
        seeded(made.worker, "seed.txt")
        assert adapter(Harness.PI).trial(made.worker, [f"Do the task {marker}."]).ending.completed
        after = failed.evidence(made.forwarder)

    # With no turn asked of the model, the harness never worked on the task: the driver's error.
    assert (before.model_turns, before.invalid, before.may_pass) == (0, InvalidReason.DRIVER_ERROR, False)
    # One that ended in failure after it worked has no reason of the adapter's: the grader decides.
    assert (after.model_turns, after.failure, after.invalid) == (3, None, None)
