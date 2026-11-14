import importlib.util

import pytest

from rhi import config


def test_named_required_key_guard_detects_schema_and_entry_regressions(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('issue28_required_tests', 'tests/unit/test_config.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with monkeypatch.context() as patch:
        patch.setitem(config.KEYS, 'new_required_setting', config.KEYS['domain'])
        with pytest.raises(AssertionError):
            module.test_only_the_owners_keys_are_required(tmp_path, patch)
    for key in ('session_windows', 'pools'):
        with monkeypatch.context() as patch:
            patch.setitem(config.KEYS, key, config.Key('entries allowed', lambda value: value))
            with pytest.raises(pytest.fail.Exception):
                module.test_only_the_owners_keys_are_required(tmp_path, patch)
