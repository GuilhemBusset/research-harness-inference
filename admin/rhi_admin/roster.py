"""The roster: who may log in, under which alias, with what budget (system design, sections 6.4, 6.6 and 17).

An entry is keyed by the student's numeric GitHub ID, which a change of handle does not change,
and carries the alias that per-student series and logs use instead of the ID. It also carries the
student's daily token budget (input plus output, cached input included) and their limit of
concurrent requests. The defaults, 100 million tokens and 4 requests, are the admin CLI's own:
nothing else reads them, and only when an entry is written. A budget of 0 is kept as 0: it is the
spent-budget student of the smoke tests.

Budgets travel with the keys: the issuer copies the entry's budget and limit into each key it
mints, and the gateway reads the key item alone. `change` therefore writes the new values to the
entry and to each of the student's active key items; the gateway applies them within its
60-second cache. `remove` deletes the entry, so no login can be issued a key any more, and then
revokes every key of the student.

An alias taken off the roster, by `remove` or by an erase (`rhi_admin.privacy`), stays out of reach
while what was written under it is kept: the series and the log records under an alias cannot tell
two students apart, so another student given it would be given the first one's too, and a request
of the first one still running could add to them. `hold_alias` replaces the entry, in one write,
by an item that holds the alias and its expiry and nothing else: no GitHub ID, no budget. Its
partition key is a negative number made from the alias, which no GitHub ID is, so no login and no
command reads it as a student; `add` refuses the alias until the table's expiry removes the item,
and `entries` lists none. A student put back on the roster is given another alias.

While an erase runs, `begin_erase` holds the GitHub ID the same way, under a negative key of its
own and with the erase's own token, until `end_erase`: `add` refuses that GitHub ID meanwhile.
Every write of the erase checks, in the same transaction, that the item still holds its token
(`erasing`), so that an erase run again takes the GitHub ID over, the first one writes nothing more,
and no erase deletes a key or a usage row of an entry added after the GitHub ID was given back.
`add` reads both items by their keys, in the transaction that writes the entry, never through the
alias index, which DynamoDB updates after the write.

Every function takes the tables as the table resources of the AWS SDK and the time as whole
seconds since the epoch. Nothing here prints or logs: the command line (`rhi_admin.main`) does.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from typing import Any

from botocore.exceptions import ClientError

from rhi_common.students import RosterEntry
from rhi_common.tables import BY_ALIAS, BY_STUDENT, EXPIRES_AT, KEYS, ROSTER
from rhi_issuer.mint import expiry
from rhi_issuer.revoke import revoke_all
from rhi_issuer.rules import every_key

DEFAULT_DAILY_TOKEN_BUDGET = 100_000_000
DEFAULT_CONCURRENCY_LIMIT = 4
# Every store is kept until the course end plus 90 days (section 6.6); a key stops at the course end.
RETENTION_DAYS = 90
# How long an alias taken off the roster is held back from any other entry: the retention of what
# was written under it, and a day for the requests still running.
ALIAS_HELD_DAYS = RETENTION_DAYS + 1
# What an erase that another erase of the same GitHub ID took over says, as it stops.
ERASE_TAKEN_OVER = "another erase of that GitHub ID took over: this one stopped, and that one finishes the erase"

# An alias is a label of a metric series: short, lower case, and never a number, so that it cannot
# be a GitHub ID.
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)
_CONDITION_FAILED = "ConditionalCheckFailedException"


class AdminError(Exception):
    """A command was refused. The message is fixed text and names no AWS output."""


def _check_alias(alias: str) -> None:
    if _ALIAS.fullmatch(alias) is None:
        raise AdminError("an alias is 1 to 32 lower-case letters, digits, '-' or '_', starting with a letter")


def _check_numbers(daily_token_budget: int | None, concurrency_limit: int | None) -> None:
    for number in (daily_token_budget, concurrency_limit):
        if number is not None and (type(number) is not int or number < 0):
            raise AdminError("a budget and a limit are whole numbers, 0 or more")


def _failed_condition(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == _CONDITION_FAILED


def check_github_id(github_id: int) -> None:
    """Refuses what is no GitHub ID: GitHub numbers its accounts from 1, and the canary is 0."""
    if type(github_id) is not int or github_id < 0:
        raise AdminError("a GitHub ID is a whole number, 0 or more")


def held_alias_id(alias: str) -> int:
    """The partition key of the item that holds back an alias: a negative number, never a GitHub ID."""
    return -1 - int.from_bytes(hashlib.sha256(alias.encode()).digest()[:7], "big")


def hold_alias(entry: RosterEntry, *, roster: Any, until: int, guard: dict[str, Any] | None = None) -> None:
    """Replaces the entry by the item that holds back its alias until `until`, in one write.

    Refused when the entry is no longer the one given (removed, or given another alias meanwhile),
    or when `guard`, a condition the same write checks (`erasing`), no longer holds.
    """
    held = {ROSTER.partition_key.name: held_alias_id(entry.alias), "alias": entry.alias, EXPIRES_AT: until}
    delete = {
        "TableName": roster.name,
        "Key": {ROSTER.partition_key.name: entry.github_id},
        "ConditionExpression": "#alias = :alias AND #added = :added",
        "ExpressionAttributeNames": {"#alias": "alias", "#added": "added_at"},
        "ExpressionAttributeValues": {":alias": entry.alias, ":added": entry.added_at},
    }
    writes = [{"Delete": delete}, {"Put": {"TableName": roster.name, "Item": held}}]
    try:
        # The table resource's client, which takes the item's values as the table resource does.
        roster.meta.client.transact_write_items(TransactItems=writes + ([guard] if guard else []))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [reason.get("Code") for reason in error.response.get("CancellationReasons") or []]
        if guard and reasons[2:3] == ["ConditionalCheckFailed"]:
            raise AdminError(ERASE_TAKEN_OVER) from None
        raise AdminError("the roster entry changed meanwhile: run the command again") from None


def erasing_id(github_id: int) -> int:
    """The partition key of the item that holds a GitHub ID while it is erased: below every `held_alias_id`."""
    check_github_id(github_id)
    return -(2**60) - github_id


def begin_erase(github_id: int, *, roster: Any, token: str, until: int) -> None:
    """Holds the GitHub ID back from `add` until `end_erase`, or the table's expiry at `until`.

    The item names this erase by `token`: an erase run again takes it over, and from then on the
    first one writes nothing more (`erasing`).
    """
    roster.put_item(Item={ROSTER.partition_key.name: erasing_id(github_id), "erase": token, EXPIRES_AT: until})


def erasing(github_id: int, *, roster: Any, token: str) -> dict[str, Any]:
    """The condition, for a transaction, that the GitHub ID is still held by the erase `token`."""
    return {
        "ConditionCheck": {
            "TableName": roster.name,
            "Key": {ROSTER.partition_key.name: erasing_id(github_id)},
            "ConditionExpression": "#erase = :token",
            "ExpressionAttributeNames": {"#erase": "erase"},
            "ExpressionAttributeValues": {":token": token},
        }
    }


def end_erase(github_id: int, *, roster: Any, token: str) -> None:
    """Gives the GitHub ID back to `add`, once the erase `token` has deleted everything; not if another took it over."""
    try:
        roster.delete_item(
            Key={ROSTER.partition_key.name: erasing_id(github_id)},
            ConditionExpression="#erase = :token",
            ExpressionAttributeNames={"#erase": "erase"},
            ExpressionAttributeValues={":token": token},
        )
    except ClientError as error:
        if not _failed_condition(error):
            raise


def get(github_id: int, *, roster: Any) -> RosterEntry | None:
    """The entry of that student, read with a consistent read; `None` when there is none."""
    item = roster.get_item(Key={ROSTER.partition_key.name: github_id}, ConsistentRead=True).get("Item")
    return None if item is None else RosterEntry.from_item(item)


def _alias_owner(alias: str, roster: Any) -> int | None:
    index = ROSTER.index(BY_ALIAS)
    answer = roster.query(
        IndexName=index.name,
        KeyConditionExpression="#alias = :alias",
        ExpressionAttributeNames={"#alias": index.partition_key.name},
        ExpressionAttributeValues={":alias": alias},
    )
    for item in answer.get("Items") or []:
        return int(item[ROSTER.partition_key.name])
    return None


def add(
    github_id: int,
    alias: str,
    *,
    roster: Any,
    course_end: datetime.date,
    now: int,
    daily_token_budget: int | None = None,
    concurrency_limit: int | None = None,
) -> RosterEntry:
    """Adds a student; refused when the GitHub ID is already on the roster or another student has the alias."""
    check_github_id(github_id)
    _check_alias(alias)
    _check_numbers(daily_token_budget, concurrency_limit)
    owner = _alias_owner(alias, roster)
    if owner is not None and owner < 0:
        raise AdminError("that alias was taken off the roster, and is held back while its series and log records are kept")
    if owner is not None and owner != github_id:
        raise AdminError("another roster entry has that alias")
    entry = RosterEntry(
        github_id=github_id,
        alias=alias,
        daily_token_budget=DEFAULT_DAILY_TOKEN_BUDGET if daily_token_budget is None else daily_token_budget,
        concurrency_limit=DEFAULT_CONCURRENCY_LIMIT if concurrency_limit is None else concurrency_limit,
        added_at=now,
        expires_at=expiry(course_end) + RETENTION_DAYS * 86400,
    )
    absent = {"ConditionExpression": "attribute_not_exists(#id)", "ExpressionAttributeNames": {"#id": ROSTER.partition_key.name}}

    def nothing_at(key: int) -> dict[str, Any]:
        return {"ConditionCheck": {"TableName": roster.name, "Key": {ROSTER.partition_key.name: key}, **absent}}

    # The alias's hold and the GitHub ID's erase are read by their keys, in the write itself.
    refusals = (
        "that alias was taken off the roster, and is held back while its series and log records are kept",
        "that GitHub ID is being erased: wait for the erase, or run it again if it stopped",
        "that GitHub ID is already on the roster",
    )
    try:
        # The table resource's client, which takes the item's values as the table resource does.
        roster.meta.client.transact_write_items(
            TransactItems=[
                nothing_at(held_alias_id(alias)),
                nothing_at(erasing_id(github_id)),
                {"Put": {"TableName": roster.name, "Item": entry.to_item(), **absent}},
            ]
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [reason.get("Code") for reason in error.response.get("CancellationReasons") or []]
        for reason, refusal in zip(reasons, refusals, strict=False):
            if reason == "ConditionalCheckFailed":
                raise AdminError(refusal) from None
        raise
    return entry


def entries(*, roster: Any) -> list[RosterEntry]:
    """Every entry of the roster, by GitHub ID."""
    scan: dict[str, Any] = {"ConsistentRead": True}
    found: list[RosterEntry] = []
    while True:
        answer = roster.scan(**scan)
        # A held-back alias (`hold_alias`) is no entry.
        items = [item for item in answer.get("Items") or [] if int(item[ROSTER.partition_key.name]) >= 0]
        found.extend(RosterEntry.from_item(item) for item in items)
        if "LastEvaluatedKey" not in answer:
            return sorted(found, key=lambda entry: entry.github_id)
        scan["ExclusiveStartKey"] = answer["LastEvaluatedKey"]


def remove(github_id: int, *, roster: Any, keys: Any, now: int) -> int:
    """Removes the student's entry and revokes every key they hold; how many keys this revoked.

    The entry goes first, so that no login is issued a key while the keys are revoked. It is
    replaced, in the same write, by the item that holds its alias back for `ALIAS_HELD_DAYS`
    (`hold_alias`): what was written under the alias stays the student's alone. Refused when the
    student is not on the roster; `rhi_admin.keys.revoke` revokes keys without an entry.
    """
    entry = get(github_id, roster=roster)
    if entry is None:
        raise AdminError("that GitHub ID is not on the roster")
    hold_alias(entry, roster=roster, until=now + ALIAS_HELD_DAYS * 86_400)
    return revoke_all(github_id, keys=keys, now=now)


def change(
    github_id: int,
    *,
    roster: Any,
    keys: Any,
    now: int,
    daily_token_budget: int | None = None,
    concurrency_limit: int | None = None,
) -> int:
    """Writes a new budget, limit or both to the entry and to each active key item; how many keys it changed.

    A key revoked in the meantime is left as it is: the write to a key is conditional on it not
    being revoked.
    """
    _check_numbers(daily_token_budget, concurrency_limit)
    values = {
        name: value
        for name, value in (("daily_token_budget", daily_token_budget), ("concurrency_limit", concurrency_limit))
        if value is not None
    }
    if not values:
        raise AdminError("give a budget, a limit or both")
    assignments = ", ".join(f"#{name} = :{name}" for name in values)
    names = {f"#{name}": name for name in values}
    numbers = {f":{name}": value for name, value in values.items()}
    try:
        roster.update_item(
            Key={ROSTER.partition_key.name: github_id},
            UpdateExpression=f"SET {assignments}",
            ConditionExpression="attribute_exists(#id)",
            ExpressionAttributeNames={**names, "#id": ROSTER.partition_key.name},
            ExpressionAttributeValues=numbers,
        )
    except ClientError as error:
        if _failed_condition(error):
            raise AdminError("that GitHub ID is not on the roster") from None
        raise
    changed = 0
    for record in every_key(github_id, keys):
        if record.revoked_at is not None or record.expires_at <= now:
            continue
        try:
            keys.update_item(
                Key={KEYS.partition_key.name: record.key_hash},
                UpdateExpression=f"SET {assignments}",
                ConditionExpression="attribute_exists(#student) AND attribute_not_exists(#revoked)",
                ExpressionAttributeNames={
                    **names,
                    "#student": KEYS.index(BY_STUDENT).partition_key.name,
                    "#revoked": "revoked_at",
                },
                ExpressionAttributeValues=numbers,
            )
        except ClientError as error:
            if _failed_condition(error):
                continue
            raise
        changed += 1
    return changed
