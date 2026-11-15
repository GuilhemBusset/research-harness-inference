import tomllib
from pathlib import Path


def test_project_sdk_pin_matches_the_single_locked_version():
    project = tomllib.loads(Path('pyproject.toml').read_text())
    lock = tomllib.loads(Path('uv.lock').read_text())
    versions = [p['version'] for p in lock['package'] if p['name'] == 'boto3']
    assert len(versions) == 1
    assert 'boto3==' + versions[0] in project['project']['dependencies']
    for group in project['dependency-groups'].values():
        for dependency in group:
            if isinstance(dependency, str) and dependency.startswith('boto3'):
                assert dependency == 'boto3==' + versions[0]
