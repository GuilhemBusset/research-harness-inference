"""The lookup of a key: one read of the keys table (system design, section 6.2, step 2).

The key item carries everything the gateway needs to answer a request: the issuer copies the
student's concurrency limit into it when it mints the key, and the admin CLI writes a change of
it into the student's active keys. So a lookup is one read, by the SHA-256 of the
key, and the gateway never reads the roster.

A store answers what the table holds and decides nothing: a revoked key and an expired one come
back as records that say so, and `rhi_gateway.auth` refuses them. It is given the SHA-256 only
(`rhi_common.keys.key_hash`), never a key.

`DynamoKeyStore` reads the table; `InMemoryKeyStore` is for tests, and gives the same answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol, Self

from rhi_common.records import RecordError
from rhi_common.students import StudentKey
from rhi_common.tables import KEYS


@dataclass(frozen=True)
class KeyRecord:
    """What the gateway knows of a key's student, from the key item.

    `github_id` addresses the student's usage item and is never logged or used as a label
    (section 6.6): `alias` is the name logs and metrics carry. `expires_at` is in whole seconds
    since the epoch. `key_hash` names the key item, so that the gateway can read it again past its
    cache (`rhi_gateway.auth.Authenticator.confirm`, `rhi_gateway.metrics`); it is neither logged
    nor shown, and two records of the same student compare equal without it.
    """

    github_id: int
    alias: str
    concurrency_limit: int
    expires_at: int
    revoked: bool
    key_hash: str = field(default="", repr=False, compare=False)

    @classmethod
    def of(cls, key: StudentKey) -> Self:
        return cls(
            github_id=key.github_id,
            alias=key.alias,
            concurrency_limit=key.concurrency_limit,
            expires_at=key.expires_at,
            revoked=key.revoked_at is not None,
            key_hash=key.key_hash,
        )


class KeyStore(Protocol):
    """Where the gateway looks a key up."""

    def lookup(self, key_hash: str) -> KeyRecord | None:
        """The record of the key with that SHA-256, or `None` when the store holds no such key."""
        ...


class DynamoKeyStore:
    """The keys table. `table` is the table resource of the AWS SDK (`boto3.resource("dynamodb").Table`)."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def lookup(self, key_hash: str) -> KeyRecord | None:
        # A consistent read: a key revoked a moment ago is seen as revoked, so that the cache of
        # `rhi_gateway.auth` is the whole of the delay before it stops working.
        answer = self._table.get_item(Key={KEYS.partition_key.name: key_hash}, ConsistentRead=True)
        item = answer.get("Item")
        if item is None:
            return None
        try:
            return KeyRecord.of(StudentKey.from_item(item))
        except RecordError:
            # An item that is not a key item gives nobody access.
            return None


class InMemoryKeyStore:
    """A store for tests: the key items it was given, and the lookups it was asked for."""

    def __init__(self, *keys: StudentKey) -> None:
        self._keys = {key.key_hash: key for key in keys}
        self.lookups: list[str] = []

    def put(self, key: StudentKey) -> None:
        """Add a key item, or replace the one under the same SHA-256."""
        self._keys[key.key_hash] = key

    def revoke(self, key_hash: str, at: int) -> None:
        """Mark the key revoked at that time, as the issuer and the admin CLI do in the table."""
        self._keys[key_hash] = replace(self._keys[key_hash], revoked_at=at)

    def lookup(self, key_hash: str) -> KeyRecord | None:
        self.lookups.append(key_hash)
        key = self._keys.get(key_hash)
        return None if key is None else KeyRecord.of(key)
