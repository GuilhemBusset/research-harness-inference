"""Exporting and erasing one student's data (system design, sections 5.5, 6.4 and 6.6).

What the system keeps of a student: the roster entry, the keys (their SHA-256, never the key, and
their times), the usage rows of each day, which also hold the student's affinity cookies, the
gateway's log records under the student's alias, and the series in the Prometheus workspace
labelled with that alias.

`export` gathers all of it as one JSON document: the entry, each key's issue, expiry and
revocation times (no key and no SHA-256), the usage rows as stored, less the budget's attributes a
row of an earlier release still holds (`rhi_common.students.without_budget`), the log records whose `alias` is the
student's, and the samples of every series labelled with the alias, read day by day over the
workspace's 90 days, from the entry's addition on. An alias taken off the roster, by an erase or
by `roster remove`, is held back from any new entry while what was written under it is kept
(`rhi_admin.roster.hold_alias`), so what an export reads under the alias is the student's own.

`erase` deletes what can be deleted: the keys, revoked first, the usage rows and the roster entry.
Logins, a `key issue` and the gateway's requests may still be at work when it starts, and its
order leaves none of them a row behind:

1. The GitHub ID is held back from `roster add` until the erase ends (`rhi_admin.roster.begin_erase`),
   under a token of this erase. Each of the erase's writes checks, in the same transaction, that
   the token is still there (`rhi_admin.roster.erasing`): an erase run again takes the GitHub ID
   over and the first one stops, so no erase deletes a key or a usage row of an entry added once
   the GitHub ID was given back. Then the roster entry goes, replaced in the same write by the
   item that holds its alias back for `ALIAS_HELD_DAYS`, and never past the course end plus 90
   days (`rhi_admin.roster.hold_alias`, `alias_held_until`): a login that reads the roster from
   then on gets no key, and no new entry is given the alias. The admin's own `key issue` writes a
   key only while the very entry it read is there (`rhi_admin.keys`).
2. Every key is revoked. A login that read the entry a moment before can still store a key until
   the issuer's function times out, so after `ISSUER_SECONDS` every key is revoked again.
   `GATEWAY_SECONDS` then outwaits the gateway's 60-second cache.
3. Then every key item, the issuer's lock of the student and every usage row are deleted, each
   read with a consistent read, and a last read confirms that none is left; only then is the
   GitHub ID given back to `roster add` (`rhi_admin.roster.end_erase`).
4. A request accepted before the erase reads its key again, past its cache, right after it writes
   its reservation, the write that makes a usage row (`rhi_gateway.auth.Authenticator.confirm`).
   When the key is revoked or gone, the read fails, or the request is cancelled before the read
   ends, it takes its reservation back and deletes the row once it holds only what the day's
   opening carried from the day before: no tokens other than an earlier release's 0, no lease but stand-ins, no settlement, and the
   cookies and rebalance numbers (`rhi_gateway.budget.Ledger.withdraw`). The rows such requests made go, in whatever order they
   take their reservations back, and what a new entry of the same student charged or holds stays.
   A settlement writes only to a row that holds its lease, so it creates none.

At its next scrape, the gateway stops serving the series of a student whose key it last accepted
is gone (`rhi_gateway.metrics`), so no sample is written under the alias after the erase.

Managed Prometheus has no call that deletes series, and CloudWatch Logs none that deletes one
record. With the entry gone the alias names nobody, and those series and records expire with
their retention, 90 days after they were written; so do the load balancers' and the WAF's logs,
whose source addresses the system cannot tie to a student. `RETENTION_ONLY` says so, in fixed
text: nothing here repeats what AWS answered.
"""

from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

from rhi_admin.roster import (
    ALIAS_HELD_DAYS,
    ERASE_TAKEN_OVER,
    RETENTION_DAYS,
    AdminError,
    alias_held_until,
    begin_erase,
    check_github_id,
    end_erase,
    erasing,
    get,
    hold_alias,
)
from rhi_common import retention
from rhi_common.students import RosterEntry, without_budget
from rhi_common.tables import BY_STUDENT, EXPIRES_AT, KEYS, USAGE
from rhi_issuer.rules import every_key, lock_key

# `RETENTION_DAYS` is the retention of the workspace and of the gateway's log group (section 6.6),
# from a sample's or a record's writing: what an export reads, and how long what an erase cannot
# delete is kept. `ALIAS_HELD_DAYS` is how long an erased alias is held back (`rhi_admin.roster`).
# The workspace is read one day at a time, so that no query asks for 90 days of samples at once.
WINDOW_SECONDS = 86_400
# Logins in flight: the issuer's function stops after 30 seconds (`rhi.access.issuer.TIMEOUT`).
ISSUER_SECONDS = 40
# The gateway's cache of a key's lookup, 60 seconds (`rhi_gateway.auth.CACHE_SECONDS`), and a
# reservation's write in flight.
GATEWAY_SECONDS = 70
# How long the GitHub ID of an erase that stopped short stays held back, unless it is run again.
ERASE_HELD_SECONDS = 86_400
SIGNING_SERVICE = "aps"
QUERY_TIMEOUT_SECONDS = 30

# What only retention removes, as the erase prints it.
RETENTION_ONLY = (
    "the workspace's series labelled with the alias, in Managed Prometheus, which deletes no series",
    "the gateway's log records under the alias, in its log group, from which CloudWatch Logs deletes no record",
    "the load balancers' access logs and the WAF's logs, whose source addresses the system cannot tie to a student",
)

# One instant query of the workspace: the PromQL and the time it is evaluated at; the result list.
Query = Callable[[str, float], list[dict[str, Any]]]


@dataclass(frozen=True)
class Stores:
    """What an export or an erase reaches beyond the roster and the keys: the usage table, a
    CloudWatch Logs client and the gateway's log group, and the workspace's query."""

    usage: Any
    logs: Any
    log_group: str
    query: Query
    sleep: Callable[[float], None] = field(default=time.sleep)


@dataclass(frozen=True)
class Erased:
    """What an erase deleted: whether there was an entry, its alias, the keys and the usage rows."""

    entry: bool
    alias: str | None
    keys: int
    usage_rows: int


def workspace_query(session: Any, endpoint: str, *, urlopen: Callable[..., Any] = urllib.request.urlopen) -> Query:
    """The instant query of the workspace whose Prometheus endpoint that is, signed with the session's credentials."""
    url = f"{endpoint.rstrip('/')}/api/v1/query"

    def query(promql: str, at: float) -> list[dict[str, Any]]:
        body = urlencode({"query": promql, "time": f"{at:.3f}"}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        signed = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(session.get_credentials(), SIGNING_SERVICE, session.region_name).add_auth(signed)
        request = urllib.request.Request(url, data=body, headers=dict(signed.headers), method="POST")
        try:
            with urlopen(request, timeout=QUERY_TIMEOUT_SECONDS) as answer:
                data = json.load(answer)
        except (urllib.error.URLError, OSError, ValueError):
            raise AdminError("the workspace could not be queried") from None
        if not isinstance(data, dict) or data.get("status") != "success":
            raise AdminError("the workspace refused a query")
        return list(data.get("data", {}).get("result") or [])

    return query


def _plain(value: Any) -> Any:
    """A DynamoDB value as JSON holds it: a whole number as an int, a set as a sorted list."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {name: _plain(inner) for name, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(inner) for inner in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(inner) for inner in value)
    return value


def _usage_rows(github_id: int, usage: Any) -> Iterator[dict[str, Any]]:
    """Every usage row of the student, read with consistent reads."""
    query: dict[str, Any] = {
        "KeyConditionExpression": "#student = :student",
        "ExpressionAttributeNames": {"#student": USAGE.partition_key.name},
        "ExpressionAttributeValues": {":student": github_id},
        "ConsistentRead": True,
    }
    while True:
        answer = usage.query(**query)
        yield from answer.get("Items") or []
        if "LastEvaluatedKey" not in answer:
            return
        query["ExclusiveStartKey"] = answer["LastEvaluatedKey"]


def _since(entry: RosterEntry, now: int) -> int:
    """When the export starts reading the alias's telemetry: the entry's addition, within the retention."""
    return max(entry.added_at, now - RETENTION_DAYS * 86_400)


def _log_records(alias: str, stores: Stores, since: int, now: int) -> list[dict[str, Any]]:
    """The gateway's log records whose `alias` is that one, written from `since` on."""
    request: dict[str, Any] = {
        "logGroupName": stores.log_group,
        "filterPattern": f"{{ $.alias = {json.dumps(alias)} }}",
        "startTime": since * 1000,
        "endTime": (now + 1) * 1000,
    }
    records: list[dict[str, Any]] = []
    while True:
        answer = stores.logs.filter_log_events(**request)
        for event in answer.get("events") or []:
            try:
                record = json.loads(event.get("message", ""))
            except ValueError:
                continue
            # The pattern selects them; a record that names another student is never exported.
            if isinstance(record, dict) and record.get("alias") == alias:
                records.append({"timestamp": event.get("timestamp"), "record": record})
        token = answer.get("nextToken")
        if not token or token == request.get("nextToken"):
            return records
        request["nextToken"] = token


def _series(alias: str, query: Query, since: int, now: int) -> list[dict[str, Any]]:
    """The samples of every series labelled with the alias, written from `since` on, by series."""
    selector = f"{{alias={json.dumps(alias)}}}[{WINDOW_SECONDS}s]"
    found: dict[tuple[tuple[str, str], ...], dict[float, str]] = {}
    # Each query reads the day that ends at `end`, from now back to the day that holds `since`.
    for end in range(now, since - WINDOW_SECONDS, -WINDOW_SECONDS):
        for result in query(selector, float(end)):
            labels = tuple(sorted((result.get("metric") or {}).items()))
            for at, value in result.get("values") or []:
                if float(at) >= since:
                    found.setdefault(labels, {})[float(at)] = value
    return [
        {"labels": dict(labels), "values": [[at, samples[at]] for at in sorted(samples)]}
        for labels, samples in sorted(found.items())
    ]


def export(github_id: int, *, roster: Any, keys: Any, stores: Stores, now: int) -> dict[str, Any]:
    """Everything kept of that student of the roster, as one JSON document; refused when they are not on it."""
    check_github_id(github_id)
    entry = get(github_id, roster=roster)
    if entry is None:
        raise AdminError("that GitHub ID is not on the roster")
    return {
        "entry": entry.to_item(),
        # The times only: a key is never stored, and its SHA-256 is not the student's to keep.
        "keys": [
            {"issued_at": record.issued_at, "expires_at": record.expires_at, "revoked_at": record.revoked_at}
            for record in sorted(every_key(github_id, keys), key=lambda record: record.issued_at)
        ],
        "usage": [_plain(without_budget(item)) for item in _usage_rows(github_id, stores.usage)],
        "logs": _log_records(entry.alias, stores, _since(entry, now), now),
        "series": _series(entry.alias, stores.query, _since(entry, now), now),
    }


class _Guarded:
    """The erase's writes to the keys and usage tables, each made only while the erase holds the GitHub ID.

    Each is one transaction with the condition `rhi_admin.roster.erasing`: once another erase
    has taken the GitHub ID over, or given it back, this one stops before it writes anything.
    """

    def __init__(self, github_id: int, roster: Any, token: str) -> None:
        self._guard = erasing(github_id, roster=roster, token=token)

    def write(self, operation: str, request: dict[str, Any]) -> bool:
        """Makes the write; whether its own condition held. Raises when the erase no longer holds the GitHub ID."""
        try:
            # The table resource's client, which takes the item's values as the table resource does.
            client = request.pop("table").meta.client
            client.transact_write_items(TransactItems=[self._guard, {operation: request}])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = [reason.get("Code") for reason in error.response.get("CancellationReasons") or []]
            if reasons[:1] == ["ConditionalCheckFailed"]:
                raise AdminError(ERASE_TAKEN_OVER) from None
            if reasons[1:2] == ["ConditionalCheckFailed"]:
                return False
            raise
        return True

    def delete(self, table: Any, key: dict[str, Any]) -> bool:
        """Deletes the item under that key; whether there was one."""
        name = next(iter(key))
        return self.write(
            "Delete",
            {
                "table": table,
                "TableName": table.name,
                "Key": key,
                "ConditionExpression": "attribute_exists(#key)",
                "ExpressionAttributeNames": {"#key": name},
            },
        )

    def revoke_all(self, github_id: int, keys: Any, now: int, course_end: datetime.date) -> None:
        """Revokes every key of the student not revoked yet, as `rhi_issuer.revoke.revoke_all` does,
        and holds each one's expiry to the course end plus 90 days (`rhi_common.retention.no_later`)."""
        for record in every_key(github_id, keys):
            if record.revoked_at is not None:
                continue
            student = KEYS.index(BY_STUDENT).partition_key.name
            self.write(
                "Update",
                {
                    "table": keys,
                    "TableName": keys.name,
                    "Key": {KEYS.partition_key.name: record.key_hash},
                    "UpdateExpression": "SET #revoked = :now, #expires = :expires",
                    "ConditionExpression": "attribute_exists(#student) AND attribute_not_exists(#revoked)",
                    "ExpressionAttributeNames": {"#student": student, "#revoked": "revoked_at", "#expires": EXPIRES_AT},
                    "ExpressionAttributeValues": {":now": now, ":expires": retention.no_later(record.expires_at, course_end)},
                },
            )

    def sweep(self, github_id: int, keys: Any, usage: Any) -> tuple[int, int]:
        """Deletes every key item of the student, their lock and every usage row; how many keys and rows it deleted."""
        deleted_keys = sum(
            self.delete(keys, {KEYS.partition_key.name: record.key_hash}) for record in every_key(github_id, keys)
        )
        self.delete(keys, lock_key(github_id))
        rows = 0
        for item in list(_usage_rows(github_id, usage)):
            parts = (USAGE.partition_key, USAGE.sort_key)
            rows += self.delete(usage, {part.name: item[part.name] for part in parts if part is not None})
        return deleted_keys, rows


def _any_left(github_id: int, keys: Any, usage: Any) -> bool:
    """Whether a key item, the issuer's lock or a usage row of the student is still there."""
    if every_key(github_id, keys) or next(_usage_rows(github_id, usage), None) is not None:
        return True
    return "Item" in keys.get_item(Key=lock_key(github_id), ConsistentRead=True)


def erase(
    github_id: int,
    *,
    roster: Any,
    keys: Any,
    stores: Stores,
    course_end: datetime.date,
    now: int,
    say: Callable[[str], None] = lambda _: None,
) -> Erased:
    """Deletes the student's entry, keys and usage rows, in the order of this module's docstring.

    Every row it writes, and leaves if it stops, expires by the course end plus 90 days
    (`rhi_common.retention`): the item that holds the GitHub ID, the one that holds the alias back,
    and each key it revokes.

    An erase that was interrupted can be run again: it takes the GitHub ID over and, without the
    entry, deletes what is left. Refused, before anything is written, when the GitHub ID is none,
    or when there is no row of it at all: no entry, no key, no lock, no usage row.
    """
    check_github_id(github_id)
    token = uuid.uuid4().hex
    begin_erase(github_id, roster=roster, token=token, until=retention.no_later(now + ERASE_HELD_SECONDS, course_end))
    guarded = _Guarded(github_id, roster, token)
    try:
        entry = get(github_id, roster=roster)
        if entry is None and not _any_left(github_id, keys, stores.usage):
            raise AdminError("that GitHub ID has no roster entry, no key and no usage row")
        if entry is not None:
            guard = erasing(github_id, roster=roster, token=token)
            hold_alias(entry, roster=roster, until=alias_held_until(now, course_end), guard=guard)
    except AdminError as error:
        if str(error) != ERASE_TAKEN_OVER:
            # Nothing was deleted: the GitHub ID is given back.
            end_erase(github_id, roster=roster, token=token)
        raise
    guarded.revoke_all(github_id, keys, now, course_end)
    say(
        f"The entry is removed and the keys revoked. Waiting {ISSUER_SECONDS + GATEWAY_SECONDS} seconds for "
        "the logins and the requests in flight before deleting the keys and the usage rows."
    )
    stores.sleep(ISSUER_SECONDS)
    guarded.revoke_all(github_id, keys, now + ISSUER_SECONDS, course_end)
    stores.sleep(GATEWAY_SECONDS)
    deleted_keys, rows = guarded.sweep(github_id, keys, stores.usage)
    if _any_left(github_id, keys, stores.usage):
        raise AdminError("rows of the student were written during the erase: run it again")
    end_erase(github_id, roster=roster, token=token)
    return Erased(
        entry=entry is not None, alias=None if entry is None else entry.alias, keys=deleted_keys, usage_rows=rows
    )


def report(erased: Erased, github_id: int) -> list[str]:
    """What the erase prints: what it deleted, then what only retention removes."""
    entry = "the roster entry" if erased.entry else "no roster entry (removed before)"
    alias = f"the alias {erased.alias}" if erased.alias else "the alias the student had"
    deleted = (
        f"Erased {github_id}: {entry}, {erased.keys} key(s) revoked and deleted, "
        f"{erased.usage_rows} usage row(s) with their affinity cookies."
    )
    retention = (
        f"Removed by retention only, {RETENTION_DAYS} days after each was written; with the entry gone, "
        f"{alias} names nobody:"
    )
    held = (
        f"No new roster entry is given {alias} for {ALIAS_HELD_DAYS} days, or until the course end plus "
        f"{RETENTION_DAYS} days if that is sooner, so that none is given what was written under it."
        if erased.entry
        else "The entry was gone before this erase, which therefore holds back no alias."
    )
    return [deleted, retention, *(f"- {line}" for line in RETENTION_ONLY), held]
