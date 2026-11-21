"""The verification script (scripts/pipeline/verify.py): with no listener or no canary key it runs
no test, exits 0 and leaves its reason; with a key it runs the smoke tests, all but those that
must reach the internal load balancer, a failed test is a non-zero exit and leaves its ID, and
what the tests print reaches the log without the key. After a failed run it leaves the last
lifecycle stage each instance of the tested pools reached, and nothing else of a heartbeat's row.
When the decision on the coursework pool left that pool, the smoke tests run again on it, with the
tests of that run only.

A stand-in for the Secrets Manager client replaces AWS, and moto holds the heartbeats table in
process: no account, no credentials, no network. The tests that pytest really collects and runs do
so in a process of their own, as the script starts one, and reach no stack.
"""

from __future__ import annotations

import json
import os
import runpy
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import verify
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat
from rhi_common.tables import HEARTBEATS

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

REPO = Path(__file__).resolve().parents[2]
SMOKE_TEST_IDS: tuple[str, ...] = runpy.run_path(str(REPO / "tests/smoke/ids.py"))["SMOKE_TEST_IDS"]
ENDPOINT = "smoke.example.com"


class Secrets:
    """A Secrets Manager client as far as the script uses one: the canary's secret holds `held`,
    does not exist when `held` is None, or cannot be read, with the error code `refused`."""

    def __init__(self, held: str | None, *, refused: str | None = None) -> None:
        self.held, self.refused, self.asked = held, refused, []

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        self.asked.append(request)
        code = self.refused or ("ResourceNotFoundException" if self.held is None else None)
        if code is not None:
            raise ClientError({"Error": {"Code": code, "Message": "refused"}}, "GetSecretValue")
        return {"Name": request["SecretId"], "SecretString": self.held}


class Run:
    """In place of the run of the smoke tests: records the environment it was given."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.environments: list[Mapping[str, str]] = []

    def __call__(self, environ: Mapping[str, str]) -> int:
        self.environments.append(environ)
        return self.exit_code


def a_key() -> str:
    """A key drawn now: none is written in the repository."""
    from rhi_common.keys import generate_key

    return generate_key()


@pytest.mark.parametrize("held", [None, "", " \n"], ids=["no secret", "an empty secret", "a blank secret"])
def test_with_no_canary_key_no_test_runs_and_the_reason_is_canary_missing(tmp_path: Path, held: str | None) -> None:
    reason, run, secrets = tmp_path / "build" / "not-run", Run(), Secrets(held)
    environ = {verify.ENDPOINT_VARIABLE: ENDPOINT, verify.REASON_FILE_VARIABLE: str(reason)}

    assert verify.main(environ, client=secrets, run=run) == 0

    assert run.environments == []
    assert reason.read_text(encoding="utf-8") == "canary-missing\n"
    assert secrets.asked == [{"SecretId": "rhi/canary-key"}]


def test_with_no_listener_no_test_runs_and_the_reason_is_listener_off(tmp_path: Path) -> None:
    reason, run, secrets = tmp_path / "not-run", Run(), Secrets(a_key())
    environ = {verify.ENDPOINT_VARIABLE: "none", verify.REASON_FILE_VARIABLE: str(reason)}

    assert verify.main(environ, client=secrets, run=run) == 0

    assert run.environments == []
    assert reason.read_text(encoding="utf-8") == "listener-off\n"
    # There is nothing to call: the key is not even read.
    assert secrets.asked == []


def test_a_secret_that_cannot_be_read_is_not_a_missing_canary(tmp_path: Path) -> None:
    reason, run = tmp_path / "not-run", Run()
    environ = {verify.ENDPOINT_VARIABLE: ENDPOINT, verify.REASON_FILE_VARIABLE: str(reason)}

    with pytest.raises(ClientError):
        verify.main(environ, client=Secrets(a_key(), refused="AccessDeniedException"), run=run)

    assert run.environments == [] and not reason.exists()


def test_no_endpoint_given_is_an_error(tmp_path: Path) -> None:
    run = Run()
    assert verify.main({verify.REASON_FILE_VARIABLE: str(tmp_path / "not-run")}, client=Secrets(a_key()), run=run) != 0
    assert run.environments == []


@pytest.mark.parametrize("exit_code", [0, 1, 5], ids=["every test passed", "a test failed", "no test was collected"])
def test_with_a_key_the_tests_run_with_it_and_their_exit_is_the_script_s(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], exit_code: int
) -> None:
    key, reason, run = a_key(), tmp_path / "not-run", Run(exit_code)
    environ = {
        verify.ENDPOINT_VARIABLE: ENDPOINT,
        verify.REASON_FILE_VARIABLE: str(reason),
        verify.RELEASE_PARAMETERS_VARIABLE: "mock=/rhi/pools/mock/release,other-1=/rhi/pools/other-1/release",
        "RHI_SMOKE_WORKSPACE_URL": "https://workspace.example.com/",
    }

    assert verify.main(environ, client=Secrets(f"{key}\n"), run=run) == exit_code

    (given,) = run.environments
    # What the smoke tests read: the endpoint and the workspace as given, the key, and the pools'
    # parameters as JSON.
    assert given[verify.ENDPOINT_VARIABLE] == ENDPOINT
    assert given["RHI_SMOKE_WORKSPACE_URL"] == "https://workspace.example.com/"
    assert given["RHI_SMOKE_KEY"] == key
    assert json.loads(given["RHI_SMOKE_RELEASE_PARAMETERS"]) == {
        "mock": "/rhi/pools/mock/release",
        "other-1": "/rhi/pools/other-1/release",
    }
    assert not reason.exists()
    assert key not in "".join(capsys.readouterr())


def coursework_pass(tmp_path: Path, left: str | None, exits: list[int]) -> tuple[int, list[Mapping[str, str]]]:
    """The script's exit after runs of the tests that end in `exits`, one after the other, once the
    decision left `left` as the pools to verify on, None for no file, and the environments the runs
    were given."""
    named, given = tmp_path / "gpu-pools", []
    if left is not None:
        named.write_text(left, encoding="utf-8")

    def run(environ: Mapping[str, str]) -> int:
        given.append(environ)
        return exits[len(given) - 1]

    environ = {
        verify.ENDPOINT_VARIABLE: ENDPOINT,
        verify.RELEASE_PARAMETERS_VARIABLE: "mock=/rhi/pools/mock/release,coursework=/rhi/pools/coursework/release",
        verify.GPU_POOLS_FILE_VARIABLE: str(named),
    }
    return verify.main(environ, client=Secrets(a_key()), run=run), given


def test_the_pools_the_decision_left_are_run_on_after_the_mock_pool(tmp_path: Path) -> None:
    exit_code, given = coursework_pass(tmp_path, "coursework\n", [0, 0])
    assert exit_code == 0
    # The mock pool's run names no pool; the coursework pool's names it, with the same key and pools.
    assert [environ.get(verify.POOL_VARIABLE) for environ in given] == [None, "coursework"]
    assert given[0][verify.KEY_VARIABLE] == given[1][verify.KEY_VARIABLE]
    # Every run is made, and the exit is the first failed one's.
    assert coursework_pass(tmp_path, "coursework\n", [0, 3])[0] == 3
    exit_code, given = coursework_pass(tmp_path, "coursework\n", [2, 0])
    assert exit_code == 2 and len(given) == 2


def test_no_pool_left_by_the_decision_is_the_mock_pool_s_run_alone(tmp_path: Path) -> None:
    exit_code, given = coursework_pass(tmp_path, "", [0])
    assert exit_code == 0 and [environ.get(verify.POOL_VARIABLE) for environ in given] == [None]


def test_a_decision_that_left_no_file_runs_no_test(tmp_path: Path) -> None:
    assert coursework_pass(tmp_path, None, []) == (1, [])


def test_no_pool_is_no_release_parameter() -> None:
    assert verify.release_parameters("none") == {}


TABLE = "heartbeats-under-test"


@pytest.fixture
def heartbeats() -> Iterator[Any]:
    """A DynamoDB client, in moto, of a region that holds the heartbeats table with the key schema
    of `rhi_common.tables.HEARTBEATS`, and no row."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        keys = [(HEARTBEATS.partition_key, "HASH"), (HEARTBEATS.sort_key, "RANGE")]
        client.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys if key is not None
            ],
            KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys if key is not None],
        )
        yield client


def instance_row(pool: str, stage: str) -> tuple[str, dict[str, Any]]:
    """An instance's heartbeat as its lifecycle agent writes it, with the stage it has reached: the
    instance's ID, drawn now, and the row's item."""
    instance = f"instance-{secrets.token_hex(8)}"
    release = {"image_digest": f"sha256:{secrets.token_hex(32)}", "model_revision": secrets.token_hex(20)}
    row = InstanceHeartbeat(instance=instance, pool=pool, running=0, waiting=0, stage=stage, written_at=1, **release)
    return instance, row.to_item()


def failed_run(tmp_path: Path, dynamodb: Any, exit_code: int = 1) -> tuple[int, str | None]:
    """The script's exit after a run of the tests of the pools `mock` and `gpu` that ends in
    `exit_code`, and what it left as the instances' last stages, None for no file."""
    left = tmp_path / "build" / "last-stages"
    environ = {
        verify.ENDPOINT_VARIABLE: ENDPOINT,
        verify.RELEASE_PARAMETERS_VARIABLE: "mock=/rhi/pools/mock/release,gpu=/rhi/pools/gpu/release",
        verify.HEARTBEATS_TABLE_VARIABLE: TABLE,
        verify.LAST_STAGES_FILE_VARIABLE: str(left),
    }
    exited = verify.main(environ, client=Secrets(a_key()), run=Run(exit_code), dynamodb=dynamodb)
    return exited, left.read_text(encoding="utf-8") if left.exists() else None


def test_a_failed_run_leaves_the_last_stage_of_each_instance_of_the_tested_pools_and_nothing_else(
    tmp_path: Path, heartbeats: Any
) -> None:
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
    # Two pools the step tested, with an instance that never served, one that serves and a gateway
    # task's own heartbeat, and a pool it did not test.
    rows = [instance_row("mock", "weights"), instance_row("mock", "serving"), instance_row("gpu", "release-check")]
    untested = instance_row("other", "boot")
    for _, item in [*rows, untested]:
        table.put_item(Item=item)
    task = GatewayHeartbeat(task="task-1", pool="mock", in_flight=1, held=0, last_request_at=None, written_at=1)
    table.put_item(Item=task.to_item())

    exit_code, left = failed_run(tmp_path, heartbeats)

    # The tests' exit is the script's, and each line is a stage: no instance, no release, no pool.
    assert exit_code == 1 and left is not None and left.endswith("\n")
    assert sorted(left.splitlines()) == ["release-check", "serving", "weights"]
    # A table read a page at a time gives the same lines.
    assert sorted((failed_run(tmp_path, OneRowAPage(heartbeats))[1] or "").splitlines()) == sorted(left.splitlines())


class OneRowAPage:
    """A DynamoDB client that answers a query one row at a time, as DynamoDB pages a long answer."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def query(self, **request: Any) -> dict[str, Any]:
        return self.client.query(**request, Limit=1)


def test_a_run_that_passed_or_a_pool_with_no_instance_leaves_no_last_stage(tmp_path: Path, heartbeats: Any) -> None:
    # No instance's row: a failed run has no stage to leave.
    assert failed_run(tmp_path, heartbeats) == (1, None)
    boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).put_item(Item=instance_row("mock", "weights")[1])
    # A run whose tests passed does not read the table.
    assert failed_run(tmp_path, None, exit_code=0) == (0, None)
    assert failed_run(tmp_path, heartbeats) == (1, "weights\n")


def test_a_table_that_cannot_be_read_leaves_the_tests_exit_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Refuses:
        def query(self, **_request: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "said by AWS"}}, "Query")

    assert failed_run(tmp_path, Refuses(), exit_code=2) == (2, None)
    printed = capsys.readouterr().err
    assert "last stages were not read" in printed and "said by AWS" not in printed


# A plugin of the collection below: it writes, as JSON, the IDs of the tests that were deselected
# and of those that were kept, to the file its variable names.
RECORDER = """
import json, os

deselected = []


def ids(items):
    return [marker.args[0] for item in items for marker in item.iter_markers("smoke_id")]


def pytest_deselected(items):
    deselected.extend(ids(items))


def pytest_collection_finish(session):
    with open(os.environ["RECORDED"], "w", encoding="utf-8") as file:
        json.dump({"deselected": deselected, "kept": ids(session.items)}, file)
"""


def run_tests_in(tmp_path: Path, tests: list[str], *, more: Mapping[str, str] | None = None) -> int:
    """`verify.run_tests` on `tests`, in an environment with an endpoint and a key, where pytest
    also takes what `more` gives it. Nothing of the machine's own smoke variables takes part."""
    environ = {name: value for name, value in os.environ.items() if not name.startswith(("RHI_SMOKE_", "PYTEST_"))}
    environ |= {verify.ENDPOINT_VARIABLE: f"https://{ENDPOINT}", verify.KEY_VARIABLE: a_key(), **(more or {})}
    return verify.run_tests(environ, tests)


@pytest.mark.parametrize("pool", [None, "coursework"], ids=["the mock pool's run", "the coursework pool's run"])
def test_the_tests_through_the_internal_load_balancer_are_deselected_and_no_other(
    tmp_path: Path, pool: str | None
) -> None:
    (tmp_path / "recorder.py").write_text(RECORDER, encoding="utf-8")
    recorded = tmp_path / "recorded.json"
    # Collection only: nothing is run, so nothing is reached.
    more = {
        "PYTHONPATH": str(tmp_path),
        "PYTEST_ADDOPTS": "-p recorder --collect-only -q -p no:cacheprovider",
        "RECORDED": str(recorded),
        **({verify.POOL_VARIABLE: pool} if pool else {}),
    }

    assert run_tests_in(tmp_path, [verify.SMOKE_TESTS], more=more) == 0

    found = json.loads(recorded.read_text(encoding="utf-8"))
    internal = {test_id for test_id in SMOKE_TEST_IDS if test_id.startswith("internal-")}
    assert len(internal) == 5 and sorted(internal) == sorted(verify.DESELECTED)
    # The tests of the coursework pool's run are left out of the mock pool's run, and only there.
    left_out = internal | (set() if pool else {"gpu-release-served"})
    assert sorted(found["deselected"]) == sorted(left_out)
    # Every other smoke test is kept, the deployed-only ones and the forbidden routes at the
    # gateway's hop among them, and on the coursework pool the first stream's wake and hold.
    assert sorted(found["kept"]) == sorted(set(SMOKE_TEST_IDS) - left_out)
    assert {"forbidden-invocations", "first-stream-wake", "metrics-pool", "release-served"} <= set(found["kept"])


def test_a_failed_test_makes_the_script_exit_non_zero(tmp_path: Path) -> None:
    def run_these(tests: Path) -> int:
        environ = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
        environ |= {verify.ENDPOINT_VARIABLE: ENDPOINT, verify.REASON_FILE_VARIABLE: str(tmp_path / "not-run")}
        return verify.main(
            environ,
            client=Secrets(a_key()),
            run=lambda given: verify.run_tests({**given, "PYTEST_ADDOPTS": "-q -p no:cacheprovider"}, [str(tests)]),
        )

    passing, failing = tmp_path / "passing" / "test_it.py", tmp_path / "failing" / "test_it.py"
    for file, body in ((passing, "assert True"), (failing, "assert False")):
        file.parent.mkdir()
        file.write_text(f"def test_it() -> None:\n    {body}\n", encoding="utf-8")

    assert run_these(passing) == 0
    assert run_these(failing) != 0


def test_the_ids_of_the_tests_that_failed_are_left_one_per_line_and_nothing_else(tmp_path: Path) -> None:
    # Tests that carry their IDs as the smoke tests do: one passes, one fails, one fails before it
    # starts, and one is among those the verification leaves out.
    tests = tmp_path / "tests" / "test_it.py"
    tests.parent.mkdir()
    tests.write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\ndef broken() -> None:\n    raise RuntimeError('what a test says is not an ID')\n\n\n"
        "@pytest.mark.smoke_id('hello')\ndef test_passes() -> None:\n    pass\n\n\n"
        "@pytest.mark.smoke_id('no-key')\ndef test_fails() -> None:\n    assert False, 'nor is this'\n\n\n"
        "@pytest.mark.smoke_id('environment')\ndef test_fails_before_it_starts(broken: None) -> None:\n    pass\n\n\n"
        "@pytest.mark.smoke_id('internal-health')\ndef test_is_left_out() -> None:\n    assert False\n",
        encoding="utf-8",
    )
    quiet = "-q -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning"

    def left_by(*selection: str) -> tuple[int, str | None]:
        """The exit of a run of those tests, and what it left as its failed tests, None for no file."""
        left = tmp_path / "build" / "failed"
        left.unlink(missing_ok=True)
        more = {verify.FAILED_TESTS_FILE_VARIABLE: str(left), "PYTEST_ADDOPTS": quiet}
        exit_code = run_tests_in(tmp_path, [str(tests), *selection], more=more)
        return exit_code, left.read_text(encoding="utf-8") if left.exists() else None

    exit_code, left = left_by()
    assert exit_code != 0 and left is not None
    assert sorted(left.splitlines()) == ["environment", "no-key"] and left.endswith("\n")
    # A run in which no test fails leaves no file: there is nothing for the step to copy.
    assert left_by("-k", "passes") == (0, None)
    # A second run of the same verification, on another pool, adds only the IDs not yet left.
    left_by()
    more = {verify.FAILED_TESTS_FILE_VARIABLE: str(tmp_path / "build" / "failed"), "PYTEST_ADDOPTS": quiet}
    assert run_tests_in(tmp_path, [str(tests)], more=more) != 0
    assert sorted((tmp_path / "build" / "failed").read_text(encoding="utf-8").splitlines()) == ["environment", "no-key"]


def test_what_the_tests_print_reaches_the_log_without_the_key(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # A test that fails holding the key, and prints it whole, then cut as pytest cuts a long value.
    key = a_key()
    (tmp_path / "test_it.py").write_text(
        "import os, sys\n\n\n"
        "def test_it() -> None:\n"
        "    key = os.environ['RHI_SMOKE_KEY']\n"
        "    print('whole:', key)\n"
        "    print('cut:', key[:20] + '...' + key[-12:], file=sys.stderr)\n"
        "    print('nothing of a key')\n"
        "    assert key == 'another'\n",
        encoding="utf-8",
    )
    more = {verify.KEY_VARIABLE: key, "PYTEST_ADDOPTS": "-p no:cacheprovider"}

    assert run_tests_in(tmp_path, [str(tmp_path)], more=more) != 0

    printed = "".join(capfd.readouterr())
    # The failure is told, with the lines that hold no key as they were.
    assert "AssertionError" in printed and "nothing of a key" in printed
    assert verify.REDACTED in printed
    assert not [at for at in range(len(key) - verify.PIECE + 1) if key[at : at + verify.PIECE] in printed]


def test_the_script_runs_as_the_step_runs_it(tmp_path: Path) -> None:
    # From the checkout, with no import path given: the script needs none.
    reason = tmp_path / "not-run"
    environ = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    environ |= {verify.ENDPOINT_VARIABLE: "none", verify.REASON_FILE_VARIABLE: str(reason)}
    command = [sys.executable, "scripts/pipeline/verify.py"]
    done = subprocess.run(command, cwd=REPO, env=environ, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert reason.read_text(encoding="utf-8") == "listener-off\n"
