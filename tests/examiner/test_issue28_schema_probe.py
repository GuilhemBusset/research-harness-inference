import re
from dataclasses import fields
from pathlib import Path

import pytest

from rhi import config

OWNER_KEYS = {'domain', 'hosted_zone_id', 'admin_email', 'instructor_phone', 'github_owner',
              'github_repo', 'login_client_id', 'monthly_budget_usd', 'time_zone', 'course_end',
              'session_windows', 'pools'}


def test_schema_private_keys_and_deferred_entries(tmp_path):
    assert {field.name for field in fields(config.Config)} == OWNER_KEYS
    assert config.required_keys() == OWNER_KEYS
    assert config.PRIVATE_KEYS == {'domain', 'hosted_zone_id', 'admin_email', 'instructor_phone', 'login_client_id'}
    text = Path('config.example.toml').read_text()
    path = tmp_path / 'config.toml'
    for key in ('session_windows', 'pools'):
        for replacement in ('[1]', '[{ name = "example" }]'):
            path.write_text(re.sub(rf'^{key} = .*$', f'{key} = {replacement}', text, flags=re.MULTILINE))
            with pytest.raises(config.ConfigError, match=key):
                config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)})
    for key, raw in [('time_zone', '"No_Such_Zone"'), ('course_end', '"2099-12-31"'),
                     ('account', '"123456789012"'), ('region', '"example-region"')]:
        if key in OWNER_KEYS:
            changed = re.sub(rf'^{key} = .*$', f'{key} = {raw}', text, flags=re.MULTILINE)
        else:
            changed = text + f'\n{key} = {raw}\n'
        path.write_text(changed)
        with pytest.raises(config.ConfigError, match=key):
            config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)})
