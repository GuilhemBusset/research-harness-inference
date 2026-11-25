"""The session gauge the scaler sends on every run (`rhi_scaler.metrics`), from the switch it reads
(`rhi_scaler.aws_io.read_session`)."""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.credentials import Credentials
from botocore.stub import Stubber

from rhi_common.pool_config import parse_pools
from rhi_common.records import Mode, RecordError, ScalerNote, SessionSwitch
from rhi_scaler import handler
from rhi_scaler.aws_io import Clients, TableNames, read_session
from rhi_scaler.metrics import SESSION_ON, UNREAD, DecisionCounts, RemoteWriter, Unread, decode, encode, fleet_series
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply
from rhi_scaler.settings import Settings

NOW = 1_800_000_000
TABLES = TableNames(heartbeats="heartbeats-table", pool_state="pool-state-table")
POOL = parse_pools(
    {
        "mock": {
            "engine": "mock",
            "instance_types": ["t4g.small"],
            "prices": {"t4g.small": 0.0168},
            "max_instances": 1,
            "warm_instances": 0,
            "wake": "on-demand",
            "target_concurrency": 4,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]


def switch(item: dict[str, Any] | None) -> SessionSwitch | None:
    """What a run reads of the switch when the pool-state table holds `item` under its key."""
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="stub", aws_secret_access_key="stub")
    clients = Clients(autoscaling=None, elbv2=None, dynamodb=client, cloudwatch=None)
    key = {"pool": {"S": "_session"}, "record": {"S": "switch"}}
    found = {} if item is None else {"Item": {name: TypeSerializer().serialize(v) for name, v in item.items()}}
    with Stubber(client) as stub:
        stub.add_response("get_item", found, {"TableName": TABLES.pool_state, "Key": key, "ConsistentRead": True})
        read = read_session(clients, TABLES)
        stub.assert_no_pending_responses()
    return read


def sent(
    session: SessionSwitch | None | Unread, milliseconds: int = NOW * 1000
) -> dict[str, tuple[dict[str, str], list[tuple[float, int]]]]:
    """The series of one run at `milliseconds`, by name: their labels and their samples (value, time)."""
    inputs = Inputs(now=NOW, pools=(PoolInputs(config=POOL, mode=Mode.ENABLED, supply=Supply(desired=0)),))
    decision = Decision(pools=(PoolDecision("mock", 0, (), False, Memory(), Reason.UNCHANGED),))
    notes = {"mock": ScalerNote(pool="mock", capacity_asked=0, no_capacity_at=None, written_at=NOW)}
    body = encode(fleet_series(inputs, decision, DecisionCounts(), milliseconds, notes=notes, session=session))
    out = {}
    for timeseries in decode(body).timeseries:
        labels = {label.name: label.value for label in timeseries.labels}
        out.setdefault(labels["__name__"], (labels, [(s.value, s.timestamp) for s in timeseries.samples]))
    return out


@pytest.mark.parametrize(
    ("item", "gauge"),
    [
        (SessionSwitch(on=True, set_by="instructor", set_at=NOW - 60).to_item(), 1.0),
        (SessionSwitch(on=False, set_by="instructor", set_at=NOW - 60).to_item(), 0.0),
        (None, 0.0),
    ],
    ids=["on", "off", "no record"],
)
def test_the_gauge_reads_the_switch(item: dict[str, Any] | None, gauge: float) -> None:
    labels, values = sent(switch(item))[SESSION_ON]
    assert [value for value, _ in values] == [gauge]
    # The gauge's name is its only label: no pool, and no student.
    assert labels == {"__name__": SESSION_ON}


def test_a_switch_that_cannot_be_read_sends_no_sample() -> None:
    # An item whose `on` is not a boolean is not the switch's record: neither on nor off.
    unreadable = {**SessionSwitch(on=True, set_by="instructor", set_at=NOW).to_item(), "on": "yes"}
    with pytest.raises(RecordError):
        switch(unreadable)
    assert SESSION_ON not in sent(UNREAD)


@pytest.mark.parametrize("on", [True, False], ids=["turned on", "turned off"])
def test_a_change_read_during_the_run_takes_effect_no_earlier_than_the_change(on: bool) -> None:
    # The run starts at NOW; the switch changes ten seconds later, before the run reads it.
    changed = SessionSwitch(on=on, set_by="instructor", set_at=NOW + 10)
    ((value, stamp),) = sent(changed)[SESSION_ON][1]
    assert value == (1.0 if on else 0.0)
    # Every scrape before the change still reads the state before it.
    assert stamp >= (NOW + 10) * 1000
    # A change before the run started is read at the run's time.
    earlier = SessionSwitch(on=on, set_by="instructor", set_at=NOW - 60)
    assert sent(earlier)[SESSION_ON][1] == [(value, NOW * 1000)]


@pytest.mark.parametrize("on", [True, False], ids=["turned on", "turned off"])
def test_a_change_read_during_a_run_keeps_the_scrapes_before_its_write(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    # The run starts at NOW + 60. The admin's command took its time at NOW + 48, but its write
    # commits only at NOW + 64, and the run's read of the switch returns at NOW + 66: a scrape at
    # NOW + 62 still saw the switch as before (on before an off, off before an on), and must stay so.
    clock = [float(NOW + 60)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    changed = SessionSwitch(on=on, set_by="instructor", set_at=NOW + 48).to_item()

    class DynamoDB:
        def get_item(self, **_: Any) -> dict[str, Any]:
            clock[0] = NOW + 66
            return {"Item": {name: TypeSerializer().serialize(value) for name, value in changed.items()}}

    class CloudWatch:
        def put_metric_data(self, **_: Any) -> None:
            pass

    pushes: list[bytes] = []
    writer = RemoteWriter("https://aps.example.com/api/v1/remote_write", "us-east-1", Credentials("stub", "stub"))
    writer = dataclasses.replace(writer, send=lambda url, headers, body: pushes.append(body))
    clients = Clients(autoscaling=None, elbv2=None, dynamodb=DynamoDB(), cloudwatch=CloudWatch())
    settings = Settings((), TABLES, writer.url, "us-east-1")
    handler.run_cycle(settings, clients, writer, NOW + 60, DecisionCounts(), (NOW + 60) * 1000)
    (gauge,) = [s for s in decode(pushes[-1]).timeseries if s.labels[0].value == SESSION_ON]
    assert [(s.value, s.timestamp) for s in gauge.samples] == [(1.0 if on else 0.0, (NOW + 66) * 1000)]
