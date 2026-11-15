"""Revoking keys: `rhi revoke` and `rhi revoke --all` (system design, section 6.4).

A key is revoked by one write: a conditional `UpdateItem` that sets `revoked_at` on its key item,
on condition that the item is a student's key and is not revoked yet. The item stays, and the
gateway refuses a key whose item carries `revoked_at`, once its 60-second lookup cache has expired.

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

from typing import Any

from botocore.exceptions import ClientError

from rhi_common.keys import is_valid_key, key_hash
from rhi_common.tables import BY_STUDENT, KEYS
from rhi_issuer.redeem import Refusal, Refused
from rhi_issuer.rules import every_key


def _mark_revoked(hashed: str, now: int, keys: Any) -> bool:
    """Sets `revoked_at` on the key item under that SHA-256; `False` when there is no such unrevoked key."""
    try:
        keys.update_item(
            Key={KEYS.partition_key.name: hashed},
            UpdateExpression="SET #revoked = :now",
            # A key item names its student; a missing item, or the lock of a login, does not.
            ConditionExpression="attribute_exists(#student) AND attribute_not_exists(#revoked)",
            ExpressionAttributeNames={"#student": KEYS.index(BY_STUDENT).partition_key.name, "#revoked": "revoked_at"},
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def revoke_key(key: object, *, keys: Any, now: int) -> None:
    """Revokes that key and no other; `Refused` with `Refusal.INVALID_KEY` when it is malformed, unknown or revoked.

    `keys` is the keys table, as the table resource of the AWS SDK, and `now` the time of the
    revocation in whole seconds since the epoch.
    """
    if not isinstance(key, str) or not is_valid_key(key):
        raise Refused(Refusal.INVALID_KEY)
    if not _mark_revoked(key_hash(key), now, keys):
        raise Refused(Refusal.INVALID_KEY)


def revoke_all(github_id: int, *, keys: Any, now: int) -> int:
    """Revokes every key of the student that is not revoked yet; how many this call revoked."""
    return sum(
        _mark_revoked(record.key_hash, now, keys) for record in every_key(github_id, keys) if record.revoked_at is None
    )
