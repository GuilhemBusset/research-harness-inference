"""The real configuration module and SSM writer, with a local service."""
import json
from pathlib import Path

from rhi import config, owner_config

ROOT = Path(__file__).resolve().parents[1]


def test_d4_a5_actual_module_writes_roundtrippable_ssm(tmp_path):
    file = tmp_path / "owner.toml"
    file.write_bytes((ROOT / "config.example.toml").read_bytes())

    class LocalSSM:
        def __init__(self):
            self.parameters = []

        def put_parameter(self, **request):
            assert request["Overwrite"] is True and request["Type"] == "String"
            self.parameters.append({k: request[k] for k in ("Name", "Value", "Type")})

        def get_parameters_by_path(self, **request):
            assert request == {"Path": "/rhi/config/", "Recursive": True}
            return {"Parameters": self.parameters}

    client = LocalSSM()
    assert owner_config.main(["check", str(file)], client) == 0 and not client.parameters
    assert owner_config.main(["write", str(file)], client) == 0
    assert config.read_ssm(client) == config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(file)})
    assert {p["Name"] for p in client.parameters} == {"/rhi/config/" + k for k in config.KEYS if k != "pools"}
    file.write_text('domain = "example.com"\n')
    before = json.dumps(client.parameters)
    assert owner_config.main(["write", str(file)], client) == 1
    assert json.dumps(client.parameters) == before


