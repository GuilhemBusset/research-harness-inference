"""D6/A3: doctor rejects forbidden permissions and unreadable check runs."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token, cli


@pytest.mark.parametrize("bad", ["workflows", "administration", "checks"])
def test_permission_refusal(monkeypatch, tmp_path, bad):
    app = app_token.App("4242", "777", str(tmp_path / "key"))
    permissions = {"checks": "read"}
    if bad == "checks":
        permissions.clear()
    else:
        permissions[bad] = "write"
    monkeypatch.setattr(app_token, "configured", lambda: app)
    monkeypatch.setattr(app_token, "installation", lambda app: {
        "app_slug": "factory-placeholder", "permissions": permissions, "repository_selection": "selected"})
    monkeypatch.setattr(cli, "_out", lambda cmd: (0, "https://github.com/o/r.git"))
    status, _name, detail = cli.github_identity_check(tmp_path)
    assert status == "FAIL" and bad in detail


def test_actual_check_read_is_required(monkeypatch):
    monkeypatch.setattr(cli, "_out", lambda cmd: (1, "HTTP 403"))
    ok, name, _detail = cli.checks_access_check("o/r", "main")
    assert not ok and name == "checks access"
