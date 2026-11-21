"""The weekly GPU canary: the coursework pool's smoke tests against the deployed endpoint, and the
measurement `gpu-canary` of their result (system design, sections 8.2, step 6, and 14). The
canary's job runs it, on the commit of `main` its schedule started it on
(`src/rhi/pipeline/canary.py`).

**The run.** The script reads the canary's key from the secret `rhi/canary-key`, as the
verification does (`verify.canary_key`), finds the query address of the stage's Prometheus
workspace, the one whose tag `rhi-stage` holds `RHI_CANARY_STAGE`, and runs the smoke tests of
`tests/smoke` once, with `RHI_SMOKE_POOL` naming the coursework pool, through the verification's
own runner (`verify.run_tests`): in a process of their own, the key in its environment and nowhere
else, what they print in the log without the key, and the tests that must reach the internal load
balancer left out. This file is a plugin of that run as well, which leaves out `CANARY_DESELECTED`
too: `release-heartbeat` reads the stage's heartbeats table, which the canary's role may not read.
The run records its results as JUnit XML, which the script reads back.

**The wake.** While the tests run, a thread of this process reads the coursework pool's state from
`GET /status` every `POLL_SECONDS` (a status read is not demand: it wakes nothing). The wake is the
time from the last reading of `cold` before the pool turned `warming` or `ready` to the first
reading of `ready`: the hold of the first stream through the pool's readiness, without the stream's
generation. A pool that reads `ready` at the first reading was not woken, and its wake is 0 seconds.
A pool that never reads `ready` has no wake measured, and the number is left out.

**The measurement.** One run of `gpu-canary`, on the commit the job ran from,
`CODEBUILD_RESOLVED_SOURCE_VERSION`, with the UTC date the run began, YYYY-MM-DD, as its run label:
a record keeps one run per label, so each week's run keeps its own. Its verdict is

- pass when at least one smoke test passed and none failed;
- fail when a smoke test failed, in its set-up, its call or its teardown;
- error, not measured, when no test ran: the endpoint has no HTTPS listener (`RHI_SMOKE_ENDPOINT`
  reads `none`), the canary's secret is missing or holds no key, or the run left no result.

and its numbers, `Number`, are the seconds the wake took and the numbers of smoke tests passed and
failed. It is sent through the measurement event (`rhi_reporter.measure.measurement_event`) on the
bus `RHI_MEASUREMENT_BUS` names. An event the bus does not take fails the script: EventBridge
answers a refused entry with a count of failed entries, not with an error.

The script exits with the tests' exit code once the event is sent, so a failed canary is a failed
build as well.

Run by the canary's job, from the checkout:

    uv run --locked python scripts/pipeline/canary_result.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from xml.etree import ElementTree

import httpx
from botocore.exceptions import BotoCoreError, ClientError

import verify
from rhi_reporter.measure import BUS_VARIABLE, Verdict, measurement_event

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import pytest

NAME = "canary_result"
MEASUREMENT = "gpu-canary"
POOL = "coursework"

COMMIT_VARIABLE = "CODEBUILD_RESOLVED_SOURCE_VERSION"
STAGE_VARIABLE = "RHI_CANARY_STAGE"
WORKSPACE_URL_VARIABLE = "RHI_SMOKE_WORKSPACE_URL"
# The tag that names the stage of a workspace (`rhi.observability.stack`).
STAGE_TAG = "rhi-stage"
# At most this many pages of the workspaces are read.
WORKSPACE_PAGES = 10

# The tests the canary's run leaves out beside those the verification does, by their IDs.
CANARY_DESELECTED: tuple[str, ...] = ("release-heartbeat",)

# The pool's states as `GET /status` gives them (section 5.3).
COLD = "cold"
READY = "ready"
# Between two readings of the pool's state, and the longest one may take.
POLL_SECONDS = 2.0
READ_SECONDS = 10.0
# The format of the run label: the UTC date.
LABEL_FORMAT = "%Y-%m-%d"


class Number(StrEnum):
    """The names of the canary's numbers."""

    WAKE_SECONDS = "wake_seconds"
    SMOKE_TESTS_PASSED = "smoke_tests_passed"
    SMOKE_TESTS_FAILED = "smoke_tests_failed"


NAMES: tuple[str, ...] = tuple(Number)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """As a plugin of the run of the smoke tests: deselects the tests whose ID is one of
    `CANARY_DESELECTED`."""
    left_out = [
        item
        for item in items
        if any(set(marker.args) & set(CANARY_DESELECTED) for marker in item.iter_markers(verify.ID_MARKER))
    ]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = [item for item in items if item not in left_out]


@dataclass(frozen=True)
class Results:
    """How many smoke tests passed and how many failed."""

    passed: int
    failed: int


def read_results(junit_xml: str) -> Results:
    """The results a run recorded as JUnit XML: a test case with a failure or an error, in any
    phase, failed; one that was skipped is neither; any other passed. A test pytest reports twice,
    a failed teardown after its call, counts once."""
    outcomes: dict[tuple[str, str], bool] = {}
    for case in ElementTree.fromstring(junit_xml).iter("testcase"):
        test = (case.get("classname", ""), case.get("name", ""))
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes[test] = True
        elif case.find("skipped") is None:
            outcomes.setdefault(test, False)
    failed = sum(outcomes.values())
    return Results(passed=len(outcomes) - failed, failed=failed)


def wake_seconds(readings: Sequence[tuple[float, str | None]]) -> float | None:
    """The seconds the wake took, from the pool's states read at the given monotonic times, a
    reading that failed as None: from the last `cold` before the pool turned `warming` or `ready`,
    or from the first reading when it was already warming, to the first `ready`. 0 when the first
    reading is `ready`, and None when no reading is."""
    began: float | None = None
    for at, state in readings:
        if state is None:
            continue
        if state == READY:
            return 0.0 if began is None else round(at - began, 1)
        if state == COLD or began is None:
            began = at
    return None


def verdict_of(results: Results | None) -> Verdict:
    """The run's verdict: not measured when no test ran, fail when one failed, pass otherwise."""
    if results is None or results.passed + results.failed == 0:
        return Verdict.NOT_MEASURED
    return Verdict.FAIL if results.failed else Verdict.PASS


def numbers_of(results: Results | None, wake: float | None) -> dict[str, int | float]:
    """The run's numbers, those it measured."""
    numbers: dict[str, int | float] = {}
    if wake is not None:
        numbers[Number.WAKE_SECONDS] = wake
    if results is not None:
        numbers[Number.SMOKE_TESTS_PASSED] = results.passed
        numbers[Number.SMOKE_TESTS_FAILED] = results.failed
    return numbers


def run_label(now: datetime) -> str:
    """The run label of a run that began at `now`: its UTC date. ValueError for a time that names
    no zone, whose date in UTC is unknown."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the run's time names no zone")
    return now.astimezone(UTC).strftime(LABEL_FORMAT)


def measurement(results: Results | None, wake: float | None, *, commit: str, bus: str, now: datetime) -> dict[str, str]:
    """The entry of PutEvents that hands the run to the reporter."""
    return measurement_event(
        MEASUREMENT,
        run_label(now),
        verdict_of(results),
        numbers_of(results, wake),
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


def workspace_url(client: Any, stage: str) -> str | None:
    """The query address of the one workspace tagged with `stage`, read with `client`, a Managed
    Prometheus client of the AWS SDK. None when there is no such workspace, or more than one."""
    if not stage:
        return None
    found: list[str] = []
    request: dict[str, str] = {}
    for _ in range(WORKSPACE_PAGES):
        answer = client.list_workspaces(**request)
        found += [
            summary["workspaceId"]
            for summary in answer.get("workspaces", [])
            if (summary.get("tags") or {}).get(STAGE_TAG) == stage
        ]
        if not answer.get("nextToken"):
            break
        request = {"nextToken": answer["nextToken"]}
    if len(found) != 1:
        return None
    address = client.describe_workspace(workspaceId=found[0])["workspace"].get("prometheusEndpoint")
    return address if isinstance(address, str) and address else None


def status_reader(endpoint: str, key: str, transport: httpx.BaseTransport | None = None) -> Callable[[], str | None]:
    """What reads the coursework pool's state from `GET /status` of `endpoint`, with the canary's
    key, through `transport` when one is given: None for a reading that failed. Nothing of the
    machine's own configuration, a proxy among it, takes part in a reading."""
    address = (endpoint if "://" in endpoint else f"https://{endpoint}").rstrip("/")

    def read() -> str | None:
        try:
            with httpx.Client(timeout=READ_SECONDS, trust_env=False, transport=transport) as http:
                answer = http.get(f"{address}/status", headers={"Authorization": f"Bearer {key}"})
            states = {record["model"]: record["state"] for record in answer.json()["pools"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        state = states.get(POOL) if answer.status_code == 200 else None
        return state if isinstance(state, str) else None

    return read


class WakeWatch:
    """Reads the pool's state with `read` every `seconds`, on a thread of its own, from when the
    context begins until it ends or the wake is measured: `readings`, each with its monotonic time."""

    def __init__(self, read: Callable[[], str | None], *, seconds: float = POLL_SECONDS) -> None:
        self.readings: list[tuple[float, str | None]] = []
        self._read, self._seconds = read, seconds
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="canary-wake", daemon=True)

    def _watch(self) -> None:
        while not self._done.is_set():
            self.readings.append((time.monotonic(), self._read()))
            if wake_seconds(self.readings) is not None:
                return
            self._done.wait(self._seconds)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._done.set()
        self._thread.join(timeout=READ_SECONDS + self._seconds)


def _client(service: str) -> Any:
    import boto3

    return boto3.client(service)


def run_canary(
    env: Mapping[str, str],
    *,
    secrets: Any,
    aps: Any,
    run: Callable[[Mapping[str, str], Sequence[str]], int],
    watch: Callable[[str, str], WakeWatch],
) -> tuple[Results | None, float | None, int]:
    """Runs the smoke tests on the coursework pool: their results, the wake's seconds and the run's
    exit code. No result when no test could run."""
    endpoint = env.get(verify.ENDPOINT_VARIABLE)
    if not endpoint:
        print(f"{NAME}: {verify.ENDPOINT_VARIABLE} is not set: no test was run", file=sys.stderr)
        return None, None, 1
    if endpoint == verify.NO_ENDPOINT:
        print(f"{NAME}: no test was run: {verify.LISTENER_OFF}")
        return None, None, 0
    key = verify.canary_key(secrets)
    if key is None:
        print(f"{NAME}: no test was run: {verify.CANARY_MISSING}")
        return None, None, 0
    given = {**env, verify.KEY_VARIABLE: key, verify.POOL_VARIABLE: POOL}
    try:
        url = workspace_url(aps, env.get(STAGE_VARIABLE, ""))
    except (BotoCoreError, ClientError) as error:
        print(f"{NAME}: the stage's workspace was not found ({type(error).__name__})", file=sys.stderr)
        url = None
    if url is not None:
        given[WORKSPACE_URL_VARIABLE] = url
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "results.xml"
        with watch(endpoint, key) as watching:
            exit_code = run(given, ("-p", NAME, f"--junitxml={report}", verify.SMOKE_TESTS))
        results = read_results(report.read_text(encoding="utf-8")) if report.is_file() else None
    return results, wake_seconds(watching.readings), exit_code


def _watch(endpoint: str, key: str) -> WakeWatch:
    return WakeWatch(status_reader(endpoint, key))


def main(
    environ: Mapping[str, str] | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    secrets: Any = None,
    aps: Any = None,
    events: Any = None,
    run: Callable[[Mapping[str, str], Sequence[str]], int] = verify.run_tests,
    watch: Callable[[str, str], WakeWatch] = _watch,
) -> int:
    """Runs the canary and sends its measurement; the tests' exit code once it is sent. Without a
    client, the build's own credentials and region make it."""
    env = os.environ if environ is None else environ
    began = clock()
    commit, bus = env.get(COMMIT_VARIABLE), env.get(BUS_VARIABLE)
    if not commit or not bus:
        print(f"{NAME}: {COMMIT_VARIABLE} and {BUS_VARIABLE} name the commit and the bus", file=sys.stderr)
        return 1
    try:
        results, wake, exit_code = run_canary(
            env,
            secrets=_client("secretsmanager") if secrets is None else secrets,
            aps=_client("amp") if aps is None else aps,
            run=run,
            watch=watch,
        )
    except (BotoCoreError, ClientError) as error:
        # The canary could not measure: it says so, as not measured, rather than sending nothing.
        print(f"{NAME}: the canary did not run ({type(error).__name__})", file=sys.stderr)
        results, wake, exit_code = None, None, 1
    entry = measurement(results, wake, commit=commit, bus=bus, now=began)
    send(entry, _client("events") if events is None else events)
    print(f"{NAME}: {MEASUREMENT} {verdict_of(results).name.lower()} sent for {run_label(began)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
