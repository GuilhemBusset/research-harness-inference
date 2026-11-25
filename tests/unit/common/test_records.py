"""The rows the gateway, the instances and the scaler exchange (`rhi_common.records`)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from rhi_common import records
from rhi_common.records import InstanceHeartbeat, LaunchFailure, RecordError, ScalerNote, Session, is_fresh

NOW = 1_800_000_000

# Items of the kinds an earlier release wrote, which the tables may still hold: a gateway task's
# heartbeat, a pool's mode and the session switch. No record reads them.
GATEWAY_ROW = {
    "pool": "coursework",
    "source": "gateway#0f3a9c",
    "in_flight": 3,
    "held": 1,
    "written_at": NOW,
    "expires_at": NOW + 3600,
}
MODE_ITEM = {"pool": "coursework", "record": "mode", "mode": "scheduled-only", "set_by": "admin", "set_at": NOW - 500}
SWITCH_ITEM = {"pool": "_session", "record": "switch", "on": True, "set_by": "admin", "set_at": NOW}

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
NOTE = ScalerNote(pool="mock", capacity_asked=1, no_capacity_at=NOW - 90, written_at=NOW)
# A pool that waits: three asked, one in service, one starting, one leaving.
SHORT = ScalerNote(
    pool="mock",
    capacity_asked=3,
    no_capacity_at=None,
    written_at=NOW,
    requested=3,
    starting=1,
    in_service=1,
    draining=1,
    waiting=1,
    waiting_since=NOW - 600,
    launch_failure=LaunchFailure.QUOTA,
)
SESSION = Session(
    pool="coursework", instances=2, starts_at=NOW + 3600, ends_at=NOW + 18000, set_by="instructor", set_at=NOW
)

RECORDS = [
    INSTANCE,
    NOTE,
    ScalerNote(pool="mock", capacity_asked=0, no_capacity_at=None, written_at=NOW),
    ScalerNote(pool="mock", capacity_asked=2, no_capacity_at=None, written_at=NOW, rebalance_number=7),
    SHORT,
    SESSION,
]


@pytest.mark.parametrize("record", RECORDS)
def test_each_record_survives_a_round_trip(record: InstanceHeartbeat | ScalerNote | Session) -> None:
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
    session = SESSION.to_item()
    assert (session["pool"], session["record"]) == ("coursework", "session")
    # The pool-state items must survive: they carry no expiry.
    assert "expires_at" not in note
    assert "expires_at" not in session

    instance = INSTANCE.to_item()
    assert (instance["pool"], instance["source"]) == ("coursework", "instance#instance-1")
    assert instance["expires_at"] == NOW + 3600


def test_the_records_are_the_instances_heartbeat_the_scalers_note_and_the_session() -> None:
    # The pool mode and its record, the session switch and the gateway's heartbeat are defined no more.
    classes = {name for name, value in vars(records).items() if isinstance(value, type) and hasattr(value, "to_item")}
    assert classes == {"InstanceHeartbeat", "ScalerNote", "Session"}
    for name in ("Mode", "MODE_RECORD", "CONFIGURED_MODE", "SESSION_PARTITION", "SWITCH_RECORD", "GATEWAY_SOURCE"):
        assert not hasattr(records, name)
    assert not hasattr(records, "switched") and not hasattr(records, "session_is_on")


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


def test_a_note_stored_without_what_waits_reads_as_zeros_no_time_and_no_cause() -> None:
    # The note as the scaler wrote it before it said what waits.
    stored = {"pool": "mock", "record": "note", "capacity_asked": 2, "no_capacity_at": NOW - 90, "written_at": NOW}
    note = ScalerNote.from_item(stored)
    assert (note.capacity_asked, note.no_capacity_at) == (2, NOW - 90)
    assert (note.requested, note.starting, note.in_service, note.draining, note.waiting) == (0, 0, 0, 0, 0)
    assert (note.waiting_since, note.launch_failure) == (None, None)


def test_a_note_says_what_waits_and_drops_the_time_and_the_cause_while_nothing_does() -> None:
    stored = SHORT.to_item()
    held = {"requested": 3, "starting": 1, "in_service": 1, "draining": 1, "waiting": 1}
    assert {name: stored[name] for name in held} == held
    assert (stored["waiting_since"], stored["launch_failure"]) == (NOW - 600, "quota")
    # The counts are written at zero too; a time and a cause that are not set are left out.
    nothing = NOTE.to_item()
    assert {name: nothing[name] for name in held} == dict.fromkeys(held, 0)
    assert "waiting_since" not in nothing and "launch_failure" not in nothing


def test_a_note_with_an_attribute_its_reader_does_not_know_still_reads() -> None:
    assert ScalerNote.from_item({**SHORT.to_item(), "written_by_a_later_scaler": "anything"}) == SHORT


def test_the_cause_of_a_failed_launch_is_one_of_four_words() -> None:
    assert {cause.value for cause in LaunchFailure} == {
        "insufficient-capacity",
        "quota",
        "launches-suspended",
        "launch-failed",
    }
    # Anything else, text from AWS included, is not a note.
    for cause in ("InsufficientInstanceCapacity", "", 3):
        with pytest.raises(RecordError):
            ScalerNote.from_item({**SHORT.to_item(), "launch_failure": cause})
    with pytest.raises(RecordError):
        ScalerNote.from_item({**SHORT.to_item(), "waiting": Decimal("1.5")})


def test_a_heartbeat_is_fresh_at_20_seconds_and_stale_after() -> None:
    assert is_fresh(INSTANCE, NOW)
    assert is_fresh(INSTANCE, NOW + 20)
    assert not is_fresh(INSTANCE, NOW + 21)


def test_an_item_of_another_record_is_refused() -> None:
    # Among them the items an earlier release left in the tables: none reads as a record of today.
    for read, item in [
        (ScalerNote.from_item, SESSION.to_item()),
        (ScalerNote.from_item, MODE_ITEM),
        (Session.from_item, NOTE.to_item()),
        (Session.from_item, MODE_ITEM),
        (Session.from_item, SWITCH_ITEM),
        (InstanceHeartbeat.from_item, GATEWAY_ROW),
        (InstanceHeartbeat.from_item, {**INSTANCE.to_item(), "source": "instance#"}),
    ]:
        with pytest.raises(RecordError):
            read(item)


@pytest.mark.parametrize("written_at", [Decimal("1800000000.5"), 1800000000.0, "1800000000", True, -1, None])
def test_a_time_that_is_not_a_whole_number_of_seconds_is_refused(written_at: object) -> None:
    for read, item in [
        (InstanceHeartbeat.from_item, INSTANCE.to_item()),
        (ScalerNote.from_item, NOTE.to_item()),
    ]:
        with pytest.raises(RecordError):
            read({**item, "written_at": written_at})
    with pytest.raises(RecordError):
        Session.from_item({**SESSION.to_item(), "set_at": written_at})


@pytest.mark.parametrize("name", ["pool", "instances", "starts_at", "ends_at", "set_by", "set_at"])
def test_a_session_with_a_missing_or_mistyped_attribute_is_refused(name: str) -> None:
    item: dict[str, object] = dict(SESSION.to_item())
    is_text = isinstance(item[name], str)
    for wrong in ("", 7) if is_text else ("2", Decimal("1.5"), 1.0, True, -1):
        with pytest.raises(RecordError):
            Session.from_item({**item, name: wrong})
    del item[name]
    with pytest.raises(RecordError):
        Session.from_item(item)
