"""The rows the gateway, the instances and the scaler exchange (`rhi_common.records`)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from rhi_common.records import (
    GatewayHeartbeat,
    InstanceHeartbeat,
    Mode,
    PoolState,
    RecordError,
    ScalerNote,
    SessionSwitch,
    is_fresh,
    session_is_on,
)

NOW = 1_800_000_000

GATEWAY = GatewayHeartbeat(
    task="0f3a9c", pool="coursework", in_flight=3, held=1, last_request_at=NOW - 7, written_at=NOW
)
INSTANCE = InstanceHeartbeat(
    instance="instance-1",
    pool="coursework",
    running=2,
    waiting=0,
    image_digest="sha256:" + "ab" * 32,
    model_revision="rev-1",
    stage="serving",
    written_at=NOW,
)
STATE = PoolState(pool="coursework", mode=Mode.BUDGET_LATCHED, set_by="admin", set_at=NOW - 500)
NOTE = ScalerNote(pool="mock", capacity_asked=1, no_capacity_at=NOW - 90, written_at=NOW)

RECORDS = [
    GATEWAY,
    GatewayHeartbeat(task="0f3a9c", pool="mock", in_flight=0, held=0, last_request_at=None, written_at=NOW),
    INSTANCE,
    STATE,
    *(PoolState(pool="mock", mode=mode, set_by="admin", set_at=NOW) for mode in Mode),
    NOTE,
    ScalerNote(pool="mock", capacity_asked=0, no_capacity_at=None, written_at=NOW),
    ScalerNote(pool="mock", capacity_asked=2, no_capacity_at=None, written_at=NOW, rebalance_number=7),
]


@pytest.mark.parametrize("record", RECORDS)
def test_each_record_survives_a_round_trip(
    record: GatewayHeartbeat | InstanceHeartbeat | PoolState | ScalerNote,
) -> None:
    item = record.to_item()
    # A time that is unknown is absent from the item; every other value is a string or a whole number.
    assert all(type(value) in (str, int) for value in item.values())
    assert type(record).from_item(item) == record
    # boto3 gives a number back as a Decimal.
    as_read = {name: Decimal(value) if isinstance(value, int) else value for name, value in item.items()}
    assert type(record).from_item(as_read) == record


def test_item_keys() -> None:
    note = NOTE.to_item()
    assert (note["pool"], note["record"]) == ("mock", "note")
    state = STATE.to_item()
    assert (state["pool"], state["record"]) == ("coursework", "mode")
    # The pool-state items must survive: they carry no expiry.
    assert "expires_at" not in note
    assert "expires_at" not in state

    gateway = GATEWAY.to_item()
    assert (gateway["pool"], gateway["source"]) == ("coursework", "gateway#0f3a9c")
    assert gateway["written_at"] == GATEWAY.written_at
    assert gateway["expires_at"] == GATEWAY.written_at + 3600
    instance = INSTANCE.to_item()
    assert (instance["pool"], instance["source"]) == ("coursework", "instance#instance-1")
    assert instance["expires_at"] == NOW + 3600


def test_the_modes_are_the_four_of_the_design() -> None:
    assert {mode.value for mode in Mode} == {"enabled", "scheduled-only", "disabled", "budget-latched"}


def test_a_note_without_a_rebalance_number_reads_as_0() -> None:
    # The number is 0 until the scaler first raises it, and the item holds none until then.
    assert NOTE.rebalance_number == 0
    stored = NOTE.to_item()
    assert "rebalance_number" not in stored
    assert ScalerNote.from_item(stored).rebalance_number == 0

    raised = ScalerNote.from_item({**stored, "rebalance_number": Decimal(3)})
    assert raised.rebalance_number == 3
    assert raised.to_item()["rebalance_number"] == 3


@pytest.mark.parametrize("number", [Decimal("1.5"), 1.0, "1", True, -1])
def test_a_rebalance_number_that_is_not_a_whole_number_is_refused(number: object) -> None:
    with pytest.raises(RecordError):
        ScalerNote.from_item({**NOTE.to_item(), "rebalance_number": number})


@pytest.mark.parametrize("mode", ["parked", "ENABLED", "scheduled_only", "", 1, None])
def test_a_mode_outside_the_four_is_refused(mode: object) -> None:
    item: dict[str, object] = {**STATE.to_item(), "mode": mode}
    with pytest.raises(RecordError):
        PoolState.from_item(item)
    del item["mode"]
    with pytest.raises(RecordError):
        PoolState.from_item(item)


@pytest.mark.parametrize("heartbeat", [GATEWAY, INSTANCE])
def test_a_heartbeat_is_fresh_at_20_seconds_and_stale_after(heartbeat: GatewayHeartbeat | InstanceHeartbeat) -> None:
    assert is_fresh(heartbeat, NOW)
    assert is_fresh(heartbeat, NOW + 20)
    assert not is_fresh(heartbeat, NOW + 21)


def test_an_item_of_another_record_is_refused() -> None:
    for read, item in [
        (PoolState.from_item, NOTE.to_item()),
        (ScalerNote.from_item, STATE.to_item()),
        (GatewayHeartbeat.from_item, INSTANCE.to_item()),
        (InstanceHeartbeat.from_item, GATEWAY.to_item()),
        (GatewayHeartbeat.from_item, {**GATEWAY.to_item(), "source": "gateway#"}),
    ]:
        with pytest.raises(RecordError):
            read(item)


@pytest.mark.parametrize("written_at", [Decimal("1800000000.5"), 1800000000.0, "1800000000", True, -1, None])
def test_a_time_that_is_not_a_whole_number_of_seconds_is_refused(written_at: object) -> None:
    for read, item in [
        (GatewayHeartbeat.from_item, GATEWAY.to_item()),
        (InstanceHeartbeat.from_item, INSTANCE.to_item()),
        (ScalerNote.from_item, NOTE.to_item()),
    ]:
        with pytest.raises(RecordError):
            read({**item, "written_at": written_at})
    with pytest.raises(RecordError):
        PoolState.from_item({**STATE.to_item(), "set_at": written_at})


@pytest.mark.parametrize("on", [True, False])
def test_the_session_switch_reads_back_and_reads_off_when_it_has_no_record(on: bool) -> None:
    switch = SessionSwitch(on=on, set_by="admin", set_at=NOW)
    item = switch.to_item()
    assert (item["pool"], item["record"]) == ("_session", "switch")
    assert SessionSwitch.from_item({**item, "set_at": Decimal(NOW)}) == switch
    assert session_is_on(switch) is on
    assert session_is_on(None) is False


@pytest.mark.parametrize("on", ["on", 1, None])
def test_a_switch_that_is_neither_true_nor_false_is_refused(on: object) -> None:
    with pytest.raises(RecordError):
        SessionSwitch.from_item({**SessionSwitch(on=True, set_by="admin", set_at=NOW).to_item(), "on": on})
