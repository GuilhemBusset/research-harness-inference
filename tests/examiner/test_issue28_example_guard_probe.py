import importlib.util
import re
from pathlib import Path

import pytest

from rhi import config


def test_named_example_guard_rejects_each_required_mutation(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('issue28_builder_tests', 'tests/unit/test_config.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = Path('config.example.toml').read_text()
    variants = [re.sub(r'^login_client_id = .*$', '', text, flags=re.MULTILINE),
                text.replace('domain = "example.com"', 'domain = "example.org"'),
                text.replace('admin@example.com', 'admin@example.org'),
                text + '\n# account 111122223333\n', text]
    path = tmp_path / 'example.toml'
    for index, changed in enumerate(variants):
        path.write_text(changed)
        with monkeypatch.context() as patch:
            patch.setattr(module, 'EXAMPLE', path)
            if index == len(variants) - 1:
                patch.setitem(config.DEFAULTS, 'monthly_budget_usd', 731)
            with pytest.raises(AssertionError):
                module.test_the_example_gives_every_key_a_placeholder(patch)
