import re
import traceback
from pathlib import Path

import pytest

from rhi import config


@pytest.mark.parametrize('key', sorted(config.KEYS))
def test_missing_and_wrong_types_are_sanitized(tmp_path, key):
    text = Path('config.example.toml').read_text()
    path = tmp_path / 'config.toml'
    for replacement in ('', f'{key} = {{ marker = "PRIVATE_SENTINEL" }}'):
        path.write_text(re.sub(rf'^{key} = .*$', replacement, text, flags=re.MULTILINE))
        with pytest.raises(config.ConfigError) as caught:
            config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)})
        assert key in str(caught.value)
        assert 'file' in str(caught.value)
        assert 'PRIVATE_SENTINEL' not in ''.join(traceback.format_exception(caught.value))


def test_unknown_keys_and_unreadable_sources_are_sanitized(tmp_path):
    path = tmp_path / 'PRIVATE_SENTINEL.toml'
    text = Path('config.example.toml').read_text()
    variants = [None, b'\xffPRIVATE_SENTINEL', b'domain = "PRIVATE_SENTINEL',
                (text + '\nunknown_setting = "PRIVATE_SENTINEL"\n').encode()]
    for content in variants:
        if content is not None:
            path.write_bytes(content)
        with pytest.raises(config.ConfigError) as caught:
            config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)})
        assert 'file' in str(caught.value)
        if content is variants[-1]:
            assert 'unknown_setting' in str(caught.value)
        assert 'PRIVATE_SENTINEL' not in ''.join(traceback.format_exception(caught.value))
