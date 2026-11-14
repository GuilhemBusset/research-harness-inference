from pathlib import Path

from rhi import config


def test_same_values_produce_equal_typed_configs(tmp_path):
    path = tmp_path / 'copy.toml'
    path.write_bytes(Path('config.example.toml').read_bytes())
    example = config.load({'RHI_CONFIG': 'example'})
    file = config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)})
    assert type(example) is type(file) is config.Config
    assert file == example
