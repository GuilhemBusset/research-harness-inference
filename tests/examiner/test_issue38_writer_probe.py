import dataclasses
import datetime
import traceback

import botocore.exceptions
import pytest

from rhi import config


class MemorySSM:
    def __init__(self):
        self.values = {}
        self.writes = []

    def put_parameter(self, **request):
        self.writes.append(request)
        self.values[request['Name']] = request['Value']

    def get_parameters_by_path(self, **request):
        return {'Parameters': [
            {'Name': name, 'Type': 'String', 'Value': value}
            for name, value in self.values.items() if name.startswith(request['Path'])
        ]}


def test_independent_writer_layout_and_both_round_trips(tmp_path):
    original = config.load({'RHI_CONFIG': 'example'})
    client = MemorySSM()
    config.write_ssm(original, client)
    assert config.SSM_PREFIX == '/rhi/config/'
    assert set(client.values) == {'/rhi/config/' + key for key in config.KEYS if key != 'pools'}
    assert len(client.writes) == len(config.KEYS) - 1
    assert all(call['Type'] == 'String' and call['Overwrite'] is True for call in client.writes)
    assert not any(name.startswith('/rhi/config/pools/') for name in client.values)
    assert config.read_ssm(client) == original
    changed = dataclasses.replace(original, github_repo='quoted "repo" \\ é\nsecond line',
                                  monthly_budget_usd=42, course_end=datetime.date(2030, 6, 1))
    config.write_ssm(changed, client)
    assert config.read_ssm(client) == changed
    path = tmp_path / 'config.toml'
    config.write_file(changed, path)
    assert config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': str(path)}) == changed


def test_independent_writer_refusal_precedes_mutation(tmp_path):
    client = MemorySSM()
    path = tmp_path / 'untouched.toml'
    path.write_text('original file')
    invalid = dataclasses.replace(config.load({'RHI_CONFIG': 'example'}), monthly_budget_usd=-42)
    for write in (lambda: config.write_ssm(invalid, client), lambda: config.write_file(invalid, path)):
        with pytest.raises(config.ConfigError, match='monthly_budget_usd'):
            write()
    assert client.values == {}
    assert path.read_text() == 'original file'


def test_independent_sdk_errors_are_redacted():
    marker = 'private-probe-value'

    class FailedSSM:
        def put_parameter(self, **request):
            raise botocore.exceptions.ClientError({'Error': {'Code': 'ValidationException',
                                                          'Message': marker}}, 'PutParameter')

    with pytest.raises(config.ConfigError) as error:
        config.write_ssm(config.load({'RHI_CONFIG': 'example'}), FailedSSM())
    assert '/rhi/config/domain' in str(error.value)
    assert marker not in ''.join(traceback.format_exception(error.value))
