import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def test_costs_command_reports_independent_measurements(tmp_path):
    package = Path("tools/implementation-loop/implement_loop")
    spec = importlib.util.spec_from_file_location(
        "examiner_engine", package / "__init__.py",
        submodule_search_locations=[str(package.resolve())],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from examiner_engine import cli

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run = tmp_path / ".git/implementation-loop/issue-60"
    run.mkdir(parents=True)
    records = [
        dict(issue=61, role="plan", vendor="claude", outcome="answered", cost_usd=2),
        dict(issue=61, role="build", vendor="claude", outcome="crash", cost_usd=6),
        dict(issue=61, role="build", vendor="claude", outcome="timeout"),
        dict(issue=61, role="examine", vendor="codex", outcome="answered",
             usage=dict(input_tokens=100, output_tokens=20, cached_input_tokens=40)),
        dict(issue=62, role="build", vendor="claude", outcome="answered", cost_usd=10),
        dict(issue=62, role="examine", vendor="codex", outcome="answered",
             usage=dict(input_tokens=250, output_tokens=30)),
        dict(issue=62, role="plan", vendor="claude", outcome="answered", cost_usd=4),
        dict(issue=None, role="explore", vendor="claude", outcome="answered", cost_usd=1),
    ]
    (run / "agents.jsonl").write_text("\n".join(map(json.dumps, records)) + "\n")
    requested = []

    def get_issue(number):
        requested.append(number)
        return dict(number=number, labels=[dict(name="size:S")])

    def invoke(*flags):
        out = io.StringIO()
        with patch.object(cli, "repo_root", return_value=tmp_path), \
             patch.object(cli, "GhCli", return_value=SimpleNamespace(get_issue=get_issue)), \
             contextlib.redirect_stdout(out):
            assert cli.main(["--repo", "example/repo", "costs", "60", *flags]) == 0
        return out.getvalue()

    rep = json.loads(invoke("--json"))
    assert requested == [61, 62]
    assert rep["total"] == dict(attempts=8, answered=6, usd=23, tokens=400)
    assert [(r["issue"], r["size"], r["answered"], r["usd"]) for r in rep["issues"]] == [
        (61, "S", 2, 8), (62, "S", 3, 14),
    ]
    roles = {r["role"]: r for r in rep["roles"]}
    assert roles["build"]["attempts"] == 3
    assert roles["build"]["answered"] == 1
    assert roles["build"]["usd"] == dict(total=16, median=8, max=10)
    assert roles["plan"]["usd"] == dict(total=6, median=3, max=4)
    assert roles["examine"]["tokens"] == dict(total=400, median=200, max=280)
    assert rep["sizes"] == [dict(size="S", issues=2,
        answered=dict(median=2.5, max=3), usd=dict(median=11, max=14),
        tokens=dict(median=200, max=280))]
    text = invoke()
    for expected in ("#61", "#62", "size S", "$23.00", "median $8.00, max $10.00",
                     "tokens 400, per call median 200, max 280", "calls median 2.5, max 3"):
        assert expected in text
    help_result = subprocess.run(["scripts/implement.sh", "costs", "--help"],
                                 text=True, capture_output=True, check=False)
    assert help_result.returncode == 0
    assert "issue" in help_result.stdout
