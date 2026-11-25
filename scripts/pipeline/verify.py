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
on the coursework pool. A run that names a pool leaves none out but the test of the unbooked pool,
below.

**The pool nobody has booked.** A request starts no machine, and a pool without a session answers
`no_session` (section 5.3). The tests of `UNBOOKED_ONLY`, the one with the ID `no-session`, assert
that, and they can only be made on a pool that has no session. So the script runs them once, alone,
on the mock pool, when it finds that pool with no session booked or active and before it books it:
a run of its own, which `RHI_VERIFY_UNBOOKED_RUN` marks for the plugin below, and whose exit counts
like any other run's. Every other run, the mock pool's in its session and each pool's of the
decision's file, leaves those tests out: its pool is booked. Only the script marks that run: the
variable is taken out of the environment the step gives before any run is made.

**A verification without that test does not pass.** The script reads the mock pool's session once,
and that reading decides: with no session, the test runs and the script then books the pool; with
a session booked or active, someone else's, the script neither books nor stops it, as below, and
cannot make the pool unbooked. It never books the mock pool without having run that test first,
also when the session it read ends a moment later. On a mock pool that has a session, the tests of
the pool's session run on the instances that are there, as do those of the decision's pools, the
test of the pool nobody has booked is not run, the script says so in a line of its own, and it
exits 1 whatever the other tests gave: the verification is not complete, and the stage is to be
retried once the mock pool is free.

**The session on a pool.** Before each of its runs of the tests, the script books the pool the run
is on through `booking.py`: a session of one instance, under the name `BOOKED_BY`, for twice
`RHI_VERIFY_CHECK_WAIT_MINUTES`, the wait below and as long again for the tests, so that a step
that dies leaves a session that ends by itself. It finds the stage's pool-state table by its
logical ID in the pools stack, as the deployment hold does. It then reads the gateway's
`GET /status`, with the canary's key, every `POLL_SECONDS`, until the pool reads `ready` or
`RHI_VERIFY_CHECK_WAIT_MINUTES` have passed since the booking. On the mock pool the tests run in
either case; on a pool of the decision's file, see below. No reading starts once the wait is over,
and each is given no longer than what is left of it. Whatever became of the run, the script then
stops the session, only while the session still reads as the booking it made. A pool that already
has a session booked or active is neither booked nor stopped: the tests run at once, on the
instances that are there, and on the mock pool the verification is then not complete, above. A
mock pool whose session cannot be read fails the script before any
test, and one that cannot be booked, or whose booking is longer than
`RHI_VERIFY_SESSION_MAX_HOURS` allows, before any test of its session; a pool of the decision's file
that cannot be booked gets no run and fails the script. A session that could not be stopped fails a
script whose tests passed: it ends by itself. The script says which of these happened in a line of
its own, never in a message of AWS, and does not check that the instance leaves once the session is
stopped.

**The coursework pool.** Before this script, the step runs the decision of `gpu_verification.py`,
which compares each GPU pool's release parameter with its last version from before the execution
and leaves, in the file `RHI_VERIFY_GPU_POOLS_FILE` names, the pools the smoke tests must also run
on: `coursework` when this execution changed a GPU pool's engine image, model revision or engine
flags, nothing otherwise. After the run on the mock pool,
and once that pool's session is stopped, the script books each pool of that file in turn, as it
booked the mock pool, and runs the same smoke tests on it, with `RHI_SMOKE_POOL` naming it: the
streams of each format, the pool's metrics and the release its lifecycle agents report as a metric
and in their heartbeat rows, `gpu-release-served` and `release-heartbeat` among them. Every run is
made, and the script's exit is that of the first that failed. With the variable set and no file,
the decision did not run, and the script fails before any test.

**No capacity is not a failed release.** A GPU that AWS does not have yet says nothing about the
release (`docs/design/evidence.md`, E32). So when the wait for a pool of the decision's file ends
and the pool does not read ready, the script asks whether an instance was launched at all: the
lifecycle agent writes its instance's heartbeat row from the first stage of a launch, so the script
queries the pool's instance rows of the table `RHI_VERIFY_HEARTBEATS_TABLE` names for those written
at the booking's time or after it, `written_at`. It reads every page of the answer, with a strongly
consistent read, so that a row written as the wait ended is seen. Three cases:

- the pool reads ready: the tests run, and their result stands;
- the pool does not read ready and no instance was launched since the booking. No test runs on
  that pool, its session is stopped, and that pool adds nothing to the script's exit, which is that
  of its other runs. Whatever that exit is, the script leaves the one line `no-capacity` in the
  file `RHI_VERIFY_GPU_REASON_FILE` names;
- the pool does not read ready and a row was written since the booking: an instance was launched
  and does not serve. The tests run and fail, and the last stages are left, below.

**What says that no instance was launched.** A row lasts `ROW_LASTS_SECONDS` after its last
writing, and a wait may be longer. So the script also asks through the wait, every
`LAUNCH_POLL_SECONDS` while the pool does not read ready, and keeps three things apart
(`Launches`): a query that found a row, which is a launch, and stays one after the row expired; a
query that found none, which says that no instance was launched up to its time, when the answer
before it is recent enough that no row could have come and gone between the two; and a query that
could not read the table, which says nothing, neither way. No instance was launched only when the
query at the end of the wait was answered, no query found a row, and the answers left no such gap
since the booking: with a wait shorter than a row lasts, the last answer alone is enough, whatever
the queries before it met. Otherwise, without the table's name among it, the script cannot tell,
says so in a line of its own, and runs the tests: a launched instance is never reported as a lack
of capacity. Without `RHI_VERIFY_GPU_REASON_FILE`, the reason cannot be left, and the script fails.

The reason is left whatever the script's exit: it says what this run did on the pool, and the
step's post-build phase copies it from a build that fails as from one that passes
(`src/rhi/pipeline/verify.py`). Which run's word counts for a success is the reporter's affair: a
retry of the stage keeps the execution's ID, so it takes the mark only of the run that succeeded
(`rhi_reporter.handler`).

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
else of a row, neither the instance's ID nor its release. It asks DynamoDB for that attribute and
the row's key, by which it tells one instance from another and which it writes nowhere.
A launch names a stage of `rhi_lifecycle.stages` there, and an instance in service the serving
module's own name. An instance the script saw during its wait for a pool, launched since the
booking, and that has no row left by then keeps the stage it was last seen at: a row may expire
before a long wait and the tests are over, and the instance that replaced it has a row of its own.
When the table cannot be read, which is a line in the log, the stages are those seen during the
waits. No stage, or no table named, leaves no file: the exit stays the tests'. The post-build phase
copies the file to where the reporter reads it, and the reporter names only the stages of its own
list.

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
| `RHI_VERIFY_GPU_REASON_FILE` | The file the reason the GPU tests did not run is left in |
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
from dataclasses import dataclass, field
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
GPU_REASON_FILE_VARIABLE = "RHI_VERIFY_GPU_REASON_FILE"
POOL_VARIABLE = "RHI_SMOKE_POOL"
POOLS_STACK_VARIABLE = "RHI_VERIFY_POOLS_STACK"
POOL_STATE_LOGICAL_ID_VARIABLE = "RHI_VERIFY_POOL_STATE_LOGICAL_ID"
CHECK_WAIT_MINUTES_VARIABLE = "RHI_VERIFY_CHECK_WAIT_MINUTES"
SESSION_MAX_HOURS_VARIABLE = "RHI_VERIFY_SESSION_MAX_HOURS"

# The pool the run that names no pool tests (`tests/smoke/conftest.py`), and the name the script
# books a pool under, that one and each pool of the decision's file.
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
# before an instance's ID in the sort key, the attribute that holds the last stage the instance
# reached, and the one that holds when the row was written, in seconds since the epoch
# (`rhi_common.tables`, `rhi_common.records`; the script imports neither).
POOL_ATTRIBUTE = "pool"
SOURCE_ATTRIBUTE = "source"
INSTANCE_SOURCE = "instance#"
STAGE_ATTRIBUTE = "stage"
WRITTEN_AT_ATTRIBUTE = "written_at"
# At most this many pages of a pool's rows are read for the instances' last stages.
ROW_PAGES = 10
# How long a row lasts after its last writing (`rhi_common.records.EXPIRY_SECONDS`), and between
# two queries for a launch while the script waits for a pool of the decision's file: far less, so
# a row written during the wait is seen before it expires.
ROW_LASTS_SECONDS = 3600
LAUNCH_POLL_SECONDS = 60.0

# What the endpoint's output reads while there is no HTTPS listener (`rhi.gateway.stack`), and the
# pools' output while there is no pool (`rhi.pools.stack`).
NO_ENDPOINT = "none"
NO_POOL = "none"

# The reasons no test ran: the fixed vocabulary the reporter reads.
LISTENER_OFF = "listener-off"
CANARY_MISSING = "canary-missing"
# The reason the tests of a pool of the decision's file did not run: no instance was launched
# during the wait. It is the one line of a file of its own, beside the reasons above.
NO_CAPACITY = "no-capacity"

# The tests that only the run on a pool the decision named makes, by their IDs
# (`tests/smoke/ids.py`): a run that names no pool, the mock pool's, leaves them out, and they are
# the only tests the verification leaves out.
GPU_PASS_ONLY: tuple[str, ...] = ("gpu-release-served",)
# The tests that can only be made on a pool nobody has booked, by their IDs: the script runs them
# alone, on the mock pool before it books it, and every other run leaves them out.
UNBOOKED_ONLY: tuple[str, ...] = ("no-session",)
# Set, to `UNBOOKED_RUN`, in the environment of that run alone: it tells the plugin which run it is in.
UNBOOKED_VARIABLE = "RHI_VERIFY_UNBOOKED_RUN"
UNBOOKED_RUN = "1"
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
    """As a plugin of the run of the smoke tests: in the run on the unbooked mock pool, keeps the
    tests whose ID is one of `UNBOOKED_ONLY` and deselects every other; in any other run, deselects
    those, and in one that names no pool the tests whose ID is one of `GPU_PASS_ONLY` too; keeps
    the IDs of the tests left for `pytest_runtest_logreport`."""

    def carries(item: pytest.Item, ids: tuple[str, ...]) -> bool:
        return any(set(marker.args) & set(ids) for marker in item.iter_markers(ID_MARKER))

    if os.environ.get(UNBOOKED_VARIABLE):
        left_out = [item for item in items if not carries(item, UNBOOKED_ONLY)]
    else:
        deselected = UNBOOKED_ONLY + (() if os.environ.get(POOL_VARIABLE) else GPU_PASS_ONLY)
        left_out = [item for item in items if carries(item, deselected)]
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


def _stage(item: Mapping[str, Any]) -> str | None:
    """The stage a heartbeat row holds, `None` when it is not one line of text."""
    stage = item.get(STAGE_ATTRIBUTE, {}).get("S")
    return stage if isinstance(stage, str) and stage.splitlines() == [stage] else None


def _by_instance(items: Iterable[Mapping[str, Any]]) -> dict[str, str | None]:
    """The stage each row of `items` holds, by the row's sort key, which names its instance."""
    return {item.get(SOURCE_ATTRIBUTE, {}).get("S", ""): _stage(item) for item in items}


def last_stages(client: Any, table: str, pool: str) -> dict[str, str]:
    """The last lifecycle stage each instance of `pool` reached, by the instance's sort key: the
    `stage` of each instance's row of the heartbeats table `table`, read by pool with `client`, a
    DynamoDB client of the AWS SDK. Only the key and that attribute are asked for, and a row whose
    stage is not one line of text gives none."""
    found: dict[str, str] = {}
    request: dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": "#pool = :pool AND begins_with(#source, :instance)",
        "ProjectionExpression": "#source, #stage",
        "ExpressionAttributeNames": {"#pool": POOL_ATTRIBUTE, "#source": SOURCE_ATTRIBUTE, "#stage": STAGE_ATTRIBUTE},
        "ExpressionAttributeValues": {":pool": {"S": pool}, ":instance": {"S": INSTANCE_SOURCE}},
    }
    for _ in range(ROW_PAGES):
        answer = client.query(**request)
        rows = _by_instance(answer.get("Items", []))
        found |= {instance: stage for instance, stage in rows.items() if stage is not None}
        if "LastEvaluatedKey" not in answer:
            break
        request["ExclusiveStartKey"] = answer["LastEvaluatedKey"]
    return found


def rows_since(client: Any, table: str, pool: str, since: int) -> dict[str, str | None]:
    """The instances of `pool` that wrote their heartbeat row at the time `since` or after it, in
    seconds since the epoch: for each such row of the heartbeats table `table`, by its sort key,
    its stage, or `None` when it is not one line of text. Read with `client`, a DynamoDB client of
    the AWS SDK, in a strongly consistent read, so that a row written a moment ago is among them,
    and every page of the pool's instance rows: an empty answer says that the table holds no such
    row."""
    request: dict[str, Any] = {
        "TableName": table,
        "ConsistentRead": True,
        "KeyConditionExpression": "#pool = :pool AND begins_with(#source, :instance)",
        "FilterExpression": "#written >= :since",
        "ProjectionExpression": "#source, #stage",
        "ExpressionAttributeNames": {
            "#pool": POOL_ATTRIBUTE,
            "#source": SOURCE_ATTRIBUTE,
            "#written": WRITTEN_AT_ATTRIBUTE,
            "#stage": STAGE_ATTRIBUTE,
        },
        "ExpressionAttributeValues": {
            ":pool": {"S": pool},
            ":instance": {"S": INSTANCE_SOURCE},
            ":since": {"N": str(since)},
        },
    }
    rows: dict[str, str | None] = {}
    while True:
        answer = client.query(**request)
        rows |= _by_instance(answer.get("Items", []))
        if "LastEvaluatedKey" not in answer:
            return rows
        request["ExclusiveStartKey"] = answer["LastEvaluatedKey"]


def launched_rows(env: Mapping[str, str], pool: str, since: int, client: Any = None) -> dict[str, str | None] | None:
    """The rows of the instances of `pool` launched since the time `since`, a booking's, as the
    heartbeats table `RHI_VERIFY_HEARTBEATS_TABLE` names holds them now (`rows_since`). `None`
    when the script cannot tell, without the table's name or when the table cannot be read, after
    a line in the log: that is neither a launch nor the lack of one."""
    table = env.get(HEARTBEATS_TABLE_VARIABLE)
    if not table:
        problem = f"{HEARTBEATS_TABLE_VARIABLE} is not set"
        print(f"{NAME}: whether an instance of the pool {pool} was launched is not known: {problem}", file=sys.stderr)
        return None
    try:
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        return rows_since(client, table, pool, since)
    except (BotoCoreError, ClientError) as error:
        problem = f"its rows were not read ({type(error).__name__})"
        print(f"{NAME}: whether an instance of the pool {pool} was launched is not known: {problem}", file=sys.stderr)
        return None


def leave_last_stages(
    env: Mapping[str, str],
    pools: Iterable[str],
    client: Any = None,
    seen: Mapping[str, Mapping[str, str]] | None = None,
) -> None:
    """After a run of the tests that failed: leaves the last stage each instance of `pools` reached,
    one per line and nothing else, in the file `RHI_VERIFY_LAST_STAGES_FILE` names, for the step's
    post-build phase. `seen` holds, by pool then by instance, the stage the script last saw during
    its wait for the pool: it stands for an instance that has no row now, and for every instance
    when the table cannot be read, which is a line in the log. Without the table's name, the file's
    or a stage, it leaves no file: the tests' exit is the script's."""
    table, named = env.get(HEARTBEATS_TABLE_VARIABLE), env.get(LAST_STAGES_FILE_VARIABLE)
    pools, seen = list(pools), seen or {}
    if not table or not named or not pools:
        return
    try:
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        # An instance's row of now stands before what was seen of it during the wait.
        by_pool = [{**seen.get(pool, {}), **last_stages(client, table, pool)} for pool in pools]
    except (BotoCoreError, ClientError) as error:
        print(f"{NAME}: the instances' last stages were not read ({type(error).__name__})", file=sys.stderr)
        by_pool = [seen.get(pool, {}) for pool in pools]
    stages = [stage for by_instance in by_pool for stage in by_instance.values()]
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


def gpu_not_run(reason: str, env: Mapping[str, str]) -> int:
    """Leaves the reason the tests of a pool of the decision's file did not run, as the one line of
    the file `RHI_VERIFY_GPU_REASON_FILE` names, for the step's post-build phase; 0 once it is
    written. The other runs' tests stand: this is not the file of a verification that ran no test."""
    named = env.get(GPU_REASON_FILE_VARIABLE)
    if not named:
        problem = f"{GPU_REASON_FILE_VARIABLE} is not set: it names the file to leave the reason in"
        print(f"{NAME}: {problem}", file=sys.stderr)
        return 1
    path = Path(named)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reason + "\n", encoding="utf-8")
    print(f"{NAME}: the GPU tests were not run: {reason}")
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
    """What the step gives for the session on a pool: the pools stack and the pool-state table's
    logical ID in it, the minutes the script waits for the session's instance, and the longest a
    session lasts, in hours."""

    stack: str
    logical_id: str
    wait_minutes: int
    session_max_hours: int


def _whole(text: str | None) -> int | None:
    """The whole number above zero the variable holds, `None` when it is missing or holds anything else."""
    return int(text) if text is not None and text.isascii() and text.isdigit() and int(text) > 0 else None


def check_of(env: Mapping[str, str]) -> Check | None:
    """What the environment gives for the session on a pool, `None` when a variable is missing or
    a number is not a whole number above zero."""
    stack, logical_id = env.get(POOLS_STACK_VARIABLE), env.get(POOL_STATE_LOGICAL_ID_VARIABLE)
    wait_minutes, hours = _whole(env.get(CHECK_WAIT_MINUTES_VARIABLE)), _whole(env.get(SESSION_MAX_HOURS_VARIABLE))
    if not stack or not logical_id or wait_minutes is None or hours is None:
        return None
    return Check(stack=stack, logical_id=logical_id, wait_minutes=wait_minutes, session_max_hours=hours)


def status_reader(
    endpoint: str, key: str, transport: httpx.BaseTransport | None = None
) -> Callable[[str, float], str | None]:
    """What reads a pool's state from `GET /status` of `endpoint`, with the canary's key, through
    `transport` when one is given: None for a reading that failed. A reading is given the pool's
    name and the seconds it may take, and `READ_SECONDS` when that is more: one that has no answer
    by then is given up, and reads as None. Nothing of the machine's own configuration, a proxy
    among it, takes part in a reading."""
    import httpx

    address = (endpoint if "://" in endpoint else f"https://{endpoint}").rstrip("/")

    def read(pool: str, within: float) -> str | None:
        # A time-out of httpx is for each step of a request, the connection then each wait for the
        # answer, not for the whole of it: the request is made aside, and left when its seconds
        # are over. Its own time-outs end it soon after.
        seconds = min(READ_SECONDS, within)
        states: list[str | None] = []
        request = threading.Thread(target=lambda: states.append(answered(pool, seconds)), daemon=True)
        request.start()
        request.join(seconds)
        return states[0] if states else None

    def answered(pool: str, seconds: float) -> str | None:
        try:
            with httpx.Client(timeout=seconds, trust_env=False, transport=transport) as http:
                answer = http.get(f"{address}/status", headers={"Authorization": f"Bearer {key}"})
            states = {record["model"]: record["state"] for record in answer.json()["pools"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        state = states.get(pool) if answer.status_code == 200 else None
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


class Launches:
    """What the heartbeats table said, through the wait for a pool, of the instances launched since
    the time `since`, a booking's. `rows` is given `since` and answers with the stage of each row
    written since then, by its instance, as the table holds them at that moment, or with `None`
    when it cannot tell; `clock` gives the time. Three answers are kept apart: a row, which is a
    launch, and stays one after the row expired; no row, which says that no instance was launched
    up to then, unless the answer before it is so old that a row may have come and gone between
    the two; and `None`, which says nothing."""

    def __init__(
        self, rows: Callable[[int], Mapping[str, str | None] | None], since: int, clock: Callable[[], float]
    ) -> None:
        self._rows, self._since, self._clock = rows, since, clock
        self._launched = False
        # The time up to which the answers tell every launch since the booking, while no answer
        # came too long after the one before it.
        self._told_until: float = since
        self._gap = False
        self._asked_at: float | None = None
        # The stage each instance was last seen at, by instance: one whose row expired, and that
        # another replaced, keeps its own.
        self.stages: dict[str, str] = {}

    def _ask(self) -> bool:
        """Asks; whether there was an answer."""
        now = self._asked_at = self._clock()
        rows = self._rows(self._since)
        if rows is None:
            return False
        # A row lasts `ROW_LASTS_SECONDS` after its writing, by the instance's clock and the
        # table's: one poll less is taken, for this clock is not theirs.
        if now - self._told_until > ROW_LASTS_SECONDS - LAUNCH_POLL_SECONDS:
            self._gap = True
        self._told_until = now
        if rows:
            self._launched = True
            self.stages |= {instance: stage for instance, stage in rows.items() if stage is not None}
        return True

    def look(self) -> None:
        """During the wait: asks, unless the last asking is less than `LAUNCH_POLL_SECONDS` old."""
        if self._asked_at is None or self._clock() - self._asked_at >= LAUNCH_POLL_SECONDS:
            self._ask()

    def none(self) -> bool:
        """Once the wait is over: asks once more; whether no instance was launched since the
        booking. True only when that last asking was answered, no answer held a row, and no
        answer came too long after the one before it."""
        return self._ask() and not self._launched and not self._gap

    @property
    def launched(self) -> bool:
        """Whether an answer held a row: an instance was launched since the booking."""
        return self._launched


@dataclass(frozen=True)
class Outcome:
    """What became of a pool's run of the tests: the run's exit, `None` when no run was made;
    whether the session was stopped or was not the script's to stop; whether no run was made
    because no instance was launched during the wait, and not because the pool was not booked;
    the stage the script last saw, during the wait, of each instance launched in it, by
    instance; the seconds the script waited for the instance of the session it booked, from the
    booking to the reading of ready, or to the wait's end when none came, and `None` when it
    booked nothing; whether that wait ended on a pool that did not read ready although an
    instance was launched since the booking: one that was launched and did not serve; and the exit
    code of the run made on the pool before it was booked, `None` when that run was not made."""

    exit_code: int | None
    in_order: bool
    no_capacity: bool = False
    stages: Mapping[str, str] = field(default_factory=dict)
    waited: float | None = None
    unserved: bool = False
    unbooked_exit: int | None = None


def run_in_a_session(
    check: Check,
    pool: str,
    run: Callable[[], int],
    *,
    cloudformation: Any,
    resource: Any,
    status: Callable[[float], str | None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    launched: Callable[[int], Mapping[str, str | None] | None] | None = None,
    set_by: str = BOOKED_BY,
    unbooked: Callable[[], int] | None = None,
) -> Outcome:
    """Makes `run`, the run of the tests on `pool`, in a session the script books on that pool,
    under the name `set_by`, the verification's unless another check gives its own, and
    stops after the run, whatever its result, or after its wait when no run is made. A pool that
    already has a session is neither booked nor stopped, and the run is made at once. A pool that
    cannot be booked gets no run. `status` reads the pool's state in the seconds it is given. With
    `launched`, which gives the rows of the instances of the pool launched since the time it is
    given, or `None` when it cannot tell: it is asked through the wait, while the pool does not
    read ready, and once more when the wait ends; when the pool does not read ready by then and its
    answers say that no instance was launched since the booking (`Launches`), no run is made, for
    lack of capacity. Without it, the run is made when the wait ends. With `unbooked`, the run of
    what can only be tested on a pool nobody has booked: one reading of the pool's session decides.
    When the pool has no session booked or active, that run is made first, and the pool is booked
    after it. When it has one, that run is not made and the pool is not booked, also when that
    session ends a moment later: `run` is made at once, and `Outcome.unbooked_exit` stays `None`."""
    # Here and not at the top: the script's paths that book nothing, and the run of the tests that
    # loads this file as its plugin, need no import path.
    import booking
    from rhi_common.records import RecordError
    from rhi_common.sessions import SessionError

    minutes = 2 * check.wait_minutes
    unbooked_exit = None
    try:
        table = resource.Table(booking.table_name(cloudformation, check.stack, check.logical_id))
        # The one reading that decides whether the pool is tested as one nobody has booked.
        occupied = unbooked is not None and booking.booked(
            table, pool, now=int(clock()), session_max_hours=check.session_max_hours
        )
    except (BotoCoreError, ClientError, KeyError, RecordError) as error:
        print(f"{NAME}: the pool {pool} was not booked ({type(error).__name__}): no test was run", file=sys.stderr)
        return Outcome(None, in_order=False)
    if unbooked is not None and not occupied:
        print(f"{NAME}: the pool {pool} has no session: the test of a pool nobody has booked runs first", flush=True)
        unbooked_exit = unbooked()
    try:
        # A pool read as occupied is not booked, whatever became of its session since: the script
        # books no pool it was to test unbooked without having run that test.
        session = (
            None
            if occupied
            else booking.book(
                table,
                pool,
                minutes=minutes,
                set_by=set_by,
                now=int(clock()),
                session_max_hours=check.session_max_hours,
            )
        )
    except SessionError:
        problem = (
            f"a session of {minutes} minutes, twice {CHECK_WAIT_MINUTES_VARIABLE}, is longer than a session may last"
        )
        print(f"{NAME}: the pool {pool} was not booked: {problem}", file=sys.stderr)
        return Outcome(None, in_order=False, unbooked_exit=unbooked_exit)
    except (BotoCoreError, ClientError, KeyError, RecordError) as error:
        print(f"{NAME}: the pool {pool} was not booked ({type(error).__name__}): no test was run", file=sys.stderr)
        return Outcome(None, in_order=False, unbooked_exit=unbooked_exit)
    if session is None:
        print(f"{NAME}: the pool {pool} already has a session: it is neither booked nor stopped")
        if occupied:
            # Someone's session: it stays, and so the pool cannot be tested as one nobody has booked.
            print(f"{NAME}: the test of a pool nobody has booked is not run: the pool {pool} has a session")
        return Outcome(run(), in_order=True, unbooked_exit=unbooked_exit)
    # Flushed: the wait that follows may be long, and the log says what the step waits for.
    print(f"{NAME}: booked the pool {pool} for {minutes} minutes", flush=True)
    exit_code, no_capacity, unserved, took = None, False, False, None
    launches = None if launched is None else Launches(launched, session.set_at, clock)

    def read(within: float) -> str | None:
        """The pool's state, and a look for a launch while it does not read ready."""
        state = status(within)
        if state != READY and launches is not None:
            launches.look()
        return state

    try:
        until = session.set_at + check.wait_minutes * MINUTE_SECONDS
        ready = wait_until_ready(read, until=until, sleep=sleep, clock=clock)
        took = max(0.0, clock() - session.set_at)
        if ready:
            print(f"{NAME}: the pool {pool} reads ready")
            exit_code = run()
        elif launches is not None and launches.none():
            # Only here: the table's own answers say that no instance was launched since the booking.
            no_capacity = True
            waited = f"no instance of the pool {pool} was launched in {check.wait_minutes} minutes"
            print(f"{NAME}: {waited}: its tests are not run, for lack of capacity")
        else:
            # A row seen since the booking: an instance was launched, and the wait ended before it served.
            unserved = launches is not None and launches.launched
            print(f"{NAME}: the pool {pool} did not read ready in {check.wait_minutes} minutes: the tests run")
            exit_code = run()
    finally:
        try:
            ended = booking.stop(table, session, now=int(clock()), session_max_hours=check.session_max_hours)
        except (BotoCoreError, ClientError, RecordError) as error:
            stopped = False
            reason = f"({type(error).__name__}): it ends by itself"
            print(f"{NAME}: the session on the pool {pool} was not stopped {reason}", file=sys.stderr)
        else:
            stopped = True
            if ended is None:
                print(f"{NAME}: the session on the pool {pool} no longer reads as the booking: it is left as it is")
            else:
                print(f"{NAME}: stopped the session it booked on the pool {pool}")
    stages = {} if launches is None else launches.stages
    return Outcome(
        exit_code,
        in_order=stopped,
        no_capacity=no_capacity,
        stages=stages,
        waited=took,
        unserved=unserved,
        unbooked_exit=unbooked_exit,
    )


def main(
    environ: Mapping[str, str] | None = None,
    client: Any = None,
    run: Callable[[Mapping[str, str]], int] = run_tests,
    dynamodb: Any = None,
    *,
    cloudformation: Any = None,
    resource: Any = None,
    status: Callable[[str, float], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    """Verifies the deployment the environment names; 0 when every test that ran passed and the
    test of a pool nobody has booked was among them, or when none could run. `client` is a Secrets
    Manager client of the AWS SDK, `dynamodb` a DynamoDB one, `cloudformation` a CloudFormation one
    and `resource` a DynamoDB resource; without one, the build's own credentials and region make
    it. `run` runs the smoke tests in the environment it is given: once on the mock pool before it
    is booked, marked by `UNBOOKED_VARIABLE`, when that pool has no session; then once on the mock
    pool and once on each pool the decision left, each in a session the script books and stops. On
    a mock pool that has a session that first run is not made, and the exit is 1 whatever the
    others gave. A pool the decision left on which no instance was launched during the wait gets no
    run, the reason is left for the step's post-build phase, and the exit is that of the other
    runs. `status` reads the state of the pool it is given in the seconds it is given, from the
    endpoint's `GET /status` when left out; `sleep` waits that many seconds and `clock` gives the
    time, in seconds since the epoch. When a run fails, the last stage each instance of the tested
    pools reached is left for the step's post-build phase."""
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
    # The script alone says which pool a run is on, and which run is the unbooked pool's.
    given.pop(POOL_VARIABLE, None)
    given.pop(UNBOOKED_VARIABLE, None)
    if cloudformation is None or resource is None:
        import boto3

        cloudformation = cloudformation or boto3.client("cloudformation")
        resource = resource or boto3.resource("dynamodb")
    read = status or status_reader(endpoint, key)

    def in_a_session(
        pool: str, environ: Mapping[str, str], *, told_of_launches: bool, unbooked_first: bool = False
    ) -> Outcome:
        """The run of the tests on `pool`, in `environ`, in a session booked for it; with
        `unbooked_first`, after the run of the test of a pool nobody has booked, if the pool is one."""
        return run_in_a_session(
            check,
            pool,
            lambda: run(environ),
            cloudformation=cloudformation,
            resource=resource,
            status=lambda within: read(pool, within),
            sleep=sleep,
            clock=clock,
            launched=(lambda since: launched_rows(env, pool, since, dynamodb)) if told_of_launches else None,
            unbooked=(lambda: run({**environ, UNBOOKED_VARIABLE: UNBOOKED_RUN})) if unbooked_first else None,
        )

    on_mock = in_a_session(MOCK_POOL, given, told_of_launches=False, unbooked_first=True)
    if on_mock.exit_code is None:
        return 1
    # The run on the unbooked mock pool counts first, when it was made.
    exits = [code for code in (on_mock.unbooked_exit, on_mock.exit_code) if code is not None]
    in_order, no_capacity = on_mock.in_order, False
    seen: dict[str, Mapping[str, str]] = {}
    for pool in passes:
        on_pool = in_a_session(pool, {**given, POOL_VARIABLE: pool}, told_of_launches=True)
        in_order = in_order and on_pool.in_order
        seen[pool] = on_pool.stages
        if on_pool.no_capacity:
            # No instance was launched: the pool adds nothing to the exit.
            no_capacity = True
        else:
            # A pool that could not be booked got no run: that fails the script.
            exits.append(1 if on_pool.exit_code is None else on_pool.exit_code)
    exit_code = next((code for code in exits if code != 0), 0)
    if on_mock.unbooked_exit is None:
        # The mock pool had a session: what a pool nobody has booked answers was not tested.
        problem = "the test of a pool nobody has booked was not run"
        print(f"{NAME}: the verification is not complete: {problem}", file=sys.stderr)
    if exit_code != 0:
        leave_last_stages(env, pools, dynamodb, seen)
    elif not in_order or on_mock.unbooked_exit is None:
        # A session the script booked and could not stop fails a verification whose tests passed,
        # and so does one that could not make the test of the unbooked pool.
        exit_code = 1
    if no_capacity:
        # Left whatever the exit: a reason that cannot be left fails a script that passed.
        left = gpu_not_run(NO_CAPACITY, env)
        exit_code = exit_code or left
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
