"""Regression for #766: a finding whose reproduction replaced PYTHONPATH could never close.

The verifier loads its result plugin through PYTHONPATH. The examiner's probes ran
`env PYTHONPATH=tools:tools/evaluate/tests uv run --locked pytest -q ...`: pytest could not load
the plugin, so every run failed whatever the code did, the findings "still reproduced" on every
head the builder made, and the builder may not change an examiner's command. The engine runs a
reproduction as written and, only when pytest could not load the plugin there, once more with the
PYTHONPATH its first command assigns extended by a directory that holds the plugin alone.

The engine runs are real (git, the verifier, real pytest, squash merges into a bare remote); the
agents and GitHub are the fakes."""

import os
import subprocess
import sys

import pytest

from helpers import HAS_PYTEST, SHIP, TempRepo, git, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE = 1, 2
PY = sys.executable
Phase = L.model.Phase
extended = L.engine.extended_pythonpath

# What the builder writes on every build: a module the probe finds only through the probe's own
# PYTHONPATH. The fake builder writes the file it names once a finding speaks of an edge case.
MODULE = "lib/edge_case.py", f"from pathlib import Path\n\nHANDLED = Path('edge_{ISSUE}.txt')\n"
PROBE = "checks/probe_edge.py"
PROBE_TEXT = "import edge_case\n\n\ndef test_the_edge_case_is_handled():\n    assert edge_case.HANDLED.exists()\n"
COMMAND = f"env PYTHONPATH=lib {PY} -m pytest -q {PROBE}"


class Agents(L.agents.FakeAgents):
    """The fake agents, with #766's examiner: its finding stands on a pytest probe whose command
    replaces PYTHONPATH."""

    def build(self, vendor, req, plan, worktree, feedback):
        result = super().build(vendor, req, plan, worktree, feedback)
        (worktree / MODULE[0]).parent.mkdir(parents=True, exist_ok=True)
        (worktree / MODULE[0]).write_text(MODULE[1])
        return result

    def examine(self, *args):
        result = super().examine(*args)
        for f in result.findings:
            f.probe, f.reproduction = PROBE, COMMAND
            result.probes.append(L.agents.Probe(PROBE, f.item, COMMAND, content=PROBE_TEXT))
        return result


@pytest.fixture
def repo():
    made = TempRepo()
    yield made
    made.cleanup()


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
def test_a_finding_whose_reproduction_sets_pythonpath_closes_once_the_code_passes(repo):
    fake = L.github.FakeGitHub(on_merge=repo.merge)
    body = issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["The test passes on main."])
    fake.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
    fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))
    store = L.state.RunStore.for_root(repo.work, EPIC)
    workspace = L.workspace.Workspace(repo.work, EPIC, SHIP, base_dir=repo.loop_dir)
    agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1, probe="none")})
    L.engine.Engine(
        L.graph.build_plan(fake, EPIC, repo.work), store, fake, workspace, agents,
        L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
        repo_root=repo.work, log=lambda m: None, sleep=lambda s: None).run()

    st = store.issue(ISSUE)
    failures = [e["problems"] for e in store.events() if e["kind"] == "round_failed"]
    assert st.phase == Phase.DONE, failures
    [finding] = st.findings
    assert (finding["status"], finding["reason"], finding["reproduction"]) == ("closed", "its probe passes", COMMAND)
    # it was blocking (the probe failed on the first build) and closed on the build that fixed it
    assert [e["closed"] for e in store.events() if e["kind"] == "verified" and e["closed"]] == [[finding["id"]]]
    assert len([c for c in agents.calls if c[0] == "build"]) == 2


@pytest.fixture
def engine(repo):
    """An engine with what running a reproduction needs."""
    made = L.engine.Engine.__new__(L.engine.Engine)
    made.ws = L.workspace.Workspace(repo.work, EPIC, SHIP, base_dir=repo.loop_dir)
    made.store = L.state.RunStore.for_root(repo.work, EPIC)
    made.caps, made.check_env = L.config.Caps(), {}
    return made


@pytest.fixture
def wt(engine):
    """The head under judgement: the repository's coordination worktree."""
    return engine.ws.coord()


def commit(wt, files: dict[str, str]) -> None:
    """Add `files` to the head under judgement."""
    for rel, text in files.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
    git("add", "-A", cwd=wt)
    git("commit", "-qm", "code under review", cwd=wt)


def reproduces(engine, wt, command: str, probe: str) -> bool | None:
    """The engine's verdict on a finding whose probe `checks/probe_x.py` holds `probe`."""
    return engine._reproduces(wt, command, ".", ("checks/probe_x.py", probe))


PASSES, FAILS = "def test_x():\n    pass\n", "def test_x():\n    assert 1 == 2\n"
# A pytest run that does not find the verifier's plugin, with the test the probe holds.
REPLACES = f"env PYTHONPATH=lib {PY} -m pytest -q checks/probe_x.py"
# The code under review's own `implement_loop`, as a repository whose package has that name holds it.
UNDER_REVIEW = {"src/implement_loop/__init__.py": "MARK = 'reviewed'\n", "src/implement_loop/engine.py": ""}
# A probe that imports `implement_loop` bare, after the plugin has loaded.
BARE = "import sys\n\nsys.path.insert(0, 'src')\n\n\ndef test_x():\n    import implement_loop\n"


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
@pytest.mark.parametrize(("probe", "holds"), [(PASSES, False), (FAILS, True)])
def test_a_pytest_run_that_cannot_load_the_plugin_is_judged_on_the_extended_run(engine, wt, probe, holds):
    assert reproduces(engine, wt, REPLACES, probe) is holds


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
@pytest.mark.parametrize(("code", "holds"), [({}, True), (UNDER_REVIEW, False)])
def test_the_extended_run_imports_the_implement_loop_under_review_not_the_verifiers(engine, wt, code, holds):
    """Codex's review of the second version, whose second run put the engine's own directory on
    PYTHONPATH: a probe of the code under review's `implement_loop` imported the verifier's when
    the code had none, and passed."""
    commit(wt, {"README.md": "code under review\n", **code})
    probe = ("import sys\n\nsys.path.insert(0, 'src')\n\n\ndef test_x():\n"
             "    from implement_loop import engine  # the verifier's copy has one too\n")
    assert reproduces(engine, wt, REPLACES, probe) is holds


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
@pytest.mark.parametrize(("code", "probe", "holds"), [
    ({}, BARE, True),
    (UNDER_REVIEW, BARE + "    assert implement_loop.MARK == 'reviewed'\n", False),
], ids=["none-under-review", "one-under-review"])
def test_a_bare_import_on_the_extended_run_finds_the_implement_loop_under_review_or_none(engine, wt, code, probe, holds):
    """Codex's review of the third version: the plugin's package left its directory on sys.path,
    so once the plugin had dropped it, a bare `import implement_loop` found it again and passed
    where the code under review had none."""
    commit(wt, {"README.md": "code under review\n", **code})
    assert reproduces(engine, wt, REPLACES, probe) is holds


# A probe whose test starts a Python process of its own, with the environment it was given: that
# process inherits the extended PYTHONPATH (the probe's `lib` and the directory that holds the
# plugin), and finds there no `implement_loop`, since the code under review has none.
CHILD = ("import os\nimport subprocess\nimport sys\n\n\ndef test_x():\n"
         "    assert len(os.environ['PYTHONPATH'].split(os.pathsep)) == 2\n"
         "    found = subprocess.run([sys.executable, '-c', 'import importlib.util; "
         "print(importlib.util.find_spec(\"implement_loop\"))'], capture_output=True, text=True, check=True)\n"
         "    assert found.stdout.strip() == 'None'\n")


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
def test_a_process_the_extended_run_starts_finds_no_implement_loop_and_its_tests_are_recorded(engine, wt):
    """Codex's review of the fourth version: the directory added to PYTHONPATH held the plugin as
    `implement_loop.loop_pytest`, under a package of that name, and only the pytest process took it
    off `sys.path`. A process a test started inherited the entry and imported that package. The
    directory now holds the plugin alone, as a top-level module of its own name
    (`verify.PLUGIN_COPY`), and the run still records its tests through it."""
    commit(wt, {"README.md": "code under review\n"})
    ran = engine._reproduction(wt, REPLACES, ".", ("checks/probe_x.py", CHILD))
    assert (ran.passed, ran.tests) == (True, {"passed": 1, "failed": 0, "skipped": 0}), ran.output_tail


def test_the_directory_added_to_pythonpath_holds_the_plugin_alone_under_its_own_name(tmp_path):
    verify = L.engine.verify
    root = L.engine.Engine._plugin_only(tmp_path)
    assert [p.relative_to(root).as_posix() for p in root.rglob("*")] == [f"{verify.PLUGIN_COPY}.py"]
    assert "." not in verify.PLUGIN_COPY, "a top-level module, under no package"
    source = verify.ENGINE_DIR.joinpath(*verify.PLUGIN.split(".")).with_suffix(".py")
    assert (root / f"{verify.PLUGIN_COPY}.py").read_bytes() == source.read_bytes()


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
@pytest.mark.parametrize(("probe", "holds"), [(PASSES, False), (FAILS, True)])
def test_a_pytest_of_the_extended_run_that_keeps_the_verifiers_pythonpath_loads_the_plugin_too(engine, wt, probe, holds):
    """A second pytest in the command, which the assignment at its head does not reach, runs with
    the verifier's PYTHONPATH: it finds the plugin there by the same name, and is recorded."""
    command = f"{REPLACES} && {PY} -m pytest -q checks/probe_x.py"
    assert reproduces(engine, wt, command, probe) is holds


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
def test_an_extended_run_under_another_verifiers_pytest_loads_the_copy_alone(engine, wt, monkeypatch):
    """The engine's own tests, run as a verifier's check, inherit that verifier's `-p` of the plugin
    by its package name, which a command that replaces PYTHONPATH cannot reach: the extended run
    leaves it out and loads the copy."""
    monkeypatch.setenv("PYTEST_ADDOPTS", f"-p {L.engine.verify.PLUGIN} -p no:cacheprovider")
    assert reproduces(engine, wt, REPLACES, PASSES) is False


def test_an_extended_run_that_left_no_pytest_record_proves_nothing(engine, wt):
    """Codex's review of the second version: its second run closed a finding on exit 0 alone. As
    written, this command runs pytest, which cannot load the plugin; extended, it runs none."""
    command = f"PYTHONPATH=lib; case $PYTHONPATH in *:*) ;; *) {PY} -m pytest -q checks/probe_x.py;; esac"
    assert extended(command, "/plugin") != command
    assert reproduces(engine, wt, command, PASSES) is True


def test_a_command_that_fails_on_its_own_is_judged_as_written(engine, wt):
    """Codex's review of the first version, which always extended: the verifier's directory holds
    `implement_loop`, so a command that fails because it cannot import it passed once extended,
    and its finding would have closed on a command other than the one the examiner wrote."""
    command = f"env PYTHONPATH=lib {PY} -S checks/probe_x.py"
    assert reproduces(engine, wt, command, "import implement_loop\n") is True
    assert reproduces(engine, wt, command, "import os\n") is False


@pytest.mark.skipif(not HAS_PYTEST, reason="pytest is needed for the verifier's accounting")
def test_a_value_that_is_not_literal_is_run_as_written_only(engine, wt):
    """The first version split `PYTHONPATH=lib:<(true)` before the process substitution."""
    command = f"PYTHONPATH=lib:<(true) {PY} -m pytest -q checks/probe_x.py"
    assert extended(command, "/plugin") == command
    assert reproduces(engine, wt, command, PASSES) is True, "it still cannot load the plugin: the examiner's to fix"


def run(command: str, directory: str) -> str:
    """What `command`, extended by `directory`, prints, run by bash as the verifier's own run is
    (`verify._run`), where the verifier's PYTHONPATH is `/verifier`."""
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": "/verifier"}
    return subprocess.run(["bash", "-c", extended(command, directory)], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.mark.parametrize(("command", "printed"), [
    ("env PYTHONPATH=tools:tools/evaluate/tests printenv PYTHONPATH", "tools:tools/evaluate/tests:/plugin"),
    ("PYTHONPATH=src printenv PYTHONPATH", "src:/plugin"),
    ("env -u VIRTUAL_ENV PYTHONPATH=client printenv PYTHONPATH", "client:/plugin"),
    ("CI=true PYTHONPATH=src printenv PYTHONPATH", "src:/plugin"),
    ("PYTHONPATH=src; printenv PYTHONPATH", "src:/plugin"),
    ("PYTHONPATH=src&&printenv PYTHONPATH", "src:/plugin"),
    ("export PYTHONPATH=src && printenv PYTHONPATH", "src:/plugin"),
    # an empty value adds no empty entry, which is the current directory
    ("PYTHONPATH= printenv PYTHONPATH", "/plugin"),
])
def test_a_literal_pythonpath_the_first_command_assigns_is_extended_by_the_directory(command, printed):
    assert extended(command, "/plugin") != command
    assert run(command, "/plugin") == printed


def test_the_directory_is_quoted_for_the_shell():
    assert run("env PYTHONPATH=src printenv PYTHONPATH", "/a b/$HOME") == "src:/a b/$HOME"


@pytest.mark.parametrize("command", [
    # already extends it
    "PYTHONPATH=src:$PYTHONPATH pytest -q tests",
    'PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" pytest -q tests',
    # sets none
    "uv run --locked pytest -q tests",
    "MYPYTHONPATH=src pytest -q tests",
    # removes it on purpose, as the examiner's prompt says for the type checker
    "env -u PYTHONPATH scripts/check.sh types",
    # a value that is not literal: where its word ends, and what it holds, is the shell's to say
    "PYTHONPATH=src:<(true) pytest -q tests",
    "PYTHONPATH=src>(cat) pytest -q tests",
    'PYTHONPATH="a b" pytest -q tests',
    "env PYTHONPATH='a b' pytest -q tests",
    "PYTHONPATH=a\\ b pytest -q tests",
    "PYTHONPATH=$HOME/src pytest -q tests",
    "PYTHONPATH=`pwd` pytest -q tests",
    "PYTHONPATH=~/src pytest -q tests",
    "env PYTHONPATH=src/* pytest -q tests",
    "env PYTHONPATH={a,b} pytest -q tests",
    "X='a b' PYTHONPATH=src pytest -q tests",
    # later in the command, where a `PYTHONPATH=` word may be an argument
    "cd services && PYTHONPATH=src pytest -q tests",
    "uv run --locked env PYTHONPATH=src pytest -q tests",
    "grep -r PYTHONPATH=src scripts",
])
def test_any_other_command_is_left_as_written(command):
    assert extended(command, "/plugin") == command
