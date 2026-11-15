import runpy

import boto3

from rhi import config


def test_delivered_test_standin_exercises_both_operations(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected real SDK client")

    monkeypatch.setattr(boto3, "client", refuse)
    namespace = runpy.run_path("tests/unit/test_config_ssm.py")
    client = namespace["StandIn"]()
    expected = config.load({"RHI_CONFIG": "example"})
    config.write_ssm(expected, client)
    assert config.read_ssm(client) == expected
    assert {operation for operation, _ in client.calls} == {"put_parameter", "get_parameters_by_path"}
    assert "test_the_layout" in namespace
