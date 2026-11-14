"""Mask a text before the engine publishes it (ADR 0001, decision 4).

What the engine sends to GitHub can carry what no source file holds: the tail of a command's
output in an evidence comment, a finding the examiner quotes, the detail of a failure. The hygiene
scanner knows what may not be published and how to mask it (`tools/hygiene/scan.py --redact`), so
every such text passes through it first (`github.RedactingGitHub`).

The scanner is the one of the checkout this file is in, found from this file's own place: never
the working directory, never a path the engine works on. An issue's worktree holds a scanner too,
and a builder can write it. The engine runs outside the sandbox, as the owner, and the scanner
runs with that environment, so it also masks the owner's local values where their list exists
(`tools/hygiene/README.md`, "Values only the owner knows").

There is no fallback. A scanner that is missing, fails or does not answer raises
`RedactionError`, and the text is not returned. The text goes through the scanner's standard
input and output, so a `\\r\\n` or a lone `\\r` comes back as `\\n`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# <checkout>/tools/implementation-loop/implement_loop/redact.py -> <checkout>/tools/hygiene/scan.py
SCANNER = Path(__file__).resolve().parents[2] / "hygiene" / "scan.py"
TIMEOUT_S = 60


class RedactionError(RuntimeError):
    """The scanner could not mask a text. The message holds nothing of the text and nothing the
    scanner wrote, so it can be journaled and, once the scanner works again, published."""


def redact(text: str, scanner: Path | None = None) -> str:
    """`text` with everything the hygiene scanner refuses masked. `scanner` is for tests; by
    default it is the scanner of the checkout the engine runs from."""
    scanner = scanner or SCANNER
    if not scanner.is_file():
        raise RedactionError("the hygiene scanner (tools/hygiene/scan.py) is missing from the engine's checkout")
    try:
        proc = subprocess.run([sys.executable, str(scanner), "--redact"], input=text, capture_output=True, text=True,
                              encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=TIMEOUT_S,
                              check=False)
    except subprocess.TimeoutExpired as e:
        raise RedactionError(f"the hygiene scanner did not answer within {TIMEOUT_S} seconds") from e
    except (OSError, ValueError) as e:   # it could not be started, or the text could not be handed to it
        raise RedactionError(f"the hygiene scanner could not be run ({type(e).__name__})") from e
    if proc.returncode != 0:   # what it wrote is not repeated: it may quote the text, or a path of this machine
        raise RedactionError(f"the hygiene scanner failed (exit {proc.returncode}); "
                             "`python3 tools/hygiene/scan.py --redact </dev/null` shows why")
    return proc.stdout
