"""Keys the admin issues and revokes directly (system design, section 6.4).

GitHub confirms at most 50 device codes an hour per app, so an admin command that issues a key
without a login is the fallback. The key is the same item a login stores (`rhi_common.students`):
its SHA-256 and no clear key, the roster entry's alias and limit, no installation, and the
course end as its expiry, which `rhi_issuer.mint.expiry` holds to the course end plus 90 days, as
for every key item. The key is written only while the entry it was read from is still on the
roster, so an erase, or a removal followed by a new entry, that runs between the read of the entry
and the write leaves no key. `issue` returns the key, which the command line prints once; it is in
no table and no log. `revoke` revokes every key of a student, as `rhi revoke --all` does, whether
or not the student is still on the roster.
"""

from __future__ import annotations

import datetime
from typing import Any

from botocore.exceptions import ClientError

from rhi_admin.roster import AdminError, get
from rhi_common.keys import generate_key, key_hash
from rhi_common.students import StudentKey
from rhi_common.tables import KEYS, ROSTER
from rhi_issuer.mint import expiry
from rhi_issuer.revoke import revoke_all


def issue(github_id: int, *, roster: Any, keys: Any, course_end: datetime.date, now: int) -> str:
    """A new key for that student of the roster, stored as its SHA-256; refused when they are not on it."""
    entry = get(github_id, roster=roster)
    if entry is None:
        raise AdminError("that GitHub ID is not on the roster")
    key = generate_key()
    record = StudentKey(
        key_hash=key_hash(key),
        github_id=entry.github_id,
        alias=entry.alias,
        installation=None,
        issued_at=now,
        expires_at=expiry(course_end),
        revoked_at=None,
        concurrency_limit=entry.concurrency_limit,
    )
    put = {
        "TableName": keys.name,
        "Item": record.to_item(),
        # Never over an item that is there: two keys do not share a SHA-256.
        "ConditionExpression": "attribute_not_exists(#hash)",
        "ExpressionAttributeNames": {"#hash": KEYS.partition_key.name},
    }
    # The entry read above may be erased, or removed and the student put back under another
    # alias, before the key is written: the write holds only while that same entry, the one added
    # at that time under that alias, is still there, so a key never outlives the entry it was read from.
    still_listed = {
        "TableName": roster.name,
        "Key": {ROSTER.partition_key.name: entry.github_id},
        "ConditionExpression": "#alias = :alias AND #added = :added",
        "ExpressionAttributeNames": {"#alias": "alias", "#added": "added_at"},
        "ExpressionAttributeValues": {":alias": entry.alias, ":added": entry.added_at},
    }
    try:
        # The table resource's client, which takes the item's values as the table resource does.
        keys.meta.client.transact_write_items(TransactItems=[{"ConditionCheck": still_listed}, {"Put": put}])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [reason.get("Code") for reason in error.response.get("CancellationReasons") or []]
        if reasons[:1] == ["ConditionalCheckFailed"]:
            raise AdminError("that roster entry changed or went while the key was issued: issue it again") from None
        if reasons[1:2] == ["ConditionalCheckFailed"]:
            raise AdminError("the new key collided with a stored one: issue it again") from None
        raise
    return key


def revoke(github_id: int, *, keys: Any, course_end: datetime.date, now: int) -> int:
    """Revokes every key of the student that is not revoked yet, each held to the course end plus
    90 days (`rhi_issuer.revoke`); how many this revoked."""
    return revoke_all(github_id, keys=keys, now=now, course_end=course_end)
