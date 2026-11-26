"""Regression for #766: a finding whose reproduction replaced PYTHONPATH could never close.

The verifier loads its result plugin through PYTHONPATH. The examiner's probes ran
`env PYTHONPATH=tools:tools/evaluate/tests uv run --locked pytest -q ...`: pytest could not load
the plugin, so every run failed whatever the code did, the findings "still reproduced" on every
head the builder made, and the builder may not change an examiner's command. The engine now runs
a reproduction with the PYTHONPATH its first command assigns extended by the verifier's.

The engine run is real (git, the verifier, real pytest, squash merges into a bare remote); the
agents and GitHub are the fakes."""

import os
import subprocess
import sys

import pytest

from helpers import HAS_PYTEST, SHIP, TempRepo, issue_body
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


def run(command: str) -> str:
    """What `command`, rewritten, prints where the verifier's PYTHONPATH is `/verifier`: bash
    expands it as the verifier's own run does (`verify._run`)."""
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": "/verifier"}
    return subprocess.run(["bash", "-c", extended(command)], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.mark.parametrize(("command", "printed"), [
    ("env PYTHONPATH=tools:tools/evaluate/tests printenv PYTHONPATH", "tools:tools/evaluate/tests:/verifier"),
    ("PYTHONPATH=src printenv PYTHONPATH", "src:/verifier"),
    ("env -u VIRTUAL_ENV PYTHONPATH=client printenv PYTHONPATH", "client:/verifier"),
    ("FOO=1 PYTHONPATH=src printenv PYTHONPATH", "src:/verifier"),
    ("PYTHONPATH=src; printenv PYTHONPATH", "src:/verifier"),
    ("export PYTHONPATH=src && printenv PYTHONPATH", "src:/verifier"),
    # quoted values are extended as they are, spaces kept
    ('PYTHONPATH="a b" printenv PYTHONPATH', "a b:/verifier"),
    ("env PYTHONPATH='a b':src printenv PYTHONPATH", "a b:src:/verifier"),
    # an empty value adds no empty entry, which is the current directory
    ("PYTHONPATH= printenv PYTHONPATH", "/verifier"),
    ('PYTHONPATH="" printenv PYTHONPATH', "/verifier"),
])
def test_a_pythonpath_the_first_command_assigns_extends_the_verifiers(command, printed):
    assert extended(command) != command
    assert run(command) == printed


@pytest.mark.parametrize("command", [
    # already extends it
    "PYTHONPATH=src:$PYTHONPATH pytest -q tests",
    'PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" pytest -q tests',
    # sets none
    "uv run --locked pytest -q tests",
    "MYPYTHONPATH=src pytest -q tests",
    # removes it on purpose, as the examiner's prompt says for the type checker
    "env -u PYTHONPATH scripts/check.sh types",
    # a value with an expansion or an escape: where its word ends is the shell's to say
    "PYTHONPATH=$HOME/src pytest -q tests",
    "PYTHONPATH=a\\ b pytest -q tests",
    # later in the command, where a `PYTHONPATH=` word may be an argument
    "cd services && PYTHONPATH=src pytest -q tests",
    "uv run --locked env PYTHONPATH=src pytest -q tests",
    "grep -r PYTHONPATH=src scripts",
])
def test_any_other_command_is_left_as_written(command):
    assert extended(command) == command
