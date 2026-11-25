"""The weekly GPU canary: the coursework pool's smoke tests against the deployed endpoint, and the
measurement `gpu-canary` of their result (system design, sections 8.2, step 6, and 14). The
canary's job runs it, on the commit of `main` its schedule started it on
(`src/rhi/pipeline/canary.py`).

**The run.** The script reads the canary's key from the secret `rhi/canary-key`, as the
verification does (`verify.canary_key`), and the coursework pool's release parameter, whose
settings name the stage's Prometheus workspace by its remote-write address and the stage's
heartbeats table by its name (`rhi_lifecycle.release`). It runs the smoke tests of `tests/smoke`
once, with `RHI_SMOKE_POOL` naming the coursework pool, through the verification's own runner
(`verify.run_tests`): in a process of their own, the key in its environment and nowhere else, what
they print in the log without the key, and the tests that must reach the internal load balancer
left out. The run makes every test the verification makes on the coursework pool after a refresh,
and no other: the workspace's query address and the heartbeats table are given as the verification
gives them, `RHI_SMOKE_WORKSPACE_URL` and `RHI_VERIFY_HEARTBEATS_TABLE`, so the release each
instance serves is compared with the deployed one both through the workspace (`release-served`,
`gpu-release-served`) and through the row the instance writes itself (`release-heartbeat`), which
the canary's role may query for the coursework pool alone (`src/rhi/pipeline/canary.py`). The run
records its results as JUnit XML, which the script reads back.

**The session.** With sessions, no instance of the coursework pool runs unless someone booked
one, so the canary books for itself, as the verification does and with its routine
(`verify.run_in_a_session`, over `booking.py`): one instance, under the name `BOOKED_BY`, for twice
`RHI_VERIFY_CHECK_WAIT_MINUTES`, the wait and as long again for the tests, so that a job that dies
leaves a session that ends by itself. It finds the stage's pool-state table by its logical ID in
the pools stack, as the deployment hold does, reads the gateway's `GET /status` with the canary's
key until the pool reads `ready` or `RHI_VERIFY_CHECK_WAIT_MINUTES` have passed since the booking,
runs the tests, and stops the session it booked, whatever became of the run and only while the
session still reads as its booking. A pool that already has a session, booked or active, is
neither booked nor stopped: the tests run at once, on the instances that are there.

**No capacity.** When the wait ends and the pool does not read ready, the canary asks, as the
verification does (`verify.Launches`), whether an instance was launched at all: the heartbeat rows
of the coursework pool's instances written since the booking, in the table the release parameter
names, which the canary's role may query for that pool alone. No instance launched is a lack of
capacity (`docs/design/evidence.md`, E32): no test runs, and the run did not measure. An instance
that was launched and did not serve by the end of the wait is a failed canary; the tests still
run, for what they show in the log. When the table cannot tell, the tests run and their result
stands: a launched instance is never reported as a lack of capacity.

**The measurement.** One run of `gpu-canary`, on the commit the job ran from,
`CODEBUILD_RESOLVED_SOURCE_VERSION`, with the UTC date the run began, YYYY-MM-DD, as its run label:
a record keeps one run per label, so each week's run keeps its own. A measurement carries a verdict
and named numbers, never text, so each of the canary's three results is a verdict, and a number
says why a run did not measure:

- pass: the wait did not end on a launched instance that did not serve, the run of the tests
  ended, at least one smoke test passed and none failed;
- fail: a smoke test failed, in its set-up, its call or its teardown, or an instance was launched
  and the pool did not read ready by the end of the wait, whatever the tests then gave, and also
  when their run left no record or one that cannot be read;
- error, not measured, with `no_capacity` 1: no instance was launched during the wait.

A run that could not measure for another reason is an error too, with `no_capacity` 0: the endpoint
has no HTTPS listener (`RHI_SMOKE_ENDPOINT` reads `none`), the canary's secret is missing or holds
no key, the pool could not be booked, the run left no result or one that cannot be read, or it
did not end (pytest was interrupted, or ended with an exit code other than its two of a finished
run, `FINISHED`).

Its numbers, `Number`, those the run has:

- `wait_seconds`: the seconds from the booking to the pool's first reading of `ready`, or to the
  end of the wait when none came; 0 on a pool that read ready when the job began, before any
  booking, and on a pool that already had a session, for which the canary does not wait. Left out
  when the pool was never reached or could not be booked;
- `smoke_tests_passed` and `smoke_tests_failed`: only the tests that ended count, since a case the
  JUnit XML records without its name is one pytest did not finish. A failed canary always has
  both: 0 and 0 when an instance was launched, did not serve, and the run of the tests left no
  record of a test. Left out of a run that did not measure and has no record of a test;
- `no_capacity`: 1 when no instance was launched during the wait, 0 otherwise.

It is sent through the measurement event (`rhi_reporter.measure.measurement_event`) on the bus
`RHI_MEASUREMENT_BUS` names. An event the bus does not take fails the script: EventBridge answers
a refused entry with a count of failed entries, not with an error.

**The exit.** Once the event is sent: the tests' exit code, and 1 when that code is 0 and a run
that made tests still is not a pass, so a canary that is not green after a run is a failed build
as well. A week without capacity ran no test and is not a failed canary: its exit is 0. A session
the script booked and could not stop, or a pool it could not book, makes the exit 1; such a
session ends by itself.

What the job gives, in the environment, beside the commit and the bus: `RHI_SMOKE_ENDPOINT`,
`RHI_SMOKE_RELEASE_PARAMETERS`, and the four variables of the verification's session,
`RHI_VERIFY_POOLS_STACK`, `RHI_VERIFY_POOL_STATE_LOGICAL_ID`, `RHI_VERIFY_CHECK_WAIT_MINUTES` and
`RHI_VERIFY_SESSION_MAX_HOURS` (`scripts/pipeline/verify.py`).

Run by the canary's job, from the checkout:

    uv run --locked python scripts/pipeline/canary_result.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from botocore.exceptions import BotoCoreError, ClientError

import verify
from rhi_reporter.measure import BUS_VARIABLE, Verdict, measurement_event

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

NAME = "canary_result"
MEASUREMENT = "gpu-canary"
POOL = "coursework"
# The name the canary's session is booked under.
BOOKED_BY = "pipeline/canary"

COMMIT_VARIABLE = "CODEBUILD_RESOLVED_SOURCE_VERSION"
WORKSPACE_URL_VARIABLE = "RHI_SMOKE_WORKSPACE_URL"
# What follows the workspace's own address in the remote-write address of a release's settings
# (`rhi.pools.release`).
REMOTE_WRITE_PATH = "api/v1/remote_write"
# The exit codes of a run of pytest that ended: every test passed, or some failed.
FINISHED = (0, 1)

# The format of the run label: the UTC date.
LABEL_FORMAT = "%Y-%m-%d"


class Number(StrEnum):
    """The names of the canary's numbers."""

    WAIT_SECONDS = "wait_seconds"
    SMOKE_TESTS_PASSED = "smoke_tests_passed"
    SMOKE_TESTS_FAILED = "smoke_tests_failed"
    NO_CAPACITY = "no_capacity"


NAMES: tuple[str, ...] = tuple(Number)


@dataclass(frozen=True)
class Wait:
    """What the wait for the session's instance gave: its seconds, `None` when none were measured;
    whether no instance was launched in it; and whether one was launched and the pool still did
    not read ready when it ended."""

    seconds: float | None = None
    no_capacity: bool = False
    unserved: bool = False


# A run that never reached the pool.
NOT_WAITED = Wait()


@dataclass(frozen=True)
class Results:
    """How many smoke tests passed and how many failed, and whether the run ended."""

    passed: int
    failed: int
    finished: bool = True


def read_results(junit_xml: str) -> Results:
    """The results a run recorded as JUnit XML: a test case with a failure or an error, in any
    phase, failed; one that was skipped is neither; any other passed. A test pytest reports twice,
    a failed teardown after its call, counts once. A case without its name is a test pytest did not
    finish, interrupted: it counts as neither, and the run as one that did not end."""
    outcomes: dict[tuple[str, str], bool] = {}
    finished = True
    for case in ElementTree.fromstring(junit_xml).iter("testcase"):
        test = (case.get("classname", ""), case.get("name", ""))
        if not test[1]:
            finished = False
        elif case.find("failure") is not None or case.find("error") is not None:
            outcomes[test] = True
        elif case.find("skipped") is None:
            outcomes.setdefault(test, False)
    failed = sum(outcomes.values())
    return Results(passed=len(outcomes) - failed, failed=failed, finished=finished)


def read_report(report: Path) -> Results | None:
    """The results in the file a run of the tests was to record them in. None when the run left no
    file, or one that is not XML, as a run that was killed while it wrote leaves: such a run has no
    result, and the canary still reports."""
    if not report.is_file():
        return None
    try:
        return read_results(report.read_text(encoding="utf-8"))
    except (ElementTree.ParseError, UnicodeDecodeError):
        print(f"{NAME}: the record of the tests' run cannot be read", file=sys.stderr)
        return None


def ended(results: Results | None, exit_code: int) -> Results | None:
    """The results of a run whose pytest exited with `exit_code`: one that did not exit as a
    finished run does, `FINISHED`, did not end, whatever its XML holds."""
    if results is None or exit_code in FINISHED:
        return results
    return replace(results, finished=False)


def wait_of(outcome: verify.Outcome, *, ready_at_first: bool) -> Wait:
    """What the wait gave, from what became of the run in its session and from whether the pool
    read ready when the job began, before any booking. Its seconds are 0 on a pool that was ready
    then, also when the canary went on to book it; the seconds the session's routine waited, to a
    tenth, on a pool the canary booked; 0 on a pool that already had a session, for which nothing
    waits; and not measured on a pool that could not be booked."""
    if ready_at_first:
        seconds: float | None = 0.0
    elif outcome.waited is not None:
        seconds = round(outcome.waited, 1)
    else:
        seconds = None if outcome.exit_code is None else 0.0
    return Wait(seconds=seconds, no_capacity=outcome.no_capacity, unserved=outcome.unserved)


def verdict_of(results: Results | None, wait: Wait) -> Verdict:
    """The run's verdict: not measured when no instance was launched during the wait; fail when one
    was launched and did not serve by the wait's end, whatever the tests then gave, or when a test
    failed; not measured when no test ended or the run did not end; pass otherwise."""
    if wait.no_capacity:
        return Verdict.NOT_MEASURED
    if wait.unserved:
        return Verdict.FAIL
    if results is None or results.passed + results.failed == 0:
        return Verdict.NOT_MEASURED
    if results.failed:
        return Verdict.FAIL
    if not results.finished:
        return Verdict.NOT_MEASURED
    return Verdict.PASS


def numbers_of(results: Results | None, wait: Wait) -> dict[str, int | float]:
    """The run's numbers: the wait's seconds and the tests' counts when the run has them, and
    always whether it was a lack of capacity that kept it from measuring. A launched instance that
    did not serve is a failed canary with or without a record of the tests, and a failed canary
    has its counts: no test is known to have passed or failed when the run left no record."""
    numbers: dict[str, int | float] = {}
    if wait.seconds is not None:
        numbers[Number.WAIT_SECONDS] = wait.seconds
    counted = Results(passed=0, failed=0, finished=False) if results is None and wait.unserved else results
    if counted is not None and not wait.no_capacity:
        numbers[Number.SMOKE_TESTS_PASSED] = counted.passed
        numbers[Number.SMOKE_TESTS_FAILED] = counted.failed
    numbers[Number.NO_CAPACITY] = int(wait.no_capacity)
    return numbers


def run_label(now: datetime) -> str:
    """The run label of a run that began at `now`: its UTC date. ValueError for a time that names
    no zone, whose date in UTC is unknown."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the run's time names no zone")
    return now.astimezone(UTC).strftime(LABEL_FORMAT)


def measurement(results: Results | None, wait: Wait, *, commit: str, bus: str, now: datetime) -> dict[str, str]:
    """The entry of PutEvents that hands the run to the reporter."""
    return measurement_event(
        MEASUREMENT,
        run_label(now),
        verdict_of(results, wait),
        numbers_of(results, wait),
        names=NAMES,
        commit=commit,
        bus=bus,
    )


def send(entry: Mapping[str, str], client: Any) -> None:
    """Puts the event on the bus with `client`, an EventBridge client of the AWS SDK. RuntimeError
    when the bus does not take it."""
    answer = client.put_events(Entries=[dict(entry)])
    if answer.get("FailedEntryCount"):
        raise RuntimeError("the measurement bus did not take the canary's event")


def stage_settings(client: Any, env: Mapping[str, str]) -> dict[str, str]:
    """What the smoke tests are told of the stage, from the settings of the coursework pool's
    release parameter, read with `client`, an SSM client of the AWS SDK: the workspace's query
    address, its remote-write address without `REMOTE_WRITE_PATH`, and the heartbeats table's
    name. Nothing when the parameter
    cannot be read or holds no release: the tests that need it then fail by the variable's name."""
    # Imported here: the script's own folder and the reporter's are all its import needs elsewhere.
    from rhi_lifecycle.release import read_current

    try:
        name = json.loads(env.get(verify.SMOKE_RELEASE_PARAMETERS_VARIABLE, "")).get(POOL)
    except (ValueError, AttributeError):
        name = None
    current = read_current(client, name) if isinstance(name, str) and name else None
    if current is None:
        print(f"{NAME}: the coursework pool's release parameter was not read", file=sys.stderr)
        return {}
    return {
        WORKSPACE_URL_VARIABLE: current.settings.workspace_url.removesuffix(REMOTE_WRITE_PATH),
        verify.HEARTBEATS_TABLE_VARIABLE: current.settings.heartbeat_table,
    }


def _client(service: str) -> Any:
    import boto3

    return boto3.client(service)


def run_canary(
    env: Mapping[str, str],
    *,
    secrets: Any,
    ssm: Any,
    dynamodb: Any,
    cloudformation: Any,
    resource: Any,
    status: Callable[[str, float], str | None] | None,
    run: Callable[[Mapping[str, str], Sequence[str]], int],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[Results | None, Wait, int]:
    """Runs the smoke tests on the coursework pool, in a session the canary books on it when it has
    none: their results, what the wait gave and the run's exit code. No result when no test could
    run or none was run. A client that is None is made, from the build's own credentials and
    region, only once it is needed."""
    endpoint = env.get(verify.ENDPOINT_VARIABLE)
    if not endpoint:
        print(f"{NAME}: {verify.ENDPOINT_VARIABLE} is not set: no test was run", file=sys.stderr)
        return None, NOT_WAITED, 1
    if endpoint == verify.NO_ENDPOINT:
        print(f"{NAME}: no test was run: {verify.LISTENER_OFF}")
        return None, NOT_WAITED, 0
    key = verify.canary_key(_client("secretsmanager") if secrets is None else secrets)
    if key is None:
        print(f"{NAME}: no test was run: {verify.CANARY_MISSING}")
        return None, NOT_WAITED, 0
    check = verify.check_of(env)
    if check is None:
        print(
            f"{NAME}: {verify.POOLS_STACK_VARIABLE} and {verify.POOL_STATE_LOGICAL_ID_VARIABLE} name the pool-state "
            f"table, and {verify.CHECK_WAIT_MINUTES_VARIABLE} and {verify.SESSION_MAX_HOURS_VARIABLE} are whole "
            "numbers above zero: no test was run",
            file=sys.stderr,
        )
        return None, NOT_WAITED, 1
    stage = stage_settings(_client("ssm") if ssm is None else ssm, env)
    given = {**env, **stage, verify.KEY_VARIABLE: key, verify.POOL_VARIABLE: POOL}
    # The canary's pool is booked: its run is never the verification's run on a pool nobody has booked.
    given.pop(verify.UNBOOKED_VARIABLE, None)
    read = status or verify.status_reader(endpoint, key)
    if cloudformation is None or resource is None:
        import boto3

        cloudformation = cloudformation or boto3.client("cloudformation")
        resource = resource or boto3.resource("dynamodb")
    # The pool's state as the job begins, before any booking: a pool that is ready then is not waited for.
    ready_at_first = read(POOL, verify.READ_SECONDS) == verify.READY
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "results.xml"
        outcome = verify.run_in_a_session(
            check,
            POOL,
            lambda: run(given, (f"--junitxml={report}", verify.SMOKE_TESTS)),
            cloudformation=cloudformation,
            resource=resource,
            status=lambda within: read(POOL, within),
            sleep=sleep,
            clock=clock,
            launched=lambda since: verify.launched_rows(given, POOL, since, dynamodb),
            set_by=BOOKED_BY,
        )
        results = read_report(report)
    if outcome.exit_code is None:
        # No run was made: for lack of capacity, which fails nothing, or because the pool was not booked.
        results, exit_code = None, 0 if outcome.no_capacity else 1
    else:
        results, exit_code = ended(results, outcome.exit_code), outcome.exit_code
        if results is None:
            # A run of the tests that left no result is not a green one, whatever its exit.
            exit_code = exit_code or 1
    if not outcome.in_order:
        # A session the canary booked and could not stop, or could not book: it ends by itself.
        exit_code = exit_code or 1
    return results, wait_of(outcome, ready_at_first=ready_at_first), exit_code


def main(
    environ: Mapping[str, str] | None = None,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    secrets: Any = None,
    ssm: Any = None,
    events: Any = None,
    dynamodb: Any = None,
    cloudformation: Any = None,
    resource: Any = None,
    status: Callable[[str, float], str | None] | None = None,
    run: Callable[[Mapping[str, str], Sequence[str]], int] = verify.run_tests,
) -> int:
    """Runs the canary and sends its measurement; the run's exit code once it is sent, or 1 for a
    run of the tests that exited 0 and is not green. `clock` gives the time, in seconds since the
    epoch, and `sleep` waits that many seconds; `status` reads the state of the pool it is given in
    the seconds it is given, from the endpoint's `GET /status` when left out. `secrets`, `ssm`,
    `events`, `dynamodb` and `cloudformation` are clients of the AWS SDK and `resource` a DynamoDB
    resource: without one, the build's own credentials and region make it, when it is needed."""
    env = os.environ if environ is None else environ
    began = datetime.fromtimestamp(clock(), UTC)
    commit, bus = env.get(COMMIT_VARIABLE), env.get(BUS_VARIABLE)
    if not commit or not bus:
        print(f"{NAME}: {COMMIT_VARIABLE} and {BUS_VARIABLE} name the commit and the bus", file=sys.stderr)
        return 1
    try:
        results, wait, exit_code = run_canary(
            env,
            secrets=secrets,
            ssm=ssm,
            dynamodb=dynamodb,
            cloudformation=cloudformation,
            resource=resource,
            status=status,
            run=run,
            sleep=sleep,
            clock=clock,
        )
    except (BotoCoreError, ClientError) as error:
        # The canary could not measure: it says so, as not measured, rather than sending nothing.
        print(f"{NAME}: the canary did not run ({type(error).__name__})", file=sys.stderr)
        results, wait, exit_code = None, NOT_WAITED, 1
    entry = measurement(results, wait, commit=commit, bus=bus, now=began)
    send(entry, _client("events") if events is None else events)
    verdict = verdict_of(results, wait)
    print(f"{NAME}: {MEASUREMENT} {verdict.name.lower()} sent for {run_label(began)}")
    if verdict is Verdict.PASS or (results is None and verdict is not Verdict.FAIL):
        return exit_code
    return exit_code or 1


if __name__ == "__main__":
    sys.exit(main())
