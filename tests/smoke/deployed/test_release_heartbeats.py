"""The release each instance of the woken pool serves, as its heartbeat reports it (system design,
sections 4.1, 4.3 and 8.2, step 6).

A lifecycle agent reports the release its instance serves in two places: the labels of
`rhi_lifecycle_release_info`, which `test_gpu_release.py` reads, and the instance's row of the
heartbeats table (`rhi_common.records.InstanceHeartbeat`), which this test reads. The metric
reaches the workspace through the collector, which can hold a report back for minutes, so a
partner's report can arrive while the woken instance's is still queued. The row has no such delay:
the agent writes it itself, at each stage of its launch and every 5 seconds once it serves, one row
per instance, and a consistent read returns it as last written.

The test wakes the pool it is given and completes a stream through it (`serving`), then reads the
rows of the pool's instances from the table `RHI_VERIFY_HEARTBEATS_TABLE` names, which the
verification step gives and its role may query. Every row written since the run began
(`run_started`) is an instance launched or in service during the run, the one that served the wake
among them, and each must carry the deployed release: one that carries another fails the test,
whatever the other rows say. The test passes once an instance in service has written its row since
the stream ended.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any

import boto3
import pytest

from rhi_common.records import INSTANCE_SOURCE, InstanceHeartbeat
from rhi_lifecycle.serving import STAGE as SERVING

HEARTBEATS_TABLE_VARIABLE = "RHI_VERIFY_HEARTBEATS_TABLE"
# The longest an instance in service is waited for, after the stream: several heartbeats.
SERVED_SECONDS = 60.0
# Between two reads of the table.
READ_SECONDS = 5.0


@dataclass(frozen=True)
class Heartbeats:
    """The rows of one pool's instances, in the heartbeats table `table`, as the AWS SDK's DynamoDB
    resource gives it."""

    table: Any
    pool: str

    def rows(self) -> list[InstanceHeartbeat]:
        """Each instance's row as it is now: a consistent read of every page."""
        request: dict[str, Any] = {
            "KeyConditionExpression": "#pool = :pool AND begins_with(#source, :instance)",
            "ExpressionAttributeNames": {"#pool": "pool", "#source": "source"},
            "ExpressionAttributeValues": {":pool": self.pool, ":instance": INSTANCE_SOURCE},
            "ConsistentRead": True,
        }
        found: list[InstanceHeartbeat] = []
        while True:
            answer = self.table.query(**request)
            found += [InstanceHeartbeat.from_item(item) for item in answer.get("Items", [])]
            if "LastEvaluatedKey" not in answer:
                return found
            request["ExclusiveStartKey"] = answer["LastEvaluatedKey"]


@pytest.fixture(scope="module")
def heartbeats(stack: Any) -> Heartbeats:
    """The rows of the pool under test. A missing table name fails the test by the variable's name."""
    table = os.environ.get(HEARTBEATS_TABLE_VARIABLE)
    assert table, f"{HEARTBEATS_TABLE_VARIABLE} is unset or empty: the deployed-only tests need it"
    return Heartbeats(table=boto3.resource("dynamodb").Table(table), pool=stack.pool)


@pytest.mark.smoke_id("release-heartbeat")
@pytest.mark.usefixtures("serving")
def test_every_instance_of_the_run_heartbeats_the_deployed_release(
    heartbeats: Heartbeats, deployed_release: tuple[str, str], run_started: float
) -> None:
    # A row's time is in whole seconds, cut down: a row of the run's first second counts.
    since, done = math.floor(run_started), math.floor(time.time())
    deadline = time.monotonic() + SERVED_SECONDS
    while True:
        rows = [row for row in heartbeats.rows() if row.written_at >= since]
        others = sorted(
            f"{row.instance} ({row.stage}): {row.image_digest} {row.model_revision}"
            for row in rows
            if (row.image_digest, row.model_revision) != deployed_release
        )
        assert not others, f"an instance of the pool heartbeats another release during the run: {others}"
        if any(row.stage == SERVING and row.written_at >= done for row in rows):
            return
        assert time.monotonic() < deadline, "no instance of the pool in service heartbeats the deployed release"
        time.sleep(READ_SECONDS)
