import ast
import subprocess
from pathlib import Path


def test_named_coverage_exists_and_engine_gate_passes():
    path = Path('tools/implementation-loop/tests/test_deploying.py')
    names = {node.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.FunctionDef)}
    assert {'test_the_landing_claim_is_held', 'test_an_approval_releases_the_claim',
            'test_a_resumed_wait_does_not_merge_again'} <= names
    result = subprocess.run(['scripts/check.sh', 'engine-tests'], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '0 skipped' in result.stdout
