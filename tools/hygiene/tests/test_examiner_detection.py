import pytest

import scan


def url(host, path):
    return ''.join(('https', '://', host, path))


@pytest.mark.parametrize('host', [
    'console.aws.amazon.com', 'signin.aws.amazon.com',
    'console.amazonaws.cn', 'signin.amazonaws.cn',
    'console.amazonaws-us-gov.com', 'signin.amazonaws-us-gov.com',
])
@pytest.mark.parametrize('prefix', ['', 'eu-north-1.', 'example-alias.'])
def test_partition_hosts(host, prefix):
    value = url(prefix + host, '/console')
    assert [f.rule for f in scan.scan_text(value)] == ['console-link']
    assert scan.redact(value) == '[redacted console-link]'


@pytest.mark.parametrize('route', ['circleci/build/github/example/repo', 'travis/com/example/repo'])
def test_shields_build_provider_is_reported(route):
    value = url('img.shields.io', '/' + route)
    assert [f.rule for f in scan.scan_text(value)] == ['build-badge']
    assert scan.redact(value) == '[redacted build-badge]'


def test_shields_build_provider_is_redacted():
    value = url('img.shields.io', '/travis/com/example/repo')
    assert scan.redact(value) == '[redacted build-badge]'


def test_license_badge_for_build_named_repo_stays_quiet():
    value = url('img.shields.io', '/github/license/example/build-tools')
    assert scan.scan_text(value) == []
    assert scan.redact(value) == value
