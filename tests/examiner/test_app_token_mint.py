"""D1: real signing and minting from the configured identity."""
import base64
import json
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token


def test_sign_and_exchange(tmp_path, monkeypatch):
    key, public, signature = (tmp_path / name for name in ("key", "public", "signature"))
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], capture_output=True, check=True)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)],
                   capture_output=True, check=True)
    for name, value in zip(app_token.VARS, ("4242", "777", str(key)), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(app_token.API_VAR, raising=False)
    monkeypatch.setattr(app_token, "_held", [])
    expected = secrets.token_hex(24)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"token": expected, "expires_at": "2099-01-01T00:00:00Z"}).encode()

    def request(req, timeout):
        assert req.full_url == "https://api.github.com/app/installations/777/access_tokens"
        assert req.method == "POST" and timeout > 0
        raw = req.get_header("Authorization").removeprefix("Bearer ")
        header, claims, signed = raw.split(".")
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        assert json.loads(decode(header))["alg"] == "RS256"
        assert json.loads(decode(claims))["iss"] == 4242
        signature.write_bytes(decode(signed))
        subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signature)],
                       input=f"{header}.{claims}".encode(), capture_output=True, check=True)
        return Response()

    monkeypatch.setattr(app_token.urllib.request, "urlopen", request)
    assert app_token.token() == expected
