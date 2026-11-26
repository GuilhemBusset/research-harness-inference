"""The invocations the operator is shown, in `docs/runbooks/agent-session-evaluation.md` and in
the paired `evaluate` skill, as the command line reads them, offline; and the runbook's offline
comparison of exported bundles, run as written in a clean temporary checkout.

The skill pair's copies are kept byte-identical by `scripts/check.sh skills`, so one is read here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evaluate import cli, driver, report

REPO = Path(__file__).resolve().parents[3]
RUNBOOK = REPO / "docs" / "runbooks" / "agent-session-evaluation.md"
SKILL = REPO / ".agents" / "skills" / "evaluate" / "SKILL.md"
FIXTURE = REPO / "tools" / "evaluate" / "fixtures" / "report"
PROGRAM = "scripts/evaluate.sh"
SKILL_NAMES = ("/evaluate", "$evaluate")
REFERENCE_RUN, CANDIDATE_RUN = "0123456789abcdef", "fedcba9876543210"
# What the operator's shell puts in each variable of the runbook's commands, and the agent in each
# placeholder of the skill's.
VALUES = {
    "$experiment_issue": "42",
    "$repository": "owner/name",
    "$run_id": REFERENCE_RUN,
    "$reference_run": REFERENCE_RUN,
    "$candidate_run": CANDIDATE_RUN,
    "$submission_id": "0" * 32,
    "$name": "one-host",
    "$reference": "one-host",
    "$candidate": "two-hosts",
    "<reference-run-id>": REFERENCE_RUN,
    "<candidate-run-id>": CANDIDATE_RUN,
    "<submission-id>": "0" * 32,
}
FENCE = re.compile(r"^\s*```(\w*)\s*$")


def shown(text: str) -> list[tuple[str, str]]:
    """Each line of each fenced block, with its block's language: a `\\` continues a line."""
    found, language, pending = [], None, ""
    for line in text.splitlines():
        fence = FENCE.match(line)
        if fence:
            language = fence[1] if language is None else None
            continue
        if language is None:
            continue
        pending += line.strip()
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        if pending:
            found.append((language, pending))
        pending = ""
    return found


def argv(line: str, values: dict[str, str]) -> list[str]:
    for placeholder in sorted(values, key=len, reverse=True):
        line = line.replace(placeholder, values[placeholder])
    return shlex.split(line)[1:]


def commands(text: str, values: dict[str, str] = VALUES) -> list[list[str]]:
    """The `scripts/evaluate.sh` commands of the text's shell blocks, less the program."""
    return [argv(line, values) for language, line in shown(text) if language == "sh" and line.startswith(PROGRAM + " ")]


def skill_forms(text: str) -> list[list[str]]:
    """The skill invocations of the text's `text` blocks, less the skill's name."""
    return [argv(line, VALUES) for language, line in shown(text) if language == "text" and line.startswith(SKILL_NAMES)]


def parses(arguments: list[str]) -> bool:
    try:
        cli.parser().parse_args(arguments)
    except SystemExit:
        return False
    return True


def test_every_documented_invocation_is_one_the_command_line_takes():
    runbook, skill = RUNBOOK.read_text(encoding="utf-8"), SKILL.read_text(encoding="utf-8")
    documented = commands(runbook) + commands(skill)
    forms = skill_forms(runbook)
    assert forms and all(form[0] in ("run", "cancel") for form in forms)
    # The skill submits a `run` to the driver, and cancels through its socket.
    submitted = [["submit", *form[1:]] for form in forms if form[0] == "run"]
    assert [one for one in documented + submitted if not parses(one)] == []

    def has(command: str, *flags: str) -> bool:
        return any(one[0] == command and all(flag in one for flag in flags) for one in commands(runbook))

    assert has("driver") and has("submit", "--repository") and has("inspect", "--submission")
    assert has("run", "--profile", "screen", "--for", "new")
    assert has("plan", "qualify") and has("run", "qualify", "new") and has("run", "--resume", "qualify", "42")
    assert has("run", "--session", "existing") and any("existing" in form for form in forms)
    assert has("cancel", "--socket", "--submission") and has("cancel", REFERENCE_RUN)
    assert has("status") and has("publish", "--resume")
    assert has("compare", "--issue", REFERENCE_RUN, CANDIDATE_RUN) and has("export", "--to")
    assert any(one[0] == "compare" and "--issue" not in one for one in commands(runbook))


def test_the_documented_plans_run_offline(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    for name in [name for name in os.environ if name.startswith("AWS_")]:
        monkeypatch.delenv(name)
    plans = [one for text in (RUNBOOK, SKILL) for one in commands(text.read_text()) if one[0] == "plan"]
    assert plans
    for one in plans:
        assert cli.main(one) == 0, capsys.readouterr().err


def test_a_documented_submission_is_a_typed_request_that_carries_its_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The driver takes the repository from the request: set on the driver's side alone, it is not
    in what the client sends, and the request is refused before anything is admitted."""
    monkeypatch.delenv(cli.REPOSITORY_VARIABLE, raising=False)
    monkeypatch.setattr(cli, "OUTBOX", tmp_path / "outbox")
    sent = []

    def call(socket_path: Path, message: dict) -> dict:
        sent.append(message)
        return {"ok": True, "submission": message["submission"], "run_id": REFERENCE_RUN, "step": "admitted"}

    monkeypatch.setattr(driver, "call", call)
    scan = report.hygiene_scan()
    submissions = [
        one for text in (RUNBOOK, SKILL) for one in commands(text.read_text()) if one[0] == "submit"
    ]
    assert submissions
    for one in submissions:
        assert cli.main(one) == 0
        typed = driver.parse(json.dumps(sent[-1]).encode(), scan)
        assert typed.run is not None and typed.run["repository"] == VALUES["$repository"]
    without = [word for one in submissions[:1] for word in one if word not in ("--repository", VALUES["$repository"])]
    assert cli.main(without) == 0
    with pytest.raises(driver.DriverError) as refused:
        driver.parse(json.dumps(sent[-1]).encode(), scan)
    assert refused.value.reason is driver.Refusal.INVALID_FIELD


# --- The offline comparison, in a clean checkout ------------------------------------------------


def clean_checkout(target: Path) -> Path:
    """The repository's tracked and new files, without what git ignores: no `.cache`, no
    environment, no build output."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO, capture_output=True, check=False
    )
    if listed.returncode != 0:  # a copy of the repository with no git metadata
        ignored = (".git", ".venv", ".cache", "cdk.out", "node_modules", "__pycache__", ".*_cache")
        shutil.copytree(REPO, target, ignore=shutil.ignore_patterns(*ignored), symlinks=True)
        return target
    for name in filter(None, listed.stdout.decode().split("\0")):
        source = REPO / name
        if source.is_file() or source.is_symlink():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name, follow_symlinks=False)
    return target


def reference_bundle(directory: Path) -> Path:
    """The comparison's reference: the qualification plan on one host, as `test_report` builds it."""
    spec = importlib.util.spec_from_file_location("_report_bundles", Path(__file__).with_name("test_report.py"))
    assert spec is not None and spec.loader is not None
    bundles = sys.modules.setdefault(spec.name, importlib.util.module_from_spec(spec))
    spec.loader.exec_module(bundles)
    bundles.build(directory, bundles.REFERENCE)
    report.write_derived(directory, bundles.scorecard(directory))
    return directory


def test_the_documented_comparison_reproduces_exported_scorecards_and_digests_in_a_clean_checkout(tmp_path: Path):
    # The operator exports from a checkout of their own, which holds the private runs. A reader
    # checks out the exported folders alone, with no run cache, and regenerates and compares them.
    operator, reader = clean_checkout(tmp_path / "operator"), clean_checkout(tmp_path / "reader")
    runs = operator / ".cache" / "eval" / "runs"
    sources = {
        "one-host": reference_bundle(runs / REFERENCE_RUN / "bundle"),
        "two-hosts": Path(shutil.copytree(FIXTURE, runs / CANDIDATE_RUN / "bundle")),
    }
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AWS_", "RHI_EVALUATE_", "GH_", "GITHUB_")) and name not in ("PYTHONPATH", "VIRTUAL_ENV")
    }
    absent = {name: str(tmp_path / "absent") for name in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE")}
    # The locked environment already synced, and no network for uv to reach.
    offline = {"UV_OFFLINE": "1", "UV_PROJECT_ENVIRONMENT": sys.prefix}

    def script(checkout: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = [str(checkout / PROGRAM), *arguments]
        return subprocess.run(command, cwd=checkout, env=kept | absent | offline, capture_output=True, text=True, check=False)

    documented = [line for language, line in shown(RUNBOOK.read_text()) if language == "sh"]
    exports = [line for line in documented if line.startswith(f"{PROGRAM} export .cache/eval/runs/")]
    reports = [line for line in documented if line.startswith(f"{PROGRAM} report docs/evaluations/")]
    compares = [line for line in documented if line.startswith(f"{PROGRAM} compare docs/evaluations/")]
    assert len(exports) == len(reports) == len(compares) == 1
    rate, source = cli._example()[1], cli.Source.EXAMPLE
    derived = {}
    for name, bundle in sources.items():
        exported = script(operator, argv(exports[0], VALUES | {"$run_id": bundle.parent.name, "$name": name}))
        assert exported.returncode == 0, exported.stderr
        derived[name] = report.regenerate(report.load(bundle), usd_per_host_hour=rate, rate_source=source)
        assert exported.stdout.strip() == f"exported: {derived[name].digest}"
        shutil.copytree(operator / "docs" / "evaluations" / name, reader / "docs" / "evaluations" / name)
    assert not (reader / ".cache").exists()
    for name in sources:
        target = reader / "docs" / "evaluations" / name
        before = {path: path.read_bytes() for path in target.rglob("*") if path.is_file()}
        regenerated = script(reader, argv(reports[0], VALUES | {"$name": name}))
        assert regenerated.returncode == 0, regenerated.stderr
        # The scorecard and its digest, regenerated, are those the export wrote.
        assert {path: path.read_bytes() for path in target.rglob("*") if path.is_file()} == before
        assert (target / report.DIGEST).read_text().strip() == derived[name].digest == report.digest_of(derived[name].summary)
        assert regenerated.stdout == derived[name].report
    assert (reader / "docs/evaluations/two-hosts" / report.DIGEST).read_text() == (FIXTURE / report.DIGEST).read_text()
    arguments = argv(compares[0], VALUES)
    printed = script(reader, arguments)
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.startswith("treatment diff")
    as_json = script(reader, [*arguments, "--json"])
    assert as_json.returncode == 0, as_json.stderr
    expected = report.compare(
        report.load(sources["one-host"]), report.load(sources["two-hosts"]), usd_per_host_hour=rate, rate_source=source
    )
    assert json.loads(as_json.stdout) == json.loads(json.dumps(expected))
