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
| `RHI_SPIKE_RUN_MINUTES` | How long the job may wait for the invocations, which the session must outlast. Required |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |

**In a booked session.** Before it sends anything the job checks the pool's session
(`common.require_session`): active, with the instances in service, lasting the run's minutes from
now. Otherwise it writes `job_failed` with the reason and sends nothing.

**The wait.** Run Command resolves the tag into invocations, one for each instance, and lists them
as it does (`ListCommandInvocations`), so an invocation may appear some time after the command was
sent. The job reads the list every `POLL_SECONDS` and stops once at least the instances it was
given have appeared, every one that has appeared has ended, and no new one has appeared for
`SETTLE_SECONDS`; or once the run's minutes have passed, with what it then holds.

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
# Seconds between two reads of the invocations, and how long no new one may appear before the job
# takes the list as whole.
POLL_SECONDS = 10
SETTLE_SECONDS = 60
# What Run Command answers for a command it does not list yet.
NOT_LISTED_YET = "InvalidCommandId"


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


def invocations(ssm: Any, command_id: str) -> list[dict[str, Any]]:
    """Every invocation of the command Run Command lists now, with its steps' output, in its order;
    none for a command it does not list yet."""
    found: list[dict[str, Any]] = []
    asked: dict[str, Any] = {"CommandId": command_id, "Details": True}
    while True:
        try:
            page = ssm.list_command_invocations(**asked)
        except botocore.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") == NOT_LISTED_YET and not found:
                return []
            raise
        found += page.get("CommandInvocations", [])
        if not page.get("NextToken"):
            return found
        asked["NextToken"] = page["NextToken"]


def check(
    ssm: Any,
    pool: str,
    *,
    instances: int,
    run_minutes: int,
    now: Callable[[], float],
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """Sends the document to the pool's instances, by the tag, and waits for its invocations
    (module docstring, "The wait"); returns each one's last listing, in order of appearance."""
    sent = ssm.send_command(
        DocumentName=DOCUMENT,
        Targets=[{"Key": f"tag:{POOL_TAG}", "Values": [pool]}],
        MaxConcurrency="100%",
        MaxErrors="100%",
        TimeoutSeconds=max(30, run_minutes * 60),
    )
    command_id = sent["Command"]["CommandId"]
    deadline = now() + run_minutes * 60
    order: list[str] = []
    held: dict[str, dict[str, Any]] = {}
    last_new = now()
    while True:
        for invocation in invocations(ssm, command_id):
            instance = str(invocation.get("InstanceId"))
            if instance not in held:
                order.append(instance)
                last_new = now()
            held[instance] = invocation
        at = now()
        ended = all(status_of(held[instance]) in ENDED for instance in order)
        if (len(order) >= instances and ended and at - last_new >= SETTLE_SECONDS) or at >= deadline:
            return [held[instance] for instance in order]
        sleep(max(0.0, min(POLL_SECONDS, deadline - at)))


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
    needs = common.needs_of(environ)
    name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
    settings = config.read_ssm()
    pools = [pool for pool in settings.pools if pool.name == name]
    if len(pools) != 1:
        raise ValueError("the deployed configuration has no pool of that name")
    (pool,) = pools
    common.require_session(
        common.pool_state_table(environ) if table is None else table,
        name,
        instances=needs.instances,
        run_minutes=needs.run_minutes,
        session_max_hours=settings.session_max_hours,
        now=now,
    )
    lines.write(Line.JOB, pool=name, engine=pool.engine)
    checked = check(
        boto3.client("ssm") if ssm is None else ssm,
        name,
        instances=needs.instances,
        run_minutes=needs.run_minutes,
        now=now,
        sleep=sleep,
    )
    return record(lines, checked, instances=needs.instances)


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
