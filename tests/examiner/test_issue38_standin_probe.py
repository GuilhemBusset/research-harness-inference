from rhi import config


def test_source_and_writer_use_an_in_process_client():
    class Client:
        def __init__(self):
            self.parameters = []

        def put_parameter(self, **request):
            self.parameters.append({key: request[key] for key in ('Name', 'Type', 'Value')})

        def get_parameters_by_path(self, **request):
            assert request['Path'] == '/rhi/config/'
            return {'Parameters': self.parameters}

    original = config.load({'RHI_CONFIG': 'example'})
    client = Client()
    config.write_ssm(original, client)
    assert config.read_ssm(client) == original
