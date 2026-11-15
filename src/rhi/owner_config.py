"""The configuration step of the owner's walkthrough (setup/owner-setup.sh): the owner's file,
checked by the configuration loader and written to SSM by the configuration writer.

    python -m rhi.owner_config check <file>   # the loader reads the file; nothing is written
    python -m rhi.owner_config write <file>   # the same check, then one parameter per key in SSM

The file goes through the loader's own `file` source and to SSM through `write_ssm`
(src/rhi/config.py), so the walkthrough and the pipeline read the same keys the same way. A file
the loader refuses is refused here, before anything is written, in the loader's words: they name
a key or a source and never a value. Nothing this module prints holds a value of the file.

That the file lies outside the working tree is the walkthrough's check, made before it starts
this module. Only `write` reaches AWS, with the caller's credentials and region.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(arguments: Sequence[str] | None = None, client: Any = None) -> int:
    """Checks the file, and writes it to SSM when asked to. `client` is as for `config.write_ssm`.
    Returns the exit status: 0, or 1 when the loader or the writer refused."""
    parser = argparse.ArgumentParser(prog="python -m rhi.owner_config", description=__doc__.partition("\n\n")[0])
    parser.add_argument("action", choices=("check", "write"), help="check the file, or check it and write it to SSM")
    parser.add_argument("file", help="the owner's configuration file, kept outside the working tree")
    options = parser.parse_args(arguments)
    try:
        loaded = config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": options.file})
        if options.action == "write":
            config.write_ssm(loaded, client)
    except config.ConfigError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    if options.action == "check":
        print("the configuration loader accepts the file")
    else:
        print(f"the configuration is written to SSM, under {config.SSM_PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
