"""Revoking keys: `rhi revoke` and `rhi revoke --all` (system design, section 6.4).

A key is revoked by one write: a conditional `UpdateItem` that sets `revoked_at` on its key item,
on condition that the item is a student's key and is not revoked yet. The item stays, and the
gateway refuses a key whose item carries `revoked_at`, once its 60-second lookup cache has expired.
The revocation also holds the item's expiry to the course end plus 90 days (`rhi_common.retention`):
a key item is personal data. When the item's expiry is already earlier, that write's condition
fails and a second one, with the same condition otherwise, revokes the key without touching it.

- `revoke_key` revokes the key it is given and no other. A key that is malformed is refused
  before any table is read; one that is unknown or already revoked fails the write's condition,
  so nothing changes. All three are `Refusal.INVALID_KEY`, one fixed answer that does not say
  which of them it was.
- `revoke_all` revokes every key of a student, read with a consistent scan of the keys table, so
  a key stored a moment before is not missed while the index by student catches up. Each is
  revoked by that same write; a key revoked in the meantime keeps its own time.

This module logs nothing, and a key is never written: only its SHA-256 names its item.
"""

from __future__ import annotations

import datetime
from typing import Any

from botocore.exceptions import ClientError

from rhi_common import retention
from rhi_common.keys import is_valid_key, key_hash
from rhi_common.tables import BY_STUDENT, EXPIRES_AT, KEYS
from rhi_issuer.redeem import Refusal, Refused
from rhi_issuer.rules import every_key


def _mark_revoked(hashed: str, now: int, keys: Any, course_end: datetime.date) -> bool:
    """Sets `revoked_at` on the key item under that SHA-256; `False` when there is no such unrevoked key.

    The write also holds the item's expiry to the course end plus 90 days (`rhi_common.retention`),
    so that a key written again after the course end was moved earlier expires by the new date. A
    write sets the lower expiry only on condition that the item's is later; when it is not, a
    second write revokes the key and leaves its earlier expiry. Each write is one conditional
    `UpdateItem`, so no expiry is ever raised.
    """
    # A key item names its student; a missing item, or the lock of a login, does not.
    condition = "attribute_exists(#student) AND attribute_not_exists(#revoked)"
    names = {"#student": KEYS.index(BY_STUDENT).partition_key.name, "#revoked": "revoked_at"}
    later = {
        "UpdateExpression": "SET #revoked = :now, #expires = :expires",
        "ConditionExpression": f"{condition} AND #expires > :expires",
        "ExpressionAttributeNames": {**names, "#expires": EXPIRES_AT},
        "ExpressionAttributeValues": {":now": now, ":expires": retention.expires_at(course_end)},
    }
    earlier = {
        "UpdateExpression": "SET #revoked = :now",
        "ConditionExpression": condition,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": {":now": now},
    }
    for update in (later, earlier):
        try:
            keys.update_item(Key={KEYS.partition_key.name: hashed}, **update)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            continue
        return True
    return False


def revoke_key(key: object, *, keys: Any, now: int, course_end: datetime.date) -> None:
    """Revokes that key and no other; `Refused` with `Refusal.INVALID_KEY` when it is malformed, unknown or revoked.

    `keys` is the keys table, as the table resource of the AWS SDK, `now` the time of the
    revocation in whole seconds since the epoch, and `course_end` the course's end date, which
    the key item's expiry is held to.
    """
    if not isinstance(key, str) or not is_valid_key(key):
        raise Refused(Refusal.INVALID_KEY)
    if not _mark_revoked(key_hash(key), now, keys, course_end):
        raise Refused(Refusal.INVALID_KEY)


def revoke_all(github_id: int, *, keys: Any, now: int, course_end: datetime.date) -> int:
    """Revokes every key of the student that is not revoked yet; how many this call revoked."""
    return sum(
        _mark_revoked(record.key_hash, now, keys, course_end)
        for record in every_key(github_id, keys)
        if record.revoked_at is None
    )
