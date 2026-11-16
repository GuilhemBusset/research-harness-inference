"""The leak check: no value of a private key of the configuration is in the source tree (system
design, section 10). The synth step runs it after the configuration fetch, before synthesis.

No public pattern could name the real domain or e-mail address without leaking them, so the
check reads the real values from the fetched configuration, the file RHI_CONFIG_FILE names, and
searches every file of the tree for the value of each key the configuration loader names as
private (`rhi.config.PRIVATE_KEYS`), in any letter case. Numbers, dates and public values are not
searched for: they would match everywhere.

A hit fails the check, which is how the pipeline alerts the owner. It is reported by its file,
its line and the name of the key, never by the value: what the check prints ends up in the
build's logs. A path the check prints is redacted too: where a private value is part of a file's
or a folder's name, the name of its key stands in its place.

The source CodeBuild receives has no `.git`, so the check cannot tell a tracked file from an
untracked one, and searches them all. The fetched file must therefore lie outside the tree: when
it lies inside, the check refuses to run, naming the path, rather than reporting the file as a
leak. It skips only what a build adds to the tree and `.gitignore` names: git's folder, the
environments of uv and npm, the cloud assembly and the tools' caches.

Run by the synth step, with PYTHONPATH holding the import path of `pyproject.toml`:

    uv run --locked python scripts/pipeline/leak_check.py [--root <tree>]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

NAME = "leak_check"
# The tree the check searches unless told otherwise: the repository this script is in.
TREE = Path(__file__).resolve().parents[2]
# What a build adds to the tree, by folder name; `.gitignore` names each.
NOT_THE_SOURCE = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "cdk.out",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".ty_cache",
        ".mypy_cache",
    }
)


def files(tree: Path) -> Iterator[Path]:
    """Every file of the tree, but those under a folder a build adds, in a stable order."""
    for folder, subfolders, names in os.walk(tree):
        subfolders[:] = sorted(name for name in subfolders if name not in NOT_THE_SOURCE)
        for name in sorted(names):
            yield Path(folder, name)


def private_values(fetched: config.Config) -> dict[str, str]:
    """The value of every private key, by key, in the letter case the search compares."""
    return {key: str(getattr(fetched, key)).casefold() for key in sorted(config.PRIVATE_KEYS)}


def redacted(text: str, values: Mapping[str, str]) -> str:
    """The text with every private value, in any letter case, replaced by the name of its key.

    When a value survives the replacement (a letter whose case folding changes its length), the
    whole text is withheld rather than shown."""
    for key, value in sorted(values.items(), key=lambda item: len(item[1]), reverse=True):
        if value:
            text = re.sub(re.escape(value), f"<{key}>", text, flags=re.IGNORECASE)
    if any(value and value in text.casefold() for value in values.values()):
        return "<a path that holds a private value>"
    return text


def hits(tree: Path, values: Mapping[str, str]) -> Iterator[str]:
    """Each line of the tree that holds a value, as `<file>:<line>: <key>`, the file relative to
    the tree, redacted. A file that cannot be read is reported too: the check does not pass what
    it did not read."""
    for path in files(tree):
        shown = redacted(path.relative_to(tree).as_posix(), values)
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            yield f"{shown}: the file cannot be read"
            continue
        for number, line in enumerate(text.casefold().splitlines(), start=1):
            for key, value in values.items():
                if value in line:
                    yield f"{shown}:{number}: the value of the private key {key!r}"


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Runs the check; 0 when the tree holds no private value."""
    parser = argparse.ArgumentParser(prog=NAME, description="Searches the source tree for the private values.")
    parser.add_argument("--root", type=Path, default=TREE, help="the tree to search (default: this repository)")
    arguments = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    tree = arguments.root.resolve()

    named = env.get("RHI_CONFIG_FILE")
    if not named:
        print(f"{NAME}: RHI_CONFIG_FILE is not set: it names the fetched configuration", file=sys.stderr)
        return 1
    fetched_file = Path(named).absolute()
    source = {"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(fetched_file)}
    for candidate in (fetched_file, fetched_file.resolve()):
        if candidate.is_relative_to(tree):
            # The path is named, redacted with the file's own values when it can be read: a
            # private value may be part of its name.
            try:
                values = private_values(config.load(source))
            except config.ConfigError:
                values = {}
            print(
                f"{NAME}: RHI_CONFIG_FILE names a file inside the tree the check searches, "
                f"{redacted(candidate.relative_to(tree).as_posix(), values)}: "
                "the configuration must be fetched outside it",
                file=sys.stderr,
            )
            return 1
    try:
        fetched = config.load(source)
    except config.ConfigError as error:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 1

    found = list(hits(tree, private_values(fetched)))
    for hit in found:
        print(hit)
    if found:
        print(
            f"{NAME}: {len(found)} line(s) of the source tree hold a private value, or were not read", file=sys.stderr
        )
        return 1
    print(f"{NAME}: no private value in the source tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
