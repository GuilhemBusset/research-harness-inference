import re
from pathlib import Path

import pytest

from rhi import config


def test_default_fills_only_the_omitted_key(tmp_path, monkeypatch):
    text = Path('config.example.toml').read_text()
    path = tmp_path / 'config.toml'
    path.write_text(re.sub(r'^monthly_budget_usd = .*$', '', text, flags=re.MULTILINE))
    env = {'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)}
    with pytest.raises(config.ConfigError, match='monthly_budget_usd'):
        config.load(env)
    monkeypatch.setitem(config.DEFAULTS, 'monthly_budget_usd', 731)
    assert config.load(env).monthly_budget_usd == 731
    path.write_text(re.sub(r'^admin_email = .*$', '', path.read_text(), flags=re.MULTILINE))
    with pytest.raises(config.ConfigError, match='admin_email'):
        config.load(env)
