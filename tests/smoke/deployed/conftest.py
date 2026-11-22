"""What the deployed-only smoke tests read, beside the endpoint (system design, sections 4.3 and 8.2).

These tests are collected only when `RHI_SMOKE_ENDPOINT` is given, which only the pipeline's
verification step does: without it `pytest_pycollect_makeitem`, below, makes no test of anything
in this folder, however the folder or a file of it was named to pytest. Nothing is skipped, so a
run with no endpoint reports no skipped test. That step runs inside the VPC, with a role that may
query the workspace and read the pools' release parameters, and gives:

| Variable | Holds |
| --- | --- |
| `RHI_SMOKE_WORKSPACE_URL` | The Prometheus workspace's query address: the workspace's own endpoint, under which `api/v1/query` answers |
| `RHI_SMOKE_RELEASE_PARAMETERS` | Each pool's release parameter, as JSON from the pool's name to the parameter's name |

A variable that is missing fails the test that needs it, by the variable's name. The AWS SDK takes
its credentials and its region from the step, as everywhere: nothing here names either.

**The pool is in service.** The pool's metrics and its release are reported by instances, so the
tests that read them take `serving`, which sends the pool one stream first. In a run of the whole
folder these tests come after `../test_smoke.py` (`../conftest.py` orders them so), and the pool
is in service already.

**When the run began.** The fixture `run_started` is the time this run's tests were collected,
before any of them sent the pool a stream: a test that must account for every report of the run,
the reports of the stream that woke the pool included, reads the workspace from then on. In a
run of the whole folder that stream is `../test_smoke.py`'s first.

**The workspace lags.** A collector scrapes every 15 seconds and then pushes, and an instant query
still returns a series for 5 minutes after its last sample. So a read is repeated until it holds
what the test asks for, or a deadline passes.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import boto3
import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from rhi_lifecycle import launch, release

ENDPOINT_VARIABLE = "RHI_SMOKE_ENDPOINT"

WORKSPACE_URL_VARIABLE = "RHI_SMOKE_WORKSPACE_URL"
RELEASE_PARAMETERS_VARIABLE = "RHI_SMOKE_RELEASE_PARAMETERS"

# The name Managed Prometheus signs its requests under.
SIGNING_SERVICE = "aps"
# Between two reads of the workspace.
POLL_SECONDS = 10.0


def pytest_pycollect_makeitem() -> list[pytest.Item] | None:
    """While no endpoint is given, nothing in this folder's files is a test: left out, never
    skipped. pytest asks this file about its own folder only."""
    return None if os.environ.get(ENDPOINT_VARIABLE) else []


# When this run's tests were collected, kept for `run_started`.
RUN_STARTED = pytest.StashKey[float]()


def pytest_collection_finish(session: pytest.Session) -> None:
    """Records when the run began: once its tests are collected, before any of them runs."""
    session.stash[RUN_STARTED] = time.time()


def given(variable: str) -> str:
    """The variable's value. A missing one fails the test by the variable's name."""
    value = os.environ.get(variable)
    assert value, f"{variable} is unset or empty: the deployed-only tests need it"
    return value


@dataclass(frozen=True)
class Workspace:
    """The Prometheus workspace, queried with requests the step's credentials sign."""

    url: str
    http: httpx.Client
    session: Any

    def series(self, selector: str, *, at: float | None = None) -> list[dict[str, str]]:
        """The labels of every series an instant query of `selector` returns now, or evaluated at
        `at`, a Unix time, when it is given."""
        url = f"{self.url}/api/v1/query"
        body = urlencode({"query": selector} if at is None else {"query": selector, "time": f"{at:.3f}"})
        request = AWSRequest(
            method="POST", url=url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        SigV4Auth(self.session.get_credentials(), SIGNING_SERVICE, self.session.region_name).add_auth(request)
        answer = self.http.post(url, content=body, headers=dict(request.headers))
        assert answer.status_code == 200, f"the workspace answered a query with {answer.status_code}"
        return [sample["metric"] for sample in answer.json()["data"]["result"]]

    def series_when(
        self, selector: str, accepted: Callable[[list[dict[str, str]]], bool], *, seconds: float
    ) -> list[dict[str, str]]:
        """The series of `selector`, read again until `accepted` takes them or `seconds` have passed."""
        deadline = time.monotonic() + seconds
        while True:
            series = self.series(selector)
            if accepted(series) or time.monotonic() >= deadline:
                return series
            time.sleep(POLL_SECONDS)

    def missing(self, selectors: Sequence[str], *, seconds: float) -> list[str]:
        """Those of `selectors` that still return no series once `seconds` have passed."""
        deadline = time.monotonic() + seconds
        while True:
            missing = [selector for selector in selectors if not self.series(selector)]
            if not missing or time.monotonic() >= deadline:
                return missing
            time.sleep(POLL_SECONDS)


@pytest.fixture(scope="session")
def run_started(request: pytest.FixtureRequest) -> float:
    """The Unix time this run began, before any of its tests sent the pool a stream."""
    return request.session.stash[RUN_STARTED]


@pytest.fixture(scope="module")
def workspace(stack: Any) -> Workspace:
    return Workspace(url=given(WORKSPACE_URL_VARIABLE).rstrip("/"), http=stack.http, session=boto3.session.Session())


@pytest.fixture(scope="module")
def deployed_release(stack: Any) -> tuple[str, str]:
    """The image digest and the model revision of the release the pool's parameter holds now, as
    a lifecycle agent reports them (`rhi_lifecycle.launch.identity`)."""
    parameters = json.loads(given(RELEASE_PARAMETERS_VARIABLE))
    assert stack.pool in parameters, f"{RELEASE_PARAMETERS_VARIABLE} names no parameter for the pool under test"
    current = release.read_current(boto3.client("ssm"), parameters[stack.pool])
    assert current is not None, "the pool's release parameter cannot be read, or does not hold a release"
    return launch.identity(current.release)


@pytest.fixture(scope="module")
def serving(stack: Any) -> None:
    """The pool under test, in service: it has answered one stream of this module."""
    body = {"model": stack.pool, "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "Hello."}]}
    stream = stack.stream("/v1/messages", body)
    assert stream.status == 200
    assert stream.events and stream.events[-1].name == "message_stop", "the pool did not answer a stream"
