"""CI's workflow keeps the rules the required check rests on (system design, section 8.3).

The ruleset requires one check, the job `required`. It is only worth what it waits for: a job it
does not need, or does not look for in the results it was given, can fail without turning the
pull request red. No job may be given a secret, because pull requests run a stranger's code. And
the implement loop reads the result of the tests that start a container from the check run named
`container-tests` (ADR 0001, amendment 5), so the job that runs that step of the gate script
carries that name.

The rules are true of the workflow before and after the job `container-tests` is added to it.
The test of the local gate is about the other side of that job: the local gate starts no container.

The other workflows: the advisory check (system design, section 9) is the only workflow whose token
may write issues, it reads the contents and nothing more, and it holds no secret and no AWS
credential. And every update Dependabot proposes waits a week after its release (section 17).
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "ci.yml"
DEPENDABOT = REPO / ".github" / "dependabot.yml"
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


ADVISORIES = "advisories.yml"
# What the advisory workflow's token may do, and the order of the levels a permission takes.
ADVISORY_GRANTS = {"contents": "read", "issues": "write"}
LEVELS = {"none": 0, "read": 1, "write": 2}
# The ways a workflow is given AWS credentials: AWS's actions, their credential inputs, and any
# `AWS_` variable (the SDKs read credentials from several, and new ones appear). The advisory
# workflow has no use for any of them, so naming one, even with no value, is enough.
AWS_CREDENTIAL = re.compile(
    r"aws-actions/|role-to-assume|aws-(access-key-id|secret-access-key|session-token)|\bAWS_\w+",
    re.IGNORECASE,
)
# Neither key given: the job runs with the repository's default token, which may write anything.
DEFAULT = "the default token permissions"
WEEK = 7


def read_workflows() -> dict[str, dict[str, Any]]:
    """Every workflow file, by name: GitHub runs both extensions."""
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.iterdir())
        if path.suffix in (".yml", ".yaml")
    }


def grants(workflow: dict[str, Any]) -> list[tuple[str, Any]]:
    """(where, permissions) for the workflow's own key and each job's effective one."""
    top = workflow.get("permissions", DEFAULT)
    found = [("the workflow", top)] if "permissions" in workflow else []
    for name, job in (workflow.get("jobs") or {}).items():
        if "permissions" in job:
            found.append((f"the job {name}", job["permissions"]))
        elif top == DEFAULT:
            found.append((f"the job {name}", DEFAULT))
    return found


def beyond_advisory_grants(permissions: Any) -> list[str]:
    """What a permissions value asks for beyond reading the contents and writing issues."""
    if not isinstance(permissions, dict):  # `read-all`, `write-all` or the default
        return [str(permissions)]
    return [
        f"{scope}: {level}"
        for scope, level in permissions.items()
        if LEVELS.get(level, LEVELS["write"] + 1) > LEVELS[ADVISORY_GRANTS.get(scope, "none")]
    ]


def keys(value: Any) -> set[Any]:
    """Every mapping key under a YAML value, at any depth."""
    if isinstance(value, dict):
        return set(value).union(*(keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(item) for item in value))
    return set()


def writes_issues(permissions: Any) -> bool:
    if isinstance(permissions, dict):
        return permissions.get("issues") == "write"
    return permissions in ("write-all", DEFAULT)


def permission_problems(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """What the workflows ask for that only the advisory workflow may, or that it may not."""
    found = [] if ADVISORIES in workflows else [f"there is no {ADVISORIES}"]
    for name, workflow in workflows.items():
        for where, permissions in grants(workflow):
            if name == ADVISORIES:
                found += [f"{name}: {where} asks for {grant}" for grant in beyond_advisory_grants(permissions)]
            elif writes_issues(permissions):
                found.append(f"{name}: {where} can write issues")
        if name != ADVISORIES:
            continue
        texts = strings(workflow)
        # A `secrets` key anywhere: one a job is given (`secrets: inherit`) or one the workflow
        # declares (`on.workflow_call.secrets`), even optional and unused.
        if any(SECRETS_CONTEXT.search(text) for text in texts) or "secrets" in keys(workflow):
            found.append(f"{name} names a secret")
        if any(AWS_CREDENTIAL.search(text) for text in texts):
            found.append(f"{name} names an AWS credential")
    return found


def test_only_the_advisory_workflow_writes_issues() -> None:
    workflows = read_workflows()
    assert {"ci.yml", ADVISORIES} <= set(workflows), "the workflows were not read"
    assert permission_problems(workflows) == []

    def changed(name: str, change: Any) -> list[str]:
        copied = copy.deepcopy(workflows)
        change(copied.setdefault(name, {"jobs": {"audit": job("true")}}))
        return permission_problems(copied)

    jobs = workflows[ADVISORIES]["jobs"]
    advisory_job = next(iter(jobs))

    def with_step(step: dict[str, Any]) -> Any:
        """A change that adds a last step to the advisory job, whatever its own steps are."""
        return lambda w: w["jobs"][advisory_job]["steps"].append({"run": "true", **step})

    def triggered(w: dict[str, Any]) -> dict[str, Any]:
        return w.get("on", w.get(True))  # PyYAML reads the key `on` as the boolean true

    aws_action = {"uses": "aws-actions/configure-aws-credentials@v6", "with": {"aws-region": "us-east-1"}}
    cases: list[tuple[str, Any, str]] = [
        (ADVISORIES, lambda w: w["permissions"].update({"pull-requests": "write"}), "asks for pull-requests: write"),
        (ADVISORIES, lambda w: w["permissions"].update({"contents": "write"}), "asks for contents: write"),
        (ADVISORIES, lambda w: w.update(permissions="write-all"), "the workflow asks for write-all"),
        (
            ADVISORIES,
            lambda w: w["jobs"][advisory_job].update(permissions={"issues": "write", "id-token": "write"}),
            f"the job {advisory_job} asks for id-token: write",
        ),
        (ADVISORIES, lambda w: w.pop("permissions"), f"the job {advisory_job} asks for {DEFAULT}"),
        (ADVISORIES, with_step({"env": {"EXTRA": "${{ secrets.DEPLOY_TOKEN }}"}}), "names a secret"),
        (
            ADVISORIES,
            lambda w: triggered(w).update(workflow_call={"secrets": {"EXAMPLE_INPUT": {"required": False}}}),
            "names a secret",
        ),
        (ADVISORIES, lambda w: w["jobs"][advisory_job].update(secrets="inherit"), "names a secret"),
        (ADVISORIES, with_step(aws_action), "names an AWS credential"),
        (ADVISORIES, with_step({"env": {"AWS_ACCESS_KEY_ID": "${{ vars.KEY }}"}}), "names an AWS credential"),
        (ADVISORIES, lambda w: w.setdefault("env", {}).update(AWS_BEARER_TOKEN_BEDROCK=""), "names an AWS credential"),
        ("ci.yml", lambda w: w["permissions"].update({"issues": "write"}), "ci.yml: the workflow can write issues"),
        (
            "ci.yml",
            lambda w: w["jobs"]["hygiene"].update(permissions={"issues": "write"}),
            "ci.yml: the job hygiene can write issues",
        ),
        ("triage.yaml", lambda w: w.update(permissions="write-all"), "triage.yaml: the workflow can write issues"),
        ("triage.yaml", lambda w: None, "triage.yaml: the job audit can write issues"),
    ]
    for name, change, problem in cases:
        found = changed(name, change)
        assert any(problem in text for text in found), (name, problem, found)


def test_the_advisory_workflow_runs_the_check_daily_one_run_at_a_time() -> None:
    workflow = read_workflows()[ADVISORIES]
    # PyYAML reads the key `on` as the boolean true (YAML 1.1).
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    [schedule] = triggers["schedule"]
    minute, hour, *rest = schedule["cron"].split()
    assert minute.isdigit() and hour.isdigit() and rest == ["*", "*", "*"], "once a day"

    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    [check] = [step for step in steps if "tools/advisories/check.py" in step.get("run", "")]
    assert "--open-issues" in check["run"]
    assert check["env"]["GH_TOKEN"] == "${{ github.token }}"

    # One group for the repository, not one per run, and a run in progress is not cancelled.
    assert "${{" not in workflow["concurrency"]["group"]
    assert not workflow["concurrency"].get("cancel-in-progress")


COOLDOWN_DAYS = ("default-days", "semver-major-days", "semver-minor-days", "semver-patch-days")


def cooldown_problems(config: dict[str, Any]) -> list[str]:
    """The entries whose version updates, or some of them, wait less than a week."""
    found = []
    for entry in config.get("updates") or []:
        where = f"the {entry.get('package-ecosystem')} entry"
        cooldown = entry.get("cooldown")
        if not isinstance(cooldown, dict) or "default-days" not in cooldown:
            found.append(f"{where} has no cooling-off period")
            continue
        for key in COOLDOWN_DAYS:
            days = cooldown.get(key, WEEK)
            if not isinstance(days, int) or days < WEEK:
                found.append(f"{where} waits {days} days ({key})")
        # Dependencies left out of the cooldown are proposed the day they are released.
        if cooldown.get("exclude") or cooldown.get("include", ["*"]) != ["*"]:
            found.append(f"{where} leaves dependencies out of its cooling-off period")
    return found


def test_updates_wait_a_week() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    assert len(config["updates"]) >= 3, "the entries were not read"
    assert cooldown_problems(config) == []

    def changed(change: Any) -> list[str]:
        copied = copy.deepcopy(config)
        change(copied["updates"][0])
        return cooldown_problems(copied)

    ecosystem = config["updates"][0]["package-ecosystem"]
    assert changed(lambda e: e.pop("cooldown")) == [f"the {ecosystem} entry has no cooling-off period"]
    assert changed(lambda e: e["cooldown"].pop("default-days")) == [f"the {ecosystem} entry has no cooling-off period"]
    assert changed(lambda e: e["cooldown"].update({"default-days": 3})) == [
        f"the {ecosystem} entry waits 3 days (default-days)"
    ]
    assert changed(lambda e: e["cooldown"].update({"semver-patch-days": 1})) == [
        f"the {ecosystem} entry waits 1 days (semver-patch-days)"
    ]
    for leave_out in ({"exclude": ["*"]}, {"include": ["requests"]}):
        assert changed(lambda e, keys=leave_out: e["cooldown"].update(keys)) == [
            f"the {ecosystem} entry leaves dependencies out of its cooling-off period"
        ]


def ruby_glob(pattern: str) -> re.Pattern[str]:
    """A Dependabot directory pattern as Ruby's Dir.glob reads it: `**/` is any depth, and `**`
    anywhere else, like `*`, stays within one folder."""
    parts = pattern.strip("/").split("/")
    regex = ""
    for index, part in enumerate(parts):
        if part == "**" and index < len(parts) - 1:
            regex += "(?:[^/]+/)*"
        else:
            regex += re.escape(part).replace(r"\*\*", "[^/]*").replace(r"\*", "[^/]*")
            regex += "/" if index < len(parts) - 1 else ""
    return re.compile(regex)


def test_dependabot_watches_every_image_under_services() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    [docker] = [entry for entry in config["updates"] if entry["package-ecosystem"] == "docker"]
    patterns = [ruby_glob(path) for path in docker.get("directories") or [docker["directory"]]]

    folders = sorted(path.parent.relative_to(REPO).as_posix() for path in (REPO / "services").rglob("Dockerfile"))
    assert "services/engine/vllm" in folders, "the Dockerfiles were not found"
    for folder in folders:
        assert any(pattern.fullmatch(folder) for pattern in patterns), folder
    # A trailing `**` matches one level only: it would leave out services/engine/vllm.
    assert not ruby_glob("/services/**").fullmatch("services/engine/vllm")
