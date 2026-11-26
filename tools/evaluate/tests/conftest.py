"""The tool is a package under `tools`: put that folder on sys.path so the tests import `evaluate`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
