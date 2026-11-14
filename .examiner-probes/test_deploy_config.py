import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import L


def test_configuration_is_neutral_and_rejects_unknown_keys(tmp_path):
    policy = L.config.load_policy('tools/implementation-loop/loop.toml')
    assert policy['deploy'] == L.config.Deploy((), 3600, '')
    path = tmp_path / 'loop.toml'
    path.write_text('[deploy]\ncontexts=["deploy/check"]\ntimeout_s=7\napproval_description="approval"\n')
    assert L.config.load_policy(path)['deploy'] == L.config.Deploy(('deploy/check',), 7, 'approval')
    path.write_text('[deploy]\ncontexts=[]\ntimeout=7\n')
    with pytest.raises(ValueError, match='timeout'):
        L.config.load_policy(path)
