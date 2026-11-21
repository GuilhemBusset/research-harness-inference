"""The weekly GPU canary: the coursework pool's smoke tests against the deployed endpoint, and the
measurement `gpu-canary` of their result (system design, sections 8.2, step 6, and 14). The
canary's job runs it, on the commit of `main` its schedule started it on
(`src/rhi/pipeline/canary.py`).

**The run.** The script reads the canary's key from the secret `rhi/canary-key`, as the
verification does (`verify.canary_key`), and the coursework pool's release parameter, whose
settings name the stage's Prometheus workspace by its remote-write address (`rhi_lifecycle.release`).
It runs the smoke tests of `tests/smoke` once, with `RHI_SMOKE_POOL` naming the coursework pool,
through the verification's own runner (`verify.run_tests`): in a process of their own, the key in
its environment and nowhere else, what they print in the log without the key, and the tests that
must reach the internal load balancer left out. These are the tests the verification runs on the
coursework pool after a refresh, the workspace's query address given as the verification gives it,
`RHI_SMOKE_WORKSPACE_URL`, less one: this file is a plugin of the run as well, which leaves out
`CANARY_DESELECTED`. `release-heartbeat` reads the stage's heartbeats table, a DynamoDB table, and
the canary's role may read the canary's secret, query the workspace, read the pools' release
parameters and send its event, and nothing else (`src/rhi/pipeline/canary.py`). The release the
pool's instances serve is still compared with the deployed one, through the workspace
(`release-served`, `gpu-release-served`). The run records its results as JUnit XML, which the
script reads back.

**The wake.** While the tests run, a thread of this process reads the coursework pool's state from
`GET /status` every `POLL_SECONDS` (a status read is not demand: it wakes nothing). The wake is the
time from the last reading of `cold` before the pool turned `warming` or `ready` to the first
reading of `ready`: the hold of the first stream through the pool's readiness, without the stream's
generation. A pool that reads `ready` at the first reading was not woken, and its wake is 0 seconds.
A pool that never reads `ready` has no wake measured, and the number is left out.

**The measurement.** One run of `gpu-canary`, on the commit the job ran from,
`CODEBUILD_RESOLVED_SOURCE_VERSION`, with the UTC date the run began, YYYY-MM-DD, as its run label:
a record keeps one run per label, so each week's run keeps its own. Its verdict is

- fail when a smoke test failed, in its set-up, its call or its teardown;
- error, not measured, when no test ran: the endpoint has no HTTPS listener (`RHI_SMOKE_ENDPOINT`
  reads `none`), the canary's secret is missing or holds no key, or the run left no result; and
  when the run did not end (pytest was interrupted, or ended with an exit code other than its two
  of a finished run, `FINISHED`), or no wake was measured;
- pass otherwise: the run ended, at least one smoke test passed, none failed, and the wake was
  measured.

and its numbers, `Number`, are the seconds the wake took and the numbers of smoke tests passed and
failed. Only the tests that ended count: a case the JUnit XML records without its name is one
pytest did not finish. It is sent through the measurement event (`rhi_reporter.measure.measurement_event`) on the
bus `RHI_MEASUREMENT_BUS` names. An event the bus does not take fails the script: EventBridge
answers a refused entry with a count of failed entries, not with an error.

The script exits with the tests' exit code once the event is sent, and with 1 when that code is 0
and the verdict still is not pass, so a canary that is not green after a run is a failed build as
well.

Run by the canary's job, from the checkout:

    uv run --locked python scripts/pipeline/canary_result.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
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
WORKSPACE_URL_VARIABLE = "RHI_SMOKE_WORKSPACE_URL"
# What follows the workspace's own address in the remote-write address of a release's settings
# (`rhi.pools.release`).
REMOTE_WRITE_PATH = "api/v1/remote_write"
# The exit codes of a run of pytest that ended: every test passed, or some failed.
FINISHED = (0, 1)

# The tests the canary's run leaves out beside those the verification does, by their IDs: what
# needs more than the canary's role may do.
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


def ended(results: Results | None, exit_code: int) -> Results | None:
    """The results of a run whose pytest exited with `exit_code`: one that did not exit as a
    finished run does, `FINISHED`, did not end, whatever its XML holds."""
    if results is None or exit_code in FINISHED:
        return results
    return replace(results, finished=False)


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


def verdict_of(results: Results | None, wake: float | None) -> Verdict:
    """The run's verdict: fail when a test failed; not measured when no test ended, when the run
    did not end or when no wake was measured; pass otherwise."""
    if results is None or results.passed + results.failed == 0:
        return Verdict.NOT_MEASURED
    if results.failed:
        return Verdict.FAIL
    if not results.finished or wake is None:
        return Verdict.NOT_MEASURED
    return Verdict.PASS


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
        verdict_of(results, wake),
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


def stage_settings(client: Any, env: Mapping[str, str]) -> dict[str, str]:
    """What the smoke tests are told of the stage, from the settings of the coursework pool's
    release parameter, read with `client`, an SSM client of the AWS SDK: the workspace's query
    address, its remote-write address without `REMOTE_WRITE_PATH`. Nothing when the parameter
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
    }


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
    ssm: Any,
    run: Callable[[Mapping[str, str], Sequence[str]], int],
    watch: Callable[[str, str], WakeWatch],
) -> tuple[Results | None, float | None, int]:
    """Runs the smoke tests on the coursework pool: their results, the wake's seconds and the run's
    exit code. No result when no test could run. A client that is None is made, from the build's
    own credentials and region, only once it is needed."""
    endpoint = env.get(verify.ENDPOINT_VARIABLE)
    if not endpoint:
        print(f"{NAME}: {verify.ENDPOINT_VARIABLE} is not set: no test was run", file=sys.stderr)
        return None, None, 1
    if endpoint == verify.NO_ENDPOINT:
        print(f"{NAME}: no test was run: {verify.LISTENER_OFF}")
        return None, None, 0
    key = verify.canary_key(_client("secretsmanager") if secrets is None else secrets)
    if key is None:
        print(f"{NAME}: no test was run: {verify.CANARY_MISSING}")
        return None, None, 0
    stage = stage_settings(_client("ssm") if ssm is None else ssm, env)
    given = {**env, **stage, verify.KEY_VARIABLE: key, verify.POOL_VARIABLE: POOL}
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
    ssm: Any = None,
    events: Any = None,
    run: Callable[[Mapping[str, str], Sequence[str]], int] = verify.run_tests,
    watch: Callable[[str, str], WakeWatch] = _watch,
) -> int:
    """Runs the canary and sends its measurement; the tests' exit code once it is sent, or 1 for a
    run that exited 0 and is not green. Without a client, the build's own credentials and region
    make it, when it is needed."""
    env = os.environ if environ is None else environ
    began = clock()
    commit, bus = env.get(COMMIT_VARIABLE), env.get(BUS_VARIABLE)
    if not commit or not bus:
        print(f"{NAME}: {COMMIT_VARIABLE} and {BUS_VARIABLE} name the commit and the bus", file=sys.stderr)
        return 1
    try:
        results, wake, exit_code = run_canary(env, secrets=secrets, ssm=ssm, run=run, watch=watch)
    except (BotoCoreError, ClientError) as error:
        # The canary could not measure: it says so, as not measured, rather than sending nothing.
        print(f"{NAME}: the canary did not run ({type(error).__name__})", file=sys.stderr)
        results, wake, exit_code = None, None, 1
    results = ended(results, exit_code)
    entry = measurement(results, wake, commit=commit, bus=bus, now=began)
    send(entry, _client("events") if events is None else events)
    verdict = verdict_of(results, wake)
    print(f"{NAME}: {MEASUREMENT} {verdict.name.lower()} sent for {run_label(began)}")
    if results is None or verdict is Verdict.PASS:
        return exit_code
    return exit_code or 1


if __name__ == "__main__":
    sys.exit(main())
