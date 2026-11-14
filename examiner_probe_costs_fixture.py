import importlib.util
import json
import sys
from pathlib import Path
from statistics import median


def test_recorded_fixture_totals_and_spreads():
    package = Path("tools/implementation-loop/implement_loop")
    spec = importlib.util.spec_from_file_location("fixture_examiner_engine", package / "__init__.py",
        submodule_search_locations=[str(package.resolve())])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from fixture_examiner_engine.cli import cost_report

    fixture = Path("tools/implementation-loop/tests/fixtures/costs")
    rows = [json.loads(line) for line in (fixture / "agents.jsonl").read_text().splitlines()]
    issues = json.loads((fixture / "issues.json").read_text())["issues"]
    sizes = {int(n): next(label["name"][5:] for label in issue["labels"]
                         if label["name"].startswith("size:")) for n, issue in issues.items()}
    rep = cost_report(rows, sizes)
    assert rep["total"] == dict(attempts=20, answered=17, usd=54.75, tokens=22000)
    for role in rep["roles"]:
        selected = [r for r in rows if r["role"] == role["role"]]
        values = [r["cost_usd"] for r in selected if r.get("cost_usd") is not None]
        assert role["usd"] == dict(total=sum(values), median=median(values) if values else None,
                                   max=max(values) if values else None)
    for size in rep["sizes"]:
        totals = [sum(r.get("cost_usd") or 0 for r in rows if r["issue"] == n)
                  for n in sizes if sizes[n] == size["size"]]
        assert size["usd"] == dict(median=median(totals), max=max(totals))
