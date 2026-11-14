import re
from pathlib import Path

import pytest

from rhi import config


def test_added_default_supplies_missing_owner_value(tmp_path, monkeypatch):
    text = Path('config.example.toml').read_text()
    path = tmp_path / 'config.toml'
    path.write_text(re.sub(r'^github_owner = .*$', '', text, flags=re.MULTILINE))
    env = {'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)}
    with pytest.raises(config.ConfigError, match='github_owner'):
        config.load(env)
    monkeypatch.setitem(config.DEFAULTS, 'github_owner', 'default-owner')
    assert config.load(env).github_owner == 'default-owner'
