import importlib.util
from pathlib import Path

import pytest


def test_named_tests_exist_and_default_contract_executes(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('issue28_coverage_tests', 'tests/unit/test_config.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('test_the_two_sources_give_the_same_typed_object',
                 'test_an_unreadable_source_raises', 'test_an_unknown_key_raises',
                 'test_a_missing_key_raises', 'test_a_value_of_the_wrong_type_raises',
                 'test_a_default_fills_a_missing_key'):
        assert callable(getattr(module, name))
    text = Path('config.example.toml').read_text()
    with monkeypatch.context() as patch:
        patch.setattr(module, 'example_with', lambda **kwargs: text)
        with pytest.raises(pytest.fail.Exception):
            module.test_a_default_fills_a_missing_key(tmp_path, patch)
