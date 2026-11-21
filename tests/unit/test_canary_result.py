"""The canary's result (scripts/pipeline/canary_result.py): the measurement gpu-canary it builds
from the smoke tests' recorded results and the wake it watched, and the event it sends, on the
commit the job ran from, with the UTC date of the run as its run label.

Stand-ins replace AWS and the run of the tests: no account, no credentials, no network.
"""

from __future__ import annotations

import json
import re
import time
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
STAGE = "Rhi"
WORKSPACE = "https://aps-workspaces.us-east-1.example.com/workspaces/ws-example/"
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


class Workspaces:
    """A Managed Prometheus client: two pages of workspaces, the stage's on the second."""

    def list_workspaces(self, **request: Any) -> dict[str, Any]:
        if "nextToken" not in request:
            return {"workspaces": [{"workspaceId": "ws-other", "tags": {"rhi-stage": "Other"}}], "nextToken": "2"}
        return {"workspaces": [{"workspaceId": "ws-example", "tags": {"rhi-stage": STAGE}}]}

    def describe_workspace(self, *, workspaceId: str) -> dict[str, Any]:
        assert workspaceId == "ws-example"
        return {"workspace": {"workspaceId": workspaceId, "prometheusEndpoint": WORKSPACE}}


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
        "RHI_SMOKE_RELEASE_PARAMETERS": json.dumps({"coursework": "/rhi/pools/coursework/release"}),
        "RHI_CANARY_STAGE": STAGE,
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
        aps=Workspaces(),
        events=Events(sent),
        run=run,
        watch=readings(WOKEN),
    )
    assert exit_code == 1
    ((env, arguments),) = given
    assert env["RHI_SMOKE_POOL"] == "coursework"
    assert env["RHI_SMOKE_KEY"] == key
    # The workspace's own query address, under which the tests ask for api/v1/query.
    assert env["RHI_SMOKE_WORKSPACE_URL"] == WORKSPACE
    assert json.loads(env["RHI_SMOKE_RELEASE_PARAMETERS"]) == {"coursework": "/rhi/pools/coursework/release"}
    assert arguments[:2] == ["-p", "canary_result"] and arguments[-1] == "tests/smoke"
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
        aps=Workspaces(),
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


def test_the_run_leaves_out_the_heartbeat_test_its_role_cannot_read() -> None:
    def item(test_id: str) -> Any:
        return SimpleNamespace(test_id=test_id, iter_markers=lambda _name: [SimpleNamespace(args=(test_id,))])

    deselected: list[Any] = []
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)))
    items = [item("first-stream-wake"), item("release-heartbeat"), item("gpu-release-served")]
    canary_result.pytest_collection_modifyitems(config, items)  # ty: ignore[invalid-argument-type] - test doubles
    assert [found.test_id for found in items] == ["first-stream-wake", "gpu-release-served"]
    assert [found.test_id for found in deselected] == ["release-heartbeat"]


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
