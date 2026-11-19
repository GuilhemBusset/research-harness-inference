"""The pilot is a script, not a package: put its folder on sys.path so the tests import `run`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
