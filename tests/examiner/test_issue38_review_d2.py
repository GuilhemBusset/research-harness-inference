import dataclasses
import tomllib
from pathlib import Path

import boto3

from rhi import config


class MemorySSM:
    def __init__(self, parameters=None):
        self.parameters = dict(parameters or {})
        self.reads = []
        self.writes = []

    def get_parameters_by_path(self, **request):
        self.reads.append(request)
        assert request["Path"] == "/rhi/config/"
        assert request["Recursive"] is True
        assert not request.get("WithDecryption", False)
        names = sorted(n for n in self.parameters if n.startswith(request["Path"]))
        offset = int(request.get("NextToken", "0"))
        page = {"Parameters": [self.parameters[n] for n in names[offset : offset + 4]]}
        if offset + 4 < len(names):
            page["NextToken"] = str(offset + 4)
        return page

    def put_parameter(self, **request):
        self.writes.append(request)
        self.parameters[request["Name"]] = {k: request[k] for k in ("Name", "Type", "Value")}
        return {"Version": 1}


def text(value):
    if isinstance(value, dict):  # a table, as a TOML inline table
        return "{ " + ", ".join(f"{k} = {v}" for k, v in value.items()) + " }"
    return str(value)


def seed():
    raw = tomllib.loads(Path("config.example.toml").read_text())
    return {
        "/rhi/config/" + key: {
            "Name": "/rhi/config/" + key,
            "Type": "String",
            "Value": "[]" if value == [] else text(value),
        }
        for key, value in raw.items() if key != "pools"
    }


def test_source_uses_caller_configuration_and_reads_every_page(monkeypatch):
    client = MemorySSM(seed())
    calls = []

    def make(*args, **kwargs):
        calls.append((args, kwargs))
        return client

    monkeypatch.setattr(boto3, "client", make)
    expected = config.load({"RHI_CONFIG": "example"})
    actual = config.load({"RHI_CONFIG": "ssm"})
    assert actual == expected
    assert all(type(getattr(actual, f.name)) is type(getattr(expected, f.name))
               for f in dataclasses.fields(expected))
    assert calls == [(("ssm",), {})]
    assert len(client.reads) == -(-len(client.parameters) // 4)  # every page of four, and no more
    assert client.writes == []
