"""Every acknowledgement file keeps the format of `acknowledged/README.md` (system design, section 8.4).

The list of accepted findings is itself an invariant: an entry is accepted only with its reason,
in the file of a module that exists. This test checks every file of the folder. The tests that
use the entries read their own table: `test_iam_wildcards.py` reads `[wildcard_actions]`.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

TABLES = ("nag", "wildcard_actions")
# The fewest words taken for a sentence: one or two are a label, such as "needed" or "see above".
WORDS_OF_A_SENTENCE = 3


def is_a_sentence(reason: Any) -> bool:
    """A reason is text of at least one sentence: several words, ending with a full stop."""
    return isinstance(reason, str) and len(reason.split()) >= WORDS_OF_A_SENTENCE and reason.rstrip().endswith(".")


def violations(folder: Path, module_names: set[str]) -> list[str]:
    """What the folder holds besides well-formed acknowledgement files; empty when nothing."""
    found = []
    for path in sorted(folder.iterdir()):
        if path.name == "README.md":
            continue
        if not path.is_file() or path.suffix != ".toml":
            found.append(f"{path.name}: not an acknowledgement file")
            continue
        if path.stem not in module_names:
            found.append(f"{path.name}: no module of the app's source tree has this name")
        try:
            tables = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            found.append(f"{path.name}: not a TOML file")
            continue
        for table, entries in tables.items():
            if table not in TABLES or not isinstance(entries, dict):
                found.append(f"{path.name}: {table!r} is not one of the tables {' and '.join(TABLES)}")
                continue
            found.extend(
                f"{path.name}: [{table}] {key!r} has no reason of at least one sentence"
                for key, reason in entries.items()
                if not is_a_sentence(reason)
            )
    return found


def test_every_file_keeps_the_format(acknowledged_folder: Path, module_names: set[str]) -> None:
    assert (acknowledged_folder / "README.md").is_file()
    assert violations(acknowledged_folder, module_names) == []


def test_admits_a_well_formed_file(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("The convention.\n", encoding="utf-8")
    (tmp_path / "pools_group.toml").write_text(
        "[nag]\n"
        '"AwsSolutions-IAM5[Resource::*]" = "The Describe calls of EC2 have no resource to name."\n'
        "[wildcard_actions]\n"
        '"ec2:DescribeInstances" = "AWS grants it only on every resource."\n',
        encoding="utf-8",
    )
    assert violations(tmp_path, {"pools_group", "stage"}) == []


@pytest.mark.parametrize(
    ("name", "content"),
    [
        pytest.param("pools_fleet.toml", "", id="a module that does not exist"),
        pytest.param(
            "stage.toml", '[wildcard_actions]\n"ec2:DescribeInstances" = ""\n', id="an entry without a reason"
        ),
        pytest.param("stage.toml", '[nag]\n"AwsSolutions-S1" = "accepted"\n', id="a reason that is not a sentence"),
        pytest.param("stage.toml", '[nag]\n"AwsSolutions-S1" = 1\n', id="a reason that is not text"),
        pytest.param(
            "stage.toml", '[accepted]\n"AwsSolutions-S1" = "The bucket holds its own logs."\n', id="a third table"
        ),
        pytest.param("stage.toml", 'nag = "Everything is accepted here."\n', id="a table that is not one"),
        pytest.param("stage.toml", "[nag\n", id="not TOML"),
        pytest.param("stage.txt", "", id="not a TOML file by its name"),
    ],
)
def test_refuses_a_bad_file(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / name).write_text(content, encoding="utf-8")
    assert len(violations(tmp_path, {"pools_group", "stage"})) == 1
