"""The session gauge the scaler sends on every run (`rhi_scaler.metrics`), from the switch it reads
(`rhi_scaler.aws_io.read_session`)."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

from rhi_common.pool_config import parse_pools
from rhi_common.records import Mode, RecordError, SessionSwitch
from rhi_scaler.aws_io import Clients, TableNames, read_session
from rhi_scaler.metrics import SESSION_ON, UNREAD, DecisionCounts, Unread, decode, encode, fleet_series
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

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
    body = encode(fleet_series(inputs, decision, DecisionCounts(), milliseconds, session=session))
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
