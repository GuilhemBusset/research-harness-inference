import json

import boto3
import botocore.exceptions
import pytest

from rhi import owner_secret

SECRET = "rhi/reporter/github-app"
MARK = "independent-example-key-text"


@pytest.mark.parametrize("exists", [False, True])
def test_d2_secret_creation_and_update(tmp_path, capsys, monkeypatch, exists):
    def no_client(*args, **kwargs):
        raise AssertionError("the probe must never create a real service client")

    monkeypatch.setattr(boto3, "client", no_client)
    key = tmp_path / "key.pem"
    key.write_text(MARK + "\nsecond line\n")
    written = []

    class Client:
        def create_secret(self, **request):
            if exists:
                raise botocore.exceptions.ClientError({"Error": {"Code": "ResourceExistsException"}}, "CreateSecret")
            written.append(("create", request))

        def put_secret_value(self, **request):
            written.append(("update", request))

    status = owner_secret.main([SECRET, "--app-id", "707", "--installation-id", "808",
                                "--private-key-file", str(key)], Client())
    assert status == 0
    [(operation, request)] = written
    assert operation == ("update" if exists else "create")
    assert request["SecretId" if exists else "Name"] == SECRET
    assert json.loads(request["SecretString"]) == {
        "app_id": "707", "installation_id": "808", "private_key": key.read_text()}
    output = capsys.readouterr()
    assert MARK not in output.out + output.err


