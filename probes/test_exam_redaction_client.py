import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))

NAME = "exam_" + Path(__file__).stem
PACKAGE = ROOT / "tools/implementation-loop/implement_loop"
spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
package = importlib.util.module_from_spec(spec)
sys.modules[NAME] = package
spec.loader.exec_module(package)
L = SimpleNamespace(**{n: __import__(NAME + "." + n, fromlist=[n]) for n in
    ("redact", "github", "cli", "agents", "config", "sandbox", "graph", "model", "state", "workspace", "engine", "supervisor")})
L.config.STANDING_CHECKS = L.config.HISTORY_CHECKS = ()
L.sandbox.use(L.sandbox.PassThrough())
ACCOUNT = "".join(("9274", "6203", "8501"))
ARN = "arn:aws:iam::" + ACCOUNT + ":role/example"
TEXT = "target " + ACCOUNT + " " + ARN

import pytest


def test_all_publication_methods_and_cli_factory(monkeypatch):
    class Sink:
        def __init__(self):
            self.sent = []
        def __getattr__(self, method):
            def send(*args, **kwargs):
                self.sent.append((method, args, kwargs))
            return send
    sink = Sink()
    monkeypatch.setattr(L.cli, "GhCli", lambda slug: sink)
    gh = L.cli.github_client("test/test")
    gh.create_comment(2, TEXT)
    gh.update_comment(20, TEXT)
    gh.create_pr("branch", "main", TEXT, TEXT)
    gh.update_pr(30, TEXT)
    gh.create_issue(TEXT, TEXT, [])
    assert len(sink.sent) == 5
    for method, args, kwargs in sink.sent:
        joined = repr(args)
        assert ACCOUNT not in joined and ARN not in joined
        assert "[redacted " in joined
    gh.edit_issue(2, body=TEXT)
    assert sink.sent[-1][2]["body"] == TEXT

    def fail(text):
        raise L.redact.RedactionError("scanner unavailable")
    gh = L.github.RedactingGitHub(sink, fail)
    before = list(sink.sent)
    for call in (lambda: gh.create_comment(2, TEXT), lambda: gh.update_comment(20, TEXT),
                 lambda: gh.create_pr("branch", "main", TEXT, TEXT), lambda: gh.update_pr(30, TEXT),
                 lambda: gh.create_issue(TEXT, TEXT, [])):
        with pytest.raises(L.github.GitHubUnavailable):
            call()
    assert sink.sent == before
