"""What the tests of the jobs that run in a booked session share: the pool-state table and the
gateway's status, as stand-ins. pytest finds the fixtures by their names, so a test file imports
nothing from here.
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
from typing import Any

import boto3
import pytest

from rhi_common.records import ScalerNote, Session

POOL = "coursework"


class PoolState:
    """Stands for the pool-state table and for the SDK session that opens it: the records it was
    given, read by their keys, and each key that was read."""

    # What a job's environment says of the session, added to the job's own variables.
    environ = {"RHI_SPIKE_POOL_STATE_TABLE": "pool-state", "RHI_SPIKE_RUN_MINUTES": "90"}  # noqa: RUF012 - never changed

    def __init__(self) -> None:
        self.items: dict[tuple[object, object], dict[str, Any]] = {}
        self.reads: list[tuple[str, str]] = []

    def hold(self, *records: Session | ScalerNote) -> None:
        for record in records:
            item = record.to_item()
            self.items[item["pool"], item["record"]] = item

    def book(self, *, in_service: int = 1, minutes: int = 600, now: int | None = None, pool: str = POOL) -> None:
        """A session of the pool, active for `minutes` from `now`, with `in_service` instances in service."""
        now = int(time.time()) if now is None else now
        self.hold(
            Session(pool, in_service, now - 60, now + minutes * 60, "an-admin", now - 60),
            ScalerNote(pool, in_service, None, now, in_service=in_service),
        )

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool) -> dict[str, Any]:
        assert ConsistentRead
        self.reads.append((Key["pool"], Key["record"]))
        item = self.items.get((Key["pool"], Key["record"]))
        return {} if item is None else {"Item": item}

    # The SDK session: its credentials, which nothing here signs with, and the table by its name.

    def get_credentials(self) -> None:
        return None

    def resource(self, service: str) -> PoolState:
        assert service == "dynamodb"
        return self

    def Table(self, name: str) -> PoolState:
        assert name == self.environ["RHI_SPIKE_POOL_STATE_TABLE"]
        return self


@pytest.fixture
def pool_state(monkeypatch: pytest.MonkeyPatch) -> PoolState:
    """The pool-state table a job reads with its own credentials, holding nothing until a test books."""
    state = PoolState()
    monkeypatch.setattr(boto3, "Session", lambda: state)
    return state


@pytest.fixture
def gateway_requests(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every request sent with urllib, which is how a job asks the gateway: its method and its
    address. The status reads the pool cold once, then ready, so a job has to wait for it."""
    sent: list[tuple[str, str]] = []
    states = iter(["cold"])

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        sent.append((request.get_method(), request.full_url))
        return io.BytesIO(json.dumps({"pools": [{"model": POOL, "state": next(states, "ready")}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent
