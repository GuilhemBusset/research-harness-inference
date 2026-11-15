import re
import tomllib
from pathlib import Path

from rhi import config


def test_example_is_complete_and_uses_placeholders():
    text = Path('config.example.toml').read_text()
    data = tomllib.loads(text)
    assert set(data) == set(config.KEYS)
    assert data['domain'] == 'example.com'
    assert data['admin_email'].rsplit('@', 1)[1] == 'example.com'
    assert data['session_windows'] == []
    assert data['pools'] == []
    assert all(number == '123456789012' for number in re.findall(r'(?<!\d)\d{12}(?!\d)', text))
    # The example turns the listener on, away from its default, so that synthesis sees it (issue E2-47).
    for key, value in (config.DEFAULTS | {'https_listener': True}).items():
        assert data[key] == value
    config.load({'RHI_CONFIG': 'example'})
