"""Verifies a deployment: the smoke tests against the deployed endpoint, with the canary's key
(system design, section 8.2, step 6). The pipeline's verification step runs it after the stage's
deployment (`src/rhi/pipeline/verify.py`).

The script reads the canary's key from the secret `rhi/canary-key`, which the admin CLI's canary
command writes, and runs the smoke tests of `tests/smoke`, the deployed-only ones included, in a
process of their own. The key reaches that process in its environment and nowhere else: it is
never on a command line, and nothing here prints it. A failed test is a non-zero exit, which fails
the step and the execution.

**What the tests print reaches the build's log without the key.** A failing test prints what it
held, and a smoke test holds the key. So that process writes to this one, not to the log, and
each line goes on with `REDACTED` in place of the key and of any piece of it `PIECE` characters
long or longer: pytest shortens a long value by cutting its middle out, which can leave part of a
key.

**The tests it leaves out.** The forbidden routes are checked at the gateway's hop: the smoke
tests call them through the gateway and expect 404 (`tests/smoke/test_smoke.py`). The internal
load balancer's own rules, the four inference routes forwarded and a fixed 404 for everything
else, the health route included, are asserted on the synthesized template
(`tests/synth/test_internal_routing.py`): only the gateway may reach that load balancer (section
6.5), and this step runs outside the private network. So the only tests left out are those of
`GPU_PASS_ONLY`, in the run on the mock pool, the one that names no pool: they belong to the run
on the coursework pool. A run that names a pool leaves none out.

**The session on the mock pool.** Before its run of the tests on the mock pool, the script books
that pool through `booking.py`: a session of one instance, under the name `BOOKED_BY`, for twice
`RHI_VERIFY_CHECK_WAIT_MINUTES`, the wait below and as long again for the tests, so that a step
that dies leaves a session that ends by itself. It finds the stage's pool-state table by its
logical ID in the pools stack, as the deployment hold does. It then reads the gateway's
`GET /status`, with the canary's key, every `POLL_SECONDS`, until the pool reads `ready` or
`RHI_VERIFY_CHECK_WAIT_MINUTES` have passed since the booking, and runs the tests in either case.
No reading starts once the wait is over, and each is given no longer than what is left of it.
Whatever the run's result, it then stops the session, only while the session still reads as the
booking it made. A pool that already has a session booked or active is neither booked nor stopped:
the tests run at once, on the instances that are there. A pool that cannot be booked fails the
script before any test, and so does a booking longer than `RHI_VERIFY_SESSION_MAX_HOURS` allows. A
session that could not be stopped fails a run whose tests passed: it ends by itself. The script
says which of these happened in a line of its own, never in a message of AWS, and does not check
that the instance leaves once the session is stopped. The coursework pool's runs book nothing.

**The coursework pool.** Before this script, the step runs the decision of `gpu_verification.py`,
which waits for the instance refresh a GPU pool's release started and leaves, in the file
`RHI_VERIFY_GPU_POOLS_FILE` names, the pools the smoke tests must also run on: `coursework` when
this execution deployed a GPU pool's release, nothing otherwise. After the run on the mock pool,
the script runs the same smoke tests once more for each pool of that file, with `RHI_SMOKE_POOL`
naming it: the first stream's wake and hold, the streams of each format, the pool's metrics and
the release its lifecycle agents report as a metric and in their heartbeat rows,
`gpu-release-served` and `release-heartbeat` among them. Every run is made, and
the script's exit is that of the first that failed. With the variable set and no file, the
decision did not run, and the script fails before any test.

**When no test runs.** Two states of a correct deployment leave nothing to test: the endpoint has
no HTTPS listener yet, which its address reading `none` says, or the canary's secret does not
exist or holds no key, because the owner has not run the canary command yet. The script then runs
no test, writes its reason, `listener-off` or `canary-missing`, as the one line of the file
`RHI_VERIFY_REASON_FILE` names, and exits 0. The step's post-build phase copies that file to where
the reporter reads that the verification was not run: neither passed nor failed.

**The tests that failed.** A smoke test is known by its ID (`tests/smoke/ids.py`). As the run's
plugin, this file adds the ID of each test that fails, when it fails, as one more line of the file
`RHI_VERIFY_FAILED_TESTS_FILE` names: the IDs, one per line, and nothing else, so a run that is
cut short still leaves those it had. No failed test leaves no file. The step's post-build phase,
which runs after a failed build too, copies the file to where the reporter reads which tests
failed.

**Where the instances stopped.** When the run of the tests fails, the script reads the heartbeat
rows of the instances of each pool it tested, the pools `RHI_VERIFY_RELEASE_PARAMETERS` names, from
the table `RHI_VERIFY_HEARTBEATS_TABLE` names, and leaves the last lifecycle stage each instance
reached as one line of the file `RHI_VERIFY_LAST_STAGES_FILE` names: the row's `stage`, and nothing
else of a row, neither the instance's ID nor its release. It asks DynamoDB for that one attribute.
A launch names a stage of `rhi_lifecycle.stages` there, and an instance in service the serving
module's own name. No row, or no table named, leaves no file, and so does a table that cannot be
read, after a line in the log: the exit stays the tests'. The post-build phase copies the file to
where the reporter reads it, and the reporter names only the stages of its own list.

What the step gives, in the environment:

| Variable | Holds |
| --- | --- |
| `RHI_SMOKE_ENDPOINT` | The endpoint's address, or `none` |
| `RHI_SMOKE_WORKSPACE_URL` | The Prometheus workspace's query address, which the smoke tests read as it is |
| `RHI_VERIFY_RELEASE_PARAMETERS` | Each pool and its release parameter: `pool=name`, comma-separated, or `none` |
| `RHI_VERIFY_REASON_FILE` | The file the reason is left in |
| `RHI_VERIFY_FAILED_TESTS_FILE` | The file the IDs of the failed tests are left in |
| `RHI_VERIFY_HEARTBEATS_TABLE` | The name of the heartbeats table of the stage |
| `RHI_VERIFY_LAST_STAGES_FILE` | The file the instances' last stages are left in |
| `RHI_VERIFY_GPU_POOLS_FILE` | The file the decision left the pools to verify on in, one per line |
| `RHI_VERIFY_POOLS_STACK` | The name of the pools stack of the stage |
| `RHI_VERIFY_POOL_STATE_LOGICAL_ID` | The logical ID of the pool-state table in that stack |
| `RHI_VERIFY_CHECK_WAIT_MINUTES` | The configuration's `check_wait_minutes`, a whole number above zero |
| `RHI_VERIFY_SESSION_MAX_HOURS` | The configuration's `session_max_hours`, a whole number above zero |

The pools' parameters come from a stack's output through the pipeline, which puts the value into
JSON as it is, so they arrive without a quote; the smoke tests read them as JSON, in
`RHI_SMOKE_RELEASE_PARAMETERS`, which the script writes for them.

Run by the verification step, from the checkout, with PYTHONPATH holding the import path of
`pyproject.toml`, which the booking needs (`rhi_common`) and no earlier path of the script does:

    uv run --locked python scripts/pipeline/verify.py

The file is also the pytest plugin of the run it starts (`-p verify`): `pytest_collection_modifyitems`,
below, is what deselects, and `pytest_runtest_logreport` what leaves the IDs of the failed tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    import httpx
    import pytest

NAME = "verify"
REPO = Path(__file__).resolve().parents[2]
SMOKE_TESTS = "tests/smoke"

# The secret the canary command writes the canary's key into (`rhi_admin.canary`).
CANARY_SECRET = "rhi/canary-key"

ENDPOINT_VARIABLE = "RHI_SMOKE_ENDPOINT"
KEY_VARIABLE = "RHI_SMOKE_KEY"
SMOKE_RELEASE_PARAMETERS_VARIABLE = "RHI_SMOKE_RELEASE_PARAMETERS"
RELEASE_PARAMETERS_VARIABLE = "RHI_VERIFY_RELEASE_PARAMETERS"
REASON_FILE_VARIABLE = "RHI_VERIFY_REASON_FILE"
FAILED_TESTS_FILE_VARIABLE = "RHI_VERIFY_FAILED_TESTS_FILE"
HEARTBEATS_TABLE_VARIABLE = "RHI_VERIFY_HEARTBEATS_TABLE"
LAST_STAGES_FILE_VARIABLE = "RHI_VERIFY_LAST_STAGES_FILE"
GPU_POOLS_FILE_VARIABLE = "RHI_VERIFY_GPU_POOLS_FILE"
POOL_VARIABLE = "RHI_SMOKE_POOL"
POOLS_STACK_VARIABLE = "RHI_VERIFY_POOLS_STACK"
POOL_STATE_LOGICAL_ID_VARIABLE = "RHI_VERIFY_POOL_STATE_LOGICAL_ID"
CHECK_WAIT_MINUTES_VARIABLE = "RHI_VERIFY_CHECK_WAIT_MINUTES"
SESSION_MAX_HOURS_VARIABLE = "RHI_VERIFY_SESSION_MAX_HOURS"

# The pool the run that names no pool tests (`tests/smoke/conftest.py`), which the script books
# for that run, and the name it books it under.
MOCK_POOL = "mock"
BOOKED_BY = "pipeline/verify"
# The state `GET /status` gives a pool that serves (section 5.3).
READY = "ready"
# Between two readings of the pool's state while the script waits for it, and the longest one may
# take: less when less is left of the wait.
POLL_SECONDS = 10.0
READ_SECONDS = 10.0
MINUTE_SECONDS = 60

# The heartbeats table as far as the script reads it: its partition key, its sort key, what comes
# before an instance's ID in the sort key, and the attribute that holds the last stage the
# instance reached (`rhi_common.tables`, `rhi_common.records`; the script imports neither).
POOL_ATTRIBUTE = "pool"
SOURCE_ATTRIBUTE = "source"
INSTANCE_SOURCE = "instance#"
STAGE_ATTRIBUTE = "stage"
# At most this many pages of a pool's rows are read.
ROW_PAGES = 10

# What the endpoint's output reads while there is no HTTPS listener (`rhi.gateway.stack`), and the
# pools' output while there is no pool (`rhi.pools.stack`).
NO_ENDPOINT = "none"
NO_POOL = "none"

# The reasons no test ran: the fixed vocabulary the reporter reads.
LISTENER_OFF = "listener-off"
CANARY_MISSING = "canary-missing"

# The tests that only the run on a pool the decision named makes, by their IDs
# (`tests/smoke/ids.py`): a run that names no pool, the mock pool's, leaves them out, and they are
# the only tests the verification leaves out.
GPU_PASS_ONLY: tuple[str, ...] = ("gpu-release-served",)
# The marker a smoke test carries its ID in.
ID_MARKER = "smoke_id"

# What the build's log reads in place of the key, and the shortest piece of the key it replaces.
REDACTED = "[canary key]"
PIECE = 8

# As a plugin, in the process that runs the tests: the IDs of each test that is run, by the name
# pytest reports the test under, and the IDs already left as failed.
_ids: dict[str, tuple[str, ...]] = {}
_left: set[str] = set()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """As a plugin of the run of the smoke tests: in a run that names no pool, deselects the tests
    whose ID is one of `GPU_PASS_ONLY`, and none in a run that names one; keeps the IDs of the
    others for `pytest_runtest_logreport`."""
    deselected = set() if os.environ.get(POOL_VARIABLE) else set(GPU_PASS_ONLY)
    left_out = [item for item in items if any(set(marker.args) & deselected for marker in item.iter_markers(ID_MARKER))]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = [item for item in items if item not in left_out]
    for item in items:
        _ids[item.nodeid] = tuple(
            test_id for marker in item.iter_markers(ID_MARKER) for test_id in marker.args if isinstance(test_id, str)
        )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """As a plugin of the run of the smoke tests: when a test fails, in its set-up, its call or its
    teardown, adds its ID as a line of the file `RHI_VERIFY_FAILED_TESTS_FILE` names, once, even
    across the runs of one verification. Only the ID is written: nothing of what the test held or
    printed. Without the variable, nothing is."""
    named = os.environ.get(FAILED_TESTS_FILE_VARIABLE)
    failed = [test_id for test_id in _ids.get(report.nodeid, ()) if test_id not in _left] if report.failed else []
    if not named or not failed:
        return
    path = Path(named)
    if path.is_file():
        _left.update(path.read_text(encoding="utf-8").splitlines())
        failed = [test_id for test_id in failed if test_id not in _left]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.writelines(f"{test_id}\n" for test_id in failed)
    _left.update(failed)


def canary_key(client: Any) -> str | None:
    """The canary's key, from its secret. None when the secret does not exist or holds no key. Any
    other failure to read it is raised: a secret the step may not read is not a missing canary."""
    try:
        held = client.get_secret_value(SecretId=CANARY_SECRET).get("SecretString")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    key = held.strip() if isinstance(held, str) else ""
    return key or None


def release_parameters(text: str) -> dict[str, str]:
    """Each pool's release parameter, by the pool's name, from the text of the pools stack's output."""
    if not text or text == NO_POOL:
        return {}
    return dict(pair.split("=", 1) for pair in text.split(","))


def last_stages(client: Any, table: str, pools: Iterable[str]) -> list[str]:
    """The last lifecycle stage each instance of `pools` reached: the `stage` of each instance's row
    of the heartbeats table `table`, read by pool with `client`, a DynamoDB client of the AWS SDK.
    Only that attribute is asked for, and a row whose stage is not one line of text gives none."""
    found: list[str] = []
    for pool in pools:
        request: dict[str, Any] = {
            "TableName": table,
            "KeyConditionExpression": "#pool = :pool AND begins_with(#source, :instance)",
            "ProjectionExpression": "#stage",
            "ExpressionAttributeNames": {
                "#pool": POOL_ATTRIBUTE,
                "#source": SOURCE_ATTRIBUTE,
                "#stage": STAGE_ATTRIBUTE,
            },
            "ExpressionAttributeValues": {":pool": {"S": pool}, ":instance": {"S": INSTANCE_SOURCE}},
        }
        for _ in range(ROW_PAGES):
            answer = client.query(**request)
            stages = [item.get(STAGE_ATTRIBUTE, {}).get("S") for item in answer.get("Items", [])]
            found += [stage for stage in stages if isinstance(stage, str) and stage.splitlines() == [stage]]
            if "LastEvaluatedKey" not in answer:
                break
            request["ExclusiveStartKey"] = answer["LastEvaluatedKey"]
    return found


def leave_last_stages(env: Mapping[str, str], pools: Iterable[str], client: Any = None) -> None:
    """After a run of the tests that failed: leaves the last stage each instance of `pools` reached,
    one per line and nothing else, in the file `RHI_VERIFY_LAST_STAGES_FILE` names, for the step's
    post-build phase. Without the table's name, the file's or a row, it leaves no file. A table
    that cannot be read is a line in the log and no file: the tests' exit is the script's."""
    table, named = env.get(HEARTBEATS_TABLE_VARIABLE), env.get(LAST_STAGES_FILE_VARIABLE)
    pools = list(pools)
    if not table or not named or not pools:
        return
    try:
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        stages = last_stages(client, table, pools)
    except (BotoCoreError, ClientError) as error:
        print(f"{NAME}: the instances' last stages were not read ({type(error).__name__})", file=sys.stderr)
        return
    if not stages:
        return
    path = Path(named)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{stage}\n" for stage in stages), encoding="utf-8")


def without(secret: str) -> Callable[[str], str]:
    """What rewrites a line of text with `REDACTED` in place of `secret` and of every piece of it
    `PIECE` characters long or longer."""
    width = min(PIECE, len(secret))
    pieces = {secret[at : at + width] for at in range(len(secret) - width + 1)} if secret else set()

    def redacted(line: str) -> str:
        # Each stretch of the line that pieces of the secret cover, end to end.
        covered: list[list[int]] = []
        for at in range(len(line) - width + 1):
            if line[at : at + width] not in pieces:
                continue
            if covered and at <= covered[-1][1]:
                covered[-1][1] = at + width
            else:
                covered.append([at, at + width])
        for start, end in reversed(covered):
            line = line[:start] + REDACTED + line[end:]
        return line

    return redacted


def run_tests(environ: Mapping[str, str], tests: Sequence[str] = (SMOKE_TESTS,)) -> int:
    """Runs the tests with pytest in a process of its own, whose environment is `environ`, with
    this file as a plugin; pytest's exit code. It collects from the repository's root. What the
    process and its children print is printed here, line by line, without the key `environ` holds."""
    # pytest loads a plugin named on its command line before it reads the import path of
    # `pyproject.toml`: the plugin's folder goes on the process's own.
    folders = [str(Path(__file__).resolve().parent), *filter(None, [environ.get("PYTHONPATH")])]
    command = [sys.executable, "-m", "pytest", "-p", NAME, *tests]
    redacted = without(environ.get(KEY_VARIABLE, ""))
    # Unbuffered, so that the log follows the tests as they run.
    env = {**environ, "PYTHONPATH": os.pathsep.join(folders), "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen(
        command, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace"
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(redacted(line))
            sys.stdout.flush()
    return process.returncode


def not_run(reason: str, env: Mapping[str, str]) -> int:
    """Leaves the reason no test ran for the step's post-build phase; 0 once it is written."""
    named = env.get(REASON_FILE_VARIABLE)
    if not named:
        print(f"{NAME}: {REASON_FILE_VARIABLE} is not set: it names the file to leave the reason in", file=sys.stderr)
        return 1
    path = Path(named)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reason + "\n", encoding="utf-8")
    print(f"{NAME}: no test was run: {reason}")
    return 0


def gpu_pools(env: Mapping[str, str]) -> list[str] | None:
    """The pools the decision left in the file `RHI_VERIFY_GPU_POOLS_FILE` names, one per line:
    none without the variable. None when the variable names a file the decision did not leave."""
    named = env.get(GPU_POOLS_FILE_VARIABLE)
    if not named:
        return []
    path = Path(named)
    if not path.is_file():
        return None
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass(frozen=True)
class Check:
    """What the step gives for the session on the mock pool: the pools stack and the pool-state
    table's logical ID in it, the minutes the script waits for the session's instance, and the
    longest a session lasts, in hours."""

    stack: str
    logical_id: str
    wait_minutes: int
    session_max_hours: int


def _whole(text: str | None) -> int | None:
    """The whole number above zero the variable holds, `None` when it is missing or holds anything else."""
    return int(text) if text is not None and text.isascii() and text.isdigit() and int(text) > 0 else None


def check_of(env: Mapping[str, str]) -> Check | None:
    """What the environment gives for the session on the mock pool, `None` when a variable is
    missing or a number is not a whole number above zero."""
    stack, logical_id = env.get(POOLS_STACK_VARIABLE), env.get(POOL_STATE_LOGICAL_ID_VARIABLE)
    wait_minutes, hours = _whole(env.get(CHECK_WAIT_MINUTES_VARIABLE)), _whole(env.get(SESSION_MAX_HOURS_VARIABLE))
    if not stack or not logical_id or wait_minutes is None or hours is None:
        return None
    return Check(stack=stack, logical_id=logical_id, wait_minutes=wait_minutes, session_max_hours=hours)


def status_reader(
    endpoint: str, key: str, transport: httpx.BaseTransport | None = None
) -> Callable[[float], str | None]:
    """What reads the mock pool's state from `GET /status` of `endpoint`, with the canary's key,
    through `transport` when one is given: None for a reading that failed. A reading is given the
    seconds it may take, and `READ_SECONDS` when that is more: one that has no answer by then is
    given up, and reads as None. Nothing of the machine's own configuration, a proxy among it,
    takes part in a reading."""
    import httpx

    address = (endpoint if "://" in endpoint else f"https://{endpoint}").rstrip("/")

    def read(within: float) -> str | None:
        # A time-out of httpx is for each step of a request, the connection then each wait for the
        # answer, not for the whole of it: the request is made aside, and left when its seconds
        # are over. Its own time-outs end it soon after.
        seconds = min(READ_SECONDS, within)
        states: list[str | None] = []
        request = threading.Thread(target=lambda: states.append(answered(seconds)), daemon=True)
        request.start()
        request.join(seconds)
        return states[0] if states else None

    def answered(seconds: float) -> str | None:
        try:
            with httpx.Client(timeout=seconds, trust_env=False, transport=transport) as http:
                answer = http.get(f"{address}/status", headers={"Authorization": f"Bearer {key}"})
            states = {record["model"]: record["state"] for record in answer.json()["pools"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        state = states.get(MOCK_POOL) if answer.status_code == 200 else None
        return state if isinstance(state, str) else None

    return read


def wait_until_ready(
    status: Callable[[float], str | None], *, until: float, sleep: Callable[[float], None], clock: Callable[[], float]
) -> bool:
    """Reads the pool's state with `status` every `POLL_SECONDS` until it reads `READY`, or until
    the time `until` has come; whether it read ready. No reading starts at that time or after it,
    and each is given what is left until then as the seconds it may take."""
    while (left := until - clock()) > 0:
        if status(left) == READY:
            return True
        sleep(max(0.0, min(POLL_SECONDS, until - clock())))
    return False


def run_in_a_session(
    check: Check,
    run: Callable[[], int],
    *,
    cloudformation: Any,
    resource: Any,
    status: Callable[[float], str | None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[int | None, bool]:
    """Makes `run`, the mock pool's run of the tests, in a session the script books on that pool
    and stops after the run, whatever its result: the run's exit, and whether the session was
    stopped or was not the script's to stop. A pool that already has a session is neither booked
    nor stopped, and the run is made at once. A pool that cannot be booked gets no run: `None`."""
    # Here and not at the top: the script's paths that book nothing, and the run of the tests that
    # loads this file as its plugin, need no import path.
    import booking
    from rhi_common.records import RecordError
    from rhi_common.sessions import SessionError

    minutes = 2 * check.wait_minutes
    try:
        table = resource.Table(booking.table_name(cloudformation, check.stack, check.logical_id))
        session = booking.book(
            table,
            MOCK_POOL,
            minutes=minutes,
            set_by=BOOKED_BY,
            now=int(clock()),
            session_max_hours=check.session_max_hours,
        )
    except SessionError:
        problem = (
            f"a session of {minutes} minutes, twice {CHECK_WAIT_MINUTES_VARIABLE}, is longer than a session may last"
        )
        print(f"{NAME}: the pool {MOCK_POOL} was not booked: {problem}", file=sys.stderr)
        return None, False
    except (BotoCoreError, ClientError, KeyError, RecordError) as error:
        print(f"{NAME}: the pool {MOCK_POOL} was not booked ({type(error).__name__}): no test was run", file=sys.stderr)
        return None, False
    if session is None:
        print(f"{NAME}: the pool {MOCK_POOL} already has a session: it is neither booked nor stopped")
        return run(), True
    # Flushed: the wait that follows may be long, and the log says what the step waits for.
    print(f"{NAME}: booked the pool {MOCK_POOL} for {minutes} minutes", flush=True)
    try:
        until = session.set_at + check.wait_minutes * MINUTE_SECONDS
        if wait_until_ready(status, until=until, sleep=sleep, clock=clock):
            print(f"{NAME}: the pool {MOCK_POOL} reads ready")
        else:
            print(f"{NAME}: the pool {MOCK_POOL} did not read ready in {check.wait_minutes} minutes: the tests run")
        exit_code = run()
    finally:
        try:
            ended = booking.stop(table, session, now=int(clock()), session_max_hours=check.session_max_hours)
        except (BotoCoreError, ClientError, RecordError) as error:
            stopped = False
            reason = f"({type(error).__name__}): it ends by itself"
            print(f"{NAME}: the session on the pool {MOCK_POOL} was not stopped {reason}", file=sys.stderr)
        else:
            stopped = True
            if ended is None:
                print(
                    f"{NAME}: the session on the pool {MOCK_POOL} no longer reads as the booking: it is left as it is"
                )
            else:
                print(f"{NAME}: stopped the session it booked on the pool {MOCK_POOL}")
    return exit_code, stopped


def main(
    environ: Mapping[str, str] | None = None,
    client: Any = None,
    run: Callable[[Mapping[str, str]], int] = run_tests,
    dynamodb: Any = None,
    *,
    cloudformation: Any = None,
    resource: Any = None,
    status: Callable[[float], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    """Verifies the deployment the environment names; 0 when every test passed or none could run.
    `client` is a Secrets Manager client of the AWS SDK, `dynamodb` a DynamoDB one, `cloudformation`
    a CloudFormation one and `resource` a DynamoDB resource; without one, the build's own
    credentials and region make it. `run` runs the smoke tests in the environment it is given: once
    on the mock pool, in a session the script books and stops, then once on each pool the decision
    left. `status` reads the mock pool's state in the seconds it is given, from the endpoint's
    `GET /status` when left out;
    `sleep` waits that many seconds and `clock` gives the time, in seconds since the epoch. When a
    run fails, the last stage each instance of the tested pools reached is left for the step's
    post-build phase."""
    env = os.environ if environ is None else environ
    endpoint = env.get(ENDPOINT_VARIABLE)
    if not endpoint:
        problem = f"{ENDPOINT_VARIABLE} is not set: it holds the endpoint's address, or {NO_ENDPOINT}"
        print(f"{NAME}: {problem}", file=sys.stderr)
        return 1
    if endpoint == NO_ENDPOINT:
        return not_run(LISTENER_OFF, env)
    if client is None:
        import boto3

        client = boto3.client("secretsmanager")
    key = canary_key(client)
    if key is None:
        return not_run(CANARY_MISSING, env)
    passes = gpu_pools(env)
    if passes is None:
        print(f"{NAME}: {GPU_POOLS_FILE_VARIABLE} names no file: the decision did not run", file=sys.stderr)
        return 1
    check = check_of(env)
    if check is None:
        print(
            f"{NAME}: {POOLS_STACK_VARIABLE} and {POOL_STATE_LOGICAL_ID_VARIABLE} name the pool-state table, and "
            f"{CHECK_WAIT_MINUTES_VARIABLE} and {SESSION_MAX_HOURS_VARIABLE} are whole numbers above zero",
            file=sys.stderr,
        )
        return 1
    pools = release_parameters(env.get(RELEASE_PARAMETERS_VARIABLE, ""))
    given = {**env, KEY_VARIABLE: key, SMOKE_RELEASE_PARAMETERS_VARIABLE: json.dumps(pools)}
    given.pop(POOL_VARIABLE, None)
    if cloudformation is None or resource is None:
        import boto3

        cloudformation = cloudformation or boto3.client("cloudformation")
        resource = resource or boto3.resource("dynamodb")
    on_mock, in_order = run_in_a_session(
        check,
        lambda: run(given),
        cloudformation=cloudformation,
        resource=resource,
        status=status or status_reader(endpoint, key),
        sleep=sleep,
        clock=clock,
    )
    if on_mock is None:
        return 1
    exits = [on_mock] + [run({**given, POOL_VARIABLE: pool}) for pool in passes]
    exit_code = next((code for code in exits if code != 0), 0)
    if exit_code != 0:
        leave_last_stages(env, pools, dynamodb)
    # A session the script booked and could not stop fails a verification whose tests passed.
    return exit_code if exit_code != 0 or in_order else 1


if __name__ == "__main__":
    sys.exit(main())
