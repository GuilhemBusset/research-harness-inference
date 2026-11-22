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
of the first one still running could add to them. Each alias therefore has an item of its own,
whose partition key is a negative number made from the alias (`held_alias_id`), which no GitHub
ID is, so no login and no command reads it as a student, and `entries` lists none. `add` writes it,
in the transaction that writes the entry and only if it is not there, naming the entry's GitHub
ID: of two additions given one alias at once, one is refused. `hold_alias` replaces the entry and
that item, in one write, by an item that holds the alias and its expiry and nothing else: no
GitHub ID, no budget. `add` refuses the alias until the table's expiry removes it, `ALIAS_HELD_DAYS`
later and never after the course end plus 90 days (`alias_held_until`). A student put
back on the roster is given another alias.

While an erase runs, `begin_erase` holds the GitHub ID the same way, under a negative key of its
own and with the erase's own token, until `end_erase`: `add` refuses that GitHub ID meanwhile.
Every write of the erase checks, in the same transaction, that the item still holds its token
(`erasing`), so that an erase run again takes the GitHub ID over, the first one writes nothing more,
and no erase deletes a key or a usage row of an entry added after the GitHub ID was given back.
`add` reads both items by their keys, in the transaction that writes the entry, never through the
alias index, which DynamoDB updates after the write. That index still finds the alias of an entry
added before aliases had items of their own.

Every function takes the tables as the table resources of the AWS SDK and the time as whole
seconds since the epoch. Nothing here prints or logs: the command line (`rhi_admin.main`) does.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from typing import Any

from botocore.exceptions import ClientError

from rhi_common import retention
from rhi_common.students import RosterEntry
from rhi_common.tables import BY_ALIAS, BY_STUDENT, EXPIRES_AT, KEYS, ROSTER
from rhi_issuer.revoke import revoke_all
from rhi_issuer.rules import every_key

DEFAULT_DAILY_TOKEN_BUDGET = 100_000_000
DEFAULT_CONCURRENCY_LIMIT = 4
# Every store is kept until the course end plus 90 days (section 6.6); a key stops at the course end.
RETENTION_DAYS = retention.RETENTION_DAYS
# How long an alias taken off the roster is held back from any other entry: the retention of what
# was written under it, and a day for the requests still running; never past the course end plus
# 90 days, when the hold itself, a personal row, expires (`alias_held_until`).
ALIAS_HELD_DAYS = RETENTION_DAYS + 1
# The attribute of an alias's own item that names the entry given the alias (`add`).
CLAIMED_BY = "claimed_by"
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
        expires_at=retention.expires_at(course_end),
    )
    absent = {"ConditionExpression": "attribute_not_exists(#id)", "ExpressionAttributeNames": {"#id": ROSTER.partition_key.name}}
    # The alias's own item, which the alias's hold replaces (`hold_alias`); it goes with the entry.
    claim = {ROSTER.partition_key.name: held_alias_id(alias), CLAIMED_BY: github_id, EXPIRES_AT: entry.expires_at}
    # The alias and the GitHub ID's erase are read by their keys, in the write itself.
    writes = [
        {"Put": {"TableName": roster.name, "Item": claim, **absent}},
        {"ConditionCheck": {"TableName": roster.name, "Key": {ROSTER.partition_key.name: erasing_id(github_id)}, **absent}},
        {"Put": {"TableName": roster.name, "Item": entry.to_item(), **absent}},
    ]
    try:
        # The table resource's client, which takes the item's values as the table resource does.
        roster.meta.client.transact_write_items(TransactItems=writes)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        failed = [reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons") or []]
        if failed[1:2] == [True]:
            raise AdminError("that GitHub ID is being erased: wait for the erase, or run it again if it stopped") from None
        if failed[2:3] == [True]:
            raise AdminError("that GitHub ID is already on the roster") from None
        if failed[:1] == [True]:
            raise AdminError(_alias_refusal(alias, github_id, roster)) from None
        raise
    return entry


def _alias_refusal(alias: str, github_id: int, roster: Any) -> str:
    """Why `add` found the alias's item there: the GitHub ID's own entry, another entry, or a hold."""
    key = {ROSTER.partition_key.name: held_alias_id(alias)}
    claimed = (roster.get_item(Key=key, ConsistentRead=True).get("Item") or {}).get(CLAIMED_BY)
    if claimed is None:
        return "that alias was taken off the roster, and is held back while its series and log records are kept"
    if int(claimed) == github_id:
        return "that GitHub ID is already on the roster"
    return "another roster entry has that alias"


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


def alias_held_until(now: int, course_end: datetime.date) -> int:
    """Until when an alias taken off the roster at `now` is held back: `ALIAS_HELD_DAYS` later, and
    never after the course end plus 90 days, when every personal row expires (`rhi_common.retention`)."""
    return retention.no_later(now + ALIAS_HELD_DAYS * 86_400, course_end)


def remove(github_id: int, *, roster: Any, keys: Any, course_end: datetime.date, now: int) -> int:
    """Removes the student's entry and revokes every key they hold; how many keys this revoked.

    The entry goes first, so that no login is issued a key while the keys are revoked. It is
    replaced, in the same write, by the item that holds its alias back until `alias_held_until`
    (`hold_alias`): what was written under the alias stays the student's alone. Refused when the
    student is not on the roster; `rhi_admin.keys.revoke` revokes keys without an entry.
    """
    entry = get(github_id, roster=roster)
    if entry is None:
        raise AdminError("that GitHub ID is not on the roster")
    hold_alias(entry, roster=roster, until=alias_held_until(now, course_end))
    return revoke_all(github_id, keys=keys, now=now, course_end=course_end)


def change(
    github_id: int,
    *,
    roster: Any,
    keys: Any,
    course_end: datetime.date,
    now: int,
    daily_token_budget: int | None = None,
    concurrency_limit: int | None = None,
) -> int:
    """Writes a new budget, limit or both to the entry and to each active key item; how many keys it changed.

    A key revoked in the meantime is left as it is: the write to a key is conditional on it not
    being revoked. The write to the entry also sets its expiry, the course end plus 90 days, as
    every write of an entry does (`add`), and the write to a key holds its expiry to that date.
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
            UpdateExpression=f"SET {assignments}, #expires = :expires",
            ConditionExpression="attribute_exists(#id)",
            ExpressionAttributeNames={**names, "#id": ROSTER.partition_key.name, "#expires": EXPIRES_AT},
            ExpressionAttributeValues={**numbers, ":expires": retention.expires_at(course_end)},
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
                UpdateExpression=f"SET {assignments}, #expires = :expires",
                ConditionExpression="attribute_exists(#student) AND attribute_not_exists(#revoked)",
                ExpressionAttributeNames={
                    **names,
                    "#student": KEYS.index(BY_STUDENT).partition_key.name,
                    "#revoked": "revoked_at",
                    "#expires": EXPIRES_AT,
                },
                ExpressionAttributeValues={**numbers, ":expires": retention.no_later(record.expires_at, course_end)},
            )
        except ClientError as error:
            if _failed_condition(error):
                continue
            raise
        changed += 1
    return changed
