"""Fetches the configuration from SSM into the file RHI_CONFIG_FILE names: the synth step's
configuration fetch (system design, section 8.2, step 2).

It reads the parameters under the configuration prefix with the `ssm` source of the configuration
loader, and writes them with the loader's writer to a file the `file` source reads back. It prints
no value: what it prints ends up in the build's logs. The loader's errors name a key or a source,
never a value.

Run by the synth step, with PYTHONPATH holding the import path of `pyproject.toml`:

    uv run --locked python scripts/pipeline/fetch_config.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Mapping

NAME = "fetch_config"


def main(environ: Mapping[str, str] | None = None, client: Any = None) -> int:
    """Fetches the configuration; 0 once it is written. `client` is an SSM client of the AWS SDK;
    without one, the build's own credentials and region make it."""
    env = os.environ if environ is None else environ
    named = env.get("RHI_CONFIG_FILE")
    if not named:
        print(f"{NAME}: RHI_CONFIG_FILE is not set: it names the file to write the configuration to", file=sys.stderr)
        return 1
    path = Path(named)
    try:
        fetched = config.read_ssm(client)
        path.parent.mkdir(parents=True, exist_ok=True)
        config.write_file(fetched, path)
    except config.ConfigError as error:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 1
    except OSError:
        print(f"{NAME}: the folder of the file RHI_CONFIG_FILE names cannot be made", file=sys.stderr)
        return 1
    print(f"{NAME}: the configuration from SSM is written to the file RHI_CONFIG_FILE names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
