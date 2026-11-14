"""The scanner is a script, not a package: put its folder on sys.path so the tests import `scan`."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The tests never read the real values of whoever runs them (scan.load_local_values): an empty
# file stands in, for this process and for every scanner it starts.
os.environ["HYGIENE_LOCAL_VALUES"] = os.devnull
