"""Independently mutate YAML and exercise the actual workflow acceptance test."""

import copy
import runpy
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("mutation", ["needs", "checked", "secret", "id", "name"])
def test_workflow_rules_reject_mutations(tmp_path: Path, mutation: str) -> None:
    root = Path(__file__).resolve().parents[2]
    module = runpy.run_path(str(root / "tests/unit/test_ci_workflow.py"))
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert module["problems"](workflow) == []
    changed = copy.deepcopy(workflow)
    jobs = changed["jobs"]
    if mutation == "needs":
        jobs["required"]["needs"].remove("synth")
    elif mutation == "checked":
        for step in jobs["required"]["steps"]:
            step["run"] = step["run"].replace(', "synth"', "")
    elif mutation == "secret":
        jobs["tests"]["env"] = {"TOKEN": "${{ secrets.TEST_TOKEN }}"}
    elif mutation == "id":
        jobs["tests"]["steps"].append({"run": "bash scripts/check.sh container-tests"})
    else:
        jobs["container-tests"] = {
            "name": "a different check", "runs-on": "ubuntu-24.04",
            "steps": [{"run": "bash scripts/check.sh container-tests"}],
        }
        jobs["required"]["needs"].append("container-tests")
        for step in jobs["required"]["steps"]:
            step["run"] = step["run"].replace('"synth"]', '"synth", "container-tests"]')
    path = tmp_path / "ci.yml"
    path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    test = module["test_ci_workflow_keeps_the_rules"]
    test.__globals__["WORKFLOW"] = path
    with pytest.raises(AssertionError):
        test()
