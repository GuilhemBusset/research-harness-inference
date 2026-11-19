import subprocess
from pathlib import Path

WORKFLOW = Path(".github/workflows/ci.yml")


def test_acceptance_literal_command():
    subprocess.run(['grep', '-q', 'scripts/check.sh container-tests', str(WORKFLOW)], check=True)
