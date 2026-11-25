"""The admin's commands on the pools' sessions (`rhi_admin.session`), through the CLI.

The pool-state table, STS and SSM are moto's, and SSM holds the example configuration in the zone
`ZONE`. CloudFormation and Lambda are stand-ins: the first answers for the deployed stack's
resources by logical ID, the second gives the scaler's settings and counts the runs it is asked.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import io
import json
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from rhi import config
from rhi_admin import main
from rhi_admin import session as admin_session
from rhi_admin.roster import AdminError
from rhi_common import sessions as rules
from rhi_common.records import LaunchFailure, ScalerNote, Session

# 2027-01-15T08:00 UTC, which the clock of `ZONE` shows as 09:00.
NOW = 1_800_000_000
HOUR = 3600
ZONE = "Europe/Zurich"
MAX_HOURS = 12
SCALER = "deployed-scaler"
TABLE = "deployed-pool-state"
# moto's caller: its account, the placeholder one, is not part of who wrote a session.
WHO = "user/moto"
# Each deployed pool's `max_instances`, in the scaler's settings. The configuration in SSM gives
# both pools 2: the settings of the running scaler are what bounds a session.
MOST = {"coursework": 4, "mock": 2}


def _entry(max_instances: int) -> dict[str, Any]:
    return {
        "engine": "mock",
        "instance_types": ["g6e.xlarge"],
        "prices": {"g6e.xlarge": 1.86},
        "max_instances": max_instances,
        "warm_instances": 1,
        "wake": "scheduled-only",
        "target_concurrency": 8,
        "context_length": 32_768,
        "largest_output": 4_096,
    }


SETTINGS = json.dumps({"pools": {name: {"entry": _entry(most)} for name, most in MOST.items()}})


class _Stack:
    """CloudFormation: the deployed resources by logical ID. While `parked`, the stack holds no scaler."""

    def __init__(self) -> None:
        self.parked = False

    def describe_stack_resource(self, **request: str) -> dict[str, Any]:
        known = {main.POOL_STATE_LOGICAL_ID: TABLE, main.SCALER_FUNCTION_LOGICAL_ID: SCALER}
        logical_id = request["LogicalResourceId"]
        if self.parked and logical_id == main.SCALER_FUNCTION_LOGICAL_ID:
            raise ClientError({"Error": {"Code": "ValidationError"}}, "DescribeStackResource")
        return {"StackResourceDetail": {"PhysicalResourceId": known.get(logical_id, f"deployed-{logical_id}")}}


class _Functions:
    """Lambda: the scaler's settings, and its runs, throttled for the reason `throttle` while it is set."""

    def __init__(self) -> None:
        self.runs, self.throttle = 0, ""

    def get_function_configuration(self, **_: str) -> dict[str, Any]:
        return {"Environment": {"Variables": {"RHI_SCALER_SETTINGS": SETTINGS}}}

    def invoke(self, **_: object) -> dict[str, Any]:
        self.runs += 1
        if self.throttle:
            raise ClientError({"Error": {"Code": "TooManyRequestsException"}, "Reason": self.throttle}, "Invoke")
        return {"StatusCode": 200, "Payload": io.BytesIO(b'{"pools": []}')}


class _Tables:
    """DynamoDB: the one table object, so that a test can step between a command's read and its write."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def Table(self, _: str) -> Any:
        return self.table


@dataclasses.dataclass
class Deployed:
    """The CLI against the deployed stack."""

    admin: main.Admin
    table: Any
    functions: _Functions
    stack: _Stack

    def run(self, *argv: str, now: int = NOW) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        status = main.run(["session", *argv], self.admin, now=now, out=out, err=err)
        return status, out.getvalue(), err.getvalue()

    def session(self, pool: str = "coursework") -> Session | None:
        item = self.table.get_item(Key={"pool": pool, "record": "session"}, ConsistentRead=True).get("Item")
        return None if item is None else Session.from_item(item)


class _Session:
    def __init__(self, real: Any, stubbed: dict[str, Any]) -> None:
        self.real, self.stubbed = real, stubbed

    def client(self, name: str) -> Any:
        return self.stubbed.get(name) or self.real.client(name)

    def resource(self, name: str) -> Any:
        return self.stubbed[name]


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Deployed]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        real = boto3.session.Session()
        keys = [("pool", "HASH"), ("record", "RANGE")]
        table = real.resource("dynamodb").create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in keys],
            KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in keys],
        )
        example = config.load({"RHI_CONFIG": "example"})
        assert (example.default_pool, example.session_max_hours) == ("coursework", MAX_HOURS)
        config.write_ssm(dataclasses.replace(example, time_zone=ZONE), real.client("ssm"))
        functions, stack = _Functions(), _Stack()
        stubbed = {"cloudformation": stack, "lambda": functions, "dynamodb": _Tables(table)}
        yield Deployed(main.connect(_Session(real, stubbed)), table, functions, stack)


def _bounds(pool: str = "coursework", who: str = WHO) -> dict[str, Any]:
    return {"set_by": who, "max_instances": MOST[pool], "session_max_hours": MAX_HOURS}


def test_times_and_lengths() -> None:
    def time(text: str) -> int:
        return admin_session.read_time(text, time_zone=ZONE, now=NOW)

    # The zone's clock shows 09:00: 08:30 has passed today and is tomorrow's, 09:30 is today's.
    assert time("08:30") == NOW + 23 * HOUR + 1800
    assert time("09:30") == NOW + 1800
    assert time("09:00") == NOW
    # A date and a time are the zone's, one hour ahead of UTC on that day.
    assert time("2027-01-15T18:00") == NOW + 9 * HOUR
    assert admin_session.show_time(NOW + 9 * HOUR, time_zone=ZONE) == "2027-01-15T18:00"

    assert admin_session.read_length("90m") == 90 * 60
    assert admin_session.read_length("6h") == 6 * HOUR

    for text in ("25:00", "9h", "2027-01-15", "2027-02-30T10:00", "tomorrow"):
        with pytest.raises(AdminError):
            time(text)
    for text in ("6", "1.5h", "h", "6 h", "2d"):
        with pytest.raises(AdminError):
            admin_session.read_length(text)


def _utc(text: str) -> int:
    return int(datetime.datetime.fromisoformat(f"{text}+00:00").timestamp())


def test_a_time_is_one_the_clock_shows_on_the_days_it_changes() -> None:
    # The clock is set back on 2027-10-31, from 03:00 to 02:00: it shows 02:30 twice, at 00:30 and
    # at 01:30 UTC. Once the first has passed, the next is the second, not the day after's.
    first, second = _utc("2027-10-31T00:30"), _utc("2027-10-31T01:30")
    for now, shown in ((first - 900, first), (first + 900, second), (second + 900, _utc("2027-11-01T01:30"))):
        assert admin_session.read_time("02:30", time_zone=ZONE, now=now) == shown
        dated = admin_session.read_time("2027-10-31T02:30", time_zone=ZONE, now=now)
        assert dated == (shown if shown <= second else first)

    # It is set forward on 2027-03-28, from 02:00 to 03:00, and never shows that day's 02:30: the
    # next 02:30 is the day after's, and that day's, written with its date, is refused.
    now = _utc("2027-03-28T00:00")
    assert admin_session.read_time("02:30", time_zone=ZONE, now=now) == _utc("2027-03-29T00:30")
    assert admin_session.read_time("03:30", time_zone=ZONE, now=now) == _utc("2027-03-28T01:30")
    with pytest.raises(AdminError):
        admin_session.read_time("2027-03-28T02:30", time_zone=ZONE, now=now)
    assert admin_session.read_time("2027-03-28T03:30", time_zone=ZONE, now=now) == _utc("2027-03-28T01:30")


def test_start_set_and_stop_write_the_session_the_shared_rules_give(deployed: Deployed) -> None:
    # Three instances: within the 4 of the scaler's settings. The default pool is the configuration's.
    assert deployed.run("start", "3", "--for", "6h")[0] == 0
    booked = rules.started(
        None, pool="coursework", instances=3, starts_at=NOW, ends_at=NOW + 6 * HOUR, now=NOW, **_bounds()
    )
    assert deployed.session() == booked
    assert deployed.functions.runs == 1

    # 12:00 in the zone is three hours from `NOW`. What is not given is kept.
    assert deployed.run("set", "--instances", "2", "--until", "12:00", now=NOW + 60)[0] == 0
    changed = rules.changed(booked, instances=2, ends_at=NOW + 3 * HOUR, now=NOW + 60, **_bounds())
    assert deployed.session() == changed
    # A length counts from the later of the start and the moment of the command.
    assert deployed.run("set", "--for", "90m", now=NOW + 90)[0] == 0
    changed = rules.changed(changed, ends_at=NOW + 90 + 90 * 60, now=NOW + 90, **_bounds())
    assert deployed.session() == changed

    assert deployed.run("stop", now=NOW + 120)[0] == 0
    assert deployed.session() == rules.stopped(changed, set_by=WHO, now=NOW + 120, session_max_hours=MAX_HOURS)
    assert deployed.functions.runs == 4
    assert deployed.session("mock") is None

    # Another pool, booked ahead: from 10:00 in the zone, an hour from `NOW`.
    assert deployed.run("start", "1", "--pool", "mock", "--from", "10:00", "--for", "90m")[0] == 0
    ahead = rules.started(
        None, pool="mock", instances=1, starts_at=NOW + HOUR, ends_at=NOW + HOUR + 90 * 60, now=NOW, **_bounds("mock")
    )
    assert deployed.session("mock") == ahead
    # Its start has not passed: it can be moved, and its end stays.
    assert deployed.run("set", "--pool", "mock", "--from", "09:30", now=NOW + 60)[0] == 0
    assert deployed.session("mock") == rules.changed(ahead, starts_at=NOW + 1800, now=NOW + 60, **_bounds("mock"))


def test_a_write_that_meets_a_changed_item_is_made_again_from_a_new_read(
    deployed: Deployed, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployed.run("start", "1", "--for", "2h")
    read = deployed.table.get_item
    other = rules.changed(deployed.session(), instances=4, now=NOW + 30, **_bounds(who="user/other"))

    def read_then_another_admin_writes(**request: Any) -> Any:
        item = read(**request)  # the command has read one instance
        monkeypatch.setattr(deployed.table, "get_item", read)
        deployed.table.put_item(Item=other.to_item())
        return item

    monkeypatch.setattr(deployed.table, "get_item", read_then_another_admin_writes)
    assert deployed.run("set", "--until", "12:00", now=NOW + 31)[0] == 0

    # The other admin's four instances are kept: the end is changed on the session read again.
    assert deployed.session() == rules.changed(other, ends_at=NOW + 3 * HOUR, now=NOW + 31, **_bounds())


# Each refusal: the command, whether the pool is booked first (one instance, for two hours from
# `NOW`), the moment of the command, and the shared rule's call that refuses the same.
type Refused = Callable[[Session | None], Session]
REFUSALS: dict[str, tuple[list[str], bool, int, Refused]] = {
    "a pool already booked": (
        ["start", "1", "--for", "1h"],
        True,
        NOW + 60,
        lambda current: rules.started(
            current, pool="coursework", instances=1, starts_at=NOW + 60, ends_at=NOW + 60 + HOUR, now=NOW + 60, **_bounds()
        ),
    ),
    "a start before now": (
        ["start", "1", "--from", "2027-01-15T08:30", "--for", "1h"],
        False,
        NOW,
        lambda current: rules.started(
            current, pool="coursework", instances=1, starts_at=NOW - 1800, ends_at=NOW + 1800, now=NOW, **_bounds()
        ),
    ),
    "more instances than the pool's": (
        ["start", "5", "--for", "1h"],
        False,
        NOW,
        lambda current: rules.started(
            current, pool="coursework", instances=5, starts_at=NOW, ends_at=NOW + HOUR, now=NOW, **_bounds()
        ),
    ),
    "an end not after the start": (
        ["start", "1", "--from", "10:00", "--until", "09:30"],
        False,
        NOW,
        lambda current: rules.started(
            current, pool="coursework", instances=1, starts_at=NOW + HOUR, ends_at=NOW + 1800, now=NOW, **_bounds()
        ),
    ),
    "an end not after now": (
        ["set", "--until", "2027-01-15T09:00"],
        True,
        NOW + 60,
        lambda current: rules.changed(current, ends_at=NOW, now=NOW + 60, **_bounds()),
    ),
    "an end past the longest session": (
        ["start", "1", "--for", "13h"],
        False,
        NOW,
        lambda current: rules.started(
            current, pool="coursework", instances=1, starts_at=NOW, ends_at=NOW + 13 * HOUR, now=NOW, **_bounds()
        ),
    ),
    "a change of no session": (
        ["set", "--instances", "2"],
        False,
        NOW,
        lambda current: rules.changed(current, instances=2, now=NOW, **_bounds()),
    ),
    "a start moved once it has passed": (
        ["set", "--from", "09:30"],
        True,
        NOW + 60,
        lambda current: rules.changed(current, starts_at=NOW + 1800, now=NOW + 60, **_bounds()),
    ),
}


@pytest.mark.parametrize("refusal", REFUSALS)
def test_each_refusal_exits_1_with_its_reason(deployed: Deployed, refusal: str) -> None:
    argv, booked, now, rule = REFUSALS[refusal]
    if booked:
        deployed.run("start", "1", "--for", "2h")
    before, runs = deployed.session(), deployed.functions.runs
    with pytest.raises(rules.SessionError) as reason:
        rule(before)

    status, _, err = deployed.run(*argv, now=now)

    assert status == 1
    assert str(reason.value) in err
    # Nothing was written, and the scaler was asked nothing.
    assert deployed.session() == before
    assert deployed.functions.runs == runs


def test_a_run_refused_because_one_is_under_way_exits_0(deployed: Deployed) -> None:
    _, ran, _ = deployed.run("start", "1", "--pool", "mock", "--for", "1h")
    # A run under way holds the function's one reserved invocation, and Lambda names that limit.
    deployed.functions.throttle = "ReservedFunctionConcurrentInvocationLimitExceeded"

    status, out, err = deployed.run("start", "2", "--for", "1h")

    assert (status, err) == (0, "")
    # The session is written, and the command says that the scaler is already running.
    booked = deployed.session()
    assert booked is not None and booked.instances == 2
    assert deployed.functions.runs == 2
    assert "already running" in out and "already running" not in ran


def test_a_run_throttled_for_its_rate_is_a_failure_not_a_run_under_way(deployed: Deployed) -> None:
    deployed.functions.throttle = "ReservedFunctionInvocationRateLimitExceeded"

    status, out, err = deployed.run("start", "2", "--for", "1h")

    # The session is written, and no run under way is claimed: the command fails on the run.
    assert status == 1 and "throttled" in err
    assert deployed.session() is not None and deployed.functions.runs == 1
    assert "already running" not in out + err


def test_stop_ends_one_pool_or_all_and_a_pool_with_no_session_has_nothing_to_stop(deployed: Deployed) -> None:
    # Nothing to stop is no refusal: nothing is written, and the scaler is asked nothing.
    assert deployed.run("stop")[0] == 0
    assert deployed.run("stop", "--all")[0] == 0
    assert deployed.table.scan()["Items"] == []
    assert deployed.functions.runs == 0

    deployed.run("start", "1", "--for", "2h")
    deployed.run("start", "1", "--pool", "mock", "--for", "2h")
    assert deployed.run("stop", "--pool", "mock", now=NOW + 60)[0] == 0
    coursework, mock = deployed.session(), deployed.session("mock")
    assert coursework is not None and mock is not None
    assert (coursework.ends_at, mock.ends_at) == (NOW + 2 * HOUR, NOW + 60)

    deployed.run("start", "1", "--pool", "mock", "--for", "2h", now=NOW + 120)
    runs = deployed.functions.runs
    assert deployed.run("stop", "--all", now=NOW + 180)[0] == 0
    assert [session and session.ends_at for session in (deployed.session(), deployed.session("mock"))] == [NOW + 180] * 2
    assert deployed.functions.runs == runs + 1

    # A pool that is not deployed is refused, and one pool or all is the command line's choice.
    status, _, err = deployed.run("start", "1", "--pool", "other", "--for", "1h")
    assert status == 1 and "no deployed pool" in err
    with pytest.raises(SystemExit):
        deployed.run("stop", "--pool", "mock", "--all")
    with pytest.raises(SystemExit):
        deployed.run("start", "1")


def test_a_stop_of_every_pool_that_fails_on_one_still_asks_for_a_run_of_what_it_stopped(
    deployed: Deployed, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployed.run("start", "1", "--for", "2h")
    deployed.run("start", "1", "--pool", "mock", "--for", "2h")
    before, runs, write = deployed.session("mock"), deployed.functions.runs, deployed.table.put_item

    def write_but_not_the_second_pool(**request: Any) -> Any:
        if request["Item"]["pool"] == "mock":
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem")
        return write(**request)

    monkeypatch.setattr(deployed.table, "put_item", write_but_not_the_second_pool)
    # The failure is still the command's.
    with pytest.raises(ClientError):
        deployed.run("stop", "--all", now=NOW + 60)

    stopped = deployed.session()
    assert stopped is not None and stopped.ends_at == NOW + 60
    assert deployed.session("mock") == before
    assert deployed.functions.runs == runs + 1


@pytest.mark.parametrize("command", ["on", "off"])
def test_no_switch_stands_beside_the_sessions(command: str) -> None:
    # A session is the one operating state, and `stop` ends it: the command line knows no switch.
    with pytest.raises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
        main.parser().parse_args(["session", command])
    assert refused.value.code == 2


def test_a_parked_stage_has_no_pool(deployed: Deployed) -> None:
    # Parked, the stack keeps the pool-state table and holds no scaler.
    deployed.stack.parked = True

    status, out, err = deployed.run("status")
    assert (status, err) == (0, "")
    # No pool is deployed: the header and no line.
    assert [line.split("\t")[0] for line in out.splitlines()] == ["pool"]

    # A booking is refused, and there is nothing to stop.
    status, _, err = deployed.run("start", "1", "--for", "1h")
    assert status == 1 and "no pool is deployed" in err
    assert deployed.run("stop", "--all") == (0, "", "")
    assert deployed.table.scan()["Items"] == []
    assert deployed.functions.runs == 0


def test_status_prints_the_supply(deployed: Deployed) -> None:
    deployed.run("start", "3", "--for", "6h")
    # A switch that an earlier `session on` left in the table has no line: nothing reads it.
    switch = {"pool": "_session", "record": "switch", "on": True, "set_by": "user/earlier", "set_at": NOW - HOUR}
    deployed.table.put_item(Item=switch)
    note = ScalerNote(
        pool="coursework",
        capacity_asked=3,
        no_capacity_at=NOW + 300,
        written_at=NOW + 600,
        requested=3,
        in_service=2,
        waiting=1,
        waiting_since=NOW + 300,
        launch_failure=LaunchFailure.INSUFFICIENT_CAPACITY,
    )
    deployed.table.put_item(Item=note.to_item())

    status, out, err = deployed.run("status", now=NOW + 660)

    assert (status, err) == (0, "")
    lines = [line.split("\t") for line in out.splitlines()]
    # The header, then one line for each deployed pool, and nothing else.
    coursework, mock = (dict(zip(lines[0], line, strict=True)) for line in lines[1:])
    assert (coursework["pool"], mock["pool"]) == ("coursework", "mock")

    # The session, its start and its end in the configured zone, who set it and when.
    booked = {key: coursework[key] for key in ("session", "instances", "starts_at", "ends_at", "set_by", "set_at")}
    assert booked == {
        "session": "active",
        "instances": "3",
        "starts_at": "2027-01-15T09:00",
        "ends_at": "2027-01-15T15:00",
        "set_by": WHO,
        "set_at": "2027-01-15T09:00",
    }
    # What the scaler's note says arrived: 3 requested, 1 waiting since 09:05, and why.
    arrived = {key: value for key, value in coursework.items() if key not in booked and key != "pool"}
    assert arrived == {
        "requested": "3",
        "asked": "3",
        "waiting": "1",
        "waiting_since": "2027-01-15T09:05",
        "starting": "0",
        "in_service": "2",
        "cause": "insufficient-capacity",
    }

    # A pool with no session, and no note yet.
    assert mock["session"] == "none"
    assert {mock[key] for key in ("instances", "starts_at", "ends_at", "set_by", "set_at", "waiting_since", "cause")} == {"-"}
    assert {mock[key] for key in ("requested", "asked", "waiting", "starting", "in_service")} == {"0"}

    # A session whose end has come is no session.
    deployed.run("stop", now=NOW + 700)
    lines = [line.split("\t") for line in deployed.run("status", now=NOW + 701)[1].splitlines()]
    assert lines[1][:2] == ["coursework", "none"]
