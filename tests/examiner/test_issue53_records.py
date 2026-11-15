"""Independent item fixtures for issue 53; no AWS client is needed."""

from decimal import Decimal

import pytest

from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode, PoolState, ScalerNote, is_fresh

TIME = 2_000_000_000
CASES = [
    (
        GatewayHeartbeat("task-a", "mock", 7, 3, TIME - 11, TIME),
        {
            "pool": "mock", "source": "gateway#task-a", "in_flight": 7, "held": 3,
            "last_request_at": TIME - 11, "written_at": TIME, "expires_at": TIME + 3600,
        },
    ),
    (
        InstanceHeartbeat("instance-a", "mock", 4, 2, "sha256:" + "ef" * 32, "revision-a", "serving", TIME),
        {
            "pool": "mock", "source": "instance#instance-a", "running": 4, "waiting": 2,
            "image_digest": "sha256:" + "ef" * 32, "model_revision": "revision-a", "stage": "serving",
            "written_at": TIME, "expires_at": TIME + 3600,
        },
    ),
    (
        PoolState("mock", Mode.SCHEDULED_ONLY, "admin", TIME - 100),
        {"pool": "mock", "record": "mode", "mode": "scheduled-only", "set_by": "admin", "set_at": TIME - 100},
    ),
    (
        ScalerNote("mock", 2, TIME - 60, TIME),
        {"pool": "mock", "record": "note", "capacity_asked": 2, "no_capacity_at": TIME - 60, "written_at": TIME},
    ),
]


@pytest.mark.parametrize(("record", "item"), CASES)
def test_independent_item_contract(record, item):
    assert record.to_item() == item
    assert type(record).from_item(item) == record
    assert type(record).from_item(record.to_item()) == record
    dynamodb_item = {key: Decimal(value) if type(value) is int else value for key, value in item.items()}
    assert type(record).from_item(dynamodb_item) == record
    assert all(type(value) in (str, int) for value in record.to_item().values())


@pytest.mark.parametrize("mode", ["enabled", "scheduled-only", "disabled", "budget-latched"])
def test_each_allowed_mode(mode):
    item = {"pool": "mock", "record": "mode", "mode": mode, "set_by": "admin", "set_at": TIME}
    state = PoolState.from_item(item)
    assert state.mode == mode
    assert state.to_item() == item


@pytest.mark.parametrize("mode", ["parked", "other", "Enabled", "", None, 17])
def test_unknown_mode_is_rejected(mode):
    with pytest.raises(ValueError):
        PoolState.from_item({"pool": "mock", "record": "mode", "mode": mode, "set_by": "admin", "set_at": TIME})


@pytest.mark.parametrize(("heartbeat", "item"), CASES[:2])
def test_freshness_and_expiry_are_distinct(heartbeat, item):
    assert item["expires_at"] == heartbeat.written_at + 3600
    assert is_fresh(heartbeat, TIME + 19)
    assert is_fresh(heartbeat, TIME + 20)
    assert not is_fresh(heartbeat, TIME + 21)
    assert not is_fresh(heartbeat, TIME + 3599)


@pytest.mark.parametrize(
    ("record", "absent"),
    [
        (GatewayHeartbeat("task-b", "mock", 0, 0, None, TIME), "last_request_at"),
        (ScalerNote("mock", 0, None, TIME), "no_capacity_at"),
    ],
)
def test_no_previous_event(record, absent):
    item = record.to_item()
    assert absent not in item
    assert type(record).from_item(item) == record


@pytest.mark.parametrize(("record", "item"), CASES)
def test_fractional_time_is_rejected(record, item):
    field = "set_at" if isinstance(record, PoolState) else "written_at"
    with pytest.raises(ValueError):
        type(record).from_item({**item, field: Decimal("2000000000.5")})
