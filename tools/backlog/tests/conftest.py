"""The tool is a script, not a package: put its folder on sys.path so the tests import `backlog`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
