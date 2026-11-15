from rhi import config


def test_prefix_string_parameters_empty_pools_and_sibling_isolation():
    writes = []
    reads = []

    class Client:
        def put_parameter(self, **request):
            writes.append(request)

        def get_parameters_by_path(self, **request):
            reads.append(request)
            parameters = [{'Name': p['Name'], 'Type': p['Type'], 'Value': p['Value']} for p in writes]
            parameters.append({'Name': '/rhi/pools/mock/release', 'Type': 'String', 'Value': 'sibling'})
            return {'Parameters': [p for p in parameters if p['Name'].startswith(request['Path'])]}

    expected = config.load({'RHI_CONFIG': 'example'})
    assert config.SSM_PREFIX == '/rhi/config/'
    config.write_ssm(expected, Client())
    assert {p['Name'] for p in writes} == {'/rhi/config/' + key for key in config.KEYS if key != 'pools'}
    assert all(p['Type'] == 'String' for p in writes)
    assert not any(p['Name'].startswith('/rhi/config/pools/') for p in writes)
    assert config.read_ssm(Client()) == expected
    assert reads == [{'Path': '/rhi/config/', 'Recursive': True}]
