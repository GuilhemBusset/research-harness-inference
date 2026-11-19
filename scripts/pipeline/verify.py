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

**The tests it leaves out.** The step runs outside the private network, and only the gateway may
reach the internal load balancer (section 6.5), so the tests that call the forbidden routes
through it cannot run from here. They are deselected by their IDs, `DESELECTED`, and no other
test is. How the verification reaches that load balancer is the owner's decision: once it is
made, those IDs leave this one tuple.

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

The pools' parameters come from a stack's output through the pipeline, which puts the value into
JSON as it is, so they arrive without a quote; the smoke tests read them as JSON, in
`RHI_SMOKE_RELEASE_PARAMETERS`, which the script writes for them.

Run by the verification step, from the checkout:

    uv run --locked python scripts/pipeline/verify.py

The file is also the pytest plugin of the run it starts (`-p verify`): `pytest_collection_modifyitems`,
below, is what deselects, and `pytest_runtest_logreport` what leaves the IDs of the failed tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

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

# The tests that call the forbidden routes through the internal load balancer, by their IDs
# (`tests/smoke/ids.py`): the only tests the verification leaves out.
DESELECTED: tuple[str, ...] = (
    "internal-invocations",
    "internal-pause",
    "internal-tokenize",
    "internal-metrics",
    "internal-health",
)
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
    """As a plugin of the run of the smoke tests: deselects the tests whose ID is one of `DESELECTED`,
    and keeps the IDs of the others for `pytest_runtest_logreport`."""
    left_out = [
        item for item in items if any(set(marker.args) & set(DESELECTED) for marker in item.iter_markers(ID_MARKER))
    ]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = [item for item in items if item not in left_out]
    for item in items:
        _ids[item.nodeid] = tuple(
            test_id for marker in item.iter_markers(ID_MARKER) for test_id in marker.args if isinstance(test_id, str)
        )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """As a plugin of the run of the smoke tests: when a test fails, in its set-up, its call or its
    teardown, adds its ID as a line of the file `RHI_VERIFY_FAILED_TESTS_FILE` names, once. Only
    the ID is written: nothing of what the test held or printed. Without the variable, nothing is."""
    named = os.environ.get(FAILED_TESTS_FILE_VARIABLE)
    failed = [test_id for test_id in _ids.get(report.nodeid, ()) if test_id not in _left] if report.failed else []
    if not named or not failed:
        return
    path = Path(named)
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


def main(
    environ: Mapping[str, str] | None = None,
    client: Any = None,
    run: Callable[[Mapping[str, str]], int] = run_tests,
    dynamodb: Any = None,
) -> int:
    """Verifies the deployment the environment names; 0 when every test passed or none could run.
    `client` is a Secrets Manager client of the AWS SDK and `dynamodb` a DynamoDB one; without one,
    the build's own credentials and region make it. `run` runs the smoke tests in the environment
    it is given. When that run fails, the last stage each instance of the tested pools reached is
    left for the step's post-build phase."""
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
    pools = release_parameters(env.get(RELEASE_PARAMETERS_VARIABLE, ""))
    exit_code = run({**env, KEY_VARIABLE: key, SMOKE_RELEASE_PARAMETERS_VARIABLE: json.dumps(pools)})
    if exit_code != 0:
        leave_last_stages(env, pools, dynamodb)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
