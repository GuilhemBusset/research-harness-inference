import datetime
from pathlib import Path

import pytest

from rhi import config


def test_sources_and_selection(tmp_path, monkeypatch):
    text = Path('config.example.toml').read_text()
    expected = config.load({'RHI_CONFIG': 'example'})
    external = tmp_path / 'outside.toml'
    external.write_text(text)
    assert config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(external)}) == expected
    assert isinstance(expected, config.Config)
    assert type(expected.course_end) is datetime.date
    assert type(expected.monthly_budget_usd) is int
    monkeypatch.setattr(config, 'REPO_ROOT', tmp_path)
    (tmp_path / 'config.example.toml').write_text(text)
    with pytest.raises(config.ConfigError, match='file'):
        config.load({'RHI_CONFIG': 'file'})
    (tmp_path / 'config.local.toml').write_text(text.replace('example-owner', 'local-owner'))
    assert config.load({'RHI_CONFIG': 'file'}).github_owner == 'local-owner'
    for selection in ({}, {'RHI_CONFIG': ''}, {'RHI_CONFIG': 'invalid-source'}):
        with pytest.raises(config.ConfigError, match='RHI_CONFIG'):
            config.load(selection)
    monkeypatch.setenv('RHI_CONFIG', 'file')
    monkeypatch.setenv('RHI_CONFIG_FILE', str(external))
    assert config.load() == expected
