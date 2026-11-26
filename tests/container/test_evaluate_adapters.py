"""The pinned student harnesses, run headlessly in trial workers by their adapters, each make their
next tool action from the answer they were really given
(docs/design/agent-session-evaluation.md, "Course quality and the grader"; tools/evaluate/adapters.py).

Real workers of the prepared trial image are started through `evaluate.workers`, each with the real
forwarder of its trial (`evaluate.forwarding`) as its only way out. Behind the forwarder, in this
process, is the scripted pool (`evaluate.scripted`): no model, no key and no cloud account. Claude
Code, Codex, OpenCode, Pi and the SDK tool loop are the ones the image pins, started by
`evaluate.adapters` with the student's profile.

Each harness is told, by the pool, to read a file of its task and then to write what the file
held, reversed. The file holds digits only this test knows, so the second action can only be made
from the result of the first, as the harness sent it back. The harnesses with a follow-up are then
given a second prompt in the same worker, which the pool answers only when the request carries the
first conversation. Claude Code is also made to delegate to two workers, which the pool holds
until both have asked, to call a local tool by its name and to go on after a tool that failed.

It needs a container runtime, so CI's job `container-tests` runs it and the local gate does not
(tests/container/README.md). Nothing here skips: without a runtime the fixture fails.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

# The evaluation tool is a package under `tools`, which only its own tests put on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from evaluate import adapters, contracts, image, workers
from evaluate.adapters import Adapter, Delegation
from evaluate.contracts import Harness, HarnessAssignment, HarnessFeature
from evaluate.forwarding import Budget, Forwarder, run_clock
from evaluate.scripted import Conversation, Delegate, Phase, Pool, Read, Say, Shell, Write
from evaluate.workers import Limits, Spec, Worker, Workers

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping, Sequence

MEBIBYTE = 1024 * 1024
LIMITS = Limits(
    wall_seconds=300,
    memory_bytes=4096 * MEBIBYTE,
    cpu_millis=2000,
    pids=512,
    scratch_bytes=256 * MEBIBYTE,
    output_bytes=8 * MEBIBYTE,
)
# The limits of the plan: 12 minutes, 32 model turns, 32,000 generated tokens.
PLAN = contracts.Limits(
    trial_wall_time_ms=720_000, trial_model_turns=32, trial_generated_tokens=32_000, drain_ms=60_000
)
# The most one answer may generate. A request in flight holds its allowance of the trial's tokens,
# and with none set the whole of what is left: the forwarder would hold one worker's request until
# the other's had ended. With an eighth of the trial's each, two workers are answered at once.
ANSWER_TOKENS = PLAN.trial_generated_tokens // 8
POOL = "pool-under-test"
CANARY_KEY = "made-up-canary-key"
DIGITS = re.compile(r"[0-9a-f]{32}")
# How long the pool holds one delegated worker for the other, and the main loop for both.
HELD_SECONDS = 60
# A local tool, called by its name: it answers with a sentence that names what it was asked.
LOOKUP = '#!/bin/sh\necho "found $1 in the packet"\n'


def docker(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run one docker command to its end, whatever its result."""
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Iterator[image.Prepared]:
    """The trial image, prepared from the committed recipe under a name of this run alone."""
    name = f"rhi-evaluate-adapters-test-{uuid4().hex}"
    try:
        yield image.prepare(name, record=tmp_path_factory.mktemp("image") / "inventory.json")
    finally:
        docker("image", "rm", "--force", image.reference(name))


@dataclass
class Bench:
    """A trial's surroundings: the scripted pool, the forwarder in front of it, and the worker."""

    pool: Pool
    forwarder: Forwarder
    worker: Worker


@contextlib.contextmanager
def bench(
    prepared: image.Prepared,
    folder: Path,
    harness: Harness,
    conversations: Sequence[Conversation],
    *,
    files: Mapping[str, str],
    tools: Mapping[str, Path] | None = None,
) -> Generator[Bench]:
    """A trial worker of the image whose task holds `files`, behind a forwarder of its own."""
    task = folder / "task"
    task.mkdir()
    for name, text in files.items():
        (task / name).write_text(text, encoding="utf-8")
    budget = Budget(PLAN, run_clock(), request_tokens=ANSWER_TOKENS)
    with (
        Pool(conversations, model=POOL) as pool,
        Forwarder(pool.url, pool=POOL, key=CANARY_KEY, harness=harness, budget=budget) as forwarder,
        Workers(folder / "scratch") as started,
    ):
        address = urllib.parse.urlsplit(forwarder.url)
        assert address.hostname is not None
        assert address.port is not None
        spec = Spec(prepared.image_id, task, LIMITS, forwarder=(address.hostname, address.port), tools=tools or {})
        yield Bench(pool, forwarder, started.start(spec))


def submitted(worker: Worker, *names: str) -> dict[str, str]:
    """Those files of the working folder as the harness left them, read through the output folder."""
    copied = worker.run(["cp", *names, workers.OUTPUT])
    assert copied.exit_code == 0, copied.transcript.read_text(encoding="utf-8")[-4000:]
    folder = worker.collect()
    return {name: (folder / name).read_text(encoding="utf-8") for name in names}


def word() -> str:
    """A marker no other prompt holds."""
    return f"marker-{uuid4().hex}"


def reversed_digits(results: Sequence[str]) -> str:
    found = DIGITS.search(results[0])
    assert found is not None, "the request did not carry the file the harness was told to read"
    return found.group(0)[::-1]


def chained(marker: str, source: str, target: str, said: str = "done") -> Phase:
    """A prompt answered with two tool actions and a word: read `source`, then write to `target`
    the digits the result of the read holds, reversed."""
    return Phase(
        marker,
        [lambda _: Read(source), lambda results: Write(target, reversed_digits(results)), lambda _: Say(said)],
    )


def printed(trial: adapters.Trial) -> str:
    """The end of what a trial's last step printed, for a failure's message."""
    return trial.steps[-1].output[-4000:] if trial.steps else "no step ran"


@pytest.mark.parametrize("harness", list(Harness))
def test_a_pinned_client_makes_its_next_tool_action_from_the_result_it_sent_back(
    prepared: image.Prepared, harness: Harness, tmp_path: Path
) -> None:
    follows = HarnessFeature.FOLLOW_UP in adapters.SUPPORTED[harness]
    first, second = word(), word()
    digits, changed = uuid4().hex, uuid4().hex
    phases = [chained(first, "seed.txt", "answer.txt")]
    prompts = [f"Do the task {first}."]
    expected = {"answer.txt": digits[::-1]}
    if follows:
        phases.append(chained(second, "changed.txt", "revised.txt"))
        prompts.append(f"Now do the same for the changed file, {second}.")
        expected["revised.txt"] = changed[::-1]
    assignment = HarnessAssignment(harness=harness, features=(HarnessFeature.FOLLOW_UP,) if follows else ())
    files = {"seed.txt": digits + "\n", "changed.txt": changed + "\n"}

    with bench(prepared, tmp_path, harness, [Conversation(phases)], files=files) as made:
        trial = Adapter(assignment, model=POOL).trial(made.worker, prompts)
        evidence = trial.ending.evidence(made.forwarder)
        assert trial.ending.completed, printed(trial)
        left = submitted(made.worker, *expected)
        generating = [one for one in made.pool.seen if one.route is not None]

    # Each action was asked for in turn, the follow-up with the first conversation in its request,
    # and what was written is what the file the harness read held.
    assert left == expected
    assert made.pool.faults == []
    assert made.pool.turns() == [(phase, step) for phase in range(len(phases)) for step in range(3)]
    # On the harness's normal route alone, to the pool, with the key the forwarder wrote.
    assert {one.route for one in generating} == {adapters.ROUTE_OF[harness]}
    assert {(one.model, one.authorization) for one in generating} == {(POOL, f"Bearer {CANARY_KEY}")}
    assert (evidence.model_turns, evidence.failure, evidence.invalid) == (3 * len(phases), None, None)


def test_claude_code_delegates_to_workers_that_overlap_and_goes_on_after_a_tool_that_failed(
    prepared: image.Prepared, tmp_path: Path
) -> None:
    main, parts = word(), (word(), word())
    digits = uuid4().hex
    # The pool answers neither worker's first request before the other's has come, and the main
    # loop's next request only once both workers were given their last answer.
    both_asked = threading.Barrier(2, timeout=HELD_SECONDS)
    ended = threading.Semaphore(0)
    alone: list[int] = []

    def held(_: Sequence[str]) -> Read:
        try:
            both_asked.wait()
        except threading.BrokenBarrierError:
            alone.append(1)
        return Read("seed.txt")

    def last(_: Sequence[str]) -> Say:
        ended.release()
        return Say("part done")

    def after_the_workers(_: Sequence[str]) -> Shell:
        for _part in parts:
            ended.acquire(timeout=HELD_SECONDS)
        return Shell("lookup alpha")

    def part(number: int, marker: str) -> Conversation:
        steps = [held, lambda results: Write(f"part-{number}.txt", reversed_digits(results)), last]
        return Conversation([Phase(marker, steps)])

    steps = [
        lambda _: [Delegate(f"Do one part, {parts[0]}."), Delegate(f"Do the other part, {parts[1]}.")],
        after_the_workers,
        lambda results: Write("found.txt", results[0].strip()),
        lambda _: Read("missing.txt"),
        lambda results: Write("failed.txt", "told" if "missing.txt" in results[0] else "not told"),
        lambda _: Say("done"),
    ]
    conversations = [Conversation([Phase(main, steps)]), part(1, parts[0]), part(2, parts[1])]
    tool = tmp_path / "search"
    tool.mkdir()
    (tool / "lookup").write_text(LOOKUP, encoding="utf-8")
    (tool / "lookup").chmod(0o755)
    features = (HarnessFeature.DELEGATION, HarnessFeature.LOCAL_SEARCH)
    assignment = HarnessAssignment(harness=Harness.CLAUDE_CODE, features=features)

    with bench(
        prepared,
        tmp_path,
        Harness.CLAUDE_CODE,
        conversations,
        files={"seed.txt": digits + "\n"},
        tools={"search": tool},
    ) as made:
        trial = Adapter(assignment, model=POOL, tools=["search"]).trial(made.worker, [f"Split the task {main}."])
        evidence = trial.ending.evidence(made.forwarder)
        assert trial.ending.completed, printed(trial)
        left = submitted(made.worker, "part-1.txt", "part-2.txt", "found.txt", "failed.txt")

    # Two delegated workers ran, each from the file it read. Both had asked before either was
    # answered, and Claude Code's output shows that they were alive at once.
    assert alone == []
    assert trial.delegation == Delegation(workers=2, overlapped=True)
    assert (left["part-1.txt"], left["part-2.txt"]) == (digits[::-1], digits[::-1])
    assert made.pool.turns(1) == made.pool.turns(2) == [(0, 0), (0, 1), (0, 2)]
    # The local tool answered by its name, and the tool that failed was the model's to handle.
    assert left["found.txt"] == "found alpha in the packet"
    assert left["failed.txt"] == "told"
    assert made.pool.faults == []
    assert made.pool.turns()[: len(steps)] == [(0, step) for step in range(len(steps))]
    # The turns of the whole agent tree are counted by the one forwarder.
    assert evidence.model_turns >= len(steps) + 2 * 3
    assert (evidence.failure, evidence.invalid) == (None, None)
