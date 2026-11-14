"""Issue #8: import and exercise the exact checkout's protected prefixes."""

import subprocess
import sys


def test_d1_pipeline_and_bootstrap_are_protected():
    source = '''
from pathlib import Path
import sys
sys.path.insert(0, str(Path('tools/implementation-loop').resolve()))
from implement_loop import config
assert Path(config.__file__).resolve() == Path('tools/implementation-loop/implement_loop/config.py').resolve()
for folder in ['src/rhi/pipeline/', 'setup/bootstrap/']:
    assert folder in config.PROTECTED_PREFIXES
    assert config.is_protected(folder + 'new.py')
    assert config.is_protected(folder + 'nested/new.py')
for path in ['src/rhi/stage.py', 'src/rhi/pipeline.py', 'setup/bootstrap.sh', 'setup/README.md']:
    assert not config.is_protected(path), path
'''
    result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
