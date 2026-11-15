import subprocess


def test_acceptance_workflow_tests():
    subprocess.run(['uv', 'run', '--locked', 'pytest', 'tests/unit/test_ci_workflow.py'], check=True, capture_output=True)
