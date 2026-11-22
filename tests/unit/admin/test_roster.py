"""The admin CLI's commands, against tables, a secret and a parameter that moto holds: the fixture
`admin` of this folder's `conftest.py`. Every key comes from a command while the tests run: none
is written in this file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from rhi_admin import main
from rhi_admin import roster as admin_roster
from rhi_admin.canary import CANARY_ALIAS, CANARY_GITHUB_ID, CANARY_SECRET
from rhi_common.keys import is_valid_key, key_hash
from rhi_common.students import RosterEntry, StudentKey

REPO = Path(__file__).resolve().parents[3]
NOW = 1_800_000_000
OTTER, HERON = 4242, 5151


def cli(admin: main.Admin, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    """Runs one command; its exit status, and everything it printed, out and err."""
    code = main.run(argv, admin, now=NOW)
    printed = capsys.readouterr()
    return code, printed.out + printed.err


def items(table: Any) -> list[dict[str, Any]]:
    return table.scan(ConsistentRead=True)["Items"]


def student_keys(admin: main.Admin, github_id: int) -> list[StudentKey]:
    return [StudentKey.from_item(item) for item in items(admin.keys) if item["github_id"] == github_id]


def test_an_entry_is_added_listed_and_removed(admin: main.Admin, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(admin, capsys, "roster", "add", str(OTTER), "otter")[0] == 0
    assert cli(admin, capsys, "roster", "add", str(HERON), "heron", "--limit", "2")[0] == 0
    # Without a limit, the default; there is no budget to give.
    # The roster's other items, under negative keys, are its aliases' own.
    entries = {item["github_id"]: RosterEntry.from_item(item) for item in items(admin.roster) if item["github_id"] >= 0}
    assert (entries[OTTER].concurrency_limit, entries[HERON].concurrency_limit) == (4, 2)
    assert all("daily_token_budget" not in item for item in items(admin.roster))
    code, listed = cli(admin, capsys, "roster", "list")
    assert code == 0
    assert re.search(rf"^{OTTER}\totter\t4$", listed, re.MULTILINE)
    assert re.search(rf"^{HERON}\theron\t2$", listed, re.MULTILINE)
    assert "budget" not in listed
    # A second entry for the ID, or the alias of another student, is refused.
    assert cli(admin, capsys, "roster", "add", str(OTTER), "stoat")[0] == 1
    assert cli(admin, capsys, "roster", "add", "6161", "otter")[0] == 1

    for _ in range(2):
        assert cli(admin, capsys, "key", "issue", str(OTTER))[0] == 0
    assert cli(admin, capsys, "roster", "remove", str(OTTER))[0] == 0

    assert str(OTTER) not in cli(admin, capsys, "roster", "list")[1]
    otter_keys = student_keys(admin, OTTER)
    assert len(otter_keys) == 2
    assert all(record.revoked_at == NOW for record in otter_keys)
    # Once off the roster, the student is issued no key.
    assert cli(admin, capsys, "key", "issue", str(OTTER))[0] == 1
    # What was written under the alias stays the student's: no other entry is given it.
    code, refused = cli(admin, capsys, "roster", "add", "6161", "otter")
    assert code == 1 and "held back" in refused
    assert cli(admin, capsys, "roster", "add", str(OTTER), "stoat")[0] == 0



def test_two_entries_added_at_once_are_never_given_one_alias(
    admin: main.Admin, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = admin.roster.meta.client
    transact = client.transact_write_items
    others: list[tuple[int, str]] = []

    def another_addition_first(**request: Any) -> Any:
        # The first addition has looked the alias up and found it free; another is made before it writes.
        monkeypatch.setattr(client, "transact_write_items", transact)
        others.append(cli(admin, capsys, "roster", "add", str(HERON), "stoat"))
        return transact(**request)

    monkeypatch.setattr(client, "transact_write_items", another_addition_first)
    code, refused = cli(admin, capsys, "roster", "add", str(OTTER), "stoat")

    assert [status for status, _ in others] == [0]
    assert code == 1 and "another roster entry has that alias" in refused
    assert [(entry.github_id, entry.alias) for entry in admin_roster.entries(roster=admin.roster)] == [(HERON, "stoat")]


def test_a_directly_issued_key_verifies_and_is_stored_as_its_sha256_only(
    admin: main.Admin, capsys: pytest.CaptureFixture[str]
) -> None:
    cli(admin, capsys, "roster", "add", str(OTTER), "otter", "--limit", "3")

    code, printed = cli(admin, capsys, "key", "issue", str(OTTER))

    assert code == 0
    key = printed.strip()
    assert is_valid_key(key)
    (item,) = items(admin.keys)
    record = StudentKey.from_item(item)
    assert record.key_hash == key_hash(key)
    assert (record.alias, record.concurrency_limit) == ("otter", 3)
    assert "daily_token_budget" not in item
    assert record.installation is None
    assert record.revoked_at is None
    assert key not in json.dumps(items(admin.keys) + items(admin.roster), default=str)

    assert cli(admin, capsys, "key", "revoke", str(OTTER))[0] == 0
    assert student_keys(admin, OTTER)[0].revoked_at == NOW


def test_a_limit_change_reaches_the_keys(admin: main.Admin, capsys: pytest.CaptureFixture[str]) -> None:
    cli(admin, capsys, "roster", "add", str(OTTER), "otter")
    cli(admin, capsys, "roster", "add", str(HERON), "heron")
    issued = [cli(admin, capsys, "key", "issue", str(OTTER))[1].strip() for _ in range(3)]
    cli(admin, capsys, "key", "issue", str(HERON))
    revoked = issued[0]
    admin.keys.update_item(
        Key={"key_hash": key_hash(revoked)},
        UpdateExpression="SET revoked_at = :now",
        ExpressionAttributeValues={":now": NOW - 60},
    )

    # A change needs a limit.
    assert cli(admin, capsys, "roster", "set", str(OTTER))[0] == 1
    assert cli(admin, capsys, "roster", "set", str(OTTER), "--limit", "1")[0] == 0

    roster_item = admin.roster.get_item(Key={"github_id": OTTER})["Item"]
    assert roster_item["concurrency_limit"] == 1
    by_hash = {record.key_hash: record for record in student_keys(admin, OTTER)}
    for key in issued[1:]:
        assert by_hash[key_hash(key)].concurrency_limit == 1
    assert by_hash[key_hash(revoked)].concurrency_limit == 4
    # Another student's key is left alone, and no item is given a budget.
    assert student_keys(admin, HERON)[0].concurrency_limit == 4
    assert all("daily_token_budget" not in item for item in items(admin.keys) + items(admin.roster))


def test_an_entry_the_release_before_wrote_with_a_budget_is_listed_and_issued_a_key_without_it(
    admin: main.Admin, capsys: pytest.CaptureFixture[str]
) -> None:
    cli(admin, capsys, "roster", "add", str(OTTER), "otter", "--limit", "3")
    admin.roster.update_item(
        Key={"github_id": OTTER},
        UpdateExpression="SET daily_token_budget = :budget",
        ExpressionAttributeValues={":budget": 0},
    )

    code, listed = cli(admin, capsys, "roster", "list")
    assert code == 0 and re.search(rf"^{OTTER}\totter\t3$", listed, re.MULTILINE)
    assert cli(admin, capsys, "key", "issue", str(OTTER))[0] == 0
    (item,) = items(admin.keys)
    assert (StudentKey.from_item(item).concurrency_limit, "daily_token_budget" in item) == (3, False)


def test_the_roster_commands_take_no_budget() -> None:
    for argv in (["roster", "add", str(OTTER), "otter", "--budget", "5"], ["roster", "set", str(OTTER), "--budget", "5"]):
        with pytest.raises(SystemExit):
            main.parser().parse_args(argv)


def test_the_canary_key_is_never_printed(admin: main.Admin, capsys: pytest.CaptureFixture[str]) -> None:
    outputs = []
    for _ in range(2):
        code, printed = cli(admin, capsys, "canary")
        assert code == 0
        outputs.append(printed)

    (secret,) = admin.secrets.list_secrets()["SecretList"]
    assert secret["Name"] == CANARY_SECRET
    key = admin.secrets.get_secret_value(SecretId=CANARY_SECRET)["SecretString"]
    assert is_valid_key(key)
    canary_keys = student_keys(admin, CANARY_GITHUB_ID)
    assert len(canary_keys) == 2
    (active,) = [record for record in canary_keys if record.revoked_at is None]
    assert active.key_hash == key_hash(key)
    assert active.alias == CANARY_ALIAS
    # The first key is neither active nor printed, and the second is not printed either.
    assert re.search(r"rhi_[0-9A-Za-z]{46}", "".join(outputs)) is None
    assert key not in "".join(outputs)


def test_the_tables_are_found_by_their_pinned_logical_ids() -> None:
    pinned = json.loads((REPO / "tests/policy/stateful_logical_ids.json").read_text())
    assert {main.KEYS_LOGICAL_ID, main.ROSTER_LOGICAL_ID} <= set(pinned[main.STACK_NAME])
    # The CLI names no account, no region and no table: the caller's credentials give them.
    source = "".join(path.read_text() for path in (REPO / "admin/rhi_admin").glob("*.py"))
    assert re.search(r"\b\d{12}\b", source) is None
    assert re.search(r"\b(?:us|eu|ap|sa|ca|me|af|il|mx)-[a-z]+-\d\b", source) is None
    # A table's name comes from its resource (`table.name`), never from a literal.
    assert re.search(r"""TableName["']?\s*[:=]\s*f?["']""", source) is None
