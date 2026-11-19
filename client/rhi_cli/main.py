"""The `rhi` command: one subcommand per module, each added here by its `register`."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from rhi_cli import login, revoke, token, wait

COMMANDS = [login, token, wait, revoke]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command `argv` names (the process's arguments by default); return its exit status."""
    parser = argparse.ArgumentParser(prog="rhi", description="Use the course's inference endpoint.")
    commands = parser.add_subparsers(title="commands", metavar="command")
    for command in COMMANDS:
        command.register(commands)
    arguments = parser.parse_args(argv)
    run = getattr(arguments, "run", None)
    if run is None:
        parser.print_help()
        return 2
    return run(arguments)
