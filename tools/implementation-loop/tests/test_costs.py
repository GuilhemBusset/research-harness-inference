"""`implement costs`: what each issue of a run cost, by role and by size (ADR 0001, decision 8).

The run log and the issues under `fixtures/costs/` are written for this test. Five issues, three
`size:S` (21, 22, 23) and two `size:L` (31, 32), and twenty attempts: one call for no issue, a
Claude attempt that timed out and reports no cost (22), one that answered with a null cost (32),
one that stopped at its budget, charged but not answered (23), and a Codex attempt that crashed
with no usage (23). Every dollar value is a multiple of 0.25, so the sums below are exact."""

import importlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import HERE, REPO_ROOT, git


def engine_of_this_tree():
    """This tree's `implement_loop`, loaded under a name of its own.

    The verifier puts the engine that runs it first on PYTHONPATH and loads its plugin from there.
    The plugin drops that package from the module cache of the pytest that records, not of an
    xdist worker, so in a worker `implement_loop` is the running engine: the one before this
    change, with no `costs`. The command under test is the one in this tree."""
    package = HERE.parent / "implement_loop"
    spec = importlib.util.spec_from_file_location("implement_loop_of_this_tree", package / "__init__.py",
                                                  submodule_search_locations=[str(package)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {package}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return spec.name


ENGINE = engine_of_this_tree()
cli = importlib.import_module(f"{ENGINE}.cli")
FakeGitHub = importlib.import_module(f"{ENGINE}.github").FakeGitHub

FIXTURES = HERE / "fixtures" / "costs"
RECORDS = [json.loads(line) for line in (FIXTURES / "agents.jsonl").read_text().splitlines()]
SIZES = {21: "S", 22: "S", 23: "S", 31: "L", 32: "L"}


class CostsCommandTests(unittest.TestCase):
    """The command itself, on a run directory that holds the recorded log, with GitHub replaced
    by the recorded issues."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="loop-costs-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git("init", "-q", cwd=self.repo)
        self.run_dir = self.repo / ".git" / "implementation-loop" / "issue-1"
        self.run_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "agents.jsonl", self.run_dir / "agents.jsonl")
        self.gh = FakeGitHub(json.loads((FIXTURES / "issues.json").read_text()))

    def costs(self, *flags: str) -> str:
        out = io.StringIO()
        with mock.patch.object(cli, "repo_root", return_value=self.repo), \
             mock.patch.object(cli, "GhCli", return_value=self.gh) as self.gh_cli, redirect_stdout(out):
            self.assertEqual(cli.main(["--repo", "test/repo", "costs", "1", *flags]), 0)
        return out.getvalue()

    def report(self) -> dict:
        return json.loads(self.costs("--json"))

    def test_each_issue_has_its_size_its_answered_calls_and_its_dollars(self):
        rows = {r["issue"]: (r["size"], r["answered"], r["usd"], r["tokens"]) for r in self.report()["issues"]}
        self.assertEqual(rows, {21: ("S", 3, 5.0, 1200), 22: ("S", 5, 11.0, 6000), 23: ("S", 1, 10.75, 0),
                                31: ("L", 3, 13.0, 6000), 32: ("L", 4, 14.5, 8800)})

    def test_the_totals_are_the_whole_log_the_call_for_no_issue_included(self):
        rep = self.report()
        self.assertEqual(rep["total"], {"attempts": 20, "answered": 17, "usd": 54.75, "tokens": 22000})
        self.assertEqual(rep["no_issue"], {"attempts": 1, "answered": 1, "usd": 0.5, "tokens": 0})
        self.assertEqual(sum(r["usd"] for r in rep["issues"]) + rep["no_issue"]["usd"], rep["total"]["usd"])

    def test_each_role_has_its_calls_and_the_median_and_maximum_per_call(self):
        roles = {r["role"]: r for r in self.report()["roles"]}
        self.assertEqual(sorted(roles), ["build", "examine", "explore", "plan"])
        self.assertEqual({k: (r["attempts"], r["answered"]) for k, r in roles.items()},
                         {"build": (8, 6), "examine": (6, 5), "explore": (1, 1), "plan": (5, 5)})
        self.assertEqual(roles["plan"]["usd"], {"total": 10.0, "median": 2.0, "max": 3.0})
        # six of the eight build attempts report a cost: 3, 4, 6, 9.25, 10 and 12
        self.assertEqual(roles["build"]["usd"], {"total": 44.25, "median": 7.625, "max": 12.0})
        self.assertEqual(roles["build"]["tokens"], {"total": 0, "median": None, "max": None})
        # five of the six Codex attempts report tokens: 1200, 2600, 3400, 6000 and 8800
        self.assertEqual(roles["examine"]["tokens"], {"total": 22000, "median": 3400, "max": 8800})
        self.assertEqual(roles["examine"]["usd"], {"total": 0, "median": None, "max": None})

    def test_each_size_has_the_median_and_the_maximum_per_issue(self):
        sizes = {r["size"]: r for r in self.report()["sizes"]}
        self.assertEqual(list(sizes), ["S", "L"])
        self.assertEqual(sizes["S"], {"size": "S", "issues": 3, "answered": {"median": 3, "max": 5},
                                      "usd": {"median": 10.75, "max": 11.0}, "tokens": {"median": 1200, "max": 6000}})
        self.assertEqual(sizes["L"], {"size": "L", "issues": 2, "answered": {"median": 3.5, "max": 4},
                                      "usd": {"median": 13.75, "max": 14.5}, "tokens": {"median": 7400, "max": 8800}})

    def test_an_issue_nothing_ran_for_is_listed_and_left_out_of_its_size(self):
        self.gh.add_issue(24, "Costs fixture: not started", labels=("size:S",))
        (self.run_dir / "state.json").write_text(json.dumps(
            {"version": 1, "issues": {"24": {"number": 24}}, "context": None, "meta": {"root": 1}}))
        rep = self.report()
        self.assertEqual([(r["size"], r["answered"], r["usd"]) for r in rep["issues"] if r["issue"] == 24], [("S", 0, 0)])
        self.assertEqual([(r["issues"], r["usd"]["median"]) for r in rep["sizes"] if r["size"] == "S"], [(3, 10.75)])

    def test_the_printed_report_names_every_issue_role_and_size(self):
        text = self.costs()
        for expected in ("#21", "#22", "#23", "#31", "#32", "no issue", "build", "examine", "explore", "plan",
                         "size S", "size L", "$54.75", "22,000"):
            self.assertIn(expected, text)

    def test_a_run_with_no_recorded_call_reads_nothing_from_github(self):
        (self.run_dir / "agents.jsonl").unlink()
        self.assertIn("No agent call recorded", self.costs())
        self.gh_cli.assert_not_called()

    def test_the_script_reaches_the_command(self):
        proc = subprocess.run([str(REPO_ROOT / "scripts" / "implement.sh"), "costs", "--help"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("issue", proc.stdout)


class NoCostTests(unittest.TestCase):
    """An attempt that reports no cost adds no dollar."""

    def test_the_fixture_holds_attempts_without_a_cost(self):
        without = [(r["seq"], r["vendor"], r["outcome"]) for r in RECORDS if r.get("cost_usd") is None]
        self.assertIn((6, "claude", "timeout"), without, "no `cost_usd` at all")
        self.assertIn((20, "claude", "answered"), without, "a null `cost_usd`")
        self.assertEqual(sum(1 for _, vendor, _ in without if vendor == "codex"), 6, "Codex never reports dollars")

    def test_without_them_every_dollar_figure_is_the_same(self):
        def dollars(rep: dict) -> tuple:
            return (rep["total"]["usd"], [(r["issue"], r["usd"]) for r in rep["issues"]],
                    [(r["role"], r["usd"]) for r in rep["roles"] if r["usd"]["total"]],
                    [(r["size"], r["usd"]) for r in rep["sizes"]])
        whole = cli.cost_report(RECORDS, SIZES)
        costed = cli.cost_report([r for r in RECORDS if r.get("cost_usd") is not None], SIZES)
        self.assertEqual(dollars(whole), dollars(costed))
        self.assertEqual((whole["total"]["attempts"], costed["total"]["attempts"]), (20, 12))

    def test_a_timeout_is_an_attempt_and_neither_a_dollar_nor_a_call_of_the_median(self):
        timeout = {"role": "build", "vendor": "claude", "issue": 21, "outcome": "timeout"}
        before, after = cli.cost_report(RECORDS, SIZES), cli.cost_report([*RECORDS, timeout], SIZES)
        build = [next(r for r in rep["roles"] if r["role"] == "build") for rep in (before, after)]
        self.assertEqual(build[1]["attempts"], build[0]["attempts"] + 1)
        self.assertEqual(build[1]["usd"], build[0]["usd"], "a zero would have pulled the median down")
        self.assertEqual(after["issues"][0], {**before["issues"][0], "attempts": before["issues"][0]["attempts"] + 1})


if __name__ == "__main__":
    unittest.main()
