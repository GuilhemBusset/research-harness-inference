import errno
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop"))

from implement_loop.agents import Probe
from implement_loop.engine import Engine


def test_real_write_error_becomes_probe_rejection():
    subject = Engine.__new__(Engine)
    subject.repair = False
    name = "checks/" + "w" * 512 + ".sh"
    probe = Probe(name, "D1", "bash " + name, content="test -f README.md\n")
    errors = []
    original = Path.write_text

    def record_write(path, *args, **kwargs):
        try:
            return original(path, *args, **kwargs)
        except OSError as error:
            errors.append(error.errno)
            raise

    with patch.object(Path, "write_text", record_write):
        reason = subject._probe_problem(probe)
    assert errors == [errno.ENAMETOOLONG]
    assert isinstance(reason, str) and reason
