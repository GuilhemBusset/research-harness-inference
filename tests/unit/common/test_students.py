"""A student's key, roster entry and day of usage (`rhi_common.students`)."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import fields
from decimal import Decimal

import pytest

from rhi_common import students
from rhi_common.keys import generate_key, key_hash
from rhi_common.records import RecordError
from rhi_common.students import Lease, RosterEntry, StudentKey, UsageDay, utc_day

NOW = 1_800_000_000
COURSE_END = NOW + 90 * 86400

# Every attribute present, the optional ones too.
KEY = StudentKey(
    key_hash=key_hash(generate_key()),
    github_id=4242,
    alias="otter",
    installation="install-1",
    issued_at=NOW,
    expires_at=COURSE_END,
    revoked_at=NOW + 60,
    concurrency_limit=4,
)
ROSTER = RosterEntry(
    github_id=4242,
    alias="otter",
    concurrency_limit=4,
    added_at=NOW - 86400,
    expires_at=COURSE_END + 90 * 86400,
)
USAGE = UsageDay(
    github_id=4242,
    day="2027-01-15",
    alias="otter",
    expires_at=COURSE_END + 90 * 86400,
    leases={"req-1": Lease(expires=NOW + 1800), "req-2": Lease(expires=NOW + 1900)},
    affinity_cookies={"coursework": "opaque-cookie", "upgrade": "another-opaque-cookie"},
    rebalance_applied={"coursework": 7},
)
FULL = [KEY, ROSTER, USAGE]

# The attributes of each item, written out here and not read from the module.
LISTED: dict[type, set[str]] = {
    StudentKey: {
        "key_hash",
        "github_id",
        "alias",
        "installation",
        "issued_at",
        "expires_at",
        "revoked_at",
        "concurrency_limit",
    },
    RosterEntry: {"github_id", "alias", "concurrency_limit", "added_at", "expires_at"},
    UsageDay: {
        "github_id",
        "day",
        "alias",
        "leases",
        "expires_at",
        "affinity_cookies",
        "rebalance_applied",
    },
}
LEASE = {"expires"}
# What items an earlier release wrote can still hold, and no item is written with any more.
BUDGET = {"daily_token_budget", "tokens", "estimate"}


def as_read(item: Mapping[str, object]) -> dict[str, object]:
    """The item as boto3 gives it back: every number a `Decimal`, in a map too."""
    return {
        name: as_read(value) if isinstance(value, Mapping) else Decimal(value) if isinstance(value, int) else value
        for name, value in item.items()
    }


def unlisted(item: Mapping[str, object], listed: set[str]) -> set[str]:
    """The attributes an item writes outside its list, a lease's attributes included."""
    extra = set(item) - listed
    leases = item.get("leases", {})
    assert isinstance(leases, Mapping)
    for request, lease in leases.items():
        extra |= {f"leases.{request}.{name}" for name in set(lease) - LEASE}
    return extra


@pytest.mark.parametrize(
    "record",
    [
        *FULL,
        StudentKey(
            key_hash="0" * 64,
            github_id=7,
            alias="heron",
            installation=None,
            issued_at=NOW,
            expires_at=COURSE_END,
            revoked_at=None,
            concurrency_limit=1,
        ),
        UsageDay(github_id=7, day="2027-01-16", alias="heron", expires_at=COURSE_END),
    ],
)
def test_each_item_survives_a_round_trip(record: StudentKey | RosterEntry | UsageDay) -> None:
    item = record.to_item()
    assert type(record).from_item(item) == record
    assert type(record).from_item(as_read(item)) == record


@pytest.mark.parametrize(
    "name",
    ["key_hash", "github_id", "alias", "issued_at", "expires_at", "concurrency_limit"],
)
def test_a_key_item_without_a_required_attribute_is_refused(name: str) -> None:
    item: dict[str, object] = dict(KEY.to_item())
    del item[name]
    with pytest.raises(RecordError):
        StudentKey.from_item(item)


def written_names(item: Mapping[str, object]) -> set[str]:
    """Every attribute of an item, a lease's too."""
    leases = item.get("leases", {})
    assert isinstance(leases, Mapping)
    return set(item) | {name for lease in leases.values() for name in lease}


def test_items_an_earlier_release_wrote_with_the_token_budget_read_as_without_it() -> None:
    # Each item as the release before wrote it: a key's and an entry's budget, a day's tokens and
    # its leases' tokens and estimate, as boto3 gives them back.
    old_lease = {"tokens": 40_000, "estimate": 32_000}
    old: list[tuple[StudentKey | RosterEntry | UsageDay, Mapping[str, object]]] = [
        (KEY, {**KEY.to_item(), "daily_token_budget": 100_000_000}),
        (ROSTER, {**ROSTER.to_item(), "daily_token_budget": 0}),
        (
            USAGE,
            {
                **USAGE.to_item(),
                "tokens": 150_000,
                "leases": {request: {**lease.to_map(), **old_lease} for request, lease in USAGE.leases.items()},
            },
        ),
    ]
    for record, item in old:
        read = type(record).from_item(as_read(item))
        assert read == record
        assert not written_names(read.to_item()) & BUDGET
    assert Lease.from_map(as_read({"expires": NOW, **old_lease})) == Lease(expires=NOW)


def test_a_key_in_clear_is_refused_as_a_key_hash() -> None:
    key = generate_key()
    with pytest.raises(RecordError):
        StudentKey.from_item({**KEY.to_item(), "key_hash": key})
    with pytest.raises(RecordError):
        StudentKey.from_item({**KEY.to_item(), "key_hash": key_hash(key).upper()})


def test_an_absent_optional_attribute_is_not_written() -> None:
    bare = StudentKey.from_item({**KEY.to_item(), "installation": None, "revoked_at": None}).to_item()
    assert "installation" not in bare
    assert "revoked_at" not in bare
    first_day = UsageDay(github_id=7, day="2027-01-16", alias="heron", expires_at=COURSE_END).to_item()
    assert first_day["leases"] == {}
    assert "affinity_cookies" not in first_day
    assert "rebalance_applied" not in first_day


def test_a_day_of_usage_reads_back_its_two_leases() -> None:
    read = UsageDay.from_item(as_read(USAGE.to_item()))
    assert set(read.leases) == {"req-1", "req-2"}
    assert (read.leases["req-1"].expires, read.leases["req-2"].expires) == (NOW + 1800, NOW + 1900)


def test_a_day_of_usage_reads_back_each_pool_s_cookie() -> None:
    cookies = {"coursework": "opaque-cookie", "upgrade": "another-opaque-cookie"}
    assert USAGE.affinity_cookies == cookies
    assert USAGE.to_item()["affinity_cookies"] == cookies
    assert UsageDay.from_item(USAGE.to_item()) == USAGE
    assert UsageDay.from_item(as_read(USAGE.to_item())).affinity_cookies == cookies

    # Absent until written: a day without the attribute holds no cookie, and writes none.
    without = {name: value for name, value in USAGE.to_item().items() if name != "affinity_cookies"}
    assert UsageDay.from_item(without).affinity_cookies == {}
    assert "affinity_cookies" not in UsageDay.from_item(without).to_item()


def test_a_day_of_usage_reads_back_each_pool_s_rebalance_number() -> None:
    usage = UsageDay.from_item({**USAGE.to_item(), "rebalance_applied": {"coursework": 7, "upgrade": 0}})
    assert usage.rebalance_applied == {"coursework": 7, "upgrade": 0}
    assert usage.to_item()["rebalance_applied"] == {"coursework": 7, "upgrade": 0}
    read = UsageDay.from_item(as_read(usage.to_item()))
    assert read == usage
    assert all(type(number) is int for number in read.rebalance_applied.values())

    # Absent until written: a day without the attribute has applied no number, and writes none.
    without = {name: value for name, value in USAGE.to_item().items() if name != "rebalance_applied"}
    assert UsageDay.from_item(without).rebalance_applied == {}
    assert "rebalance_applied" not in UsageDay.from_item(without).to_item()
    # The empty map the gateway's reservation leaves in the table reads as none too.
    assert UsageDay.from_item({**without, "rebalance_applied": {}}).rebalance_applied == {}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("day", "2027-02-30"),
        ("day", "20270115"),
        ("expires_at", Decimal("1.5")),
        ("github_id", "4242"),
        ("leases", {"req-1": {"tokens": 1, "estimate": 1}}),
        ("leases", {"req-1": {"expires": "soon"}}),
        ("leases", {"req-1": 40_000}),
        ("leases", ["req-1"]),
        ("affinity_cookies", {"coursework": 7}),
        ("rebalance_applied", {"coursework": Decimal("7.5")}),
        ("rebalance_applied", {"coursework": "7"}),
        ("rebalance_applied", {"coursework": -1}),
        ("rebalance_applied", 7),
    ],
)
def test_a_mistyped_day_of_usage_is_refused(name: str, value: object) -> None:
    with pytest.raises(RecordError):
        UsageDay.from_item({**USAGE.to_item(), name: value})


def test_the_day_is_the_utc_date() -> None:
    assert utc_day(1_800_000_000) == "2027-01-15"
    assert utc_day(1_800_000_000 - 1_800_000_000 % 86400 - 1) == "2027-01-14"


def test_attributes_are_the_listed_ones() -> None:
    item_classes = {cls for _, cls in inspect.getmembers(students, inspect.isclass) if hasattr(cls, "to_item")}
    assert item_classes == set(LISTED), "an item of the module has no list of attributes here"
    assert {type(record) for record in FULL} == set(LISTED)
    for record in FULL:
        # A sample that leaves an attribute out would not show it being written.
        assert all(getattr(record, attribute.name) not in (None, {}) for attribute in fields(record))
        assert unlisted(record.to_item(), LISTED[type(record)]) == set()


def test_an_attribute_outside_the_lists_is_reported() -> None:
    item = {**KEY.to_item(), "github_token": "x"}
    assert unlisted(item, LISTED[StudentKey]) == {"github_token"}
    lease = {**USAGE.leases["req-2"].to_map(), "request_body": "x"}
    usage = {**USAGE.to_item(), "leases": {"req-2": lease}}
    assert unlisted(usage, LISTED[UsageDay]) == {"leases.req-2.request_body"}


def test_the_owners_entries_are_the_canary_and_the_spikes_students() -> None:
    assert students.is_owner_entry(0)
    assert students.is_owner_entry(10**18)
    assert students.is_owner_entry(10**18 + 3)
    assert not students.is_owner_entry(1)
    assert not students.is_owner_entry(4242)
    assert not students.is_owner_entry(10**18 - 1)
    # The admin CLI puts its entries under these same numbers.
    from rhi_admin import canary, spikes

    assert canary.CANARY_GITHUB_ID == students.CANARY_GITHUB_ID == 0
    assert spikes.SPIKE_GITHUB_ID_BASE == students.SPIKE_GITHUB_ID_BASE == 10**18
    assert students.is_owner_entry(spikes.spike_github_id(1))
