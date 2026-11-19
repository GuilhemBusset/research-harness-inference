"""`rhi token`: print the stored key and nothing else, for the harnesses' key helpers to call.

`rhi login` configures Claude Code and Codex to run this command when they need the key
(`rhi_cli.harness`), so the key is in no settings file. Standard output holds the key and a line
end, or nothing: without a key the command says so on standard error and exits 1, so a harness is
never handed something that is not the key.
"""

from __future__ import annotations

import argparse
import sys

from rhi_cli import store


def _run(arguments: argparse.Namespace) -> int:
    try:
        key = store.load_key()
    except store.StoreError as error:
        print(f"rhi token: {error}", file=sys.stderr)
        return 1
    if key is None:
        print("rhi token: no key is stored. Run rhi login.", file=sys.stderr)
        return 1
    print(key)
    return 0


def register(commands: argparse._SubParsersAction) -> None:
    """Add `rhi token` to the CLI's commands."""
    parser = commands.add_parser("token", help="print your stored key, for a harness's key helper")
    parser.set_defaults(run=_run)
