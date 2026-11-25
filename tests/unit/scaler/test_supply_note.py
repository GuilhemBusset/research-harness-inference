"""What a pool was asked, what waits and why, in the scaler's note (system design, section 5.4).

Each run goes the whole way: `rhi_scaler.aws_io.read` against a scripted group, `decide`, then
`apply`. The two tables are moto's, so a run reads the memory the run before it stored.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.stub import Stubber
from moto import mock_aws

from rhi_common.pool_config import parse_pools
from rhi_common.records import LaunchFailure, ScalerNote, Session
from rhi_scaler.aws_io import Clients, PoolSettings, TableNames, apply, launch_history, memory_to_item, read
from rhi_scaler.decide import decide
from rhi_scaler.model import Memory

NOW = 1_800_000_000
GROUP = "coursework-group"
TARGETS = "coursework-targets"
TABLES = TableNames(heartbeats="heartbeats-table", pool_state="pool-state-table")
POOL = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "prices": {"g6e.xlarge": 1.86},
            "max_instances": 4,
            "warm_instances": 0,
            "wake": "scheduled-only",
            "target_concurrency": 8,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]
SETTINGS = PoolSettings(config=POOL, group=GROUP, target_group=TARGETS)
# The session that asks the pool for three instances, through every run below.
SESSION = Session("coursework", 3, NOW - 600, NOW + 7200, set_by="instructor", set_at=NOW - 900)
# The instances in service of a pool that waits for two of its three, and of one that has them all.
ONE = ("instance-a",)
ALL = ("instance-a", "instance-b", "instance-c")
MEMORY_KEY = {"pool": "coursework", "record": "memory"}
NOTE_KEY = {"pool": "coursework", "record": "note"}

# Synthetic status messages: each carries the error code, or the words, its cause is told from.
NO_CAPACITY = "Could not launch On-Demand Instances. InsufficientInstanceCapacity - no capacity in the zone"
OVER_QUOTA = "Could not launch On-Demand Instances. VcpuLimitExceeded - over the family's limit"
OVER_QUOTA_IN_WORDS = "the launch is over the account's vCPU limit"
# The same two causes as Auto Scaling words them when the activity carries no error code.
NO_CAPACITY_IN_WORDS = "we do not have sufficient example-gpu capacity in the zone asked"
NO_SPOT_CAPACITY = "there is no Spot capacity available for the request"
NO_QUOTA_LEFT = "the quota allows for 0 more running instance(s). 2 requested"
ALREADY_RUNNING = "4 instance(s) are already running"
ABANDONED = "the instance's launch lifecycle action was abandoned"
NO_IMAGE = "the image of the launch template is not there"


def launch(status: str, ended: int, message: str = "") -> dict[str, Any]:
    at = datetime.fromtimestamp(ended, UTC)
    activity = {"Description": "Launching a new EC2 instance: instance-x", "StatusCode": status, "StartTime": at, "EndTime": at}
    return {**activity, "StatusMessage": message} if message else activity


class Pool:
    """The pool's group, scripted run by run, with the group asking for three instances, and the
    two tables, which keep what each run wrote."""

    def __init__(self) -> None:
        options = {"region_name": "us-east-1", "aws_access_key_id": "stub", "aws_secret_access_key": "stub"}
        self.clients = Clients(
            autoscaling=boto3.client("autoscaling", **options),
            elbv2=boto3.client("elbv2", **options),
            dynamodb=boto3.client("dynamodb", **options),
            cloudwatch=None,
        )
        self.autoscaling = Stubber(self.clients.autoscaling)
        self.elbv2 = Stubber(self.clients.elbv2)
        keys = [("pool", "HASH"), ("record", "RANGE")], [("pool", "HASH"), ("source", "RANGE")]
        for table, schema in zip((TABLES.pool_state, TABLES.heartbeats), keys, strict=True):
            self.clients.dynamodb.create_table(
                TableName=table,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in schema],
                KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in schema],
            )
        self.put(SESSION.to_item())

    def put(self, item: Mapping[str, Any]) -> None:
        stored = {name: TypeSerializer().serialize(value) for name, value in item.items()}
        self.clients.dynamodb.put_item(TableName=TABLES.pool_state, Item=stored)

    def stored(self, key: Mapping[str, str]) -> dict[str, Any]:
        """An item of the pool-state table, as it is stored."""
        found = self.clients.dynamodb.get_item(
            TableName=TABLES.pool_state, Key={name: {"S": value} for name, value in key.items()}
        )
        return {name: TypeDeserializer().deserialize(value) for name, value in found["Item"].items()}

    def run(
        self,
        now: int,
        *,
        in_service: Sequence[str] = (),
        starting: Sequence[str] = (),
        draining: Sequence[str] = (),
        activities: Sequence[dict[str, Any]] = (),
        launch_suspended: bool = False,
    ) -> ScalerNote:
        """One run at `now`, of a group that holds these instances and this history: the note it
        stored. No call that changes the group is queued, so one would fail the run."""
        states = {"InService": in_service, "Pending": starting, "Terminating:Wait": draining}
        group = {
            "AutoScalingGroupName": GROUP,
            "MinSize": 0,
            "MaxSize": 4,
            "DesiredCapacity": 3,
            "DefaultCooldown": 300,
            "AvailabilityZones": ["us-east-1a"],
            "HealthCheckType": "ELB",
            "CreatedTime": datetime(2026, 1, 1, tzinfo=UTC),
            "Instances": [
                {"InstanceId": name, "LifecycleState": state, "AvailabilityZone": "us-east-1a", "HealthStatus": "Healthy", "ProtectedFromScaleIn": True}
                for state, names in states.items()
                for name in names
            ],
            "SuspendedProcesses": [
                {"ProcessName": name, "SuspensionReason": "by the stack"}
                for name in ("AZRebalance", *(("Launch",) if launch_suspended else ()))
            ],
        }
        named = {"AutoScalingGroupName": GROUP}
        history = [{"ActivityId": f"activity-{n}", **named, "Cause": "the scaler", **a} for n, a in enumerate(activities)]
        self.autoscaling.add_response("describe_auto_scaling_groups", {"AutoScalingGroups": [group]}, {"AutoScalingGroupNames": [GROUP]})
        self.autoscaling.add_response("describe_scaling_activities", {"Activities": history}, named)
        health = [{"Target": {"Id": name}, "TargetHealth": {"State": "healthy"}} for name in in_service]
        self.elbv2.add_response("describe_target_health", {"TargetHealthDescriptions": health}, {"TargetGroupArn": TARGETS})
        this = read(self.clients, TABLES, [SETTINGS], now)
        apply(self.clients, TABLES, this, decide(this.inputs))
        self.autoscaling.assert_no_pending_responses()
        return ScalerNote.from_item(self.stored(NOTE_KEY))


@pytest.fixture
def pool() -> Iterator[Pool]:
    with mock_aws():
        pool = Pool()
        with pool.autoscaling, pool.elbv2:
            yield pool


def test_what_waits_is_what_was_asked_and_has_not_come_since_the_first_run_that_saw_it(pool: Pool) -> None:
    # Three asked, one in service and one starting: one waits, from this run on.
    first = pool.run(NOW, in_service=["instance-a"], starting=["instance-b"])
    assert (first.requested, first.capacity_asked) == (3, 3)
    assert (first.in_service, first.starting, first.draining) == (1, 1, 0)
    assert (first.waiting, first.waiting_since) == (1, NOW)
    # The time is kept in the pool's memory item, under a name of its own.
    assert pool.stored(MEMORY_KEY)["short_since"] == NOW

    # The next run sees the same wait: it began at the first.
    second = pool.run(NOW + 60, in_service=["instance-a"], starting=["instance-b"])
    assert (second.waiting, second.waiting_since, second.written_at) == (1, NOW, NOW + 60)
    assert pool.stored(MEMORY_KEY)["short_since"] == NOW

    # Every instance asked for has come, one of them still starting: nothing waits, and the note
    # and the memory drop the time. An instance that leaves is counted apart.
    third = pool.run(NOW + 120, in_service=["instance-a", "instance-b"], starting=["instance-c"], draining=["instance-d"])
    assert (third.in_service, third.starting, third.draining) == (2, 1, 1)
    assert (third.waiting, third.waiting_since, third.launch_failure) == (0, None, None)
    assert "waiting_since" not in pool.stored(NOTE_KEY)
    assert "short_since" not in pool.stored(MEMORY_KEY)

    # A wait that begins again is timed from the run that sees it, not from the wait before.
    fourth = pool.run(NOW + 180, in_service=["instance-a", "instance-b"])
    assert (fourth.waiting, fourth.waiting_since) == (1, NOW + 180)


def test_more_instances_than_were_asked_for_is_nothing_waiting(pool: Pool) -> None:
    note = pool.run(NOW, in_service=["instance-a", "instance-b"], starting=["instance-c", "instance-d"])
    assert (note.capacity_asked, note.waiting, note.waiting_since) == (3, 0, None)


@pytest.mark.parametrize(
    ("message", "cause"),
    [
        (NO_CAPACITY, LaunchFailure.INSUFFICIENT_CAPACITY),
        (NO_CAPACITY_IN_WORDS, LaunchFailure.INSUFFICIENT_CAPACITY),
        (NO_SPOT_CAPACITY, LaunchFailure.INSUFFICIENT_CAPACITY),
        (OVER_QUOTA, LaunchFailure.QUOTA),
        (OVER_QUOTA_IN_WORDS, LaunchFailure.QUOTA),
        (NO_QUOTA_LEFT, LaunchFailure.QUOTA),
        (ALREADY_RUNNING, LaunchFailure.QUOTA),
        (ABANDONED, LaunchFailure.LAUNCH_FAILED),
        (NO_IMAGE, LaunchFailure.LAUNCH_FAILED),
    ],
)
def test_the_cause_is_that_of_the_last_launch_that_failed_since_the_wait_began(
    pool: Pool, message: str, cause: LaunchFailure
) -> None:
    # A launch that failed before the pool waited says nothing of this wait.
    before = [launch("Failed", NOW - 300, NO_CAPACITY), launch("Cancelled", NOW - 200, ABANDONED)]
    assert pool.run(NOW, activities=before, in_service=ONE).launch_failure is None
    # One that failed since does, and so does one that was cancelled.
    for status in ("Failed", "Cancelled"):
        history = [launch("Successful", NOW + 50), launch(status, NOW + 40, message), *before]
        assert pool.run(NOW + 60, activities=history, in_service=ONE).launch_failure is cause
    # The last one that failed gives the cause, wherever the history lists it.
    newer = [*history, launch("Failed", NOW + 100, NO_IMAGE)]
    assert pool.run(NOW + 120, activities=newer, in_service=ONE).launch_failure is LaunchFailure.LAUNCH_FAILED


def test_the_last_launch_that_failed_is_the_one_that_completed_last_even_within_one_second() -> None:
    second = datetime.fromtimestamp(NOW, UTC)

    def failed(started_ms: int, ended_ms: int, message: str) -> dict[str, Any]:
        times = {"StartTime": second + timedelta(milliseconds=started_ms), "EndTime": second + timedelta(milliseconds=ended_ms)}
        return {**launch("Failed", NOW, message), **times}

    last, first = failed(600, 900, OVER_QUOTA), failed(0, 200, NO_CAPACITY)
    # Whatever order the history lists them in: Auto Scaling's is by start, the latest first.
    for history in ([last, first], [first, last]):
        found = launch_history(history, None).last_failed
        assert found is not None and (found.at, found.cause) == (NOW, LaunchFailure.QUOTA)
    # A launch that started first can complete last: it is then the last that failed.
    outlasted = failed(700, 800, NO_IMAGE)
    found = launch_history([outlasted, last, first], None).last_failed
    assert found is not None and found.cause is LaunchFailure.QUOTA


def test_a_launch_that_found_no_capacity_is_told_with_or_without_its_error_code(pool: Pool) -> None:
    # The time the note carries for the gateway is told as the cause is.
    for message in (NO_CAPACITY, NO_CAPACITY_IN_WORDS, NO_SPOT_CAPACITY):
        history = launch_history([launch("Failed", NOW - 120, message), launch("Failed", NOW - 60, NO_QUOTA_LEFT)], None)
        assert history.no_capacity_at == NOW - 120
    note = pool.run(NOW, activities=[launch("Failed", NOW - 120, NO_CAPACITY_IN_WORDS)], in_service=ALL)
    assert (note.no_capacity_at, note.launch_failure) == (NOW - 120, None)


def test_each_cause(pool: Pool) -> None:
    # The pool waits from this run on, and a launch that failed before it gives no cause.
    assert pool.run(NOW, activities=[launch("Failed", NOW - 300, NO_CAPACITY)], in_service=ONE).launch_failure is None
    # Each of the three causes a launch gives, from the activity that failed since the wait began.
    for minute, (message, cause) in enumerate(
        [
            (NO_CAPACITY, LaunchFailure.INSUFFICIENT_CAPACITY),
            (OVER_QUOTA, LaunchFailure.QUOTA),
            (NO_IMAGE, LaunchFailure.LAUNCH_FAILED),
        ],
        start=1,
    ):
        now = NOW + 60 * minute
        note = pool.run(now, activities=[launch("Failed", now - 30, message)], in_service=ONE)
        assert (note.waiting, note.launch_failure) == (2, cause)
        # What the note stores is the word, and nothing of the activity's message.
        assert pool.stored(NOTE_KEY)["launch_failure"] == cause.value

    # The fourth, from the scaler's own mark: whatever the group's last launch met.
    failed = [launch("Failed", NOW + 270, NO_CAPACITY)]
    memory = Memory(launch_suspended=True, suspended_at=NOW + 280, short_since=NOW)
    pool.put(memory_to_item("coursework", memory))
    note = pool.run(NOW + 300, activities=failed, launch_suspended=True, in_service=ONE)
    assert (note.waiting, note.waiting_since, note.launch_failure) == (2, NOW, LaunchFailure.LAUNCHES_SUSPENDED)

    # And no cause while nothing waits, for the same failed launch and the same mark.
    note = pool.run(NOW + 360, activities=failed, launch_suspended=True, in_service=ALL)
    assert (note.waiting, note.launch_failure) == (0, None)
    assert "launch_failure" not in pool.stored(NOTE_KEY)
    pool.put(memory_to_item("coursework", Memory()))
    assert pool.run(NOW + 420, activities=[launch("Failed", NOW + 400, NO_CAPACITY)], in_service=ALL).launch_failure is None
