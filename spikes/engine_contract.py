"""The engine contract check on a pool's instances, as a job the owner starts (system design,
sections 4.1, 8.4 and 12).

The pools stack's Systems Manager document `rhi-engine-contract` (`rhi.pools.contract_check`)
runs the release's contract suite on an instance against that instance's own engine, and prints
only `rhi-contract <test> <outcome>` for each of the four tests and `rhi-contract exit <status>`.
This job sends it, in a session the owner booked, to the instances whose tag `rhi-pool` is the
pool's name, and records what each invocation reported. It names no instance, and reads nothing
of EC2 or Auto Scaling: Run Command finds the instances by the tag.

| Variable | What it gives |
| --- | --- |
| `RHI_SPIKE_POOL` | The pool, `coursework` unless given |
| `RHI_SPIKE_INSTANCES` | The instances the session must have in service, and the fewest that must answer; 1 unless given |
| `RHI_SPIKE_RUN_MINUTES` | How long the job sends and waits for the invocations, which the session must outlast. Required |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |

**In a booked session.** Before it sends anything the job checks the pool's session
(`common.require_session`): active, with the instances in service, lasting the run's minutes from
now. Otherwise it writes `job_failed` with the reason and sends nothing.

**The wait.** The run's minutes start when the session check admits the run, and the send and
every read of the invocations are taken out of them: no request to Systems Manager starts once they
have ended, the send none at their end, so none is made after the session the check read has ended.
Run Command resolves the tag into invocations, one for each instance, and lists them as it does
(`ListCommandInvocations`), so an invocation may appear at any time after the command was sent,
and nothing the role may read says how many instances the tag reaches. So the job reads the list
every `POLL_SECONDS` until the run's minutes end, and records each instance's last listing read
within them: a page of the list that comes back once they have ended is not taken, and an
invocation that has not ended by then does not pass. A listing the end of the run's minutes cut
after a page that said more follow is incomplete: what it read is recorded, and the job fails with
`IncompleteListing`. When a read fails, the job records what it read until then, and then the
error.

**Within the build.** A build ends at its project's timeout, which the project gives the job as
`RHI_SPIKE_BUILD_MINUTES` (`rhi.pipeline.spikes`), counted from CodeBuild's
`CODEBUILD_START_TIME`, or from the job's own start outside a build. A run whose minutes, with
`BUILD_MARGIN_SECONDS` to write what it read, would end after that is refused before anything is
read or sent (`RunOutlastsBuild`); one admitted later than that allows still ends its wait in time.
A run whose minutes ended before the command could be sent fails with `RunElapsed`, nothing sent.

**What is recorded**, one JSON object a line on standard output, which the spike jobs' log group
keeps. Every line has `at`, its UTC time, and `kind`:

| Kind | What it holds |
| --- | --- |
| `job` | The pool and its engine |
| `instance_check` | For each instance the command reached, `instance`, its order of appearance from 1, never its ID; `status`, the invocation's status, one of `STATUSES` or `other`; `tests`, each of the four tests and its outcome, `null` for one the output does not give; `exit`, the exit status the output gives, or `null` |
| `job_failed` | An error that stopped the job: the type of the error, never its text, and for a job the pool's session does not carry, the reason |

Of an invocation's output only the lines of the vocabulary of `services/engine/contract/run.sh`
are read: `rhi-contract <test> <outcome>`, the test one of `TESTS` and the outcome one of
`OUTCOMES`, and `rhi-contract exit <status>`, the status a number. Every other line is dropped and
written nowhere.

The job exits 0 only when at least the instances it was given answered, and every invocation
ended in `Success` with each of the four tests `passed`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import boto3
import botocore.config
import botocore.exceptions

import common
from rhi import config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

JOB = "engine_contract"
POOL_VARIABLE = "RHI_SPIKE_POOL"
DEFAULT_POOL = "coursework"
# The document of the pools stack, and the tag every instance of a pool carries with its name
# (`rhi.pools.contract_check.DOCUMENT_NAME`, `rhi.pools.group.POOL_TAG`).
DOCUMENT = "rhi-engine-contract"
POOL_TAG = "rhi-pool"
# The vocabulary of `services/engine/contract/run.sh`.
PREFIX = "rhi-contract"
TESTS = ("test_inference", "test_health", "test_metrics", "test_bearer_secret")
OUTCOMES = frozenset({"passed", "failed", "error", "skipped", "missing"})
PASSED = "passed"
# The statuses of an invocation Run Command lists; any other is written `other`. Those after which
# an invocation does nothing more are `ENDED`.
STATUSES = ("Pending", "InProgress", "Delayed", "Success", "Cancelled", "TimedOut", "Failed", "Cancelling")
ENDED = frozenset({"Success", "Cancelled", "TimedOut", "Failed"})
SUCCESS = "Success"
OTHER = "other"
# Seconds between two reads of the invocations.
POLL_SECONDS = 10
# What Run Command answers for a command it does not list yet.
NOT_LISTED_YET = "InvalidCommandId"
# What CodeBuild gives a build: the minutes its project lets it run (`rhi.pipeline.spikes`), and
# the time it started, in milliseconds since the epoch.
BUILD_MINUTES_VARIABLE = "RHI_SPIKE_BUILD_MINUTES"
BUILD_START_VARIABLE = "CODEBUILD_START_TIME"
# Seconds kept, at the end of a build, for a request started at the end of the run's minutes to
# come back, bounded by `SSM_CLIENT`, and for the lines to be written.
BUILD_MARGIN_SECONDS = 300
# Each request to Systems Manager ends within about two minutes, its retries included.
SSM_CLIENT = botocore.config.Config(
    connect_timeout=10, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3}
)


class RunOutlastsBuild(Exception):
    """The run's minutes would end after the build does."""


class RunElapsed(Exception):
    """The run's minutes ended before the command could be sent."""


class IncompleteListing(Exception):
    """The run's minutes ended within a listing of the invocations that said more follow."""


class Listing(StrEnum):
    """How one listing of the invocations ended."""

    WHOLE = "whole"  # every page, within the run's minutes
    LATE = "late"  # nothing of it taken: the listing before it stands
    CUT = "cut"  # some pages taken, and one more known that was not


class Line(StrEnum):
    """The kinds of line the job writes."""

    JOB = "job"
    INSTANCE_CHECK = "instance_check"
    JOB_FAILED = "job_failed"


def utc(seconds: float) -> str:
    """A time in seconds since the epoch, as a UTC ISO time to the millisecond."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Lines:
    """Writes each line as one JSON object with its UTC time and its kind."""

    def __init__(self, *, wall: Callable[[], float] = time.time, out: Callable[[str], None] = print) -> None:
        self.wall, self.out = wall, out

    def write(self, kind: Line, **fields: Any) -> None:
        self.out(json.dumps({"at": utc(self.wall()), "kind": kind.value, **fields}, separators=(",", ":")))


def reported(output: str) -> tuple[dict[str, str | None], int | None]:
    """Each test's outcome and the exit status an invocation's output gives, from the lines of the
    vocabulary alone: a test the output does not give is None, and so is a missing exit status."""
    tests: dict[str, str | None] = dict.fromkeys(TESTS)
    status: int | None = None
    for line in output.splitlines():
        words = line.split()
        if len(words) != 3 or words[0] != PREFIX:
            continue
        _, name, value = words
        if name in tests and value in OUTCOMES:
            tests[name] = value
        elif name == "exit" and value.isascii() and value.isdigit() and len(value) <= 3:
            status = int(value)
    return tests, status


def status_of(invocation: Mapping[str, Any]) -> str:
    """The invocation's status, when it is one Run Command lists; `other` otherwise."""
    status = invocation.get("Status")
    return status if status in STATUSES else OTHER


def output_of(invocation: Mapping[str, Any]) -> str:
    """What the steps of an invocation printed."""
    return "\n".join(str(plugin.get("Output") or "") for plugin in invocation.get("CommandPlugins", []))


def read_invocations(
    ssm: Any, command_id: str, held: dict[str, Mapping[str, Any]], *, deadline: float, now: Callable[[], float]
) -> Listing:
    """Reads every invocation of the command Run Command lists now into `held`, by instance, page by
    page, an instance first held keeping its place; none for a command it does not list yet. No
    request starts after `deadline`, and a page that comes back after it is not taken: LATE when
    that stops the first page, CUT when it stops a later one, WHOLE otherwise."""
    asked: dict[str, Any] = {"CommandId": command_id, "Details": True}
    while True:
        stopped = Listing.CUT if "NextToken" in asked else Listing.LATE
        if now() > deadline:
            return stopped
        try:
            page = ssm.list_command_invocations(**asked)
        except botocore.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") == NOT_LISTED_YET and "NextToken" not in asked:
                return Listing.WHOLE if now() <= deadline else Listing.LATE
            raise
        if now() > deadline:
            return stopped
        for invocation in page.get("CommandInvocations", []):
            held[str(invocation.get("InstanceId"))] = invocation
        if not page.get("NextToken"):
            return Listing.WHOLE
        asked["NextToken"] = page["NextToken"]


def check(
    ssm: Any,
    pool: str,
    held: dict[str, Mapping[str, Any]],
    *,
    deadline: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
) -> Listing:
    """Sends the document to the pool's instances, by the tag, and reads its invocations into
    `held` until `deadline` (module docstring, "The wait"); how the last listing ended. `RunElapsed`,
    nothing sent, once `deadline` has come."""
    if now() >= deadline:
        raise RunElapsed
    sent = ssm.send_command(
        DocumentName=DOCUMENT,
        Targets=[{"Key": f"tag:{POOL_TAG}", "Values": [pool]}],
        MaxConcurrency="100%",
        MaxErrors="100%",
        # How long an instance may take to start the check: no longer than the run has left.
        TimeoutSeconds=max(30, int(deadline - now())),
    )
    command_id = sent["Command"]["CommandId"]
    last = Listing.LATE
    while True:
        listing = read_invocations(ssm, command_id, held, deadline=deadline, now=now)
        if listing is not Listing.LATE:
            last = listing
        if listing is not Listing.WHOLE or now() >= deadline:
            return last
        sleep(min(POLL_SECONDS, deadline - now()))


def record(lines: Lines, checked: Iterable[Mapping[str, Any]], *, instances: int) -> int:
    """Writes one `instance_check` for each invocation, by its order; 0 when at least `instances`
    answered and every one ended in `Success` with the four tests passed, 1 otherwise."""
    passed = []
    for number, invocation in enumerate(checked, start=1):
        status = status_of(invocation)
        tests, exit_status = reported(output_of(invocation))
        lines.write(Line.INSTANCE_CHECK, instance=number, status=status, tests=tests, exit=exit_status)
        passed.append(status == SUCCESS and all(outcome == PASSED for outcome in tests.values()))
    return 0 if len(passed) >= instances and all(passed) else 1


def run_job(
    environ: Mapping[str, str],
    lines: Lines,
    *,
    ssm: Any = None,
    table: Any = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The job, from its environment and credentials. `common.SessionRefused`, with nothing sent,
    when the pool's session does not carry the run. `ssm` and `table` are a Systems Manager client
    and the pool-state table of the AWS SDK, made with the job's own credentials unless given."""
    started = now()
    needs = common.needs_of(environ)
    # The latest the run may end: early enough that the build outlives what the job then writes.
    ends_by = build_end(environ, started) - BUILD_MARGIN_SECONDS
    if started + needs.run_minutes * 60 > ends_by:
        raise RunOutlastsBuild
    name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
    settings = config.read_ssm()
    pools = [pool for pool in settings.pools if pool.name == name]
    if len(pools) != 1:
        raise ValueError("the deployed configuration has no pool of that name")
    (pool,) = pools
    # The time the session check reads, at which the run's minutes start.
    admitted: list[float] = []

    def admitting() -> float:
        admitted.append(now())
        return admitted[-1]

    common.require_session(
        common.pool_state_table(environ) if table is None else table,
        name,
        instances=needs.instances,
        run_minutes=needs.run_minutes,
        session_max_hours=settings.session_max_hours,
        now=admitting,
    )
    deadline = min(admitted[-1] + needs.run_minutes * 60, ends_by)
    lines.write(Line.JOB, pool=name, engine=pool.engine)
    held: dict[str, Mapping[str, Any]] = {}
    client = boto3.client("ssm", config=SSM_CLIENT) if ssm is None else ssm
    try:
        listing = check(client, name, held, deadline=deadline, now=now, sleep=sleep)
    except Exception:
        # What was read before the error is recorded all the same; the error then fails the job.
        record(lines, held.values(), instances=needs.instances)
        raise
    passed = record(lines, held.values(), instances=needs.instances)
    if listing is Listing.CUT:
        raise IncompleteListing
    return passed


def build_end(environ: Mapping[str, str], started: float) -> float:
    """When the build ends, in seconds since the epoch: its start, CodeBuild's or `started`, and
    the minutes its project lets it run; never, outside a build that gives them."""
    minutes = environ.get(BUILD_MINUTES_VARIABLE)
    if minutes is None:
        return float("inf")
    start = environ.get(BUILD_START_VARIABLE)
    return (started if start is None else int(start) / 1000) + int(minutes) * 60


def main(environ: Mapping[str, str] = os.environ, out: Callable[[str], None] = print, **given: Any) -> int:
    """Runs the job; exits 1 when it did not pass, or when an error stopped it: that error is a
    line `job_failed` with its type's name, never its text, which may be what AWS returned, and the
    reason when the pool's session does not carry the run. `given` goes to `run_job`."""
    lines = Lines(wall=given.get("now", time.time), out=out)
    try:
        return run_job(environ, lines, **given)
    except common.SessionRefused as refused:
        lines.write(Line.JOB_FAILED, error=type(refused).__name__, reason=refused.reason.value)
        return 1
    except Exception as error:  # noqa: BLE001 - the job's boundary: no traceback reaches the log
        lines.write(Line.JOB_FAILED, error=type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
