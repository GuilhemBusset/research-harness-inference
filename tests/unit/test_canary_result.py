"""The canary's result (scripts/pipeline/canary_result.py): the measurement gpu-canary it builds
from the smoke tests' recorded results and the wake it watched, and the event it sends, on the
commit the job ran from, with the UTC date of the run as its run label.

Stand-ins replace AWS and the run of the tests: no account, no credentials, no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from botocore.exceptions import ClientError

import canary_result
from rhi_reporter.measure import read_event

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BUS = "rhi-measurements"
WORKSPACE = "https://aps-workspaces.us-east-1.example.com/workspaces/ws-example/"
HEARTBEATS = "Rhi-Pools-TablesHeartbeatsEXAMPLE-SUFFIX"
PARAMETER = "/rhi/pools/coursework/release"
REPO = Path(__file__).resolve().parents[2]
NAME = re.compile(r"[a-z0-9_]+")
SUNDAY = datetime(2026, 11, 22, 4, 0, 7, tzinfo=UTC)

# Results as pytest's --junitxml records them: three smoke tests passed, one of them parametrized.
ALL_PASSED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3">
<testcase classname="tests.smoke.test_smoke" name="test_the_hello_route_answers" time="0.120" />
<testcase classname="tests.smoke.test_smoke" name="test_the_first_stream_wakes_a_cold_pool" time="412.5" />
<testcase classname="tests.smoke.test_smoke" name="test_a_stream_completes_with_its_usage[/v1/messages]" time="3.2" />
</testsuite></testsuites>"""
# The same run, where one test failed and the internal ones were deselected, which records nothing.
ONE_FAILED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="3">
<testcase classname="tests.smoke.test_smoke" name="test_the_hello_route_answers" time="0.120" />
<testcase classname="tests.smoke.test_smoke" name="test_the_first_stream_wakes_a_cold_pool" time="412.5">
<failure message="assert 503 == 200">the pool did not answer</failure></testcase>
<testcase classname="tests.smoke.test_smoke" name="test_a_stream_completes_with_its_usage[/v1/messages]" time="3.2" />
</testsuite></testsuites>"""
# A run pytest did not finish: one test ended, and the one that was running is recorded unnamed.
INTERRUPTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1">
<testcase classname="tests.smoke.test_smoke" name="test_the_hello_route_answers" time="0.120" />
<testcase time="0.000" />
</testsuite></testsuites>"""
# The pool read cold until the first stream, warming through its start, then ready.
WOKEN = [(10.0, "cold"), (12.0, "cold"), (14.0, "warming"), (16.0, None), (318.0, "warming"), (320.5, "ready")]


def detail(entry: Mapping[str, str]) -> dict[str, Any]:
    assert entry["Source"] == "rhi.measurement"
    assert entry["EventBusName"] == BUS
    return json.loads(entry["Detail"])


def test_every_test_passed_gives_pass_with_the_wake_s_seconds() -> None:
    results = canary_result.read_results(ALL_PASSED)
    wake = canary_result.wake_seconds(WOKEN)
    found = detail(canary_result.measurement(results, wake, commit=COMMIT, bus=BUS, now=SUNDAY))
    assert found["measurement"] == "gpu-canary"
    assert found["verdict"] == "pass"
    # From the last reading of cold before the start to the first of ready.
    assert found["numbers"] == {"wake_seconds": 308.5, "smoke_tests_passed": 3, "smoke_tests_failed": 0}
    assert found["commit"] == COMMIT


def test_one_failed_test_gives_fail() -> None:
    results = canary_result.read_results(ONE_FAILED)
    found = detail(canary_result.measurement(results, 308.5, commit=COMMIT, bus=BUS, now=SUNDAY))
    assert found["verdict"] == "fail"
    assert found["numbers"]["smoke_tests_passed"] == 2
    assert found["numbers"]["smoke_tests_failed"] == 1


def test_every_name_is_lower_case_letters_digits_and_underscores() -> None:
    entry = canary_result.measurement(canary_result.read_results(ALL_PASSED), 1.5, commit=COMMIT, bus=BUS, now=SUNDAY)
    found = detail(entry)
    assert found["names"] == list(canary_result.NAMES)
    assert all(NAME.fullmatch(name) for name in [*found["names"], *found["numbers"]])
    # The reporter builds its status from the event as it is sent.
    (status,) = read_event({"detail": found})
    assert status.context == "measure/gpu-canary/2026-11-22/1"


@pytest.mark.parametrize(
    ("now", "label"),
    [
        pytest.param(SUNDAY, "2026-11-22", id="in UTC"),
        # Late on Saturday five hours west of UTC is already Sunday in UTC.
        pytest.param(datetime(2026, 11, 21, 23, 30, tzinfo=timezone(timedelta(hours=-5))), "2026-11-22", id="west"),
    ],
)
def test_the_run_label_is_the_utc_date_of_the_clock(now: datetime, label: str) -> None:
    sent: list[dict[str, Any]] = []
    canary_result.main(
        {"CODEBUILD_RESOLVED_SOURCE_VERSION": COMMIT, "RHI_MEASUREMENT_BUS": BUS, "RHI_SMOKE_ENDPOINT": "none"},
        clock=lambda: now,
        events=Events(sent),
    )
    (entry,) = sent
    assert detail(entry)["label"] == label
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", label)


def test_a_pool_already_serving_had_no_wake_and_one_never_ready_has_none_measured() -> None:
    assert canary_result.wake_seconds([(1.0, "ready")]) == 0.0
    assert canary_result.wake_seconds([(1.0, "cold"), (3.0, "warming"), (5.0, None)]) is None
    found = detail(canary_result.measurement(canary_result.Results(0, 1), None, commit=COMMIT, bus=BUS, now=SUNDAY))
    assert "wake_seconds" not in found["numbers"]


def test_a_pass_always_carries_the_wake_s_seconds() -> None:
    results = canary_result.read_results(ALL_PASSED)
    already_serving = detail(canary_result.measurement(results, 0.0, commit=COMMIT, bus=BUS, now=SUNDAY))
    assert (already_serving["verdict"], already_serving["numbers"]["wake_seconds"]) == ("pass", 0.0)
    # Every test passed, but no reading of the pool's state gave a wake: not measured.
    unmeasured = canary_result.wake_seconds([(0.0, None), (2.0, None)])
    found = detail(canary_result.measurement(results, unmeasured, commit=COMMIT, bus=BUS, now=SUNDAY))
    assert found["verdict"] == "error"
    assert "wake_seconds" not in found["numbers"]


def test_a_run_pytest_did_not_finish_is_not_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = canary_result.read_results(INTERRUPTED)
    # The unfinished case counts as neither passed nor failed.
    assert (partial.passed, partial.failed, partial.finished) == (1, 0, False)
    # Whatever the XML holds, a run pytest ended as interrupted (2) did not end.
    whole = canary_result.read_results(ALL_PASSED)
    monkeypatch.setattr(canary_result, "run_canary", lambda *_args, **_kwargs: (whole, 3.0, 2))
    sent: list[dict[str, Any]] = []
    assert canary_result.main(environ(), clock=lambda: SUNDAY, events=Events(sent)) == 2
    (entry,) = sent
    assert detail(entry)["verdict"] == "error"
    assert detail(entry)["numbers"]["smoke_tests_passed"] == 3
    # A run that exited 0 and is still not green fails the build.
    monkeypatch.setattr(canary_result, "run_canary", lambda *_args, **_kwargs: (whole, None, 0))
    assert canary_result.main(environ(), clock=lambda: SUNDAY, events=Events([])) == 1


class Events:
    """An EventBridge client as far as the script uses one: it keeps each entry, and refuses them
    when `failed` is set, as EventBridge does, with HTTP 200 and a count."""

    def __init__(self, sent: list[dict[str, Any]], *, failed: bool = False) -> None:
        self.sent, self.failed = sent, failed

    def put_events(self, *, Entries: list[dict[str, Any]]) -> dict[str, Any]:
        if self.failed:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalException"}]}
        self.sent += Entries
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}


class Secrets:
    def __init__(self, held: str | None) -> None:
        self.held = held

    def get_secret_value(self, **_request: Any) -> dict[str, Any]:
        if self.held is None:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "-"}}, "GetSecretValue")
        return {"SecretString": self.held}


class Parameters:
    """An SSM client that holds the coursework pool's release parameter, as the pools stack writes
    it (`rhi.pools.release`)."""

    def get_parameter(self, *, Name: str) -> dict[str, Any]:
        assert Name == PARAMETER
        settings = {
            "pool": "coursework",
            "group": "rhi-coursework",
            "launch_hook": "launch",
            "termination_hook": "termination",
            "log_group": "/rhi/pools/coursework",
            "workspace_url": f"{WORKSPACE}api/v1/remote_write",
            "heartbeat_table": HEARTBEATS,
            "secret_id": "rhi/pools/coursework",
        }
        release = {
            "engine_image": f"registry.example.com/engine@sha256:{'1' * 64}",
            "model_revision": "2" * 40,
            "engine_flags": [],
            "files": {"bucket": "files", "key": "release.tar", "sha256": "3" * 64},
            "settings": settings,
        }
        return {"Parameter": {"Name": Name, "Value": json.dumps(release)}}


def readings(watched: Sequence[tuple[float, str | None]]) -> Any:
    """A watch that holds the readings it was given, in place of one that reads `GET /status`."""

    def watch(_endpoint: str, _key: str) -> Any:
        holder = SimpleNamespace(readings=list(watched))

        class Context:
            def __enter__(self) -> Any:
                return holder

            def __exit__(self, *_exception: object) -> None:
                return None

        return Context()

    return watch


def environ() -> dict[str, str]:
    return {
        "CODEBUILD_RESOLVED_SOURCE_VERSION": COMMIT,
        "RHI_MEASUREMENT_BUS": BUS,
        "RHI_SMOKE_ENDPOINT": "canary.example.com",
        "RHI_SMOKE_RELEASE_PARAMETERS": json.dumps({"coursework": PARAMETER}),
    }


def test_the_job_runs_the_coursework_pool_s_smoke_tests_and_sends_their_result() -> None:
    key, given, sent = "a-key-drawn-for-this-test", [], []

    def run(env: Mapping[str, str], arguments: Sequence[str]) -> int:
        given.append((dict(env), list(arguments)))
        (report,) = [
            argument.removeprefix("--junitxml=") for argument in arguments if argument.startswith("--junitxml=")
        ]
        Path(report).write_text(ONE_FAILED, encoding="utf-8")
        return 1

    exit_code = canary_result.main(
        environ(),
        clock=lambda: SUNDAY,
        secrets=Secrets(key),
        ssm=Parameters(),
        events=Events(sent),
        run=run,
        watch=readings(WOKEN),
    )
    assert exit_code == 1
    ((env, arguments),) = given
    assert env["RHI_SMOKE_POOL"] == "coursework"
    assert env["RHI_SMOKE_KEY"] == key
    # The workspace's own query address, under which the tests ask for api/v1/query, from the
    # release parameter's settings; and no table, which the canary's role may not read.
    assert env["RHI_SMOKE_WORKSPACE_URL"] == WORKSPACE
    assert "RHI_VERIFY_HEARTBEATS_TABLE" not in env
    assert json.loads(env["RHI_SMOKE_RELEASE_PARAMETERS"]) == {"coursework": PARAMETER}
    assert arguments[-1] == "tests/smoke"
    (entry,) = sent
    found = detail(entry)
    assert (found["verdict"], found["label"], found["commit"]) == ("fail", "2026-11-22", COMMIT)
    assert found["numbers"] == {"wake_seconds": 308.5, "smoke_tests_passed": 2, "smoke_tests_failed": 1}
    assert key not in entry["Detail"]


@pytest.mark.parametrize(
    ("endpoint", "held"), [pytest.param("none", "a-key", id="no listener"), pytest.param("canary.example.com", None)]
)
def test_no_test_run_is_sent_as_not_measured(endpoint: str, held: str | None) -> None:
    sent: list[dict[str, Any]] = []

    def run(_env: Mapping[str, str], _arguments: Sequence[str]) -> int:
        raise AssertionError("no test runs")

    exit_code = canary_result.main(
        environ() | {"RHI_SMOKE_ENDPOINT": endpoint},
        clock=lambda: SUNDAY,
        secrets=Secrets(held),
        ssm=Parameters(),
        events=Events(sent),
        run=run,
        watch=readings([]),
    )
    assert exit_code == 0
    (entry,) = sent
    assert (detail(entry)["verdict"], detail(entry)["numbers"]) == ("error", {})


def test_an_event_the_bus_refuses_fails_the_script() -> None:
    with pytest.raises(RuntimeError, match="did not take"):
        canary_result.main(
            environ() | {"RHI_SMOKE_ENDPOINT": "none"}, clock=lambda: SUNDAY, events=Events([], failed=True)
        )


def collected(env: Mapping[str, str], arguments: Sequence[str]) -> set[str]:
    """The smoke tests a run of the verification's runner would make, by their node IDs, with the
    repository's package roots on the import path, as the canary's job has them."""
    command = [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "verify", *arguments]
    roots = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    folders = os.pathsep.join(["scripts/pipeline", *roots["pythonpath"], *filter(None, [os.environ.get("PYTHONPATH")])])
    answer = subprocess.run(
        command, cwd=REPO, env={**os.environ, **env, "PYTHONPATH": folders}, capture_output=True, text=True, check=True
    )
    return {line for line in answer.stdout.splitlines() if line.startswith("tests/smoke/") and "::" in line}


def test_the_run_makes_the_verification_s_tests_on_the_coursework_pool_but_the_heartbeat_one() -> None:
    given: list[tuple[dict[str, str], list[str]]] = []

    def run(env: Mapping[str, str], arguments: Sequence[str]) -> int:
        given.append((dict(env), [argument for argument in arguments if not argument.startswith("--junitxml=")]))
        return 5

    canary_result.main(
        environ(),
        clock=lambda: SUNDAY,
        secrets=Secrets("a-key-drawn-for-this-test"),
        ssm=Parameters(),
        events=Events([]),
        run=run,
        watch=readings([]),
    )
    ((env, arguments),) = given
    endpoint = {"RHI_SMOKE_ENDPOINT": env["RHI_SMOKE_ENDPOINT"], "RHI_SMOKE_POOL": env["RHI_SMOKE_POOL"]}
    after_a_refresh = collected(endpoint | {"RHI_SMOKE_POOL": "coursework"}, ["tests/smoke"])
    heartbeat = {test for test in after_a_refresh if "release_heartbeats" in test}
    assert heartbeat
    assert any("gpu_release" in test for test in after_a_refresh)
    # Every test of the verification's run after a refresh, the release served among them, but the
    # one that reads the heartbeats table, which the canary's role may not read.
    assert collected(endpoint, arguments) == after_a_refresh - heartbeat


def test_the_watch_reads_the_coursework_pool_s_state_until_it_is_ready() -> None:
    states = iter(["cold", "warming", "ready"])

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status" and request.headers["Authorization"] == "Bearer a-key"
        pools = [{"model": "mock", "state": "ready"}, {"model": "coursework", "state": next(states)}]
        return httpx.Response(200, json={"pools": pools})

    read = canary_result.status_reader("canary.example.com", "a-key", httpx.MockTransport(answer))
    with canary_result.WakeWatch(read, seconds=0.01) as watch:
        deadline = time.monotonic() + 10
        while len(watch.readings) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    # Once the pool reads ready the wake is measured, and the watch reads no more.
    assert [state for _, state in watch.readings] == ["cold", "warming", "ready"]
    assert canary_result.wake_seconds(watch.readings) is not None
