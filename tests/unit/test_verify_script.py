"""The verification script (scripts/pipeline/verify.py): with no listener or no canary key it runs
no test, exits 0 and leaves its reason; with a key it runs the smoke tests, all but those that
must reach the internal load balancer, a failed test is a non-zero exit, and what the tests print
reaches the log without the key.

A stand-in for the Secrets Manager client replaces AWS. The tests that pytest really collects and
runs do so in a process of their own, as the script starts one, and reach no stack.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

import verify

if TYPE_CHECKING:
    from collections.abc import Mapping

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


def test_no_pool_is_no_release_parameter() -> None:
    assert verify.release_parameters("none") == {}


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


def test_the_tests_through_the_internal_load_balancer_are_deselected_and_no_other(tmp_path: Path) -> None:
    (tmp_path / "recorder.py").write_text(RECORDER, encoding="utf-8")
    recorded = tmp_path / "recorded.json"
    # Collection only: nothing is run, so nothing is reached.
    more = {
        "PYTHONPATH": str(tmp_path),
        "PYTEST_ADDOPTS": "-p recorder --collect-only -q -p no:cacheprovider",
        "RECORDED": str(recorded),
    }

    assert run_tests_in(tmp_path, [verify.SMOKE_TESTS], more=more) == 0

    found = json.loads(recorded.read_text(encoding="utf-8"))
    internal = {test_id for test_id in SMOKE_TEST_IDS if test_id.startswith("internal-")}
    assert len(internal) == 5
    assert sorted(found["deselected"]) == sorted(internal) == sorted(verify.DESELECTED)
    # Every other smoke test is kept, the deployed-only ones and the forbidden routes at the
    # gateway's hop among them.
    assert sorted(found["kept"]) == sorted(set(SMOKE_TEST_IDS) - internal)
    assert {"forbidden-invocations", "metrics-pool", "release-served"} <= set(found["kept"])


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
