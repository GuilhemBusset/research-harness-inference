"""The rules of an issuance: one key per installation, two installations, a daily limit (section 6.4).

`issue` runs on every login, once the device code has named a student of the roster. It reads
every key of the student through the keys table's index by student, then:

- refuses the login when the student's active keys belong to two installations other than the
  one logging in (`Refusal.TOO_MANY_INSTALLATIONS`, whose sentence names `rhi revoke --all`). A
  key is active when it is not revoked and has not expired; a key issued without a login, by
  the admin, belongs to no installation and is not counted;
- refuses the login when the student has been issued as many keys as the limit allows over the
  current UTC day, revoked keys included, so a replaced key still counts
  (`Refusal.TOO_MANY_ISSUANCES`). The limit is the number the function is given, 5 by default
  (section 17). A refused login writes nothing and does not count;
- otherwise stores the new key, and revokes every active key of the same installation in the
  same write: one `TransactWriteItems` that either does both or neither, so the store never
  holds the new key beside an unrevoked old key of its installation. A revoked key keeps its
  item, with `revoked_at` set, which the gateway refuses.

Two logins of one student may run at the same time, in two workers of the function. The rules
hold across them because a student's issuances happen one after the other: every key issued at
a login names, in its item's `successor`, the key issued next to the same student, and the write
that stores a key also sets `successor` on the student's latest key, on condition that it has
none yet. Of two logins that read the same keys, one write lands and the other is cancelled; the
cancelled login reads the keys again, following `successor` with consistent reads, and applies
the rules to what it now finds. A student with no key from a login has no latest key to name:
the first issuance holds a short lock instead, an item under `lock#` and the student's GitHub ID,
taken before the keys are read again and deleted in the write that stores the key. A lock older
than `LOCK_SECONDS` is taken over. A login that loses this race `ATTEMPTS` times is refused with
`Refusal.LOGIN_CONFLICT`.

The index by student is updated after the table, so it may not show yet a key stored a moment
before, or show only some of the student's keys. The rules do not rest on it alone:

- every key issued at a login carries, in its item's `ledger`, the SHA-256 of each key of the
  student that the rules must still see: the keys left active, and the keys issued over the same
  UTC day, itself included. A login reads the ledger of the latest key and reads, with a
  consistent read, every key it names that the index did not show;
- when the index shows no key from a login, the student's keys are read with a consistent scan
  of the table before the student is taken for one who has none. This happens at a student's
  first login, or when the index has not caught up with it.

This module logs nothing, and the key is in what `issue` returns and nowhere else.
"""

from __future__ import annotations

import datetime
import secrets
import time
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from rhi_common.records import RecordError
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import BY_STUDENT, KEYS
from rhi_issuer.mint import Minted, mint
from rhi_issuer.redeem import Refusal, Refused

DEFAULT_ISSUANCES_PER_DAY = 5
MAX_INSTALLATIONS = 2
ATTEMPTS = 12
LOCK_SECONDS = 30
_DAY_SECONDS = 86400

_REVOKED_AT = "revoked_at"
_SUCCESSOR = "successor"
_LEDGER = "ledger"
_LOCK_PREFIX = "lock#"
_LOCKED_AT = "locked_at"
_HOLDER = "holder"
# What a cancelled write may report for an item and still be a race lost, to be tried again.
_RACE_REASONS = frozenset({"None", "ConditionalCheckFailed", "TransactionConflict"})


@dataclass(frozen=True)
class _Known:
    """A key of the student, and the key issued after it, if any."""

    record: StudentKey
    successor: str | None
    ledger: tuple[str, ...] = ()


def _known(item: dict[str, Any]) -> _Known | None:
    try:
        record = StudentKey.from_item(item)
    except RecordError:
        return None
    successor = item.get(_SUCCESSOR)
    ledger = item.get(_LEDGER)
    named = tuple(value for value in ledger if isinstance(value, str)) if isinstance(ledger, (list, set)) else ()
    return _Known(record, successor if isinstance(successor, str) else None, named)


def _read(key_hash: str, keys: Any) -> _Known | None:
    """The key under that SHA-256, read with a consistent read; `None` when there is none."""
    item = keys.get_item(Key={KEYS.partition_key.name: key_hash}, ConsistentRead=True).get("Item")
    return None if item is None else _known(item)


def _student_keys(github_id: int, keys: Any) -> dict[str, _Known]:
    """Every key of the student, by SHA-256, read through the index by student. An item that is not a key is skipped."""
    index = KEYS.index(BY_STUDENT)
    query: dict[str, Any] = {
        "IndexName": index.name,
        "KeyConditionExpression": "#student = :student",
        "ExpressionAttributeNames": {"#student": index.partition_key.name},
        "ExpressionAttributeValues": {":student": github_id},
    }
    found: dict[str, _Known] = {}
    while True:
        answer = keys.query(**query)
        for item in answer.get("Items") or []:
            known = _known(item)
            if known is not None:
                found[known.record.key_hash] = known
        if "LastEvaluatedKey" not in answer:
            return found
        query["ExclusiveStartKey"] = answer["LastEvaluatedKey"]


def _scanned_keys(github_id: int, keys: Any) -> dict[str, _Known]:
    """Every key of the student, by SHA-256, read with a consistent scan of the table."""
    scan: dict[str, Any] = {
        "ConsistentRead": True,
        "FilterExpression": "#student = :student",
        "ExpressionAttributeNames": {"#student": KEYS.index(BY_STUDENT).partition_key.name},
        "ExpressionAttributeValues": {":student": github_id},
    }
    found: dict[str, _Known] = {}
    while True:
        answer = keys.scan(**scan)
        for item in answer.get("Items") or []:
            known = _known(item)
            if known is not None and known.record.github_id == github_id:
                found[known.record.key_hash] = known
        if "LastEvaluatedKey" not in answer:
            return found
        scan["ExclusiveStartKey"] = answer["LastEvaluatedKey"]


def every_key(github_id: int, keys: Any) -> list[StudentKey]:
    """Every key of the student, revoked or not, read with a consistent scan: none stored before the call is missed."""
    return [known.record for known in _scanned_keys(github_id, keys).values()]


def _latest(found: dict[str, _Known], keys: Any, *, consistent: bool) -> _Known | None:
    """The student's latest key from a login, the one with no successor; `None` when there is none.

    The walk starts at the newest key in `found` and follows `successor`, reading with a
    consistent read a key that `found` lacks, or, when `consistent`, every key it walks. The keys
    it reads replace or join those in `found`.
    """
    issued = [known for known in found.values() if known.record.installation is not None]
    if not issued:
        return None
    current: _Known | None = max(issued, key=lambda known: (known.record.issued_at, known.record.key_hash))
    walked: set[str] = set()
    while current is not None:
        if consistent:
            fresh = _read(current.record.key_hash, keys)
            if fresh is not None:
                current = fresh
                found[current.record.key_hash] = current
        walked.add(current.record.key_hash)
        if current.successor is None or current.successor in walked:
            return current
        following = found.get(current.successor)
        if following is None or consistent:
            following = _read(current.successor, keys)
            if following is None:
                # A successor that is gone: the write's condition names the value this key holds.
                return current
            found[following.record.key_hash] = following
        current = following
    return None


def _recall(latest: _Known, found: dict[str, _Known], github_id: int, keys: Any) -> None:
    """Adds to `found` every key the latest key's ledger names that `found` lacks, read with a consistent read."""
    for key_hash in latest.ledger:
        if key_hash in found:
            continue
        known = _read(key_hash, keys)
        if known is not None and known.record.github_id == github_id:
            found[key_hash] = known


def _day_start(now: int) -> int:
    """The first second of the UTC day that holds `now`."""
    return now - now % _DAY_SECONDS


def _is_active(record: StudentKey, now: int) -> bool:
    return record.revoked_at is None and record.expires_at > now


def _apply_rules(records: list[StudentKey], installation: str, now: int, issuances_per_day: int) -> None:
    """Raises `Refused` when a rule forbids a new key on that installation."""
    active = [record for record in records if _is_active(record, now)]
    others = {record.installation for record in active if record.installation not in (None, installation)}
    if len(others) >= MAX_INSTALLATIONS:
        raise Refused(Refusal.TOO_MANY_INSTALLATIONS)
    day_start = _day_start(now)
    issued_today = sum(1 for record in records if day_start <= record.issued_at < day_start + _DAY_SECONDS)
    if issued_today >= issuances_per_day:
        raise Refused(Refusal.TOO_MANY_ISSUANCES)


def _lock_key(github_id: int) -> dict[str, str]:
    return {KEYS.partition_key.name: f"{_LOCK_PREFIX}{github_id}"}


def _take_lock(github_id: int, now: int, keys: Any) -> str | None:
    """Takes the student's lock, unless another login holds it; the holder's token, or `None`."""
    holder = secrets.token_hex(16)
    try:
        keys.put_item(
            Item={**_lock_key(github_id), _LOCKED_AT: now, _HOLDER: holder},
            ConditionExpression="attribute_not_exists(#hash) OR #locked < :stale",
            ExpressionAttributeNames={"#hash": KEYS.partition_key.name, "#locked": _LOCKED_AT},
            ExpressionAttributeValues={":stale": now - LOCK_SECONDS},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return holder


def _release_lock(github_id: int, holder: str, keys: Any) -> None:
    """Deletes the student's lock, if this login still holds it."""
    try:
        keys.delete_item(
            Key=_lock_key(github_id),
            ConditionExpression="#holder = :holder",
            ExpressionAttributeNames={"#holder": _HOLDER},
            ExpressionAttributeValues={":holder": holder},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _ledger(record: StudentKey, existing: list[StudentKey], replaced: list[StudentKey], now: int) -> list[str]:
    """What the new key's `ledger` names: the keys left active, and the keys issued over this UTC day, itself included."""
    revoked = {old.key_hash for old in replaced}
    day_start = _day_start(now)
    named = {record.key_hash}
    for old in existing:
        if (
            _is_active(old, now) and old.key_hash not in revoked
        ) or day_start <= old.issued_at < day_start + _DAY_SECONDS:
            named.add(old.key_hash)
    return sorted(named)


def _store(
    record: StudentKey,
    ledger: list[str],
    replaced: list[StudentKey],
    latest: _Known | None,
    holder: str | None,
    now: int,
    keys: Any,
) -> bool:
    """Writes the new key, revokes the replaced ones and names the new key as the latest key's successor, in one write.

    `False` when the write is cancelled because another login wrote first.
    """
    hash_name = KEYS.partition_key.name
    names = {"#hash": hash_name}
    items: list[dict[str, Any]] = [
        {
            "Put": {
                "TableName": keys.name,
                "Item": {**record.to_item(), _LEDGER: ledger},
                # Never over an item that is there: two keys do not share a SHA-256.
                "ConditionExpression": "attribute_not_exists(#hash)",
                "ExpressionAttributeNames": names,
            }
        }
    ]
    revoked = {old.key_hash for old in replaced}
    revoke = "#revoked = if_not_exists(#revoked, :now)"
    for key_hash in revoked - ({latest.record.key_hash} if latest else set()):
        items.append(
            {
                "Update": {
                    "TableName": keys.name,
                    "Key": {hash_name: key_hash},
                    # A revocation that landed in the meantime keeps its own time.
                    "UpdateExpression": f"SET {revoke}",
                    "ConditionExpression": "attribute_exists(#hash)",
                    "ExpressionAttributeNames": {**names, "#revoked": _REVOKED_AT},
                    "ExpressionAttributeValues": {":now": now},
                }
            }
        )
    if latest is not None:
        assignments = ["#successor = :new"]
        values: dict[str, Any] = {":new": record.key_hash}
        update_names = {**names, "#successor": _SUCCESSOR}
        if latest.record.key_hash in revoked:
            assignments.append(revoke)
            values[":now"] = now
            update_names["#revoked"] = _REVOKED_AT
        if latest.successor is None:
            condition = "attribute_exists(#hash) AND attribute_not_exists(#successor)"
        else:
            condition = "attribute_exists(#hash) AND #successor = :seen"
            values[":seen"] = latest.successor
        items.append(
            {
                "Update": {
                    "TableName": keys.name,
                    "Key": {hash_name: latest.record.key_hash},
                    "UpdateExpression": "SET " + ", ".join(assignments),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": update_names,
                    "ExpressionAttributeValues": values,
                }
            }
        )
    if holder is not None:
        items.append(
            {
                "Delete": {
                    "TableName": keys.name,
                    "Key": _lock_key(record.github_id),
                    "ConditionExpression": "#holder = :holder",
                    "ExpressionAttributeNames": {"#holder": _HOLDER},
                    "ExpressionAttributeValues": {":holder": holder},
                }
            }
        )
    try:
        # The table resource's client, which takes the item's values as the table resource does.
        keys.meta.client.transact_write_items(TransactItems=items)
    except ClientError as error:
        reasons = error.response.get("CancellationReasons") or []
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and all(
            reason.get("Code", "None") in _RACE_REASONS for reason in reasons
        ):
            return False
        raise
    return True


def _pause(attempt: int) -> None:
    """A short wait before trying again, longer at each attempt, with jitter."""
    time.sleep(min(0.02 * 2**attempt, 0.5) * (1 + secrets.randbelow(100) / 100))


def issue(
    entry: RosterEntry,
    installation: str,
    *,
    course_end: datetime.date,
    now: int,
    keys: Any,
    issuances_per_day: int = DEFAULT_ISSUANCES_PER_DAY,
) -> Minted:
    """A new key for the student of that roster entry, on that installation; `Refused` when a rule forbids it.

    `installation` is the identifier the CLI sent with the device code, `now` the time of issue in
    whole seconds since the epoch, `keys` the keys table, as the table resource of the AWS SDK, and
    `issuances_per_day` the most keys the student may be issued over one UTC day.
    """
    for attempt in range(ATTEMPTS):
        if attempt:
            _pause(attempt)
        # A login that lost a race reads the chain of keys again with consistent reads.
        consistent = attempt > 0
        found = _student_keys(entry.github_id, keys)
        latest = _latest(found, keys, consistent=consistent)
        holder = None
        if latest is None:
            # The index may not show yet a key stored a moment before.
            found = _scanned_keys(entry.github_id, keys)
            latest = _latest(found, keys, consistent=consistent)
        if latest is None:
            holder = _take_lock(entry.github_id, now, keys)
            if holder is None:
                continue
            # What another login stored before the lock was taken.
            found = _scanned_keys(entry.github_id, keys)
            latest = _latest(found, keys, consistent=True)
        if latest is not None:
            _recall(latest, found, entry.github_id, keys)
        existing = [known.record for known in found.values()]
        try:
            _apply_rules(existing, installation, now, issuances_per_day)
        except Refused:
            if holder is not None:
                _release_lock(entry.github_id, holder, keys)
            raise
        key, record = mint(entry, installation, course_end=course_end, now=now)
        replaced = [old for old in existing if _is_active(old, now) and old.installation == installation]
        if _store(record, _ledger(record, existing, replaced, now), replaced, latest, holder, now, keys):
            previous = max((old.issued_at for old in existing), default=None)
            return Minted(
                key=key, issued_at=record.issued_at, expires_at=record.expires_at, previous_issued_at=previous
            )
        if holder is not None:
            _release_lock(entry.github_id, holder, keys)
    raise Refused(Refusal.LOGIN_CONFLICT)
