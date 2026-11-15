import tomllib
import traceback
from pathlib import Path

import boto3
import pytest

from rhi import config


class MemorySSM:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.reads = []
        self.writes = []

    def get_parameters_by_path(self, **request):
        self.reads.append(request)
        assert request['Path'] == '/rhi/config/'
        assert request['Recursive'] is True
        assert not request.get('WithDecryption', False)
        names = sorted(name for name in self.values if name.startswith(request['Path']))
        offset = int(request.get('NextToken', '0'))
        page = {'Parameters': [
            {'Name': name, 'Type': 'String', 'Value': self.values[name]}
            for name in names[offset:offset + 3]
        ]}
        if offset + 3 < len(names):
            page['NextToken'] = str(offset + 3)
        return page

    def put_parameter(self, **request):
        assert request['Type'] == 'String'
        assert request['Overwrite'] is True
        self.writes.append(request)
        self.values[request['Name']] = request['Value']
        return {'Version': 1}


def text(value):
    if isinstance(value, dict):  # a table, as a TOML inline table
        return '{ ' + ', '.join(f'{k} = {v}' for k, v in value.items()) + ' }'
    return str(value)


def seed():
    raw = tomllib.loads(Path('config.example.toml').read_text())
    return {'/rhi/config/' + key: '[]' if value == [] else text(value)
            for key, value in raw.items() if key != 'pools'}


def test_independent_source_selection_pagination_and_layout(monkeypatch):
    client = MemorySSM(seed() | {'/rhi/pools/mock/release': 'outside-prefix'})
    calls = []

    def make(*args, **kwargs):
        calls.append((args, kwargs))
        return client

    monkeypatch.setattr(boto3, 'client', make)
    assert config.load({'RHI_CONFIG': 'ssm'}) == config.load({'RHI_CONFIG': 'example'})
    assert calls == [(('ssm',), {})]
    assert len(client.reads) >= 4
    assert client.writes == []


@pytest.mark.parametrize('case,key', [
    ('missing', 'domain'), ('unknown', 'surprise'), ('wrong-type', 'monthly_budget_usd'),
    ('pool-entry', 'pools'),
])
def test_independent_source_refusals_hide_values(case, key):
    values = seed()
    marker = 'private-probe-value'
    if case == 'missing':
        del values['/rhi/config/' + key]
    elif case == 'pool-entry':
        values['/rhi/config/pools/mock'] = marker
    else:
        values['/rhi/config/' + key] = marker
    with pytest.raises(config.ConfigError) as error:
        config.read_ssm(MemorySSM(values))
    assert key in str(error.value)
    assert marker not in ''.join(traceback.format_exception(error.value))
