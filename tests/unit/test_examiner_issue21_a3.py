"""Independent negative controls for jobs outside required and a renamed container job."""

import runpy
from pathlib import Path

import yaml


def test_new_and_misnamed_jobs_are_detected() -> None:
    root = Path(__file__).resolve().parents[2]
    module = runpy.run_path(str(root / "tests/unit/test_ci_workflow.py"))
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    workflow["jobs"]["extra"] = {
        "runs-on": "ubuntu-24.04", "name": "extra",
        "steps": [{"run": "bash scripts/check.sh container-tests"}],
    }
    found = module["problems"](workflow)
    assert any("does not need" in message and "extra" in message for message in found)
    assert any("id is not container-tests" in message for message in found)
    assert any("name is not container-tests" in message for message in found)
    module["test_detects_a_job_left_out_of_required"]()
