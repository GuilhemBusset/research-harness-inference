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
  item, with `revoked_at` set, which the gateway refuses. With nothing to revoke, the write is
  the key's own `PutItem`.

This module logs nothing, and the key is in what `issue` returns and nowhere else.
"""

from __future__ import annotations

import datetime
from typing import Any

from rhi_common.records import RecordError
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import BY_STUDENT, KEYS
from rhi_issuer.mint import Minted, mint
from rhi_issuer.redeem import Refusal, Refused

DEFAULT_ISSUANCES_PER_DAY = 5
MAX_INSTALLATIONS = 2
_DAY_SECONDS = 86400

_REVOKED_AT = "revoked_at"


def _student_keys(github_id: int, keys: Any) -> list[StudentKey]:
    """Every key of the student, read through the index by student. An item that is not a key is skipped."""
    index = KEYS.index(BY_STUDENT)
    query: dict[str, Any] = {
        "IndexName": index.name,
        "KeyConditionExpression": "#student = :student",
        "ExpressionAttributeNames": {"#student": index.partition_key.name},
        "ExpressionAttributeValues": {":student": github_id},
    }
    found: list[StudentKey] = []
    while True:
        answer = keys.query(**query)
        for item in answer.get("Items") or []:
            try:
                found.append(StudentKey.from_item(item))
            except RecordError:
                continue
        if "LastEvaluatedKey" not in answer:
            return found
        query["ExclusiveStartKey"] = answer["LastEvaluatedKey"]


def _day_start(now: int) -> int:
    """The first second of the UTC day that holds `now`."""
    return now - now % _DAY_SECONDS


def _is_active(record: StudentKey, now: int) -> bool:
    return record.revoked_at is None and record.expires_at > now


def _store(record: StudentKey, replaced: list[StudentKey], now: int, keys: Any) -> None:
    """Writes the new key, and revokes the replaced ones, in one write."""
    hash_name = KEYS.partition_key.name
    names = {"#hash": hash_name}
    # Never over an item that is there: two keys do not share a SHA-256.
    condition = "attribute_not_exists(#hash)"
    if not replaced:
        keys.put_item(Item=record.to_item(), ConditionExpression=condition, ExpressionAttributeNames=names)
        return
    put = {
        "Put": {
            "TableName": keys.name,
            "Item": record.to_item(),
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
        }
    }
    revocations = [
        {
            "Update": {
                "TableName": keys.name,
                "Key": {hash_name: old.key_hash},
                # A revocation that landed in the meantime keeps its own time.
                "UpdateExpression": "SET #revoked = if_not_exists(#revoked, :now)",
                "ConditionExpression": "attribute_exists(#hash)",
                "ExpressionAttributeNames": {**names, "#revoked": _REVOKED_AT},
                "ExpressionAttributeValues": {":now": now},
            }
        }
        for old in replaced
    ]
    # The table resource's client, which takes the item's values as the table resource does.
    keys.meta.client.transact_write_items(TransactItems=[put, *revocations])


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
    existing = _student_keys(entry.github_id, keys)
    active = [record for record in existing if _is_active(record, now)]

    others = {record.installation for record in active if record.installation not in (None, installation)}
    if len(others) >= MAX_INSTALLATIONS:
        raise Refused(Refusal.TOO_MANY_INSTALLATIONS)

    day_start = _day_start(now)
    issued_today = sum(1 for record in existing if day_start <= record.issued_at < day_start + _DAY_SECONDS)
    if issued_today >= issuances_per_day:
        raise Refused(Refusal.TOO_MANY_ISSUANCES)

    key, record = mint(entry, installation, course_end=course_end, now=now)
    replaced = [old for old in active if old.installation == installation]
    _store(record, replaced, now, keys)
    previous = max((old.issued_at for old in existing), default=None)
    return Minted(key=key, issued_at=record.issued_at, expires_at=record.expires_at, previous_issued_at=previous)
