"""The admin CLI's command for the spikes' canary students, against the tables and the secrets that
moto holds: the fixture `admin` of this folder's `conftest.py`. Every key comes from the command
while the tests run: none is written in this file.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from rhi_admin import main, roster
from rhi_admin.roster import DEFAULT_DAILY_TOKEN_BUDGET
from rhi_common import spike_keys
from rhi_common.keys import KEY_PATTERN, is_valid_key, key_hash
from rhi_common.students import StudentKey

if TYPE_CHECKING:
    import pytest

NOW = 1_800_000_000


def stored(admin: main.Admin, key: str) -> StudentKey:
    """The item the keys table holds for a key."""
    return StudentKey.from_item(admin.keys.get_item(Key={"key_hash": key_hash(key)})["Item"])


def test_the_command_fills_the_spikes_secret_and_prints_no_key(
    admin: main.Admin, capsys: pytest.CaptureFixture[str]
) -> None:
    printed, runs = "", []
    for run in range(2):
        # A later run is later, so that a revocation shows which run made it.
        assert main.run(["spike-keys", "3"], admin, now=NOW + run) == 0
        output = capsys.readouterr()
        printed += output.out + output.err
        # One secret, whether the command made it or found it.
        assert [secret["Name"] for secret in admin.secrets.list_secrets()["SecretList"]] == [
            spike_keys.SPIKE_KEYS_SECRET
        ]
        runs.append(spike_keys.read(admin.secrets))
    first, second = runs

    # The secret holds the students' keys and nothing else.
    value = admin.secrets.get_secret_value(SecretId=spike_keys.SPIKE_KEYS_SECRET)["SecretString"]
    assert set(json.loads(value)) == {"students"}

    # Three students with the default budget, each under its fixed alias, and no one else.
    assert len(second.students) == 3
    assert all(is_valid_key(key) for key in second.students)
    students = [stored(admin, key) for key in second.students]
    assert [(record.alias, record.daily_token_budget) for record in students] == [
        (f"spike-{number}", DEFAULT_DAILY_TOKEN_BUDGET) for number in (1, 2, 3)
    ]
    assert len({record.github_id for record in students}) == 3
    assert all(record.revoked_at is None for record in students)

    # The second run replaced all three: none is kept, and the first run's are revoked.
    earlier = first.students
    assert not set(earlier) & set(second.students)
    assert [stored(admin, key).revoked_at for key in earlier] == [NOW + 1] * 3
    # The same three students both times, and only they are on the roster.
    entries = roster.entries(roster=admin.roster)
    assert sorted(entry.alias for entry in entries) == ["spike-1", "spike-2", "spike-3"]

    assert "3 student" in printed
    assert re.search(KEY_PATTERN, printed) is None
    assert not any(key in printed for key in (*earlier, *second.students))


def test_a_run_for_no_student_is_refused_and_writes_nothing(
    admin: main.Admin, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main.run(["spike-keys", "0"], admin, now=NOW) == 1

    assert "Refused" in capsys.readouterr().err
    assert admin.secrets.list_secrets()["SecretList"] == []
    assert admin.roster.scan(ConsistentRead=True)["Items"] == []
