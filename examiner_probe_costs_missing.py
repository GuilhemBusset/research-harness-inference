import importlib.util
import json
import sys
from pathlib import Path


def test_missing_cost_is_not_invented_or_used_in_dollar_spread():
    package = Path("tools/implementation-loop/implement_loop")
    spec = importlib.util.spec_from_file_location("missing_examiner_engine", package / "__init__.py",
        submodule_search_locations=[str(package.resolve())])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from missing_examiner_engine.cli import cost_report

    fixture = Path("tools/implementation-loop/tests/fixtures/costs/agents.jsonl")
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    sizes = {r["issue"]: "S" for r in rows if r["issue"] is not None}
    baseline = cost_report(rows, sizes)
    extra = [dict(issue=21, role="build", vendor="claude", outcome="timeout"),
             dict(issue=21, role="build", vendor="claude", outcome="answered", cost_usd=None)]
    result = cost_report(rows + extra, sizes)
    assert result["total"]["usd"] == baseline["total"]["usd"] == 54.75
    assert result["total"]["attempts"] == baseline["total"]["attempts"] + 2
    assert result["total"]["answered"] == baseline["total"]["answered"] + 1
    assert [r["usd"] for r in result["issues"]] == [r["usd"] for r in baseline["issues"]]
    assert [r["usd"] for r in result["roles"]] == [r["usd"] for r in baseline["roles"]]
    assert [r["usd"] for r in result["sizes"]] == [r["usd"] for r in baseline["sizes"]]
