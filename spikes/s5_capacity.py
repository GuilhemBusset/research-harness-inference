"""Spike S5: replayed agent sessions against a pool, what each run measured, and the verdict
against the floors (system design, sections 0, 4.7, 7.2, 7.3, 12 and 17).

What one instance serves is an estimate until it is measured (section 4.7). This job takes the
measurements: it replays agent sessions of both shapes against a pool, and reads what each run
measured, from the engine through the workspace and from the students' side through the load
generator's records. It prints each run's numbers, one JSON line a run, then posts the numbers of
all its runs and their verdict with `common.run_spike`, as one run of spike S5 (below).

**Nothing in it is the coursework pool's own.** The pool, the stream counts, the class size, the
duration and the lever are inputs, so the upgrade pool's spike S6 runs the same job with its own.

**The runs** (`plan`). For each shape of section 4.7, `hands-on` and `several-agents`, one run at
each number of streams the job is given, then one at the class size it is given:

- at a number of streams, that many agent sessions run at once: hands-on, one student each; several
  agents, two sessions a student (`loadgen.AGENTS_PER_STUDENT`), so half as many students, and an
  odd count leaves the last student one session;
- at the class size, every student of the class runs at once in the shape: hands-on, one session
  each, as many streams as students; several agents, two each, twice as many streams.

The students are the run's label. Every student is sent as a canary student of its own, with its
own key (`common.canary_keys`), as the load generator sends them (`loadgen.replay`, which keys each
session by its student): its own cache salt and limits, and its own in the records; the sessions of
a several-agents student share their student's key, as a real student's agents would. A run needs a
key for each of its students, and the job refuses, before it sends anything, runs that need more
keys than the spikes' secret holds.

**In a booked session.** The job starts no machine: the owner books a session of the pool first,
and before it sends anything the job checks it (`common.require_session`): the session is active,
it has the job's instances in service, and it lasts `RHI_SPIKE_RUN_MINUTES` from now. Otherwise
the job prints the reason, `no-session`, `too-few-instances` or `session-too-short`, sends nothing
to the gateway, and ends as not measured.

**One run** first waits until the gateway's status reads the pool ready, so that a run
measures a pool that serves. It then replays its sessions (`loadgen.agent_session`, of
section 4.7's sizes) for the duration it is given: no session starts a pause that would end after
the duration (`Until`), none sends a request once the duration has passed, however late a pause
ends (`loadgen.Load.sending`), and the run ends when the requests in flight have. A session holds
`TURNS_PER_SESSION` turns, which keeps its transcript inside the table's longest row; a session
that ends before the duration is followed, once every session of its round has ended, by a new
round of sessions, whose first turns are first turns again: new text after the opening.

**As Claude Code sends them** (`AsClaudeCode`). Every request carries the reasoning Claude Code
asks for by default, adaptive thinking at the effort `high`, so the pool's `reasoning_effort`
mapping decides what the engine is asked for, and the lever that maps it lower changes it. Every
session starts with the same opening, `OPENING_TOKENS` of Claude Code's instructions and tools
(section 4.7), as its system prompt, in the place of as many tokens of its transcript: the rest is
the session's own text. With the per-student cache salt each student computes the opening once;
with the class's salt (`cache_salt_scope = "class"`) the students share it.

**What the engine measured**, per instance, read from the workspace over the run's window by the
names of the per-engine table (`rhi_common.engine_metrics`), as section 7.2 defines each:

| Quantity | How |
| --- | --- |
| Output speed per stream | The mean: output tokens of the requests over their decode time. The median and the slowest tenth: the inverse of the time per output token at its 50th and 90th percentiles |
| Output throughput | Output tokens over the window's seconds |
| Prefill speed | Tokens computed in prefill over prefill time |
| Time to first token | The engine's histogram at the median and the 95th percentile, with the requests behind it |
| Cached share of input | Cached input tokens over input tokens |
| Cache use | The share in use, its mean and its peak |
| Preemptions | Their count in the window |
| Running and waiting requests | Each one's mean and peak |
| Input and output tokens per request | Each distribution's requests, by bucket |

A quantity an engine's row leaves out (the mock's has none of the further ones) reads `None`, not
zero, as does one with nothing to divide. Counters and histograms are increased per series before
they are summed per instance: an instance's counters restart from zero with its engine. The job
asks the workspace for no increase: Amazon Managed Prometheus refuses the `anchored` range
modifier, and a plain `increase` extrapolates to its range's edges. It reads each counter's samples
as they were scraped, with a range selector read one scrape after the run's end, over a range to
the millisecond from the lookback before the run's start (a stale marker is never among them), and
computes the increase itself (`counted_of`). A series' increase runs from its last sample at or
before the run's start, what the counter held when the run began, to its last sample, each drop
counted as a restart from zero: what the run added before its first scrape is counted, nothing
from before the run is, and nothing is extended past the samples seen. A series with no sample
within the lookback before the run's start, an instance that joined during the run, began at zero
with its engine, and its first sample is added in full; a series with one counts from it, even one
that went stale and resumed in the run. A series with no sample after the run's start counts
nothing, not zero. The quantiles are computed from the buckets so counted (`quantile_of`), as
`histogram_quantile` would. The scrape after the run's end holds the tokens of the last answers,
which the workspace sees only then; nothing else is sent to the pool in it. The gauges are read
over the run alone, on a grid of a second (`fine_step`) that ends at the run's end, so that every
scrape of the run shows, however short the run. Rates are over the run's seconds.

**What each instance's lifecycle agent reported**: the cache capacity in tokens the engine gave at
start (`rhi_lifecycle.cache.METRIC`), the last reported from a scrape before the run to a scrape
after it, and its release (`rhi_lifecycle.serving.RELEASE_METRIC`), which an instance writes only
while in service: the build, its `model_revision`, and from the times of its samples, the seconds
the instance was in service during the window, within a scrape of a sample, so that a warm instance
stopped between two periods of service is not charged for the gap. The run reports the instances
that served in it, and only those. The samples are read on the same fine grid to one
scrape past the window's end, so that an instance in service at the end is seen there, and a run
shorter than a scrape, whose instances are first scraped after it ends, is still labelled. An
instance whose last sample is older than a scrape before the window, which the workspace still
returns for some minutes, or whose first is a whole scrape after it, did not serve in it and is
left out, as is a release an instance reported only that far from the window. A pool whose
instances report two builds in one window is refused: the run is no one build's.

**What the students got**, from the load generator's records (`experience_of`): the time to first
token at the median and the 95th percentile, for later turns and for first turns apart; the speed
per stream at the median and the slowest tenth; and the cost per million output tokens
(`cost_per_million`): each instance's seconds in service at the configured price of its instance
type, over the output tokens the records report.

**The failed requests** (`failed`): every request whose record is not answered 200, or names one of
the gateway's causes (`rhi_common.causes.Cause`). A stream answered 200 and then ended by the
gateway's error event, as an engine restart ends one, is among them: the load generator records the
event's cause. Each cause has one status, so the counts by cause, with the streams ended by an
error event counted apart, are counts by status and cause. A failed request that named no cause of
the gateway's, such as an answer of the edge, is counted without a cause, so the counts by cause and
that one add up to the failed requests.

**The label** of each run: the pool, the instance types that ran, the build, the lever, the shape,
the number of streams, the students, and the class size when the run is at it.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s5_capacity`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, or an `https://` address |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL` | The pool, `coursework` unless given |
| `RHI_S5_STREAMS` | The numbers of streams, separated by commas: `1,4,8,16` unless given |
| `RHI_S5_CLASS_SIZE` | The class's size, in students |
| `RHI_S5_DURATION_SECONDS` | How long each run sends requests, in seconds |
| `RHI_S5_LEVER` | The name of the lever the pool is deployed with, lower-case letters, digits and hyphens: `baseline` unless given |
| `RHI_S5_BUILD` | The name of the build the pool serves, lower-case letters and digits: `8bit` or `4bit` |
| `RHI_SPIKE_INSTANCES` | The instances the job runs on, which the session has in service: `1` unless given |
| `RHI_SPIKE_RUN_MINUTES` | How long the job lasts, in minutes, which the session must outlast. Required |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |
| `RHI_S5_WINDOW_TOKENS` | The window at which each session compacts, in tokens, for the smaller window of section 4.7; unset, no session compacts |

The pool's engine and prices, and the floors, come from the deployed configuration
(`rhi.config.read_ssm`), the region from CodeBuild's `AWS_REGION`. Each lever is a configuration
the owner deploys before starting the job; the job never changes the pool. Run it while nothing
else sends to the pool.

**The instances.** The job is told how many instances it runs on (`RHI_SPIKE_INSTANCES`): one
number, which the session must have in service before the job sends anything, which labels the
run, and which every run must be served by. The job
refuses, as not measured, a run that any other number of instances served, or that one of them
did not serve from its start to its end (`held_at`): the numbers of a job are always those many
instances'. `RHI_S5_INSTANCES`, the variable's name before the job ran in a session, is not read.
What one instance serves is a job at one; the class's size
over the session's instances is a job at the session's instances (`docs/measurements/s5.md` says
how the owner holds the pool there).

**A smaller window** is a setting of the client, not of the pool: Claude Code compacts a session
when its transcript nears its window. With `RHI_S5_WINDOW_TOKENS`, each session compacts as such a
client would (`Windowed`): a turn that would take its transcript past the window starts a new
transcript of `COMPACTED_TOKENS`, the opening then `SUMMARY_TOKENS` of new text, then the turn's
input, which the engine computes in full but for the opening. Without it no session compacts: a
session's transcript stays inside the 200,000 tokens of Claude Code's own window.

**The numbers** (`Number`), one per quantity (`Figure`) and condition: the shape, and the number of
streams or the class's size. A name is `<quantity>_<shape>_<streams>`, or `<quantity>_<shape>_class`
at the class's size, with the shape `hands_on` or `several_agents`: `ttft_p95_hands_on_8`,
`stream_median_several_agents_class`. The numbers of streams are those of `DECLARED_STREAMS`, and
the job refuses any other before it sends anything. `class_size` is the class's size itself. A
quantity with no sample behind it is left out of a run's numbers. The failed requests are posted as
their count, `failed_requests`, and as `failed_share`, their percentage of the requests sent, for
every run that sent a request; then, each only when above zero, as one count for each cause of the
gateway's, `failed_` followed by the cause's name (`failed_engine_error_several_agents_class`), as
`failed_in_stream`, those answered 200 whose stream then carried the error event, which are also
counted under their cause, and as `failed_without_cause`. The students' quantities come from
the load generator's records; the engine's, from its instances: the output throughput, the prefill
speed and the cached share are their means over the instances, the cache capacity the smallest any
reported, the running requests the largest peak, and the preemptions their sum, given only when
every instance reported its count.

**The verdict** (`spike_of`) reads the class-size runs of both shapes only. They pass when each
has a 95th-percentile time to first token of later turns under `target_ttft_p95_seconds`, a median
stream at `target_stream_median_tokens_per_second` or more, a slowest tenth at
`target_stream_p10_tokens_per_second` or more, no preemption, and a failed share at
`target_failed_requests_percent` or less. The first three keys of the configuration (`rhi.config`,
issue E4-31) default to section 17's 5 seconds, 15 and 10 tokens a second. The times and the speeds
come from the requests that got an answer, so the failed share is read beside them: without it the
survivors of a failing run could pass. The design gives no floor for it; the key's default, 0.5, is
the share of failures that section 7.3's objective for the gateway's answers allows, until the
owner confirms it or names another. A class-size number that misses its floor fails the job, a
preemption or a failed share among them, whatever else is missing. Short of that, a job whose
class-size runs lack one of those numbers (an engine that does not count preemptions, a run that
sent no request and so has no failed share) has no verdict, and is posted as not measured. The
numbers of a job are one build's: a run served by another build than the job's earlier runs stops
the job, as not measured.

**The record.** The run label is `<build>-<lever>-<instances>`. `record_statuses` writes a commit's
runs of S5 into `docs/measurements/s5.md` (`tools/measurements/record.py`), and then gives each
row its quantity as its name, and the conditions as its labels: the run, the pool, the instance
type, the build, the lever, the instances, the session's shape and the streams (`streams = "class"`
at the class's size). The same quantity of every run and condition is then one name, and the
record holds the whole matrix:

    gh api --paginate "repos/{owner}/{repo}/commits/<sha>/statuses" |
        env PYTHONPATH=src:services/common:services/gateway:services/reporter:services/engine/lifecycle:spikes \\
        uv run --locked python spikes/s5_capacity.py record --commit <sha> --instance-type <type>

It reads the pages `gh api --paginate` writes one after the other (`statuses_of`). Each run is read
from its newest posting alone (`postings_of`): a retry of a run label is not joined to that run's
older parts, and a run is a pass only from one whole posting. A job posts `numbers_posted`, how
many numbers its posting holds, so a posting is whole once that many are listed, whatever the
order the reporter took them in: a posting with some not listed yet is written pending, and one
that began before the posting before it had ended, which nothing in the statuses tells apart from
it, is written pending with no row. A status listed twice, on two pages, is read once. `--run`, repeated, names the runs
to write, every run of S5 on the commit unless given; a run the record already holds measured on
another pool or instance type is refused, with nothing written, so the runs of each instance type
are written apart, each with its own. A job is one instance type's, since its rows take one: one
whose runs another type served, or two, is refused (`Job.measure`). GitHub dates a status to the second: a retry in the same
second as the run before it is told apart by the statuses' ids.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import importlib.util
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import boto3

import common
import loadgen
from rhi import config
from rhi_common.causes import Cause
from rhi_common.engine_metrics import Further, Kind, Name, Quantity, further_names, metric_names
from rhi_lifecycle.cache import METRIC as CACHE_CAPACITY_METRIC
from rhi_lifecycle.serving import RELEASE_METRIC
from rhi_reporter.measure import MEASURE_CONTEXT, Verdict, context_parts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Generator, Iterable, Mapping, Sequence
    from types import ModuleType

JOB = "s5_capacity"
MEASUREMENT = "S5"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_VARIABLE = "RHI_SPIKE_POOL"
STREAMS_VARIABLE = "RHI_S5_STREAMS"
CLASS_SIZE_VARIABLE = "RHI_S5_CLASS_SIZE"
DURATION_VARIABLE = "RHI_S5_DURATION_SECONDS"
LEVER_VARIABLE = "RHI_S5_LEVER"
BUILD_VARIABLE = "RHI_S5_BUILD"
WINDOW_VARIABLE = "RHI_S5_WINDOW_TOKENS"
DEFAULT_POOL = "coursework"
DEFAULT_STREAMS = (1, 4, 8, 16)
DEFAULT_LEVER = "baseline"
# The numbers of streams the names of the numbers are declared for: section 4.7's 1, 4, 8 and 16,
# and the larger pools' 32 and 64, which spike S6 runs with this job.
DECLARED_STREAMS = (1, 4, 8, 16, 32, 64)
# Tokens. Claude Code's opening, its instructions and its tools, about 20,000 tokens (section 4.7):
# the same text at the start of every session of every student, which a cache salt shared by the
# class lets the students share.
OPENING_TOKENS = 20_000
# A seed no session of the job draws its text from: the opening's.
OPENING_SEED = 2**63
# Tokens. The summary a compacted session holds after the opening: new text.
SUMMARY_TOKENS = 5_000
# Tokens. The transcript a compacted session goes on from: the opening, then the summary.
COMPACTED_TOKENS = OPENING_TOKENS + SUMMARY_TOKENS
# The reasoning Claude Code asks for by default (tests/captures/profile-reasoning-effort/): adaptive
# thinking at the effort `high`, which the pool's `reasoning_effort` mapping rewrites (section 6.2).
CLAUDE_CODE_THINKING = {"type": "adaptive"}
CLAUDE_CODE_EFFORT = "high"
# The smallest window a session may compact at: a compacted transcript, the largest turn and its answer.
SMALLEST_WINDOW_TOKENS = COMPACTED_TOKENS + loadgen.TURN_INPUT_TOKENS[1] + loadgen.TURN_OUTPUT_TOKENS

# Turns after a session's first. A transcript starts at up to 100,000 tokens and a turn adds up to
# 5,000 and the answer before it: ten keep it inside section 4.7's longest row, 150,000.
TURNS_PER_SESSION = 10
# Seconds. The collector scrapes every 15 seconds; the samples of a run's end reach the workspace
# within a few scrapes, and the job waits that long before it reads.
SCRAPE_SECONDS = 15
SETTLE_SECONDS = 4 * SCRAPE_SECONDS
# Seconds. Prometheus' lookback delta, its default: how far back before the run's start the job
# looks for the sample a counter's increase starts from, as the `anchored` modifier would.
LOOKBACK_SECONDS = 300
# Seconds. Until the pool is ready its status is read this often, for as long as a cold launch
# may take at worst (spike S1).
POLL_SECONDS = 5
READY_WAIT_SECONDS = 40 * 60
READY = "ready"

_LEVER = re.compile(r"[a-z0-9-]+")
_BUILD = re.compile(r"[a-z0-9]+")
# A run label of the job: its build, its lever and its instances.
_RUN = re.compile(r"(?P<build>[a-z0-9]+)-(?P<lever>[a-z0-9-]+)-(?P<instances>[1-9][0-9]*)")


class Shape(StrEnum):
    """The two shapes of section 4.7."""

    HANDS_ON = "hands-on"
    SEVERAL_AGENTS = "several-agents"


# The runs.


@dataclass(frozen=True)
class Planned:
    """One run to make: its shape, the sessions it runs at once, the students they are spread over,
    and the class size when it is the run at the class's size."""

    shape: Shape
    streams: int
    students: int
    class_size: int | None = None


def plan(streams: Sequence[int], class_size: int) -> list[Planned]:
    """The runs, for each shape: one at each number of streams, then one at the class size.
    ValueError for no number of streams, or a count below one."""
    if not streams or any(count < 1 for count in (*streams, class_size)):
        raise ValueError("spike S5 runs one stream or more, and a class of one student or more")
    per = loadgen.AGENTS_PER_STUDENT
    runs = []
    for shape in Shape:
        for count in streams:
            students = count if shape is Shape.HANDS_ON else -(-count // per)
            runs.append(Planned(shape, count, students))
        sessions = class_size if shape is Shape.HANDS_ON else class_size * per
        runs.append(Planned(shape, sessions, class_size, class_size))
    return runs


def load_of(planned: Planned, model: str, seed: int, window_tokens: int | None = None) -> loadgen.Load:
    """The sessions of one round of `planned`, for the pool `model` names, drawn from `seed` on,
    each student's sessions sent as that canary student (`loadgen.hands_on` and
    `loadgen.several_agents`, an odd count of several agents leaving its last student one); each
    compacting at `window_tokens` when it is given, and each sent as Claude Code sends it
    (`AsClaudeCode`)."""
    synthetic = [loadgen.agent_session(model, TURNS_PER_SESSION, seed + number) for number in range(planned.streams)]
    sessions: tuple[loadgen.Session, ...] = (
        tuple(synthetic) if window_tokens is None else tuple(Windowed(one, window_tokens) for one in synthetic)
    )
    per = 1 if planned.shape is Shape.HANDS_ON else loadgen.AGENTS_PER_STUDENT
    return loadgen.Load(tuple(AsClaudeCode(one) for one in sessions), per)


@functools.cache
def opening() -> str:
    """Claude Code's opening as the job's sessions send it: `OPENING_TOKENS` of text, drawn as a
    synthetic session draws its own, from `OPENING_SEED`."""
    _, first = next(loadgen.SyntheticSession("", OPENING_TOKENS, (), 0, OPENING_SEED).requests())
    return json.loads(first.body)["messages"][0]["content"]


def as_claude_code(request: loadgen.Request) -> loadgen.Request:
    """`request`, a synthetic session's, as Claude Code would send it: the opening every session
    shares as its system prompt, in the place of as many tokens at the start of its transcript, so
    the transcript keeps its size; and the reasoning Claude Code asks for by default."""
    body = json.loads(request.body)
    first, *rest = body["messages"]
    words = first["content"].split(" ")
    if len(words) > OPENING_TOKENS:
        first = {**first, "content": " ".join(words[OPENING_TOKENS:])}
    body |= {
        "system": opening(),
        "messages": [first, *rest],
        "thinking": CLAUDE_CODE_THINKING,
        "output_config": {"effort": CLAUDE_CODE_EFFORT},
    }
    return replace(request, body=json.dumps(body).encode())


@dataclass(frozen=True)
class AsClaudeCode:
    """`session`, each of its requests sent as Claude Code would send it (`as_claude_code`)."""

    session: loadgen.Session

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        steps = self.session.requests()
        step = next(steps, None)
        while step is not None:
            answer = yield step[0], as_claude_code(step[1])
            try:
                step = steps.send(answer)
            except StopIteration:
                return


@dataclass(frozen=True)
class Windowed:
    """`session` as a client that compacts at `window_tokens` sends it: a turn that would take the
    transcript, as its sizes count it, past the window starts a new transcript instead, of
    `COMPACTED_TOKENS` of new text and the turn's input, after the turn's pause; `AsClaudeCode`
    puts the opening at its start."""

    session: loadgen.SyntheticSession
    window_tokens: int

    def parts(self) -> list[tuple[float, loadgen.SyntheticSession]]:
        """The transcripts the session is sent as, each with the pause before its first request."""
        whole = self.session
        held, parts = whole.start_tokens, [(0.0, whole.start_tokens, list[loadgen.Turn]())]
        for turn in whole.turns:
            added = whole.output_tokens + turn.input_tokens
            if held + added > self.window_tokens:
                held = COMPACTED_TOKENS + turn.input_tokens
                parts.append((turn.pause_seconds, held, []))
            else:
                held += added
                parts[-1][2].append(turn)
        # Each later transcript is new text: a seed no session of the job draws its own from.
        return [
            (pause, replace(whole, start_tokens=start, turns=tuple(turns), seed=(whole.seed + 1) * 2**32 + number))
            if number
            else (pause, replace(whole, turns=tuple(turns)))
            for number, (pause, start, turns) in enumerate(parts)
        ]

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        for pause, part in self.parts():
            steps = part.requests()
            _, request = next(steps)
            step = (pause, request)
            while True:
                answer = yield step
                try:
                    step = steps.send(answer)
                except StopIteration:
                    break


@dataclass(frozen=True)
class Until:
    """A session that sends no request once `deadline`, on `clock`, has passed: none whose pause
    would end at the deadline or after it."""

    session: loadgen.Session
    deadline: float
    clock: Callable[[], float]

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        steps = self.session.requests()
        step = next(steps, None)
        while step is not None and self.clock() + step[0] < self.deadline:
            answer = yield step
            try:
                step = steps.send(answer)
            except StopIteration:
                return


# What the students got.


@dataclass(frozen=True)
class Experience:
    """What the records of a run say each request got. A number with no sample behind it is None."""

    requests: int
    failed: int
    later_turns: int
    first_turns: int
    first_token_seconds_median: float | None
    first_token_seconds_p95: float | None
    first_turn_first_token_seconds_median: float | None
    first_turn_first_token_seconds_p95: float | None
    tokens_per_second_median: float | None
    tokens_per_second_slowest_tenth: float | None
    output_tokens: int
    failed_by_cause: Mapping[str, int] = field(default_factory=dict)
    """How many of the failed requests named each cause of the gateway's, by the cause's name; a
    cause none named is not there. The failed requests they leave named no cause."""
    failed_in_stream: int = 0
    """How many of them were answered 200, their stream then carrying the gateway's error event."""


CAUSES = frozenset(cause.value for cause in Cause)


def named_cause(record: loadgen.Record) -> bool:
    """Whether the record names one of the gateway's causes, in its answer or in an error event."""
    return record.cause in CAUSES


def failed(record: loadgen.Record) -> bool:
    """Whether the request failed: not answered 200, or one of the gateway's causes named, as by the
    error event that ends a stream answered 200."""
    return record.status != 200 or named_cause(record)


def _at(samples: Sequence[float], percent: float) -> float | None:
    return common.percentile(samples, percent) if samples else None


def experience_of(records: Sequence[loadgen.Record]) -> Experience:
    """The time to first token at the median and the 95th percentile, later turns and first turns
    apart; the speed per stream at the median and the slowest tenth, its 10th percentile; the
    output tokens the answers reported; and the failed requests (`failed`), by the cause they named."""
    later = [r.first_token_seconds for r in records if not r.first_turn and r.first_token_seconds is not None]
    first = [r.first_token_seconds for r in records if r.first_turn and r.first_token_seconds is not None]
    speeds = [r.output_tokens_per_second for r in records if r.output_tokens_per_second is not None]
    named = Counter(record.cause for record in records if named_cause(record))
    return Experience(
        requests=len(records),
        failed=sum(failed(record) for record in records),
        later_turns=len(later),
        first_turns=len(first),
        first_token_seconds_median=_at(later, 50),
        first_token_seconds_p95=_at(later, 95),
        first_turn_first_token_seconds_median=_at(first, 50),
        first_turn_first_token_seconds_p95=_at(first, 95),
        tokens_per_second_median=_at(speeds, 50),
        tokens_per_second_slowest_tenth=_at(speeds, 10),
        output_tokens=sum(record.output_tokens or 0 for record in records),
        failed_by_cause={cause.value: named[cause.value] for cause in Cause if named[cause.value]},
        failed_in_stream=sum(record.status == 200 and named_cause(record) for record in records),
    )


# What the lifecycle agents reported.


@dataclass(frozen=True)
class InService:
    """An instance's release in the window: its instance type, its build, and its seconds in service."""

    instance_type: str
    build: str
    seconds: float


def release_query(pool: str) -> str:
    """The times of the pool's release samples, with their labels: a range query over the window."""
    return f'timestamp({RELEASE_METRIC}{{pool="{pool}"}})'


def in_service_of(release: Sequence[common.Series], start: float, end: float) -> dict[str, InService]:
    """Each instance's release from `start` to `end`, from the times of its release samples
    (`release_query`, read to a scrape past `end`): in service within a scrape of each sample, since
    a sample shows the instance in service at some time in the scrape before it, so from a scrape
    before its first sample to a scrape after its last, inside the window, but not across a gap in
    its samples longer than two scrapes, a warm instance stopped and started again. Only the
    releases whose samples so give some seconds in the window count: an instance with none did not
    serve in the window and is not in the answer, and a release it reported only a whole scrape
    before or after the window, one it served outside the run, labels nothing; one first scraped
    after a short window ended still labels it. ValueError for an instance whose releases in the
    window carry no instance type, or are two."""
    times: dict[str, dict[tuple[str, str], list[float]]] = {}
    for one in release:
        instance = one.labels.get("instance", "")
        if instance:
            kind = (one.labels.get("instance_type", ""), one.labels.get("model_revision", ""))
            times.setdefault(instance, {}).setdefault(kind, []).extend(one.values())
    found = {}
    for instance, by_kind in times.items():
        served = {kind: seconds_covered(taken, start, end) for kind, taken in by_kind.items()}
        served = {kind: seconds for kind, seconds in served.items() if seconds > 0}
        if not served:
            continue
        if len(served) != 1 or not all(next(iter(served))):
            raise ValueError("an instance reported no instance type, no build, or two of either in the window")
        (((kind, build), seconds),) = served.items()
        found[instance] = InService(kind, build, seconds)
    return found


def seconds_covered(taken: Sequence[float], start: float, end: float) -> float:
    """The seconds from `start` to `end` within a scrape either side of a time of `taken`: their
    union, inside the window."""
    seconds, covered = 0.0, start
    for at in sorted(set(taken)):
        low, high = max(covered, at - SCRAPE_SECONDS), min(end, at + SCRAPE_SECONDS)
        if high > low:
            seconds, covered = seconds + high - low, high
    return seconds


def build_of(in_service: Mapping[str, InService]) -> str:
    """The one build the pool's instances served. ValueError for none, or two."""
    builds = {one.build for one in in_service.values()}
    if len(builds) != 1:
        raise ValueError(f"the pool's instances reported {len(builds)} builds in the run, not one")
    (build,) = builds
    return build


def cost_per_million(
    in_service: Mapping[str, InService], prices: Mapping[str, float], output_tokens: int
) -> float | None:
    """Dollars per million output tokens: each instance's seconds in service at the configured
    hourly price of its type, over `output_tokens`. None for no token. ValueError for an instance of
    a type with no price."""
    if any(one.instance_type not in prices for one in in_service.values()):
        raise ValueError("an instance of a type the pool does not price ran")
    if output_tokens <= 0:
        return None
    dollars = sum(prices[one.instance_type] * one.seconds / 3600 for one in in_service.values())
    return dollars / output_tokens * 1_000_000


# What the engine measured.


class Key(StrEnum):
    """One query of the engine's quantities, each answered per instance."""

    OUTPUT_TOKENS = "output_tokens"
    INPUT_TOKENS = "input_tokens"
    CACHED_INPUT_TOKENS = "cached_input_tokens"
    FIRST_TOKEN_REQUESTS = "first_token_requests"
    FIRST_TOKEN_MEDIAN = "first_token_median"
    FIRST_TOKEN_P95 = "first_token_p95"
    TOKEN_TIME_MEDIAN = "token_time_median"
    TOKEN_TIME_P90 = "token_time_p90"
    RUNNING_MEAN = "running_mean"
    RUNNING_PEAK = "running_peak"
    WAITING_MEAN = "waiting_mean"
    WAITING_PEAK = "waiting_peak"
    CACHE_USE_MEAN = "cache_use_mean"
    CACHE_USE_PEAK = "cache_use_peak"
    CACHE_CAPACITY = "cache_capacity"
    REQUEST_OUTPUT_TOKENS = "request_output_tokens"
    DECODE_SECONDS = "decode_seconds"
    PREFILL_COMPUTED_TOKENS = "prefill_computed_tokens"
    PREFILL_SECONDS = "prefill_seconds"
    PREEMPTIONS = "preemptions"
    INPUT_DISTRIBUTION = "input_distribution"
    OUTPUT_DISTRIBUTION = "output_distribution"


def selector(name: Name, suffix: str, pool: str) -> str:
    """The series of `name` on the pool, the samples with `suffix` after the family's name: the
    pool's, and of those the part the name's own labels select."""
    matchers = [f'pool="{pool}"', *(f'{label}="{value}"' for label, value in name.labels.items())]
    return f"{name}{suffix}{{{','.join(matchers)}}}"


def range_of(seconds: float) -> str:
    """A range of `seconds` to the millisecond, so that it starts where the run did, not at a whole
    second before it: in seconds when they are whole, else in milliseconds."""
    millis = max(1, round(seconds * 1000))
    return f"{millis // 1000}s" if millis % 1000 == 0 else f"{millis}ms"


class How(StrEnum):
    """How the answer to one query becomes one number an instance."""

    SUM = "sum"
    """A counter's samples, as scraped, their increase over the run computed (`counted_of`)."""
    QUANTILE = "quantile"
    """A histogram's buckets, so counted, then its quantile at `Read.fraction`."""
    MEAN = "mean"
    """A gauge, read over the run on a fine grid: the mean of its points."""
    PEAK = "peak"
    """The same: the largest of its points."""
    LAST = "last"
    """A number read at one time as it is."""


@dataclass(frozen=True)
class Read:
    """One query of the engine's quantities, and how its answer becomes a number an instance. A
    counter's or a histogram's query is the selector of its series, whose samples the job reads."""

    query: str
    how: How
    fraction: float = 0.0


def reads(pool: str, engine: str, window_seconds: float, counted_seconds: float | None = None) -> dict[Key, Read]:
    """The reads of the engine's quantities on the pool, by the names of `engine`'s row; a further
    quantity the row leaves out has none. The counters and histograms are their series' selectors,
    whose increase over the run the job computes from their samples (`counted_of`); the lifecycle
    agent's cache capacity is the last it reported over the `counted_seconds` that end where it is
    read, the window's unless given, and a scrape before them; the gauges are read over the run as
    they are."""
    names, further = metric_names(engine), further_names(engine)
    counted_seconds = window_seconds if counted_seconds is None else counted_seconds

    def increase(name: Name, suffix: str = "") -> Read:
        if not suffix:
            suffix = "_total" if name.kind is Kind.COUNTER else ""
        return Read(selector(name, suffix, pool), How.SUM)

    def quantile(fraction: float, name: Name) -> Read:
        return Read(selector(name, "_bucket", pool), How.QUANTILE, fraction)

    def gauge(how: How, aggregate: str, name: Name) -> Read:
        return Read(f"{aggregate} by (instance) ({selector(name, '', pool)})", how)

    ttft, tpot = names[Quantity.TIME_TO_FIRST_TOKEN], names[Quantity.TIME_PER_OUTPUT_TOKEN]
    capacity = f'{CACHE_CAPACITY_METRIC}{{pool="{pool}"}}[{range_of(counted_seconds + SCRAPE_SECONDS)}]'
    found = {
        Key.OUTPUT_TOKENS: increase(names[Quantity.OUTPUT_TOKENS]),
        Key.INPUT_TOKENS: increase(names[Quantity.INPUT_TOKENS]),
        Key.CACHED_INPUT_TOKENS: increase(names[Quantity.CACHED_INPUT_TOKENS]),
        Key.FIRST_TOKEN_REQUESTS: increase(ttft, "_count"),
        Key.FIRST_TOKEN_MEDIAN: quantile(0.5, ttft),
        Key.FIRST_TOKEN_P95: quantile(0.95, ttft),
        Key.TOKEN_TIME_MEDIAN: quantile(0.5, tpot),
        # The slowest tenth of the streams' speed is the longest tenth of the time per token.
        Key.TOKEN_TIME_P90: quantile(0.9, tpot),
        Key.RUNNING_MEAN: gauge(How.MEAN, "sum", names[Quantity.RUNNING_REQUESTS]),
        Key.RUNNING_PEAK: gauge(How.PEAK, "sum", names[Quantity.RUNNING_REQUESTS]),
        Key.WAITING_MEAN: gauge(How.MEAN, "sum", names[Quantity.WAITING_REQUESTS]),
        Key.WAITING_PEAK: gauge(How.PEAK, "sum", names[Quantity.WAITING_REQUESTS]),
        Key.CACHE_USE_MEAN: gauge(How.MEAN, "max", names[Quantity.CACHE_USAGE]),
        Key.CACHE_USE_PEAK: gauge(How.PEAK, "max", names[Quantity.CACHE_USAGE]),
        Key.CACHE_CAPACITY: Read(f"max by (instance) (last_over_time({capacity}))", How.LAST),
    }
    sums = {
        Key.REQUEST_OUTPUT_TOKENS: Further.REQUEST_OUTPUT_TOKENS,
        Key.DECODE_SECONDS: Further.DECODE_TIME,
        Key.PREFILL_COMPUTED_TOKENS: Further.REQUEST_PREFILL_COMPUTED_TOKENS,
        Key.PREFILL_SECONDS: Further.PREFILL_TIME,
    }
    found |= {key: increase(further[quantity], "_sum") for key, quantity in sums.items() if quantity in further}
    if Further.PREEMPTIONS in further:
        found[Key.PREEMPTIONS] = increase(further[Further.PREEMPTIONS])
    distributions = {
        Key.INPUT_DISTRIBUTION: Further.REQUEST_INPUT_TOKENS,
        Key.OUTPUT_DISTRIBUTION: Further.REQUEST_OUTPUT_TOKENS,
    }
    found |= {
        key: increase(further[quantity], "_bucket") for key, quantity in distributions.items() if quantity in further
    }
    return found


def queries(pool: str, engine: str, window_seconds: float, counted_seconds: float | None = None) -> dict[Key, str]:
    """The query of each read (`reads`): a quantile's is the selector of the buckets it is computed from."""
    return {key: one.query for key, one in reads(pool, engine, window_seconds, counted_seconds).items()}


def _where(one: common.Series) -> tuple[str, str]:
    return one.labels.get("instance", ""), one.labels.get("le", "")


def counted_of(samples: Sequence[common.Series], start: float) -> dict[tuple[str, str], float]:
    """Each instance's increase over the run from `start`, by bucket (`le`, empty for none): the sum
    of its series' increases. `samples` are each series' samples as they were scraped, from the
    lookback before `start` to one scrape after the run's end; an instance's engines are series of
    their own (vLLM labels each with its engine index), and a series with one label more or less is
    another series. A series' increase runs from its last sample at or before `start`, or from zero
    for a series with none, which began with its engine during the run, to its last sample, each
    rise added and each drop counted as a restart from zero, its new value added in full. A series
    with no sample after `start` was not scraped in the run, and counts nothing."""
    totals: dict[tuple[str, str], float] = {}
    for one in samples:
        where = _where(one)
        ordered = [(at, value) for at, value in sorted(one.samples) if not math.isnan(value)]
        before = [value for at, value in ordered if at <= start]
        inside = [value for at, value in ordered if at > start]
        if not where[0] or not inside:
            continue
        held, added = before[-1] if before else 0.0, 0.0
        for value in inside:
            added += value - held if value >= held else value
            held = value
        totals[where] = totals.get(where, 0.0) + added
    return totals


def quantile_of(fraction: float, buckets: Mapping[str, float]) -> float | None:
    """The quantile at `fraction` of one histogram's cumulative bucket counts by their upper bound,
    as Prometheus' `histogram_quantile` interpolates it. None for no `+Inf` bucket or no count."""
    bounds = sorted((float(le), count) for le, count in buckets.items())
    if len(bounds) < 2 or bounds[-1][0] != math.inf:
        return None
    cumulative, most = [], 0.0
    for bound, count in bounds:
        most = max(most, count)
        cumulative.append((bound, most))
    total = cumulative[-1][1]
    if total <= 0:
        return None
    rank = fraction * total
    at = next(index for index, (_, count) in enumerate(cumulative) if count >= rank)
    if at == len(cumulative) - 1:
        return cumulative[-2][0]
    upper, count = cumulative[at]
    if at == 0 and upper <= 0:
        return upper
    lower, below = cumulative[at - 1] if at else (0.0, 0.0)
    return lower + (upper - lower) * (rank - below) / (count - below)


def fine_step(seconds: float) -> float:
    """A range query's step finer than a scrape, so every scrape shows: a second, or the coarsest
    the workspace's 11,000 points a series allow."""
    return float(max(1, math.ceil(seconds / 10_000)))


def grid_start(start: float, end: float, step: float) -> float:
    """Where a range query from `start` begins so that `end` is on its grid: `start` or a step less
    after it."""
    return end - math.floor((end - start) / step) * step


@dataclass(frozen=True)
class EngineQuantities:
    """What one instance's engine measured in a run. None where the engine's row has no name for
    it, the workspace gave nothing, or there was nothing to divide."""

    output_tokens_per_second_per_stream_mean: float | None = None
    output_tokens_per_second_per_stream_median: float | None = None
    output_tokens_per_second_per_stream_slowest_tenth: float | None = None
    output_tokens_per_second: float | None = None
    prefill_tokens_per_second: float | None = None
    first_token_requests: float | None = None
    first_token_seconds_median: float | None = None
    first_token_seconds_p95: float | None = None
    cached_share: float | None = None
    cache_use_mean: float | None = None
    cache_use_peak: float | None = None
    cache_capacity_tokens: float | None = None
    preemptions: float | None = None
    running_requests_mean: float | None = None
    running_requests_peak: float | None = None
    waiting_requests_mean: float | None = None
    waiting_requests_peak: float | None = None
    input_tokens_per_request: dict[str, float] | None = None
    """Requests by the upper bound of their bucket, Prometheus' `le`."""
    output_tokens_per_request: dict[str, float] | None = None


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _ratio(above: float | None, below: float | None) -> float | None:
    return None if above is None or not below else _finite(above / below)


def quantities_of(answers: Mapping[Key, Sequence[common.Series]], window_seconds: float) -> dict[str, EngineQuantities]:
    """Each instance's quantities, from the workspace's answers to `queries`, by instance."""
    scalars: dict[str, dict[Key, float | None]] = {}
    buckets: dict[str, dict[Key, dict[str, float]]] = {}
    for key, series in answers.items():
        for one in series:
            instance = one.labels.get("instance", "")
            if not instance or not one.samples:
                continue
            value = one.samples[-1][1]
            if key in (Key.INPUT_DISTRIBUTION, Key.OUTPUT_DISTRIBUTION):
                buckets.setdefault(instance, {}).setdefault(key, {})[one.labels.get("le", "")] = value
            else:
                scalars.setdefault(instance, {})[key] = _finite(value)
    found = {}
    for instance in sorted({*scalars, *buckets}):
        held, counts = scalars.get(instance, {}), buckets.get(instance, {})

        def at(key: Key, held: Mapping[Key, float | None] = held) -> float | None:
            return held.get(key)

        output = at(Key.OUTPUT_TOKENS)
        found[instance] = EngineQuantities(
            output_tokens_per_second_per_stream_mean=_ratio(at(Key.REQUEST_OUTPUT_TOKENS), at(Key.DECODE_SECONDS)),
            output_tokens_per_second_per_stream_median=_ratio(1.0, at(Key.TOKEN_TIME_MEDIAN)),
            output_tokens_per_second_per_stream_slowest_tenth=_ratio(1.0, at(Key.TOKEN_TIME_P90)),
            output_tokens_per_second=None if output is None else output / window_seconds,
            prefill_tokens_per_second=_ratio(at(Key.PREFILL_COMPUTED_TOKENS), at(Key.PREFILL_SECONDS)),
            first_token_requests=at(Key.FIRST_TOKEN_REQUESTS),
            first_token_seconds_median=at(Key.FIRST_TOKEN_MEDIAN),
            first_token_seconds_p95=at(Key.FIRST_TOKEN_P95),
            cached_share=_ratio(at(Key.CACHED_INPUT_TOKENS), at(Key.INPUT_TOKENS)),
            cache_use_mean=at(Key.CACHE_USE_MEAN),
            cache_use_peak=at(Key.CACHE_USE_PEAK),
            cache_capacity_tokens=at(Key.CACHE_CAPACITY),
            preemptions=at(Key.PREEMPTIONS),
            running_requests_mean=at(Key.RUNNING_MEAN),
            running_requests_peak=at(Key.RUNNING_PEAK),
            waiting_requests_mean=at(Key.WAITING_MEAN),
            waiting_requests_peak=at(Key.WAITING_PEAK),
            input_tokens_per_request=counts.get(Key.INPUT_DISTRIBUTION),
            output_tokens_per_request=counts.get(Key.OUTPUT_DISTRIBUTION),
        )
    return found


# One run, and the job.


@dataclass(frozen=True)
class Label:
    """What a run is: the pool, the instance types that ran, the build, the lever, the shape, the
    sessions at once, the students, and the class size when the run is at it."""

    pool: str
    instance_types: tuple[str, ...]
    build: str
    lever: str
    shape: Shape
    streams: int
    students: int
    class_size: int | None


@dataclass(frozen=True)
class Instance:
    """One instance of a run: what its lifecycle agent reported, and what its engine measured."""

    instance_type: str
    seconds_in_service: float
    engine: EngineQuantities


@dataclass(frozen=True)
class Measured:
    """What one run measured."""

    label: Label
    seconds: float
    experience: Experience
    cost_per_million_output_tokens: float | None
    instances: dict[str, Instance] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def held_at(measured: Measured, instances: int) -> bool:
    """Whether `instances` instances served the run, and each of them all of it, to within two
    scrapes: a pool that grew or shrank during the run was not held at that many."""
    whole = measured.seconds - 2 * SCRAPE_SECONDS
    return len(measured.instances) == instances and all(
        one.seconds_in_service >= whole for one in measured.instances.values()
    )


class Reader(Protocol):
    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]: ...

    def read_samples(self, query: str, *, at: float) -> list[common.Series]: ...


class Replay(Protocol):
    def __call__(
        self, load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int
    ) -> Awaitable[list[loadgen.Record]]: ...


class Job:
    """The runs of `plan(streams, class_size)` against the pool `pool`, served by `engine` at
    `prices`, each `duration` seconds, labelled with `lever`: sent through the gateway at
    `endpoint` as the canary students of `keys`, the pool's state read through `gateway`, read from
    `workspace`. Each session compacts at `window_tokens` when it is given, and each run must be
    served by `instances` instances when that is given."""

    def __init__(
        self,
        *,
        pool: str,
        engine: str,
        prices: Mapping[str, float],
        lever: str,
        streams: Sequence[int],
        class_size: int,
        duration: float,
        keys: Sequence[str],
        endpoint: str,
        gateway: Any,
        workspace: Reader,
        replay: Replay = loadgen.replay,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
        instances: int | None = None,
        window_tokens: int | None = None,
    ) -> None:
        if duration <= 0:
            raise ValueError("each run of spike S5 lasts some seconds")
        self.runs = plan(streams, class_size)
        metric_names(engine)
        self.pool, self.engine, self.prices, self.lever = pool, engine, dict(prices), lever
        self.duration, self.keys, self.endpoint = duration, tuple(keys), endpoint
        self.gateway, self.workspace, self.replay = gateway, workspace, replay
        self.wall, self.sleep, self.log = wall, sleep, log
        self.instances, self.window_tokens = instances, window_tokens
        self._seed = 0

    def _ready(self) -> None:
        started = self.wall()
        while self.gateway.state() != READY:
            if self.wall() - started >= READY_WAIT_SECONDS:
                raise TimeoutError("the pool was not ready in time")
            self.sleep(POLL_SECONDS)

    def _keyed(self, runs: Sequence[Planned]) -> None:
        """ValueError for a run with more students than there are canary students' keys."""
        if max(run.students for run in runs) > len(self.keys):
            raise ValueError("a run needs a canary student's key for each student, and there are fewer keys")

    def _replay(self, planned: Planned, deadline: float) -> list[loadgen.Record]:
        records: list[loadgen.Record] = []

        def sending() -> bool:
            return self.wall() < deadline

        while sending():
            load = load_of(planned, self.pool, self._seed, self.window_tokens)
            self._seed += len(load.sessions)
            bounded = loadgen.Load(
                tuple(Until(session, deadline, self.wall) for session in load.sessions),
                load.sessions_per_student,
                sending,
            )
            began = self.wall()
            round_records = asyncio.run(self._send(bounded))
            records += round_records
            # A round in which no time passed would be repeated, on a clock the requests do not
            # move, for ever: it is the last.
            if not round_records or self.wall() <= began:
                break
        return records

    async def _send(self, load: loadgen.Load) -> list[loadgen.Record]:
        return await self.replay(load, self.keys, self.endpoint, at_once=len(load.sessions))

    def _read(self, query: str, start: float, end: float, step: float) -> list[common.Series]:
        return self.workspace.read_series(query, start=start, end=end, step=step)

    def _engine(self, start: float, end: float) -> dict[Key, list[common.Series]]:
        """The workspace's answers to `reads` for the run from `start` to `end`, each made one
        number an instance at the time it was read, as `quantities_of` takes them."""
        after = end + SCRAPE_SECONDS
        held: dict[tuple[str, float, float], list[common.Series]] = {}

        def read(query: str, low: float, high: float, step: float) -> list[common.Series]:
            if (query, low, high) not in held:
                held[query, low, high] = self._read(query, low, high, step)
            return held[query, low, high]

        def one(instance: str, at: float, value: float, le: str = "") -> common.Series:
            return common.Series({"instance": instance, **({"le": le} if le else {})}, ((at, value),))

        covered = range_of(after - start + LOOKBACK_SECONDS)
        answers: dict[Key, list[common.Series]] = {}
        for key, how in reads(self.pool, self.engine, max(end - start, 1.0), after - start).items():
            if how.how is How.LAST:
                answers[key] = read(how.query, after, after, SCRAPE_SECONDS)
            elif how.how in (How.MEAN, How.PEAK):
                step = fine_step(end - start)
                points: dict[str, list[float]] = {}
                for series in read(how.query, grid_start(start, end, step), end, step):
                    instance = series.labels.get("instance", "")
                    inside = [value for at, value in series.samples if start <= at <= end]
                    if instance and inside:
                        points.setdefault(instance, []).extend(inside)
                summary = statistics.fmean if how.how is How.MEAN else max
                answers[key] = [one(instance, end, summary(values)) for instance, values in points.items()]
            else:
                counted = counted_of(self.workspace.read_samples(f"{how.query}[{covered}]", at=after), start)
                if how.how is How.SUM:
                    answers[key] = [one(instance, after, value, le) for (instance, le), value in counted.items()]
                    continue
                buckets: dict[str, dict[str, float]] = {}
                for (instance, le), value in counted.items():
                    buckets.setdefault(instance, {})[le] = value
                found = {instance: quantile_of(how.fraction, counts) for instance, counts in buckets.items()}
                answers[key] = [one(instance, after, value) for instance, value in found.items() if value is not None]
        return answers

    def run(self, planned: Planned) -> Measured:
        """One run: the pool ready, the sessions replayed for the duration, then what it measured.
        ValueError, with nothing sent, for a run with more students than there are keys."""
        self._keyed([planned])
        self._ready()
        start = self.wall()
        records = self._replay(planned, start + self.duration)
        end = self.wall()
        self.sleep(SETTLE_SECONDS)
        window = max(end - start, 1.0)
        # The scrape after the run's end holds what the run's last answers added; the counters'
        # increase starts from what they held at the run's start.
        after = end + SCRAPE_SECONDS
        step = fine_step(after - start)
        release = self._read(release_query(self.pool), grid_start(start, after, step), after, step)
        in_service = in_service_of(release, start, end)
        build = build_of(in_service)
        engine = quantities_of(self._engine(start, end), window)
        experience = experience_of(records)
        # The instances that served the run, and only those: one the workspace reads after the run,
        # or that had stopped before it, measured nothing of it.
        instances = {
            instance: Instance(
                instance_type=served.instance_type,
                seconds_in_service=served.seconds,
                engine=engine.get(instance, EngineQuantities()),
            )
            for instance, served in sorted(in_service.items())
        }
        label = Label(
            pool=self.pool,
            instance_types=tuple(sorted({one.instance_type for one in in_service.values()})),
            build=build,
            lever=self.lever,
            shape=planned.shape,
            streams=planned.streams,
            students=planned.students,
            class_size=planned.class_size,
        )
        return Measured(
            label=label,
            seconds=window,
            experience=experience,
            cost_per_million_output_tokens=cost_per_million(in_service, self.prices, experience.output_tokens),
            instances=instances,
        )

    def measure(self) -> list[Measured]:
        """Every run, in the plan's order, each printed as one JSON line once measured. ValueError,
        with nothing sent, for runs with more students than there are keys; and once printed, for
        a run that another number of instances than `instances` served, or that another build or
        another instance type than the job's earlier runs served, or two instance types, with no run
        after it: the job's numbers are one build's on one instance type, the one its record's rows
        are labelled with."""
        self._keyed(self.runs)
        measured: list[Measured] = []
        for planned in self.runs:
            one = self.run(planned)
            self.log(one.to_json())
            if self.instances is not None and not held_at(one, self.instances):
                raise ValueError("a run was served by another number of instances than the pool is held at")
            if measured and one.label.build != measured[0].label.build:
                raise ValueError("a run was served by another build than the job's earlier runs")
            types = one.label.instance_types
            if len(types) > 1 or (measured and types != measured[0].label.instance_types):
                raise ValueError("a run was served by another instance type than the job's earlier runs, or by two")
            measured.append(one)
        return measured


# The numbers.


class Figure(StrEnum):
    """A quantity the job posts for each condition, its name in the record."""

    REQUESTS = "requests"
    FAILED_REQUESTS = "failed_requests"
    FAILED_SHARE = "failed_share"
    """The failed requests in percent of the requests sent: what the verdict reads."""
    FAILED_INVALID_KEY = "failed_invalid_key"
    """The failed requests that named a cause of the gateway's: one quantity a cause (`FAILED_BY_CAUSE`)."""
    FAILED_NOT_FOUND = "failed_not_found"
    FAILED_REQUEST_REFUSED = "failed_request_refused"
    FAILED_CONCURRENCY_LIMIT = "failed_concurrency_limit"
    FAILED_WARMING = "failed_warming"
    FAILED_CAPACITY_UNAVAILABLE = "failed_capacity_unavailable"
    FAILED_NO_SESSION = "failed_no_session"
    FAILED_ENGINE_ERROR = "failed_engine_error"
    FAILED_TIME_LIMIT = "failed_time_limit"
    FAILED_IN_STREAM = "failed_in_stream"
    """Those answered 200 whose stream then carried the gateway's error event, also under their cause."""
    FAILED_WITHOUT_CAUSE = "failed_without_cause"
    """Those that named no cause of the gateway's, such as an answer of the edge."""
    INSTANCES = "instances"
    TTFT_MEDIAN = "ttft_median"
    """Later turns' time to first token: first turns are apart."""
    TTFT_P95 = "ttft_p95"
    FIRST_TURN_TTFT_P95 = "first_turn_ttft_p95"
    STREAM_MEDIAN = "stream_median"
    STREAM_P10 = "stream_p10"
    """The slowest tenth of the streams' speed."""
    OUTPUT_THROUGHPUT_PER_INSTANCE = "output_throughput_per_instance"
    PREFILL_SPEED = "prefill_speed"
    CACHED_SHARE = "cached_share"
    CACHE_CAPACITY = "cache_capacity"
    RUNNING_REQUESTS_PEAK = "running_requests_peak"
    PREEMPTIONS = "preemptions"
    COST_PER_MILLION_OUTPUT_TOKENS = "cost_per_million_output_tokens"


# The quantity of each cause of the gateway's vocabulary: a cause with none does not load.
FAILED_BY_CAUSE = {cause: Figure(f"failed_{cause.value}") for cause in Cause}

UNITS = {
    Figure.REQUESTS: "count",
    Figure.FAILED_REQUESTS: "count",
    Figure.FAILED_SHARE: "percent",
    **dict.fromkeys(FAILED_BY_CAUSE.values(), "count"),
    Figure.FAILED_IN_STREAM: "count",
    Figure.FAILED_WITHOUT_CAUSE: "count",
    Figure.INSTANCES: "count",
    Figure.TTFT_MEDIAN: "s",
    Figure.TTFT_P95: "s",
    Figure.FIRST_TURN_TTFT_P95: "s",
    Figure.STREAM_MEDIAN: "tokens/s",
    Figure.STREAM_P10: "tokens/s",
    Figure.OUTPUT_THROUGHPUT_PER_INSTANCE: "tokens/s",
    Figure.PREFILL_SPEED: "tokens/s",
    Figure.CACHED_SHARE: "percent",
    Figure.CACHE_CAPACITY: "tokens",
    Figure.RUNNING_REQUESTS_PEAK: "count",
    Figure.PREEMPTIONS: "count",
    Figure.COST_PER_MILLION_OUTPUT_TOKENS: "USD per million output tokens",
}
CLASS = "class"
CLASS_SIZE = "class_size"
POSTED = "numbers_posted"
"""How many numbers a posting holds, this one included: what tells a whole posting, whatever order
its numbers reach the reporter in (`postings_of`). Like the class's size, its row is the run's."""


def name_of(figure: Figure, shape: Shape, streams: int | None) -> str:
    """The name of `figure` at `streams`, or at the class's size for None, in `shape`."""
    return f"{figure}_{shape.value.replace('-', '_')}_{CLASS if streams is None else streams}"


# Each declared name, and its quantity and condition.
CONDITIONS: dict[str, tuple[Figure, Shape, int | None]] = {
    name_of(figure, shape, streams): (figure, shape, streams)
    for figure in Figure
    for shape in Shape
    for streams in (*DECLARED_STREAMS, None)
}
Number = StrEnum("Number", {name.upper(): name for name in (CLASS_SIZE, POSTED, *CONDITIONS)})
"""The names of the numbers the job posts."""
NAME_UNITS = {
    CLASS_SIZE: "count",
    POSTED: "count",
    **{name: UNITS[figure] for name, (figure, _, _) in CONDITIONS.items()},
}


def _present(values: Sequence[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def _mean(values: Sequence[float | None]) -> float | None:
    present = _present(values)
    return statistics.fmean(present) if present else None


def figures_of(measured: Measured) -> dict[Figure, int | float | None]:
    """What one run measured, as the quantities it posts: None where it has nothing. A count of
    failed requests by cause, in a stream or without a cause is None when it is zero, to keep
    postings short; their count and their share are given for every run that sent a request."""
    seen, engines = measured.experience, [one.engine for one in measured.instances.values()]
    cached = _mean([one.cached_share for one in engines])
    capacity = _present([one.cache_capacity_tokens for one in engines])
    running = _present([one.running_requests_peak for one in engines])
    preemptions = [one.preemptions for one in engines]
    observed = sum(_present(preemptions))
    # A preemption one instance saw is one, whatever another instance left unsaid; none seen is
    # none only when every instance said so.
    complete = bool(engines) and None not in preemptions
    return {
        Figure.REQUESTS: seen.requests,
        Figure.FAILED_REQUESTS: seen.failed,
        Figure.FAILED_SHARE: 100 * seen.failed / seen.requests if seen.requests else None,
        **{figure: seen.failed_by_cause.get(cause.value) or None for cause, figure in FAILED_BY_CAUSE.items()},
        Figure.FAILED_IN_STREAM: seen.failed_in_stream or None,
        Figure.FAILED_WITHOUT_CAUSE: seen.failed - sum(seen.failed_by_cause.values()) or None,
        Figure.INSTANCES: len(measured.instances),
        Figure.TTFT_MEDIAN: seen.first_token_seconds_median,
        Figure.TTFT_P95: seen.first_token_seconds_p95,
        Figure.FIRST_TURN_TTFT_P95: seen.first_turn_first_token_seconds_p95,
        Figure.STREAM_MEDIAN: seen.tokens_per_second_median,
        Figure.STREAM_P10: seen.tokens_per_second_slowest_tenth,
        Figure.OUTPUT_THROUGHPUT_PER_INSTANCE: _mean([one.output_tokens_per_second for one in engines]),
        Figure.PREFILL_SPEED: _mean([one.prefill_tokens_per_second for one in engines]),
        Figure.CACHED_SHARE: None if cached is None else 100 * cached,
        Figure.CACHE_CAPACITY: min(capacity) if capacity else None,
        Figure.RUNNING_REQUESTS_PEAK: max(running) if running else None,
        Figure.PREEMPTIONS: observed if observed > 0 or complete else None,
        Figure.COST_PER_MILLION_OUTPUT_TOKENS: measured.cost_per_million_output_tokens,
    }


def numbers_of(measured: Sequence[Measured], class_size: int) -> dict[str, int | float]:
    """The numbers of a job's runs under their declared names, the class's size, and how many
    numbers they are (`POSTED`). ValueError for a run at a number of streams no name is declared
    for."""
    numbers: dict[str, int | float] = {}
    for one in measured:
        streams = None if one.label.class_size is not None else one.label.streams
        if streams is not None and streams not in DECLARED_STREAMS:
            raise ValueError("a run is at a number of streams no name is declared for")
        for figure, value in figures_of(one).items():
            if value is not None:
                numbers[name_of(figure, one.label.shape, streams)] = value
    numbers[CLASS_SIZE] = class_size
    numbers[POSTED] = len(numbers) + 1
    return numbers


# The verdict.


@dataclass(frozen=True)
class Floors:
    """The floors of section 7.3: the 95th percentile of the time to first token, in seconds, which
    a run stays under, and the median and the slowest tenth of a stream's speed, in tokens a
    second, which it reaches. And one the design does not give yet: the failed requests, in percent
    of the requests sent, which a run does not exceed."""

    ttft_p95_seconds: float
    stream_median_tokens_per_second: float
    stream_p10_tokens_per_second: float
    failed_requests_percent: float


def floors_of(settings: config.Config) -> Floors:
    """The floors the configuration holds (issue E4-31), section 17's unless it gives others, and
    its floor on the failed requests, `target_failed_requests_percent`."""
    return Floors(
        ttft_p95_seconds=settings.target_ttft_p95_seconds,
        stream_median_tokens_per_second=settings.target_stream_median_tokens_per_second,
        stream_p10_tokens_per_second=settings.target_stream_p10_tokens_per_second,
        failed_requests_percent=settings.target_failed_requests_percent,
    )


# What the verdict reads, at the class's size in each shape.
JUDGED = (Figure.TTFT_P95, Figure.STREAM_MEDIAN, Figure.STREAM_P10, Figure.PREEMPTIONS, Figure.FAILED_SHARE)


def meets(figure: Figure, value: float, floors: Floors) -> bool:
    """Whether one number the verdict reads meets its floor: the time to first token under its
    floor, each speed at its floor or above, no preemption, the failed share at its floor or below."""
    if figure is Figure.TTFT_P95:
        return value < floors.ttft_p95_seconds
    if figure is Figure.STREAM_MEDIAN:
        return value >= floors.stream_median_tokens_per_second
    if figure is Figure.STREAM_P10:
        return value >= floors.stream_p10_tokens_per_second
    if figure is Figure.PREEMPTIONS:
        return value == 0
    if figure is Figure.FAILED_SHARE:
        return value <= floors.failed_requests_percent
    raise ValueError("the verdict does not read that quantity")


def passes(numbers: common.Numbers, floors: Floors) -> bool:
    """Whether the class-size runs of both shapes meet the floors, with no preemption and no more
    failed requests than their floor allows."""
    return all(meets(figure, numbers[name_of(figure, shape, None)], floors) for shape in Shape for figure in JUDGED)


@dataclass(frozen=True)
class CapacitySpike(common.Spike):
    """Spike S5 against `floors`: a class-size number that misses its floor fails the job, a failed
    share above its floor among them, whatever else is missing; with none missed, a class-size run
    that lacks a number the verdict reads, its failed share among them, leaves the job with no
    verdict."""

    floors: Floors

    def verdict(self, numbers: common.Numbers) -> Verdict:
        judged = {(figure, shape): numbers.get(name_of(figure, shape, None)) for shape in Shape for figure in JUDGED}
        if any(value is not None and not meets(figure, value, self.floors) for (figure, _), value in judged.items()):
            return Verdict.FAIL
        if None in judged.values():
            return Verdict.NOT_MEASURED
        return super().verdict(numbers)


def spike_of(floors: Floors) -> CapacitySpike:
    """What the job declares to `common.run_spike`, judged against `floors`."""
    return CapacitySpike(MEASUREMENT, Number, functools.partial(passes, floors=floors), floors)


# The record.

REPO = Path(__file__).resolve().parents[1]
RECORD = REPO / "docs" / "measurements" / "s5.md"
RECORD_TOOL = REPO / "tools" / "measurements" / "record.py"


def record_tool() -> ModuleType:
    """`tools/measurements/record.py`, a script and not a package."""
    spec = importlib.util.spec_from_file_location("measurement_record", RECORD_TOOL)
    if spec is None or spec.loader is None:
        raise ValueError("the record tool is not there")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def row_of(run: str, name: str, *, pool: str, instance_type: str) -> tuple[str, dict[str, str]]:
    """The name and the labels of the row of the number `name` the run `run` posted: its quantity,
    and its conditions. ValueError for a run label that is not `<build>-<lever>-<instances>`, or a
    name the job does not declare."""
    match = _RUN.fullmatch(run)
    if match is None or (name not in (CLASS_SIZE, POSTED) and name not in CONDITIONS):
        raise ValueError("the run label or the name is not one of spike S5's")
    labels = {"run": run, "pool": pool, "instance_type": instance_type, **match.groupdict()}
    if name in (CLASS_SIZE, POSTED):
        return name, labels
    figure, shape, streams = CONDITIONS[name]
    return figure.value, labels | {"session_shape": shape.value, "streams": CLASS if streams is None else str(streams)}


def statuses_of(text: str) -> list[Any]:
    """The statuses GitHub's API lists, as `gh api --paginate` writes them: one JSON list a page,
    the pages one after the other, or, with `--slurp`, one list of the pages. ValueError, which
    quotes nothing of the text, for any other text."""
    decoder, found, at, pages = json.JSONDecoder(), [], 0, 0
    while True:
        at = len(text) - len(text[at:].lstrip())
        if at == len(text):
            break
        try:
            page, at = decoder.raw_decode(text, at)
        except ValueError:
            page = None
        if not isinstance(page, list):
            raise ValueError("the statuses are not GitHub's list of a commit's statuses")  # noqa: TRY004 - one refusal
        found += [one for item in page for one in (item if isinstance(item, list) else [item])]
        pages += 1
    if not pages:
        raise ValueError("the statuses are not GitHub's list of a commit's statuses")
    return found


def _measured(status: object) -> tuple[tuple[str, str], int, datetime, int | None] | None:
    """The run a measurement's status is of, its part, when it was posted and GitHub's id of it;
    None for a status that is not a measurement's, or that this cannot read."""
    context = status.get("context") if isinstance(status, dict) else None
    created = status.get("created_at") if isinstance(status, dict) else None
    number = status.get("id") if isinstance(status, dict) else None
    try:
        measurement, label, part = context_parts(context) if isinstance(context, str) else ("", "", 0)
        posted = datetime.fromisoformat(created) if isinstance(created, str) else None
    except ValueError:
        return None
    if not part or posted is None or posted.tzinfo is None:
        return None
    return (measurement, label), part, posted, number if type(number) is int else None


class Posting(StrEnum):
    """What the statuses of one run label show of its newest posting."""

    WHOLE = "whole"
    INCOMPLETE = "incomplete"
    OVERLAPPING = "overlapping"


def _pairs(status: Mapping[str, Any]) -> list[str]:
    description = status.get("description") or ""
    return description.split(" ") if isinstance(description, str) and description else []


def _counts(statuses: Iterable[Mapping[str, Any]]) -> tuple[int, list[int | None], bool]:
    """Of some statuses of one run label: the numbers they hold, the counts of their postings
    (`POSTED`) they hold, None for one that is not a whole number, and whether one of them is a
    first part with no number, all a job posts that measured nothing."""
    numbers, counts, empty = 0, [], False
    for status in statuses:
        pairs = _pairs(status)
        numbers += len(pairs)
        for pair in pairs:
            name, _, value = pair.partition("=")
            if name == POSTED:
                counts.append(int(value) if value.isdigit() else None)
        empty = empty or not pairs
    return numbers, counts, empty


def postings_of(
    statuses: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], dict[str, tuple[Posting, datetime]]]:
    """The statuses of each run of S5 that its newest posting holds, and what they show of it,
    with the time of the label's newest status. A job posts a run's parts in their order, one
    after the other, part 1 first; one of them, whichever the order its numbers reached the
    reporter in, holds how many numbers the posting holds (`POSTED`), or the posting is one part
    with no number, all a job posts that measured nothing. A posting carries nothing else that
    tells it from another of its label. The newest posting is the newest part 1 and the parts
    posted at its time or after it:

    - `WHOLE` when every posting before it had ended before it began (all its numbers, its count
      among them, were there), and it holds one status of each part from 1 and the numbers its
      count says: a retry is read alone, never joined to an earlier posting's parts;
    - `INCOMPLETE` when, with the same, some of its numbers are not there yet: the parts that are
      there, a part between them missing or not, are returned numbered again from 1;
    - `OVERLAPPING` when an earlier posting had not ended when it began, or two statuses of one
      part, two counts, or more numbers than its count were posted from its time on: the postings
      cannot be told apart, and none of their statuses is returned.

    GitHub's id of a status is the status: one listed twice, on two pages a new status shifted the
    boundary of, is read once. GitHub's date of a status is to the second, and its id grows with
    each status it takes, so when
    every measurement's status carries its id the statuses are ordered by date then id, and a
    retry in the same second as the run before it is still told apart; without the ids, by date
    alone, and statuses of one instant may be of either posting. The statuses of other
    measurements are left out; a measurement's status this cannot read is kept, for
    `tools/measurements/record.py` to refuse."""
    seen: set[int] = set()
    unique: list[Mapping[str, Any]] = []
    for status in statuses:
        number = status.get("id") if isinstance(status, dict) else None
        if type(number) is int:
            if number in seen:
                continue  # a page boundary moved while the pages were read: the same status twice
            seen.add(number)
        unique.append(status)
    read = [_measured(status) for status in unique]
    with_ids = all(one[3] is not None for one in read if one is not None)
    by_label: dict[str, list[tuple[tuple[datetime, int], int, Mapping[str, Any]]]] = {}
    kept: list[Mapping[str, Any]] = []
    for status, one in zip(unique, read, strict=True):
        if one is None:
            context = status.get("context") if isinstance(status, dict) else None
            if isinstance(context, str) and context.startswith(f"measure/{MEASUREMENT}/"):
                kept.append(status)
        elif one[0][0] == MEASUREMENT:
            order = (one[2], (one[3] or 0) if with_ids else 0)
            by_label.setdefault(one[0][1], []).append((order, one[1], status))

    found: dict[str, tuple[Posting, datetime]] = {}
    for label, of_label in by_label.items():
        firsts = [order for order, part, _ in of_label if part == 1]
        when = max(order[0] for order, _, _ in of_label)
        if not firsts:
            kept += [status for _, _, status in of_label]  # parts with no first: refused when decoded
            continue
        newest = max(firsts)
        before = [(part, status) for order, part, status in of_label if order < newest]
        # The earlier postings had all ended when it began: each that holds numbers holds its
        # count, and all their numbers are there.
        begun = sum(part == 1 and bool(_pairs(status)) for part, status in before)
        numbers, counts, _ = _counts(status for _, status in before)
        ended = len(counts) == begun and None not in counts and numbers == sum(c or 0 for c in counts)
        since: dict[int, list[Mapping[str, Any]]] = {}
        for order, part, status in of_label:
            if order >= newest:
                since.setdefault(part, []).append(status)
        numbers, counts, empty = _counts(one for of_part in since.values() for one in of_part)
        if not _pairs(since[1][0]):
            read = Posting.WHOLE if len(since) == len(since[1]) == 1 else Posting.OVERLAPPING
        elif empty or len(counts) > 1 or None in counts:
            read = Posting.OVERLAPPING
        elif not counts or numbers < (counts[0] or 0):
            read = Posting.INCOMPLETE
        else:
            whole = numbers == counts[0] and sorted(since) == list(range(1, len(since) + 1))
            read = Posting.WHOLE if whole else Posting.OVERLAPPING
        if not ended or any(len(one) > 1 for one in since.values()):
            read = Posting.OVERLAPPING
        found[label] = (read, when)
        if read is Posting.INCOMPLETE:
            # A part not listed yet may lie between two that are: the parts that are, numbered
            # again from 1, hold its rows, and the decoder joins them.
            kept += [
                {**since[part][0], "context": f"{MEASURE_CONTEXT}{MEASUREMENT}/{label}/{at}"}
                for at, part in enumerate(sorted(since), start=1)
            ]
        elif read is Posting.WHOLE:
            kept += [one[0] for one in since.values()]
    return kept, found


# A pass rests on these: a posting that does not hold them all is not all there yet.
JUDGED_NAMES = frozenset(name_of(figure, shape, None) for shape in Shape for figure in JUDGED)
INCOMPLETE = "the posting lacks a part"
OVERLAPPING = "the postings of the run overlap"


def record_statuses(
    statuses: Sequence[Mapping[str, Any]],
    commit: str,
    *,
    pool: str,
    instance_type: str,
    path: Path = RECORD,
    only: Collection[str] | None = None,
) -> list[str]:
    """Writes the runs of S5 a commit's statuses hold, each from its newest posting
    (`postings_of`), into the record at `path`, with `tools/measurements/record.py`, each row
    named after its quantity and labelled with its conditions; returns the run labels written.
    `only` names the runs to write, every run of S5 on the commit unless given. ValueError, with
    nothing written, for no run of S5 to write, one whose label or numbers are not the job's, and
    one the record already holds measured on another pool or instance type: the runs of each
    instance type are written apart, each with its own.

    A run is never a pass but from one whole posting. A posting whose last part is not there yet
    is written pending, with the rows of its parts that are, and a reason; a posting that overlaps
    an earlier one of its label is written pending with no row, since their parts cannot be told
    apart. A run posted as a pass that lacks a class-size number its verdict reads is written
    pending as well."""
    tool = record_tool()
    selected, postings = postings_of(statuses)
    wanted = {label for label in postings if only is None or label in only}
    found = [(run, day) for run, day in tool.runs_of(selected, commit) if run.measurement == MEASUREMENT]
    runs = [(run, day) for run, day in found if run.label in wanted]
    runs += [
        (tool.Run(MEASUREMENT, label, Verdict.NOT_MEASURED, {}), when.astimezone(UTC).date())
        for label, (read, when) in postings.items()
        if label in wanted and read is Posting.OVERLAPPING
    ]
    if not runs or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("the statuses hold no run of spike S5 on that commit")
    rows = {run.label: [row_of(run.label, name, pool=pool, instance_type=instance_type) for name in run.numbers]
            for run, _ in runs}  # fmt: skip
    record, rest = tool.read(path.read_text(encoding="utf-8"))
    held = {
        (row["labels"].get("run"), row["labels"].get("pool"), row["labels"].get("instance_type"))
        for row in record["rows"]
        if isinstance(row.get("labels"), dict) and row["labels"].get("run") in rows
    }
    if any((pool, instance_type) != (held_pool, held_type) for _, held_pool, held_type in held):
        raise ValueError("the record holds one of these runs measured on another pool or instance type")
    for run, day in runs:
        tool.with_run(record, run, day, commit, NAME_UNITS)
        named = iter(rows[run.label])
        for row in record["rows"]:
            if row["labels"].get("run") == run.label:
                row["name"], row["labels"] = next(named)
        read = postings[run.label][0]
        reason = {Posting.INCOMPLETE: INCOMPLETE, Posting.OVERLAPPING: OVERLAPPING}.get(read)
        if reason is None and run.verdict is Verdict.PASS and not JUDGED_NAMES <= run.numbers.keys():
            reason = INCOMPLETE
        if reason is not None:
            entry = next(entry for entry in record["runs"] if entry.get("run") == run.label)
            entry["verdict"], entry["reason"] = "pending", reason
    if record.get("verdict") != "waived":
        record["verdict"] = tool.verdict_of([run.get("verdict") for run in record["runs"]])
    path.write_text(tool.written(record) + rest, encoding="utf-8")
    return list(rows)


def record_main(argv: Sequence[str]) -> int:
    """`record`: a commit's statuses, as GitHub's API lists them, into the record."""
    parser = argparse.ArgumentParser(prog="s5_capacity.py record", description="Record spike S5's runs.")
    parser.add_argument("--commit", required=True, help="the commit the statuses are of: its full id")
    parser.add_argument("--instance-type", required=True, help="the instance type the pool ran")
    parser.add_argument("--pool", default=DEFAULT_POOL, help="the pool (default: coursework)")
    parser.add_argument("--run", action="append", help="a run label to write, repeated (default: every run of S5)")
    parser.add_argument("--statuses", default="-", help="the statuses, as JSON (default: stdin)")
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.statuses == "-" else Path(args.statuses).read_text(encoding="utf-8")
    try:
        labels = record_statuses(
            statuses_of(text), args.commit, pool=args.pool, instance_type=args.instance_type, only=args.run
        )
    except ValueError as refusal:  # each refusal is fixed text, and quotes nothing of the statuses
        print(f"record: {refusal}", file=sys.stderr)
        return 1
    print(f"{RECORD.relative_to(REPO)}: {', '.join(labels)}")
    return 0


# The job's inputs.


def streams_of(environ: Mapping[str, str]) -> tuple[int, ...]:
    """The numbers of streams, `DEFAULT_STREAMS` unless given. ValueError for a count that is not a
    whole number of one or more, one given twice, or one no name is declared for."""
    written = environ.get(STREAMS_VARIABLE, "")
    counts = tuple(int(one) for one in written.split(",")) if written.strip() else DEFAULT_STREAMS
    if any(count < 1 for count in counts) or len(set(counts)) != len(counts):
        raise ValueError("the numbers of streams are distinct whole numbers of one or more")
    if not set(counts) <= set(DECLARED_STREAMS):
        raise ValueError(f"the numbers of streams are among {', '.join(map(str, DECLARED_STREAMS))}")
    return counts


def whole_of(environ: Mapping[str, str], variable: str, default: int | None = None) -> int:
    """A whole number of one or more the job is given, `default` unless given when there is one.
    ValueError for any other."""
    written = environ.get(variable)
    number = default if written is None and default is not None else int(written or "")
    if number < 1:
        raise ValueError(f"{variable} is a whole number of one or more")
    return number


def lever_of(environ: Mapping[str, str]) -> str:
    """The lever's name, `DEFAULT_LEVER` unless given. ValueError for another shape."""
    lever = environ.get(LEVER_VARIABLE, DEFAULT_LEVER)
    if not _LEVER.fullmatch(lever):
        raise ValueError("the lever's name is lower-case letters, digits and hyphens")
    return lever


def build_name_of(environ: Mapping[str, str]) -> str:
    """The build's name. ValueError when it is not given, or is not lower-case letters and digits."""
    build = environ.get(BUILD_VARIABLE, "")
    if not _BUILD.fullmatch(build):
        raise ValueError("the build's name is lower-case letters and digits, such as 8bit or 4bit")
    return build


def window_of(environ: Mapping[str, str]) -> int | None:
    """The window the sessions compact at, None unless given. ValueError for one smaller than
    `SMALLEST_WINDOW_TOKENS`, at which a compacted session could not go on."""
    if WINDOW_VARIABLE not in environ:
        return None
    window = int(environ[WINDOW_VARIABLE])
    if window < SMALLEST_WINDOW_TOKENS:
        raise ValueError(f"{WINDOW_VARIABLE} is {SMALLEST_WINDOW_TOKENS} tokens or more")
    return window


def main(environ: Mapping[str, str] = os.environ) -> Verdict:
    """Runs every run with the job's environment and credentials, prints what each measured, and
    posts their numbers and verdict as the run `<build>-<lever>-<instances>` of S5. The pool's
    session is checked first: a job it does not carry prints the reason, sends nothing to the
    gateway, and is posted as not measured."""
    streams, lever, build = streams_of(environ), lever_of(environ), build_name_of(environ)
    class_size, duration = whole_of(environ, CLASS_SIZE_VARIABLE), whole_of(environ, DURATION_VARIABLE)
    window = window_of(environ)
    # One number of instances: the session's check, the run's label and each run's own check read it.
    needs = common.needs_of(environ)
    instances = needs.instances
    # Refused before anything is sent: the canary keys go over HTTPS only.
    endpoint = common.gateway_address(environ.get(ENDPOINT_VARIABLE, ""))
    name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
    settings = config.read_ssm()
    pools = [pool for pool in settings.pools if pool.name == name]
    if len(pools) != 1:
        raise ValueError("the deployed configuration has no pool of that name")
    (pool,) = pools
    floors = floors_of(settings)
    label = f"{build}-{lever}-{instances}"

    def measure() -> dict[str, int | float]:
        try:
            common.require_session(
                common.pool_state_table(environ),
                name,
                instances=instances,
                run_minutes=needs.run_minutes,
                session_max_hours=settings.session_max_hours,
                now=time.time,
            )
        except common.SessionRefused as refused:
            print(f"refused: {refused.reason}")
            raise
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        keys = common.canary_keys().students
        print(f"pool {name}, run {label}, streams {list(streams)}, class of {class_size}, {duration} s a run")
        measured = Job(
            pool=name,
            engine=pool.engine,
            prices=pool.prices,
            lever=lever,
            streams=streams,
            class_size=class_size,
            duration=duration,
            keys=keys,
            endpoint=endpoint,
            gateway=common.Gateway(endpoint, name, keys[0]),
            workspace=workspace,
            instances=instances,
            window_tokens=window,
        ).measure()
        return numbers_of(measured, class_size)

    return common.run_spike(spike_of(floors), JOB, measure, environ={**environ, common.RUN_LABEL_VARIABLE: label})


if __name__ == "__main__":
    if sys.argv[1:2] == ["record"]:
        sys.exit(record_main(sys.argv[2:]))
    main()
