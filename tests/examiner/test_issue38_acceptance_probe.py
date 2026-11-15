import tomllib
import traceback
from pathlib import Path

import pytest

from rhi import config


def test_typed_source_and_file_round_trip_and_parameter_errors(tmp_path):
    raw = tomllib.loads(Path('config.example.toml').read_text())
    def text(value):
        if isinstance(value, dict):  # a table, as a TOML inline table
            return '{ ' + ', '.join(f'{k} = {v}' for k, v in value.items()) + ' }'
        return '[]' if value == [] else str(value)

    parameters = [{'Name': '/rhi/config/' + key, 'Type': 'String',
                   'Value': text(value)}
                  for key, value in raw.items() if key != 'pools']

    class Client:
        def get_parameters_by_path(self, **request):
            return {'Parameters': parameters}

    expected = config.load({'RHI_CONFIG': 'example'})
    assert config.read_ssm(Client()) == expected
    output = tmp_path / 'written.toml'
    config.write_file(expected, output)
    assert config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(output)}) == expected
    marker = 'private-probe-value'
    for kind, key in [('missing', 'domain'), ('unknown', 'extra'), ('wrong', 'monthly_budget_usd')]:
        original = list(parameters)
        if kind == 'missing':
            parameters[:] = [p for p in parameters if p['Name'] != '/rhi/config/domain']
        else:
            parameters[:] = [p for p in parameters if p['Name'] != '/rhi/config/' + key]
            parameters.append({'Name': '/rhi/config/' + key, 'Type': 'String', 'Value': marker})
        with pytest.raises(config.ConfigError) as error:
            config.read_ssm(Client())
        assert key in str(error.value)
        assert marker not in ''.join(traceback.format_exception(error.value))
        parameters[:] = original
