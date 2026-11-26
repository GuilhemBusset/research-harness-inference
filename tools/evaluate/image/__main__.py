"""`uv run --locked python tools/evaluate/image`: prepare the trial image (see `evaluate.image`).

Run as a folder, Python puts this folder alone on the path. The package is `evaluate.image`, under
`tools`, and it imports the harness pins by name from `spikes`.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "tools"), str(REPO / "spikes")]

from evaluate.image import main

if __name__ == "__main__":
    raise SystemExit(main())
