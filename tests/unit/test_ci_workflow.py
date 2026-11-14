"""CI's workflow keeps the rules the required check rests on (system design, section 8.3).

The ruleset requires one check, the job `required`. It is only worth what it waits for: a job it
does not need, or does not look for in the results it was given, can fail without turning the
pull request red. No job may be given a secret, because pull requests run a stranger's code. And
the implement loop reads the result of the tests that start a container from the check run named
`container-tests` (ADR 0001, amendment 5), so the job that runs that step of the gate script
carries that name.

The rules are true of the workflow before and after the job `container-tests` is added to it.
The last test is about the other side of that job: the local gate starts no container.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
GATE = REPO / "scripts" / "check.sh"

REQUIRED = "required"
CONTAINER_TESTS = "container-tests"
# A command line that runs the gate script's step container-tests.
RUNS_CONTAINER_STEP = re.compile(r"check\.sh\s+container-tests\b")
# The `secrets` context in an expression: `secrets.NAME`, `secrets['NAME']`, `toJSON(secrets)`.
SECRETS_CONTEXT = re.compile(r"\bsecrets\s*(\.\s*\w|\[)|\(\s*secrets\s*\)")
# The list the step of `required` compares with the results it was given: `[...] - keys == []`.
CHECKED_LIST = re.compile(r"(\[[^\]]*\])\s*-\s*keys\b")


def strings(value: Any) -> list[str]:
    """Every string under a YAML value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*strings(key), *strings(item))]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    return []


def commands(job: dict[str, Any]) -> list[str]:
    """The shell text of a job's `run` steps."""
    return [step["run"] for step in job.get("steps") or [] if isinstance(step.get("run"), str)]


def problems(workflow: dict[str, Any]) -> list[str]:
    """The rules a workflow breaks; empty when it keeps them all."""
    found = []
    jobs: dict[str, dict[str, Any]] = workflow.get("jobs") or {}

    required = jobs.get(REQUIRED)
    if required is None:
        return [f"there is no job {REQUIRED}"]
    needs = required.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    lists = [match for command in commands(required) for match in CHECKED_LIST.findall(command)]
    if len(lists) != 1:
        found.append(f"the step of {REQUIRED} does not check one list of jobs")
    checked = json.loads(lists[0]) if len(lists) == 1 else []
    for name in jobs:
        if name == REQUIRED:
            continue
        if name not in needs:
            found.append(f"{REQUIRED} does not need the job {name}")
        if name not in checked:
            found.append(f"the step of {REQUIRED} does not check the job {name}")

    if any(SECRETS_CONTEXT.search(text) for text in strings(workflow.get("env"))):
        found.append("the workflow's env gives every job a secret")
    for name, job in jobs.items():
        if "secrets" in job or any(SECRETS_CONTEXT.search(text) for text in strings(job)):
            found.append(f"the job {name} is given a secret")

    for name, job in jobs.items():
        if not any(RUNS_CONTAINER_STEP.search(command) for command in commands(job)):
            continue
        if name != CONTAINER_TESTS:
            found.append(f"the job {name} runs the step {CONTAINER_TESTS}: its id is not {CONTAINER_TESTS}")
        # A job with no `name` reports its check run under its id.
        if job.get("name", name) != CONTAINER_TESTS:
            found.append(f"the job {name} runs the step {CONTAINER_TESTS}: its name is not {CONTAINER_TESTS}")
    return found


def job(*run: str, **keys: Any) -> dict[str, Any]:
    return {"runs-on": "ubuntu-24.04", "steps": [{"run": command} for command in run], **keys}


def sound_workflow() -> dict[str, Any]:
    """A workflow that keeps every rule, in the shape of CI's once it runs the container tests."""
    return {
        "jobs": {
            "tests": job("bash scripts/check.sh tests"),
            CONTAINER_TESTS: job("docker info", "bash scripts/check.sh container-tests", name=CONTAINER_TESTS),
            REQUIRED: {
                "if": "always()",
                "needs": ["tests", CONTAINER_TESTS],
                "runs-on": "ubuntu-24.04",
                "steps": [
                    {
                        "env": {"NEEDS": "${{ toJSON(needs) }}"},
                        "run": 'echo "$NEEDS" | jq -e \'(["tests", "container-tests"] - keys == [])'
                        ' and all(.[]; .result == "success")\'',
                    }
                ],
            },
        }
    }


def test_ci_workflow_keeps_the_rules() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    assert len(workflow["jobs"]) > 1, "the workflow was not read"
    assert problems(workflow) == []


def test_detects_a_job_left_out_of_required() -> None:
    assert problems(sound_workflow()) == []

    not_needed = sound_workflow()
    not_needed["jobs"][REQUIRED]["needs"].remove("tests")
    assert problems(not_needed) == ["required does not need the job tests"]

    not_checked = sound_workflow()
    step = not_checked["jobs"][REQUIRED]["steps"][0]
    step["run"] = step["run"].replace('["tests", "container-tests"]', '["tests"]')
    assert problems(not_checked) == ["the step of required does not check the job container-tests"]

    new_job = sound_workflow()
    new_job["jobs"]["synth"] = job("bash scripts/check.sh synth")
    assert problems(new_job) == [
        "required does not need the job synth",
        "the step of required does not check the job synth",
    ]

    # The step container-tests in a job of another id: here, added to the job `tests`.
    other_id = sound_workflow()
    other_id["jobs"]["tests"]["steps"].append({"run": "bash scripts/check.sh container-tests"})
    assert problems(other_id) == [
        "the job tests runs the step container-tests: its id is not container-tests",
        "the job tests runs the step container-tests: its name is not container-tests",
    ]

    other_name = sound_workflow()
    other_name["jobs"][CONTAINER_TESTS]["name"] = "Tests that start a container"
    assert problems(other_name) == [
        "the job container-tests runs the step container-tests: its name is not container-tests"
    ]


def test_detects_a_job_given_a_secret() -> None:
    in_a_step = sound_workflow()
    in_a_step["jobs"]["tests"]["steps"][0]["env"] = {"TOKEN": "${{ secrets.DEPLOY_TOKEN }}"}
    assert problems(in_a_step) == ["the job tests is given a secret"]

    inherited = sound_workflow()
    inherited["jobs"]["tests"] = {"uses": "./.github/workflows/tests.yml", "secrets": "inherit"}
    assert problems(inherited) == ["the job tests is given a secret"]

    for_every_job = sound_workflow()
    for_every_job["env"] = {"TOKEN": "${{ secrets['DEPLOY_TOKEN'] }}"}
    assert problems(for_every_job) == ["the workflow's env gives every job a secret"]


def arm(script: str, name: str) -> list[tuple[str, str]]:
    """The steps one arm of the gate script's `case` runs, as (step, function) pairs."""
    match = re.search(rf"^  {re.escape(name)}\)(.*?);;", script, re.DOTALL | re.MULTILINE)
    assert match, f"scripts/check.sh has no arm {name}"
    return re.findall(r"\bstep\s+(\S+)\s+(\S+)", match.group(1))


def test_the_local_gate_starts_no_container(tmp_path: Path) -> None:
    script = GATE.read_text(encoding="utf-8")

    # The step exists under its own name, and neither `fast` nor `all` runs it.
    assert arm(script, CONTAINER_TESTS) == [(CONTAINER_TESTS, "do_container_tests")]
    for name in ("fast", "all"):
        steps = arm(script, name)
        assert steps, f"the arm {name} was not read"
        assert CONTAINER_TESTS not in [step for step, _ in steps]
        assert "do_container_tests" not in [function for _, function in steps]
    # Its function is defined once and called once, from its own arm: no other step runs it.
    assert len(re.findall(r"\bdo_container_tests\b", script)) == 2

    # The default test run, in a tree with the repository's pytest configuration and one test
    # in each folder: the unit test is collected, the container test is not.
    (tmp_path / "pyproject.toml").write_text((REPO / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    for folder in ("tests/unit", "tests/container"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / f"test_in_{Path(folder).name}.py").write_text(
            "def test_it():\n    pass\n", encoding="utf-8"
        )
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert "tests/unit/test_in_unit.py::test_it" in collected.stdout
    assert "test_in_container" not in collected.stdout
