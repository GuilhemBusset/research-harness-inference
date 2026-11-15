"""The configuration step of the owner's walkthrough (setup/owner-setup.sh): the owner's file,
checked by the configuration loader and written to SSM by the configuration writer. Its second
half writes the file's private values to the hygiene scanner's list of local values.

    python -m rhi.owner_config check <file>   # the loader reads the file; nothing is written
    python -m rhi.owner_config write <file>   # the same check, then one parameter per key in SSM
    python -m rhi.owner_config values <file> --list <list>   # the private values, onto the list

The file goes through the loader's own `file` source and to SSM through `write_ssm`
(src/rhi/config.py), so the walkthrough and the pipeline read the same keys the same way. A file
the loader refuses is refused here, before anything is written, in the loader's words: they name
a key or a source and never a value. Nothing this module prints holds a value of the file.

`values` adds the value of each key of `config.PRIVATE_KEYS`, and each line of its standard
input, to the list the scanner reports as `local-value` (tools/hygiene/README.md, "Values only
the owner knows"): one value per line, in a file only its owner can read.

That the file and the list lie outside the working tree is the walkthrough's check, made before
it starts this module. Only `write` reaches AWS, with the caller's credentials and region.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# What the scanner (tools/hygiene/scan.py, `load_local_values`) asks of a line of the list: a
# shorter value stops every scan of the machine, and a line that starts with `#` is a comment.
VALUE_MIN = 4


class ListError(Exception):
    """The list of local values was refused. The message never holds a value."""


def _listed(text: str) -> list[str]:
    """The values a list holds, as the scanner reads them."""
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def write_values(loaded: config.Config, further: Iterable[str], path: Path) -> int:
    """Adds the private values of the configuration, then the further ones, to the list at
    `path`, each only if the list does not hold it yet, and returns how many values the list
    then holds. The list is created for its owner alone, and one that exists is made so."""
    values = {f"the value of {key!r}": str(getattr(loaded, key)).strip() for key in sorted(config.PRIVATE_KEYS)}
    entered = [line.strip() for line in further]
    values |= {f"entered value {number}": value for number, value in enumerate(filter(None, entered), start=1)}
    for name, value in values.items():
        if len(value) < VALUE_MIN or value.startswith("#") or len(value.splitlines()) != 1:
            raise ListError(f"{name} cannot go on the list: a value is one line of {VALUE_MIN} characters or more")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "rb+") as file:
            os.fchmod(descriptor, 0o600)
            text = file.read().decode("utf-8-sig")
            listed = _listed(text)
            # The scanner matches a value in any letter case, so that is how one is "on the list".
            held = {value.casefold() for value in listed}
            new = {value.casefold(): value for value in values.values() if value.casefold() not in held}
            lines = "".join(f"{value}\n" for value in new.values())
            if text and not text.endswith("\n"):
                lines = "\n" + lines
            file.write(lines.encode())
    except (OSError, UnicodeDecodeError) as error:
        raise ListError(f"the list cannot be read and written: {error.__class__.__name__}") from error
    return len(set(listed)) + len(new)


def main(arguments: Sequence[str] | None = None, client: Any = None) -> int:
    """Checks the file, and writes it to SSM, or its private values to the list, when asked to.
    `client` is as for `config.write_ssm`. Returns the exit status: 0, or 1 on a refusal."""
    parser = argparse.ArgumentParser(prog="python -m rhi.owner_config", description=__doc__.partition("\n\n")[0])
    parser.add_argument(
        "action",
        choices=("check", "write", "values"),
        help="check the file; check it and write it to SSM; or add its private values to the list",
    )
    parser.add_argument("file", help="the owner's configuration file, kept outside the working tree")
    parser.add_argument("--list", type=Path, help="values: the scanner's list of local values, outside the tree too")
    options = parser.parse_args(arguments)
    held = 0
    if (options.action == "values") != (options.list is not None):
        parser.error("--list goes with values, and only with it")
    try:
        loaded = config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": options.file})
        if options.action == "write":
            config.write_ssm(loaded, client)
        elif options.action == "values":
            # Further values, one per line: the walkthrough passes on what the owner entered.
            held = write_values(loaded, sys.stdin, options.list)
    except (config.ConfigError, ListError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    if options.action == "check":
        print("the configuration loader accepts the file")
    elif options.action == "write":
        print(f"the configuration is written to SSM, under {config.SSM_PREFIX}")
    else:
        print(f"the list of local values holds {held} values, readable by its owner alone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
