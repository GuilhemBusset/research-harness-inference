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
    daily_token_budget=2_000_000,
    concurrency_limit=4,
)
ROSTER = RosterEntry(
    github_id=4242,
    alias="otter",
    daily_token_budget=2_000_000,
    concurrency_limit=4,
    added_at=NOW - 86400,
    expires_at=COURSE_END + 90 * 86400,
)
USAGE = UsageDay(
    github_id=4242,
    day="2027-01-15",
    alias="otter",
    tokens=150_000,
    expires_at=COURSE_END + 90 * 86400,
    leases={
        "req-1": Lease(tokens=40_000, estimate=32_000, expires=NOW + 1800),
        "req-2": Lease(tokens=9_000, estimate=1_000, expires=NOW + 1900),
    },
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
        "daily_token_budget",
        "concurrency_limit",
    },
    RosterEntry: {"github_id", "alias", "daily_token_budget", "concurrency_limit", "added_at", "expires_at"},
    UsageDay: {
        "github_id",
        "day",
        "alias",
        "tokens",
        "leases",
        "expires_at",
        "affinity_cookies",
        "rebalance_applied",
    },
}
LEASE = {"tokens", "estimate", "expires"}


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
            daily_token_budget=0,
            concurrency_limit=1,
        ),
        UsageDay(github_id=7, day="2027-01-16", alias="heron", tokens=0, expires_at=COURSE_END),
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


def without(item: Mapping[str, object], *names: str) -> dict[str, object]:
    return {name: value for name, value in item.items() if name not in names}


def test_items_without_the_token_budget_s_attributes_read_back_and_are_written_with_them() -> None:
    key = StudentKey.from_item(as_read(without(KEY.to_item(), "daily_token_budget")))
    roster = RosterEntry.from_item(as_read(without(ROSTER.to_item(), "daily_token_budget")))
    lease = {"req-1": without(USAGE.leases["req-1"].to_map(), "tokens", "estimate")}
    day = UsageDay.from_item(as_read({**without(USAGE.to_item(), "tokens"), "leases": lease}))

    assert (key.daily_token_budget, key.concurrency_limit) == (0, KEY.concurrency_limit)
    assert (roster.daily_token_budget, roster.concurrency_limit) == (0, ROSTER.concurrency_limit)
    assert (day.tokens, day.leases) == (0, {"req-1": Lease(tokens=0, estimate=0, expires=NOW + 1800)})
    # A task of the release before reads all four: each item is still written with them.
    assert key.to_item()["daily_token_budget"] == roster.to_item()["daily_token_budget"] == 0
    assert day.to_item()["tokens"] == 0
    assert day.to_item()["leases"] == {"req-1": {"tokens": 0, "estimate": 0, "expires": NOW + 1800}}


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
    first_day = UsageDay(github_id=7, day="2027-01-16", alias="heron", tokens=0, expires_at=COURSE_END).to_item()
    assert first_day["leases"] == {}
    assert "affinity_cookies" not in first_day
    assert "rebalance_applied" not in first_day


def test_a_day_of_usage_reads_back_its_two_leases() -> None:
    read = UsageDay.from_item(as_read(USAGE.to_item()))
    assert set(read.leases) == {"req-1", "req-2"}
    first, second = read.leases["req-1"], read.leases["req-2"]
    assert (first.tokens, first.estimate, first.expires) == (40_000, 32_000, NOW + 1800)
    assert (second.tokens, second.estimate, second.expires) == (9_000, 1_000, NOW + 1900)


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
        ("tokens", Decimal("1.5")),
        ("github_id", "4242"),
        ("leases", {"req-1": {"tokens": 1, "estimate": 1}}),
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
