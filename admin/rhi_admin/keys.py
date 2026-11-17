"""Keys the admin issues and revokes directly (system design, section 6.4).

GitHub confirms at most 50 device codes an hour per app, so an admin command that issues a key
without a login is the fallback. The key is the same item a login stores (`rhi_common.students`):
its SHA-256 and no clear key, the roster entry's alias, budget and limit, no installation, and the
course end as its expiry. `issue` returns the key, which the command line prints once; it is in
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
from rhi_common.tables import KEYS
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
        daily_token_budget=entry.daily_token_budget,
        concurrency_limit=entry.concurrency_limit,
    )
    try:
        keys.put_item(
            Item=record.to_item(),
            # Never over an item that is there: two keys do not share a SHA-256.
            ConditionExpression="attribute_not_exists(#hash)",
            ExpressionAttributeNames={"#hash": KEYS.partition_key.name},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise AdminError("the new key collided with a stored one: issue it again") from None
        raise
    return key


def revoke(github_id: int, *, keys: Any, now: int) -> int:
    """Revokes every key of the student that is not revoked yet; how many this revoked."""
    return revoke_all(github_id, keys=keys, now=now)
