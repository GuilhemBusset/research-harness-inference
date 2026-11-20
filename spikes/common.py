"""What the validation spikes share (system design, sections 7.1, 8.4 and 12).

A spike is a short experiment with a pass condition. One that needs AWS runs as a pipeline job,
and each job is one module of this folder, which is on the import path: a spike imports this
module as `common`. Nothing here changes a pool: a spike reaches a pool through the gateway only,
as a canary student, and reads what it measures from the Prometheus workspace.

**Timing.** `Stopwatch` and `timed` count seconds on the monotonic clock.

**Samples.** `percentile` gives any percentile of samples and `median` the 50th. A percentile
that falls between two samples is interpolated between them, as Prometheus's own
`quantile_over_time` does, so a spike and a dashboard give one number for the same samples.

**The workspace.** `Workspace.read_series` runs a range query, with a request signed with SigV4
for the service `aps`, and returns each series as its labels and its samples.

**The canary students.** `canary_keys` reads their keys from the spikes' secret
(`rhi_common.spike_keys`): the students with the default budget, and the one whose budget is spent.

**Posting.** A job cannot post a commit status. `run_spike` hands one run's numbers to the reporter
as the measurement event of `rhi_reporter.measure`, on the bus the variable `RHI_MEASUREMENT_BUS`
names, for the commit CodeBuild gives in `CODEBUILD_RESOLVED_SOURCE_VERSION`. A spike declares
itself as a `Spike`: its id, the enumeration of its numbers' names, and its pass condition as a
function of the numbers, from which the run's verdict follows.

**The run label.** One event is one run, and its label keeps two runs of one measurement apart.
The label is the variable `RHI_RUN_LABEL`, which the owner sets when starting the job; without
it, the job's name with its underscores replaced by hyphens (`s3-harness-task` for the job
`s3_harness_task`), so that two jobs never share a label by default. A label that is not lower-case
letters, digits and hyphens stops the job before it measures anything.

A spike that raises before it has its numbers still sends one event, with the verdict error and
no number, and the job then fails as it would have. No error raised here repeats text from AWS.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from rhi_common import spike_keys
from rhi_reporter.measure import BUS_VARIABLE, Verdict, measurement_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from enum import StrEnum

COMMIT_VARIABLE = "CODEBUILD_RESOLVED_SOURCE_VERSION"
RUN_LABEL_VARIABLE = "RHI_RUN_LABEL"

# The name Managed Prometheus signs its requests under.
SIGNING_SERVICE = "aps"
# How long one read of the workspace may take.
QUERY_TIMEOUT_SECONDS = 30

_RUN_LABEL = re.compile(r"[a-z0-9-]+")

type Numbers = Mapping[str, int | float]


# Timing.


class Stopwatch:
    """Counts seconds from the moment it is made, on a clock that never goes back."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()

    def seconds(self) -> float:
        """The seconds since the stopwatch was made."""
        return self._clock() - self._started


def timed[Result](action: Callable[[], Result], clock: Callable[[], float] = time.monotonic) -> tuple[Result, float]:
    """What `action` returns, and the seconds it took."""
    watch = Stopwatch(clock)
    result = action()
    return result, watch.seconds()


# Samples.


def percentile(samples: Iterable[float], percent: float) -> float:
    """The value `percent` percent of the samples are at or below, interpolated between the two
    samples it falls between. ValueError for no sample, a sample that is not a number, or a
    percentage outside 0 to 100."""
    ordered = sorted(samples)
    if not ordered or any(math.isnan(sample) for sample in ordered):
        raise ValueError("a percentile needs one sample or more, each a number")
    if not 0 <= percent <= 100:
        raise ValueError("a percentage is from 0 to 100")
    rank = (len(ordered) - 1) * percent / 100
    below = math.floor(rank)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (rank - below)


def median(samples: Iterable[float]) -> float:
    """The 50th percentile of the samples."""
    return percentile(samples, 50)


# The workspace.


@dataclass(frozen=True)
class Series:
    """One series of a query's answer: its labels, the name among them, and its samples (time in seconds, value)."""

    labels: Mapping[str, str]
    samples: tuple[tuple[float, float], ...]

    def values(self) -> list[float]:
        """The values of the samples, in the order of their times."""
        return [value for _, value in self.samples]


def send_with_urllib(url: str, headers: Mapping[str, str], body: bytes) -> bytes:
    """POSTs the body and returns the answer's; an answer that is not 2xx raises."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=QUERY_TIMEOUT_SECONDS) as answer:
        return answer.read()


@dataclass(frozen=True)
class Workspace:
    """The Prometheus workspace at the query address `url`, read with requests that the credentials
    given sign for the region given."""

    url: str
    region: str
    credentials: Any
    send: Callable[[str, Mapping[str, str], bytes], bytes] = send_with_urllib

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[Series]:
        """Every series the range query `query` returns from `start` to `end`, times in seconds since
        the epoch, one sample each `step` seconds. ValueError for an answer that is not a range query's."""
        url = f"{self.url.rstrip('/')}/api/v1/query_range"
        body = urlencode({"query": query, "start": repr(start), "end": repr(end), "step": repr(step)}).encode()
        request = AWSRequest(
            method="POST", url=url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        SigV4Auth(self.credentials, SIGNING_SERVICE, self.region).add_auth(request)
        return series_of(self.send(url, dict(request.headers.items()), body))


def series_of(answer: bytes | str) -> list[Series]:
    """The series an answer to a range query holds, in the format of the Prometheus HTTP API.
    ValueError, which repeats nothing of the answer, for any other answer."""
    refusal = ValueError("the workspace's answer is not the result of a range query")
    try:
        held = json.loads(answer)
        if held["status"] != "success" or held["data"]["resultType"] != "matrix":
            raise refusal
        return [
            Series(
                labels={str(name): str(value) for name, value in one["metric"].items()},
                samples=tuple((float(at), float(value)) for at, value in one["values"]),
            )
            for one in held["data"]["result"]
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        # Whatever the parser or a missing field raised would quote the answer: it does not travel.
        raise refusal from None


# The canary students.


def canary_keys(secrets: Any = None) -> spike_keys.SpikeKeys:
    """The canary students' keys, from the spikes' secret. `secrets` is a Secrets Manager client of
    the AWS SDK; without it, one made with the job's own credentials."""
    return spike_keys.read(boto3.client("secretsmanager") if secrets is None else secrets)


# Posting.


@dataclass(frozen=True)
class Spike:
    """What a spike declares: its id (`rhi_reporter.measure.IDS`), the enumeration of the names of
    its numbers, and its pass condition, which is given the numbers of a run and says whether they pass."""

    measurement: str
    names: type[StrEnum]
    passes: Callable[[Numbers], bool]

    def verdict(self, numbers: Numbers) -> Verdict:
        """Pass or fail: what the pass condition makes of these numbers."""
        return Verdict.PASS if self.passes(numbers) else Verdict.FAIL


def run_label(job: str, environ: Mapping[str, str]) -> str:
    """The label of this run of the job `job`: `RHI_RUN_LABEL` when it is set, and only without it
    the job's name with its underscores replaced by hyphens. ValueError for a label of another shape,
    which an empty one is."""
    label = environ.get(RUN_LABEL_VARIABLE, job.replace("_", "-"))
    if not _RUN_LABEL.fullmatch(label):
        raise ValueError("the run label is not lower-case letters, digits and hyphens")
    return label


def _send(events: Any, entry: Mapping[str, str]) -> None:
    answer = events.put_events(Entries=[entry])
    if answer.get("FailedEntryCount"):
        raise RuntimeError("the measurement bus did not take the event")


def run_spike(
    spike: Spike,
    job: str,
    measure: Callable[[], Numbers],
    *,
    environ: Mapping[str, str] | None = None,
    events: Any = None,
) -> Verdict:
    """Runs `measure`, which returns the numbers of one run of the spike under their declared
    names, and sends them with their verdict as one measurement event; returns the verdict.

    `job` is the job's name, the module's. `environ` is the job's environment, the process's own
    unless given, and `events` an EventBridge client of the AWS SDK, made with the job's own
    credentials unless given.

    The run label, the bus, the commit and the spike's declaration are checked before `measure`
    is called: ValueError, with nothing measured and nothing sent, when one is missing or of
    another shape. When `measure` raises, or returns what is not the declared names with finite
    numbers, one event is sent with the verdict error and no number, and the error is raised again.
    """
    environ = os.environ if environ is None else environ
    label = run_label(job, environ)
    bus, commit = environ.get(BUS_VARIABLE), environ.get(COMMIT_VARIABLE)
    if not bus or not commit:
        raise ValueError("the measurement bus or the commit is not in the job's environment")
    names = [name.value for name in spike.names]

    def event(verdict: Verdict, numbers: Numbers) -> dict[str, str]:
        return measurement_event(spike.measurement, label, verdict, numbers, names=names, commit=commit, bus=bus)

    # Built before anything is measured: it refuses an id, a commit or a name of another shape.
    not_measured = event(Verdict.NOT_MEASURED, {})
    if events is None:
        events = boto3.client("events")
    try:
        numbers = dict(measure())
        verdict = spike.verdict(numbers)
        entry = event(verdict, numbers)
    except BaseException:
        # Whatever ends the measurement, an exit included: the run is recorded as not measured.
        _send(events, not_measured)
        raise
    _send(events, entry)
    return verdict
