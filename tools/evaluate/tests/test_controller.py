"""The operator's terminal workflow (tools/evaluate/controller.py), driven through `cli.main`, the
entry point of scripts/evaluate.sh, with fake external services: GitHub in memory (the publisher
tests' `FakeGitHub`), the deployed pool over moto (the booking tests' `Deployed`: table, scaler,
probes and a clock only a sleep moves), and a measurement that journals each cell as `phases`
does: the observations of a bundle the report tests build, the evidence a checkpoint keeps
(`controller.keep_evidence`), and every request it would send to the gateway counted. The local
report is the controller's own bundle of what was checkpointed (`controller.write_bundle`), so
the verdicts and the exit codes are the real report's of the cells measured. No account, no
network.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import itertools
import json
import math
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from evaluate import booking, cli, contracts, controller, phases, publisher, report
from evaluate import live as lookups
from evaluate import plan as planning
from evaluate.contracts import Profile, Settings, TrialOutcome, TrialRecord
from evaluate.journal import Journal
from evaluate.tests import test_phases as lab
from evaluate.tests import test_report as report_lab
from evaluate.tests import test_telemetry as lab_telemetry
from evaluate.tests.test_booking import ENTRY, POOL, RELEASE, Deployed, pool, stored  # noqa: F401 - `pool` is a fixture
from evaluate.tests.test_publisher import REPO, FakeGitHub
from evaluate.tests.test_report import CANDIDATE, build
from rhi_admin import pools as admin_pools
from rhi_admin import session as admin_session
from rhi_common import sessions as rules
from rhi_common.keys import generate_key
from rhi_common.records import Session

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from evaluate.plan import Plan

# A deployment whose sessions may last two days, so that one booking holds the whole profile.
SESSION_HOURS = 48
PASSING = replace(CANDIDATE, fails=frozenset(), slow_several_agents_repetition=None)
FEW_TURNS = replace(PASSING, later_turns=20)  # too few later turns to grade capacity: inconclusive
SERVED = "sha256:" + "5" * 64
OPERATOR = "user/operator"
PLAN = planning.expand(planning.load_suite(), Profile.QUALIFY, 2)
ESTIMATES = PLAN.spec.estimates
# The window that holds every cell of the plan, its warm-up, the wait for capacity and the cleanup.
WHOLE = math.ceil(
    (sum(PLAN.reserved_ms(cell) for cell in PLAN.cells) + ESTIMATES.capacity_wait_ms + ESTIMATES.cleanup_ms) / 60_000
)


class Died(BaseException):
    """The driver's process ending where it stands: no handler of it runs."""


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The evidence a measurement leaves, built once: a pass, a measured miss, too few turns."""
    root = tmp_path_factory.mktemp("bundles")
    return {name: build(root / name, variant) for name, variant in (("pass", PASSING), ("miss", CANDIDATE), ("few", FEW_TURNS))}


class GitHub(FakeGitHub):
    """The fake GitHub, logging each record it stores in the shared order of events."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.on_comment: Callable[[], None] | None = None

    def post_issue(self, login: str, body: str) -> int:
        self.events.append("issue")
        return super().post_issue(login, body)

    def post_comment(self, login: str, number: int, body: str) -> int:
        self.events.append("comment")
        if self.on_comment is not None:
            self.on_comment()
        return super().post_comment(login, number, body)


class Pool(Deployed):
    """The deployed pool, logging each write of the driver: the booking, then its stop."""

    def __init__(self, table: Any, events: list[str]) -> None:
        super().__init__(table)
        self.events, self.markers = events, set()
        self.hosts = 2
        self.on_book: Callable[[], None] | None = None

    def put_item(self, **request: Any) -> dict[str, Any]:
        written = Session.from_item(request["Item"])
        kind = "stop" if written.set_by in self.markers else "book"
        self.markers.add(written.set_by)
        if kind == "book" and self.on_book is not None:
            self.on_book()
        self.events.append(kind)
        return super().put_item(**request)


@dataclass
class Script:
    """What the fake measurement does: the cell it raises in, the cell the operator cancels in, a
    hook before each cell, and the bundle whose evidence each cell observes."""

    bundle: str = "pass"
    fail_at: int | None = None
    cancel_at: int | None = None
    before_cell: Callable[[int], None] | None = None


@dataclass
class Driver:
    """The fake services one invocation is connected to, and what each was asked for."""

    pool: Pool
    github: GitHub
    events: list[str]
    bundles: dict[str, Path]
    runs: Path
    script: Script = field(default_factory=Script)
    sent: list[int] = field(default_factory=list)  # each request sent to the gateway, by cell
    asked: list[str] = field(default_factory=list)  # what the runtimes were asked for
    scope_pool: str | None = None

    @contextlib.contextmanager
    def __call__(self, scope: controller.Scope) -> Generator[Runtime]:
        yield Runtime(self, replace(scope, pool=self.scope_pool or scope.pool))


class Runtime:
    def __init__(self, driver: Driver, scope: controller.Scope) -> None:
        self.driver, self.scope = driver, scope

    def github(self) -> publisher.GitHub:
        return publisher.GitHub(self.driver.github, self.scope.repository)

    def access(self) -> booking.Access:
        self.driver.asked.append("access")
        deployed = self.driver.pool
        pools = admin_pools.Pools(table=deployed, functions=deployed, scaler="scaler-under-test", caller=lambda: "user/driver")
        sessions = admin_session.Sessions(pools=pools, time_zone="UTC", session_max_hours=SESSION_HOURS, default_pool=None)
        return booking.Access(sessions, deployed.gateway_ready, deployed.releases, clock=deployed.clock, sleep=deployed.sleep)

    def release(self) -> str:
        return RELEASE

    def requested(self) -> Settings:
        return report.read_manifest(self.driver.bundles["pass"]).requested

    def source_commit(self) -> str:
        return "a" * 40

    def clock(self) -> float:
        return self.driver.pool.clock()

    def preflight(self, plan: Plan) -> Sequence[str]:
        self.driver.events.append("preflight")
        return []

    async def measure(
        self,
        plan: Plan,
        admitted: phases.Admitted,
        journal: Journal,
        cells: Sequence[int],
        *,
        cancel: asyncio.Event,
        deadline_s: float,
    ) -> phases.Ended:
        """Each cell as `phases` journals it: its observations, then the evidence a checkpoint
        keeps, then the checkpoint. A cell the bundle holds observes what it measured; another one
        is checkpointed with no observation. The booking's run clock starts at zero, and the pool's
        clock moves on by the time the batch took."""
        driver, script = self.driver, self.driver.script
        driver.asked.append("measure")
        assert admitted.outcome.ready
        evidence = report.load(driver.bundles[script.bundle])
        manifest = journal.directory / phases.MANIFEST
        if not manifest.exists():
            manifest.write_text(contracts.dumps(evidence.manifest) + "\n", encoding="utf-8")
        by_cell = {one.record.cell: one for one in evidence.attempts}
        template, origin, now_ms = evidence.attempts[0], float(driver.pool.now), 0
        try:
            for cell in (0, *cells):
                if script.before_cell is not None:
                    script.before_cell(cell)
                again = any(one.cell == cell and one.reusable for one in journal.attempts)
                attempt = journal.start_cell(cell, plan.workload_hash, SERVED, again=again).ordinal
                driver.sent.append(cell)
                driver.events.append("cell")
                measured = by_cell.get(cell)
                if measured is None:
                    slots = plan.cells[cell].identity.agent_slots
                    times = {"started_ms": now_ms + 1, "admissions_ended_ms": now_ms + 2, "ended_ms": now_ms + 3}
                    counts = {"conversations_planned": slots, "conversations_started": slots, "conversations_completed": slots}
                    measured = replace(template, checkpoint=replace(template.checkpoint, record=replace(template.record, cell=cell, **times, **counts)), requests=(), trials=(), samples=())
                observations = (*measured.requests, *measured.trials, *measured.samples)
                stopped = cell in (script.fail_at, script.cancel_at)
                for one in observations[:1] if stopped else observations:
                    journal.append(attempt, one)
                if cell == script.fail_at:
                    journal.interrupt(attempt)
                    raise RuntimeError("the trial worker could not be started")
                if cell == script.cancel_at:
                    assert cli.main(["cancel", journal.run_id, "--runs", str(driver.runs)]) == 0
                    await asyncio.wait_for(cancel.wait(), timeout=10)
                    journal.interrupt(attempt)
                    return phases.Ended.CANCELLED
                kept = measured.checkpoint
                controller.keep_evidence(
                    journal,
                    attempt,
                    tally=kept.tally,
                    hosts=kept.hosts,
                    requested_launch_fingerprint=kept.requested_launch_fingerprint,
                    origin_s=origin,
                )
                journal.checkpoint_cell(attempt, measured.record)
                now_ms = measured.record.ended_ms
                await asyncio.sleep(0)
            return phases.Ended.COMPLETED
        finally:
            driver.pool.sleep(now_ms // 1000 + 1)

    def write_bundle(self, plan: Plan, journal: Journal, directory: Path) -> bool:
        self.driver.events.append("report")
        return controller.write_bundle(journal, directory)


@pytest.fixture
def driver(pool: Deployed, bundles: dict[str, Path], tmp_path: Path) -> Driver:  # noqa: F811 - the booking tests' fixture
    events: list[str] = []
    return Driver(Pool(pool.table, events), GitHub(events), events, bundles, tmp_path / "runs")


def run(driver: Driver, *extra: str, minutes: int = WHOLE, issue: str = "new") -> int:
    window = [] if "--session" in extra else ["--for", f"{minutes}m"]
    argv = ["run", "--profile", "qualify", "--pool", POOL, "--students", "2", "--instances", "2", *window]
    argv += ["--issue", issue, "--repository", REPO, "--runs", str(driver.runs), *extra]
    return cli.main(argv, connect=driver)


def only_run(driver: Driver) -> str:
    (run_id,) = sorted(path.name for path in driver.runs.iterdir())
    return run_id


def state(driver: Driver, run_id: str) -> dict[str, Any]:
    return controller.saved_state(driver.runs, run_id)


def comments(driver: Driver) -> list[str]:
    return [one["body"] for one in driver.github.comments]


def booking_writes(driver: Driver) -> list[Session]:
    return driver.pool.puts


def shown_status(driver: Driver, run_id: str) -> str:
    out = io.StringIO()
    assert controller.status(driver.runs, run_id, out=out) == 0
    return out.getvalue()


def held_by_operator(driver: Driver) -> Session:
    """A session the operator booked by hand, written past the driver."""
    now = driver.pool.now
    session = rules.started(
        None,
        pool=POOL,
        instances=2,
        starts_at=now,
        ends_at=now + (SESSION_HOURS - 1) * 3600,
        set_by=OPERATOR,
        now=now,
        max_instances=2,
        session_max_hours=SESSION_HOURS,
    )
    driver.pool.table.put_item(Item=session.to_item())
    return session


def failed_trial_cell(bundle: Path) -> int:
    """The first cell of a bundle whose observations hold a failed trial: a measured failure."""
    return min(
        one.record.cell for one in report.load(bundle).attempts if any(t.outcome is TrialOutcome.FAILED for t in one.trials)
    )


# --- The whole workflow, in order ---


def test_a_run_goes_from_preflight_through_its_issue_booking_cells_cleanup_and_report_to_its_comment(
    driver: Driver, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def at_booking() -> None:
        seen["issue_at_booking"] = state(driver, only_run(driver))["issue_number"]

    def at_comment() -> None:
        seen["report_at_comment"] = (driver.runs / only_run(driver) / "bundle" / report.SUMMARY).is_file()

    driver.pool.on_book, driver.github.on_comment = at_booking, at_comment

    assert run(driver) == controller.Exit.PASS

    cells = len(PLAN.cells)
    assert driver.events == ["preflight", "issue", "book", *["cell"] * cells, "stop", "report", "comment"]
    run_id = only_run(driver)
    saved = state(driver, run_id)
    # The issue was confirmed and journaled before the booking was written.
    assert seen == {"issue_at_booking": saved["issue_number"], "report_at_comment": True}
    # The exact booking: what the booking's journal saved is what the table stored, and the stop is its own.
    booked, stop = booking_writes(driver)
    capacity_journal = booking.Journal.load(driver.runs / run_id / "booking-0.json")
    assert capacity_journal.booking == booked and capacity_journal.marker == booked.set_by
    assert stop in capacity_journal.stops and capacity_journal.cleanup is booking.Cleanup.RELEASED
    assert stored(driver.pool) == stop and stop.set_by == booked.set_by
    # Every planned cell measured once, and checkpointed; the report holds what was checkpointed.
    assert driver.sent == list(range(cells))
    assert all(attempt["status"] == "complete" for attempt in saved["attempts"])
    assert len(report.load(driver.runs / run_id / "bundle").attempts) == cells
    # One terminal comment, with the run's marker and its cleanup.
    (body,) = comments(driver)
    assert f"<!-- evaluate/{run_id}/" in body and "`released`" in body and f"Completed cells: {cells}/{cells}" in body
    shown = capsys.readouterr().out
    url = controller.issue_url(REPO, saved["issue_number"])
    for line in (
        f"issue: {url}",
        "capacity: pass",
        "quality: pass",
        "cleanup: released",
        "publication: delivered",
        "exit: 0",
    ):
        assert any(one.startswith(line) for one in shown.splitlines()), line
    assert "limiting hands_on:" in shown and "left" in shown and f"{cells}/{cells} cells measured" in shown
    assert str(driver.runs / run_id / "bundle" / report.REPORT) in shown


def test_in_a_session_the_operator_booked_nothing_is_written_to_the_pool(driver: Driver) -> None:
    operator = held_by_operator(driver)

    assert run(driver, "--session", "existing") == controller.Exit.PASS

    assert driver.events[:2] == ["preflight", "issue"] and "book" not in driver.events and "stop" not in driver.events
    assert booking_writes(driver) == [] and stored(driver.pool) == operator
    assert "`not-owned`" in comments(driver)[0]


def test_capacity_too_late_for_the_authorized_window_is_refused_in_an_operator_session(driver: Driver) -> None:
    operator = held_by_operator(driver)
    start = driver.pool.now
    driver.pool.hosts = 1
    driver.pool.hosts_at(start + 3660, 2)  # past the hour the invocation is authorized for

    assert run(driver, "--session", "existing", "--for", "60m") == controller.Exit.INCOMPLETE

    assert driver.pool.now <= start + 3600 and driver.sent == [] and stored(driver.pool) == operator


def test_hosts_that_come_within_the_planned_wait_run_the_batch_the_planner_printed(driver: Driver) -> None:
    window = 790
    first = planning.schedule(PLAN, window * 60_000).batches[0]
    driver.pool.hosts = 0
    driver.pool.hosts_at(driver.pool.now + ESTIMATES.capacity_wait_ms // 1000, 2)

    assert run(driver, minutes=window) == controller.Exit.INCOMPLETE  # one batch of several

    assert driver.sent == [0, *first.cells]


def test_a_runtime_bound_to_another_pool_is_refused_before_anything_is_created(driver: Driver) -> None:
    driver.scope_pool = "elsewhere"

    assert run(driver) == controller.Exit.INCOMPLETE

    assert driver.github.writes() == [] and booking_writes(driver) == [] and driver.sent == []
    assert not driver.runs.exists() or not any(driver.runs.iterdir())


# --- The experiment's issue ---


def test_a_selected_issue_is_read_from_github_and_must_record_the_requested_workload(driver: Driver) -> None:
    assert run(driver) == controller.Exit.PASS
    issue = str(state(driver, only_run(driver))["issue_number"])
    writes = list(booking_writes(driver))

    # Another workload on the same issue: refused before anything is booked.
    assert run(driver, "--input-seed", "41", issue=issue) == controller.Exit.INCOMPLETE
    assert booking_writes(driver) == writes

    # The same workload, from a driver with no local record of the issue: attached to it.
    driver.runs = driver.runs.parent / "second-driver"
    assert run(driver, issue=issue) == controller.Exit.PASS
    assert len(booking_writes(driver)) > len(writes) and len(driver.github.issues) == 1
    assert state(driver, only_run(driver))["issue_number"] == int(issue)


# --- Faults ---


def test_an_issue_github_does_not_confirm_stops_the_run_before_it_books_and_a_restart_creates_it_once(
    driver: Driver,
) -> None:
    driver.github.faults.append(("POST", "unsent"))

    assert run(driver) == controller.Exit.INCOMPLETE

    run_id = only_run(driver)
    assert driver.github.issues == {} and booking_writes(driver) == [] and driver.sent == []
    assert "publication: pending" in shown_status(driver, run_id)

    assert run(driver, "--resume", run_id) == controller.Exit.PASS

    assert len(driver.github.issues) == 1 and len(comments(driver)) == 1
    assert state(driver, run_id)["issue_number"] == next(iter(driver.github.issues))
    assert driver.sent == list(range(len(PLAN.cells)))


@pytest.mark.parametrize("refusal", ["pool-already-booked", "capacity-unavailable"])
def test_a_capacity_refusal_measures_nothing_releases_what_it_booked_and_says_why(driver: Driver, refusal: str) -> None:
    operator = held_by_operator(driver) if refusal == "pool-already-booked" else None
    if operator is None:
        driver.pool.hosts = 1  # one of the two hosts never comes

    assert run(driver) == controller.Exit.INCOMPLETE

    assert driver.sent == [] and "measure" not in driver.asked
    (body,) = comments(driver)
    assert f"Reason: `{refusal}`" in body
    if operator is not None:
        # The race was lost: nothing of the operator's was touched, and nothing was ours to stop.
        assert stored(driver.pool) == operator and "`not-owned`" in body and booking_writes(driver) == []
        assert driver.events == ["preflight", "issue", "report", "comment"]
    else:
        booked, stop = booking_writes(driver)
        assert stop.set_by == booked.set_by and stored(driver.pool) == stop and "`released`" in body
        assert driver.events == ["preflight", "issue", "book", "stop", "report", "comment"]


def test_a_worker_error_releases_the_booking_and_a_resume_keeps_every_measured_cell_and_failure(driver: Driver) -> None:
    failed = failed_trial_cell(driver.bundles["miss"])
    stop_at = failed + 1
    driver.script = Script(bundle="miss", fail_at=stop_at)

    assert run(driver) == controller.Exit.INCOMPLETE

    run_id = only_run(driver)
    assert driver.sent == list(range(stop_at + 1))
    assert driver.events[-3:] == ["stop", "report", "comment"]
    (first,) = comments(driver)
    assert "Reason: `driver-error`" in first and "`released`" in first and f"Completed cells: {stop_at}/" in first
    # The report is of the cells checkpointed, and of no other.
    with Journal.open(driver.runs, run_id) as journal:
        completed = {one.cell for one in journal.attempts if one.reusable}
    assert {one.record.cell for one in report.load(driver.runs / run_id / "bundle").attempts} == completed
    before = {attempt["cell"]: attempt for attempt in state(driver, run_id)["attempts"]}
    issue = state(driver, run_id)["issue_number"]

    # The same request again, on the same issue.
    driver.script = Script(bundle="miss")
    assert run(driver, "--resume", issue=str(issue)) == controller.Exit.MEASURED_MISS

    assert len(driver.github.issues) == 1  # no second issue
    # Only the warm-up, which every booking runs, the interrupted cell and those never started.
    assert driver.sent[stop_at + 1 :] == [0, *range(stop_at, len(PLAN.cells))]
    after = state(driver, run_id)["attempts"]
    # The measured cells, the measured failure among them, are the attempts the first booking made.
    for cell in range(1, stop_at):
        assert after[before[cell]["ordinal"]] == before[cell]
    interrupted = after[before[stop_at]["ordinal"]]
    with Journal.open(driver.runs, run_id) as journal:
        trials = journal.observations(before[failed]["ordinal"])
        assert any(isinstance(one, TrialRecord) and one.outcome is TrialOutcome.FAILED for one in trials)
        # The interrupted attempt stays, with the observation it made, superseded by the fresh one.
        assert len(journal.observations(interrupted["ordinal"])) == 1
    assert interrupted["status"] == "interrupted" and interrupted["replaced_by"] is not None
    assert after[interrupted["replaced_by"]]["status"] == "complete"
    # Both bookings' cells in one report, on one timeline, each cell read once.
    cells = [one.record.cell for one in report.load(driver.runs / run_id / "bundle").attempts]
    assert sorted(cells) == list(range(len(PLAN.cells)))
    # One comment for each booking's invocation, and each booking stopped.
    assert len(comments(driver)) == 2 and "Completed cells: 58/58" in comments(driver)[1]
    assert driver.events.count("stop") == 2


def test_a_checkpointed_cell_whose_evidence_cannot_be_read_is_no_completed_result(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = Path.read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.parent.name == controller.EVIDENCE and path.name == "attempt-1.json":
            raise OSError
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    assert run(driver) == controller.Exit.INCOMPLETE

    (body,) = comments(driver)
    assert ": failed" in body and "Reason: `invalid-evidence`" in body and "`released`" in body


def test_a_report_that_cannot_be_written_ends_the_run_failed_with_its_comment(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object) -> bool:
        raise OSError

    monkeypatch.setattr(Runtime, "write_bundle", unavailable)

    assert run(driver) == controller.Exit.INCOMPLETE

    (body,) = comments(driver)
    assert ": failed" in body and "Reason: `driver-error`" in body and "`released`" in body


def test_a_driver_stopped_between_the_booking_write_and_its_confirmation_resumes_on_that_booking(
    driver: Driver, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def died() -> None:
        raise Died

    def no_cleanup(_self: booking.Capacity) -> booking.Cleanup:
        raise Died

    driver.pool.after_put = died
    monkeypatch.setattr(booking.Capacity, "release", no_cleanup)
    with pytest.raises(Died):
        run(driver)
    monkeypatch.undo()
    run_id = only_run(driver)
    (booked,) = booking_writes(driver)
    assert booking.Journal.load(driver.runs / run_id / "booking-0.json").booking is None  # never confirmed

    assert run(driver, "--resume", run_id) == controller.Exit.PASS

    # The saved intent was found in the pool's record, waited on and used, then stopped: no second booking.
    assert "reconciled: booking booking-0.json held" in capsys.readouterr().out
    assert booking_writes(driver)[0] == booked and len(booking_writes(driver)) == 2
    last = stored(driver.pool)
    assert last is not None and last.set_by == booked.set_by and last != booked
    assert driver.sent == list(range(len(PLAN.cells)))


def test_a_restart_that_cannot_reach_github_still_releases_the_booking_the_run_holds(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    def died(*_args: object) -> None:
        raise Died

    driver.pool.after_put = died
    with monkeypatch.context() as patched:
        patched.setattr(booking.Capacity, "release", died)
        with pytest.raises(Died):
            run(driver)
    (booked,) = booking_writes(driver)
    driver.github.down = True

    assert run(driver, "--resume", only_run(driver)) == controller.Exit.INCOMPLETE

    last = stored(driver.pool)
    assert last is not None and last != booked and last.set_by == booked.set_by and last.ends_at <= driver.pool.now
    assert driver.sent == [] and driver.github.writes() == [("POST", f"/repos/{REPO}/issues")]


def test_a_restart_whose_measurement_cannot_be_prepared_still_stops_the_booking_it_holds(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver.pool.now = int(time.time())  # the live runtime reads the pool on the real clock

    def died(*_args: object) -> None:
        raise Died

    driver.pool.after_put = died
    with monkeypatch.context() as patched:
        patched.setattr(booking.Capacity, "release", died)
        with pytest.raises(Died):
            run(driver)
    (booked,) = booking_writes(driver)
    sessions = Runtime(driver, controller.Scope(POOL, REPO)).access().sessions
    monkeypatch.setattr(lookups, "pool_sessions", lambda *_args: sessions)
    monkeypatch.setattr(publisher, "HttpTransport", lambda _token: driver.github)

    def no_trial_image(*_args: object) -> Any:
        raise lookups.Unresolved(controller.Check.HARNESS_VERSIONS)

    monkeypatch.setattr(lookups, "resolve", no_trial_image)

    @contextlib.contextmanager
    def connect(scope: controller.Scope) -> Generator[controller.LiveRuntime]:
        yield controller.LiveRuntime(scope, instruments=None, session=object, token="fixture")

    argv = ["run", "--resume", only_run(driver), "--profile", "qualify", "--pool", POOL, "--students", "2"]
    argv += ["--instances", "2", "--for", f"{WHOLE}m", "--issue", "new", "--repository", REPO, "--runs", str(driver.runs)]
    assert cli.main(argv, connect=connect) == controller.Exit.INCOMPLETE

    last = stored(driver.pool)
    assert last is not None and last != booked and last.set_by == booked.set_by and driver.sent == []


def timed_out(*_args: object) -> Any:
    raise subprocess.TimeoutExpired("docker", 120)


@pytest.mark.parametrize("check", ["live harness check", "runtime's own check"])
def test_a_restart_whose_preflight_check_raises_still_stops_the_booking_it_holds(
    driver: Driver, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    def died(*_args: object) -> None:
        raise Died

    driver.pool.after_put = died
    with monkeypatch.context() as patched:
        patched.setattr(booking.Capacity, "release", died)
        with pytest.raises(Died):
            run(driver)
    (booked,) = booking_writes(driver)
    if check == "live harness check":
        instruments = held(artifacts=SimpleNamespace(trial_image=timed_out))
        monkeypatch.setattr(Runtime, "preflight", lambda _self, plan: live(instruments).preflight(plan))
    else:
        monkeypatch.setattr(Runtime, "preflight", timed_out)

    assert run(driver, "--resume", only_run(driver)) == controller.Exit.INCOMPLETE

    stop = stored(driver.pool)
    assert stop is not None and stop.set_by == booked.set_by and stop.ends_at <= driver.pool.now
    assert driver.sent == []


def test_a_restart_after_the_last_checkpoint_releases_and_comments_without_sending_again(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    def died(*_args: object) -> None:
        raise Died

    with monkeypatch.context() as patched:
        patched.setattr(booking.Capacity, "release", died)
        with pytest.raises(Died):
            run(driver)
    sent = list(driver.sent)
    assert sent == list(range(len(PLAN.cells)))

    assert run(driver, "--resume", only_run(driver)) == controller.Exit.PASS

    assert driver.sent == sent  # no inference again, the warm-up included
    stop = stored(driver.pool)
    assert stop is not None and stop.ends_at <= driver.pool.now
    (body,) = comments(driver)
    assert ": completed" in body and "`released`" in body


def died_before_cleanup(driver: Driver, monkeypatch: pytest.MonkeyPatch) -> str:
    """A whole run whose driver dies as it stops the booking: every cell checkpointed, the booking held."""

    def died(*_args: object) -> None:
        raise Died

    with monkeypatch.context() as patched:
        patched.setattr(booking.Capacity, "release", died)
        with pytest.raises(Died):
            run(driver)
    return only_run(driver)


def test_checkpointed_cells_without_their_manifest_are_no_completed_result(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = died_before_cleanup(driver, monkeypatch)
    (driver.runs / run_id / phases.MANIFEST).unlink()

    assert run(driver, "--resume", run_id) == controller.Exit.INCOMPLETE

    (body,) = comments(driver)
    assert ": failed" in body and "Reason: `invalid-evidence`" in body and "`released`" in body
    assert not (driver.runs / run_id / controller.BUNDLE).exists()


def test_a_restart_whose_checkpoint_is_damaged_still_stops_the_booking_it_holds(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = died_before_cleanup(driver, monkeypatch)
    (booked,) = booking_writes(driver)
    sent = list(driver.sent)
    files = (driver.runs / run_id).glob("observations/cell-*.jsonl")
    (observed, *_) = sorted(one for one in files if not one.name.startswith("cell-0-"))  # a measured cell's
    observed.write_text(observed.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    assert run(driver, "--resume", run_id) == controller.Exit.INCOMPLETE

    stop = stored(driver.pool)
    assert stop is not None and stop.set_by == booked.set_by and stop.ends_at <= driver.pool.now
    assert driver.sent == sent
    (body,) = comments(driver)
    assert ": failed" in body and "`released`" in body


def test_a_booking_written_late_in_the_window_still_ends_by_it(driver: Driver, monkeypatch: pytest.MonkeyPatch) -> None:
    start, begun = driver.pool.clock(), booking.Capacity._begun

    def slow(self: booking.Capacity, minutes: int | None) -> booking.Reason | None:
        driver.pool.sleep(35)  # the pool's limits read slowly, after the minutes were counted
        return begun(self, minutes)

    monkeypatch.setattr(booking.Capacity, "_begun", slow)
    run(driver, minutes=60)

    (booked,) = booking_writes(driver)[:1]
    assert booked.ends_at <= start + 3600


def test_a_session_the_driver_cannot_make_is_a_preflight_refusal_not_a_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import boto3
    from botocore.exceptions import NoRegionError

    class NoRegion:
        def client(self, _service: str) -> Any:
            raise NoRegionError

    monkeypatch.setattr(boto3.session, "Session", NoRegion)
    monkeypatch.setattr(publisher, "HttpTransport", lambda *_args: FakeGitHub())
    monkeypatch.setenv(controller.GITHUB_TOKEN_VARIABLE, "fixture")
    argv = ["run", "--profile", "screen", "--pool", POOL, "--students", "8", "--instances", "1", "--for", "3h"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main([*argv, "--issue", "new", "--repository", REPO, "--runs", str(tmp_path)])
    assert code == controller.Exit.INCOMPLETE and controller.Check.DEPLOYMENT.value in out.getvalue()


def test_time_spent_before_the_booking_counts_against_the_window_and_the_booking_ends_by_it(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, post = driver.pool.clock(), driver.github.post_issue

    def slow(login: str, body: str) -> int:
        driver.pool.sleep(35)
        return post(login, body)

    deadlines: list[float] = []
    measure = Runtime.measure

    async def recorded(self: Runtime, *args: Any, deadline_s: float, **kwargs: Any) -> phases.Ended:
        deadlines.append(deadline_s)
        return await measure(self, *args, deadline_s=deadline_s, **kwargs)

    monkeypatch.setattr(driver.github, "post_issue", slow)
    monkeypatch.setattr(Runtime, "measure", recorded)
    run(driver, minutes=60)

    (minutes,) = [one["payload"]["minutes"] for one in state(driver, only_run(driver))["intents"] if one["action"] == "book"]
    assert deadlines and max(deadlines) <= start + 3600 and minutes == 59  # 59m25s left, booked whole minutes
    # The record the pool keeps, which ends the booking if the driver dies, expires by the deadline.
    booked = booking_writes(driver)[0]
    assert booked.ends_at <= start + 3600


def test_capacity_is_read_from_the_selected_pool_alone(driver: Driver, monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    settings, read = driver.pool.get_function_configuration, driver.pool.get_item
    others: list[str] = []

    def with_another_pool(**request: Any) -> Any:
        found = settings(**request)
        variables = found["Environment"]["Variables"]
        document = json.loads(variables["RHI_SCALER_SETTINGS"])
        document["pools"]["other"] = {"entry": ENTRY}
        variables["RHI_SCALER_SETTINGS"] = json.dumps(document)
        return found

    def denied_elsewhere(**request: Any) -> Any:
        if request["Key"]["pool"] != POOL:
            others.append(request["Key"]["pool"])
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetItem")
        return read(**request)

    monkeypatch.setattr(driver.pool, "get_function_configuration", with_another_pool)
    monkeypatch.setattr(driver.pool, "get_item", denied_elsewhere)

    assert run(driver) == controller.Exit.PASS
    assert others == [] and driver.sent == list(range(len(PLAN.cells)))


def test_bookings_a_week_apart_make_one_report_of_every_cell_in_order(driver: Driver) -> None:
    driver.script = Script(fail_at=3)
    assert run(driver) == controller.Exit.INCOMPLETE
    run_id = only_run(driver)
    driver.pool.sleep(8 * 24 * 3600)
    driver.script = Script()

    assert run(driver, "--resume", run_id) == controller.Exit.PASS

    kept = report.load(driver.runs / run_id / "bundle").attempts
    assert sorted(one.record.cell for one in kept) == list(range(len(PLAN.cells)))
    records = [one.record for one in kept]
    assert all(later.started_ms >= earlier.ended_ms for earlier, later in itertools.pairwise(records))
    # Each cell keeps the duration it measured.
    with Journal.open(driver.runs, run_id) as journal:
        measured = {one.cell: one.record for one in journal.attempts if one.reusable and one.record is not None}
    assert all(one.ended_ms - one.started_ms == measured[one.cell].ended_ms - measured[one.cell].started_ms for one in records)


def test_a_result_the_driver_stopped_before_commenting_is_commented_once_on_resume(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver.script = Script(fail_at=3)
    assert run(driver) == controller.Exit.INCOMPLETE
    run_id = only_run(driver)
    driver.script = Script()

    def died(*_args: object, **_kwargs: object) -> None:
        raise Died

    with monkeypatch.context() as patched:
        patched.setattr(publisher.Publisher, "publish_run", died)
        with pytest.raises(Died):
            run(driver, "--resume", run_id)
    sent = list(driver.sent)

    assert run(driver, "--resume", run_id) == controller.Exit.PASS
    assert run(driver, "--resume", run_id) == controller.Exit.PASS

    assert driver.sent == sent  # nothing measured again
    first, last = comments(driver)
    assert ": failed" in first and ": completed" in last and "`released`" in last


def test_evidence_the_report_rejects_is_commented_as_invalid(driver: Driver, monkeypatch: pytest.MonkeyPatch) -> None:
    def rejected(*_args: object, **_kwargs: object) -> Any:
        raise report.ReportError(report.Refusal.UNREADABLE_BUNDLE)

    monkeypatch.setattr(report, "regenerate", rejected)

    assert run(driver) == controller.Exit.INCOMPLETE

    (body,) = comments(driver)
    assert ": failed" in body and "Reason: `invalid-evidence`" in body and "`released`" in body


def test_a_comment_whose_answer_is_lost_is_found_by_its_marker_and_not_posted_again(driver: Driver) -> None:
    last = len(PLAN.cells) - 1

    def lose_the_next_post(cell: int) -> None:
        if cell == last:
            driver.github.faults.append(("POST", "lost"))

    driver.script = Script(bundle="miss", before_cell=lose_the_next_post)

    assert run(driver) == controller.Exit.MEASURED_MISS

    (body,) = comments(driver)
    assert driver.github.writes().count(("POST", f"/repos/{REPO}/issues/1/comments")) == 1
    assert "publication: delivered" in shown_status(driver, only_run(driver))
    assert state(driver, only_run(driver))["intents"][-1]["status"] == "confirmed"
    assert "Capacity by shape: hands_on pass, several_agents measured-miss" in body


def test_a_finished_run_resumed_again_does_no_work_and_posts_nothing(driver: Driver) -> None:
    assert run(driver) == controller.Exit.PASS
    before = (list(comments(driver)), list(booking_writes(driver)), list(driver.sent))

    assert run(driver, "--resume", only_run(driver)) == controller.Exit.PASS

    assert (comments(driver), booking_writes(driver), driver.sent) == before


def test_a_publication_failure_leaves_the_verdict_and_publish_resume_delivers_once_without_the_pool(
    driver: Driver, capsys: pytest.CaptureFixture[str]
) -> None:
    last = len(PLAN.cells) - 1

    def github_goes_down(cell: int) -> None:
        if cell == last:
            driver.github.down = True

    driver.script = Script(bundle="miss", before_cell=github_goes_down)

    # The verdict's exit code, whether or not the comment was delivered.
    assert run(driver) == controller.Exit.MEASURED_MISS

    run_id = only_run(driver)
    assert comments(driver) == [] and driver.events[-2:] == ["stop", "report"]  # capacity released first
    assert "publication: pending (1)" in capsys.readouterr().out
    shown = shown_status(driver, run_id)
    assert '"measured-miss"' in shown and "waiting: 1" in shown and "publication: pending" in shown
    sent, writes, asked = list(driver.sent), list(booking_writes(driver)), len(driver.asked)

    driver.github.down = False
    assert cli.main(["publish", "--resume", run_id, "--runs", str(driver.runs)], connect=driver) == 0
    assert cli.main(["publish", "--resume", run_id, "--runs", str(driver.runs)], connect=driver) == 0

    assert len(comments(driver)) == 1 and "measured-miss" in comments(driver)[0]
    # Delivery alone: no booking, no inference, the pool not even read.
    assert driver.sent == sent and booking_writes(driver) == writes and len(driver.asked) == asked
    shown = shown_status(driver, run_id)
    assert "waiting: 0" in shown and '"measured-miss"' in shown and "publication: delivered" in shown


def test_a_comment_the_hygiene_scan_refuses_is_shown_refused_not_delivered(
    driver: Driver, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = publisher.hygiene_scan()

    def refusing() -> publisher.Scan:
        return lambda text, path: ["finding"] if text.startswith("### Run") else scan(text, path)

    monkeypatch.setattr(publisher, "hygiene_scan", refusing)

    assert run(driver) == controller.Exit.PASS  # the verdict, whatever was published

    assert comments(driver) == [] and "publication: refused" in capsys.readouterr().out
    assert "publication: refused" in shown_status(driver, only_run(driver))


# --- Cancellation and the exit codes ---


def test_a_cancel_request_stops_the_batch_and_releases_the_booking_even_through_a_failed_stop(driver: Driver) -> None:
    def fail_the_first_stop(cell: int) -> None:
        if cell == 3:
            driver.pool.faults.append("failed")

    driver.script = Script(cancel_at=3, before_cell=fail_the_first_stop)

    assert run(driver) == controller.Exit.INCOMPLETE

    run_id = only_run(driver)
    assert driver.sent == [0, 1, 2, 3]
    assert [one["status"] for one in state(driver, run_id)["attempts"]][-1] == "interrupted"
    booked, failed, stop = booking_writes(driver)
    assert failed.set_by == stop.set_by == booked.set_by and stored(driver.pool) == stop
    assert stop.ends_at <= driver.pool.now
    (body,) = comments(driver)
    assert ": interrupted" in body and "Reason: `operator-cancelled`" in body and "`released`" in body

    # A resume is a new invocation: the earlier request does not cancel it.
    driver.script = Script()
    assert run(driver, "--resume", run_id) == controller.Exit.PASS
    assert driver.sent[4:] == [0, *range(3, len(PLAN.cells))]


@pytest.mark.parametrize(
    ("bundle", "code"),
    [("pass", controller.Exit.PASS), ("miss", controller.Exit.MEASURED_MISS), ("few", controller.Exit.INCOMPLETE)],
    ids=["meets the floors", "measured miss", "inconclusive evidence"],
)
def test_the_exit_code_is_the_verdict(driver: Driver, bundle: str, code: controller.Exit) -> None:
    driver.script = Script(bundle=bundle)

    assert run(driver) == code

    assert len(comments(driver)) == 1
    shown = shown_status(driver, only_run(driver))
    assert f"exit: {int(code)}" in shown and "publication: delivered" in shown


def test_an_invocation_that_failed_or_was_cut_short_exits_2_whatever_it_measured() -> None:
    for ending in (controller.Ending.FAILED, controller.Ending.INTERRUPTED):
        assert controller.exit_code(ending, controller.Verdict.MEASURED_MISS, controller.Verdict.PASS) == 2
    assert controller.exit_code(controller.Ending.COMPLETED, controller.Verdict.MEASURED_MISS, controller.Verdict.PASS) == 1


def test_an_incomplete_run_exits_2_and_a_window_too_short_books_nothing(driver: Driver) -> None:
    # The window holds the first cells only: the rest wait for another booking.
    assert run(driver, minutes=60) == controller.Exit.INCOMPLETE

    assert 0 < len(driver.sent) < len(PLAN.cells)
    (body,) = comments(driver)
    assert "Reason: `window-exhausted`" in body and ": interrupted" in body


def test_the_report_prices_the_deployed_pool_unless_the_operator_gives_a_rate(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(ENTRY, "instance_types", ["g7e.2xlarge"])
    monkeypatch.setitem(ENTRY, "prices", {"g7e.2xlarge": 1.25})

    assert run(driver) == controller.Exit.PASS
    summary = json.loads((driver.runs / only_run(driver) / "bundle" / report.SUMMARY).read_text())
    assert summary["host_price"] == {"usd_per_host_hour": 1.25, "source": "deployed", "estimate": True}

    driver.runs = driver.runs.parent / "supplied"
    assert run(driver, "--rate", "2.5") == controller.Exit.PASS
    summary = json.loads((driver.runs / only_run(driver) / "bundle" / report.SUMMARY).read_text())
    assert summary["host_price"]["usd_per_host_hour"] == 2.5 and summary["host_price"]["source"] == "supplied"


# --- Comparing on the issue ---


def test_an_issue_backed_comparison_is_posted_once(driver: Driver) -> None:
    assert run(driver) == controller.Exit.PASS
    reference = only_run(driver)
    issue = str(state(driver, reference)["issue_number"])
    driver.script = Script(bundle="miss")
    assert run(driver, issue=issue) == controller.Exit.MEASURED_MISS
    (candidate,) = {path.name for path in driver.runs.iterdir()} - {reference}
    compare = ["compare", "--issue", issue, reference, candidate, "--runs", str(driver.runs)]

    assert cli.main(compare, connect=driver) == 0
    assert cli.main(compare, connect=driver) == 0

    assert len(driver.github.issues) == 1
    found = [one for one in comments(driver) if one.startswith("### Comparison")]
    assert len(found) == 1 and f"candidate `{candidate}` against reference `{reference}`" in found[0]
    assert len(comments(driver)) == 3
    # The comment names the measurement behind each capacity verdict: the candidate's missed floor.
    graded = json.loads((driver.runs / candidate / "bundle" / report.SUMMARY).read_text())["capacity"]
    missed = [row for one in graded.values() if (row := one["limiting_row"]) and row["missed"]]
    assert missed
    assert all(f"`{row['floor']}` missed (headroom {row['headroom']}" in found[0] for row in missed)


def test_runs_of_another_repository_are_not_compared(driver: Driver, monkeypatch: pytest.MonkeyPatch) -> None:
    other = "example/second"
    with monkeypatch.context() as patched:
        patched.setattr("evaluate.tests.test_publisher.REPO", other)
        assert run(replace(driver, github=GitHub([])), "--repository", other) == controller.Exit.PASS
    reference = only_run(driver)
    issue = state(driver, reference)["issue_number"]
    assert run(driver) == controller.Exit.PASS
    (candidate,) = {path.name for path in driver.runs.iterdir()} - {reference}
    assert state(driver, candidate)["issue_number"] == issue  # the same number, in another repository

    compare = ["compare", "--issue", str(issue), reference, candidate, "--runs", str(driver.runs)]
    assert cli.main(compare, connect=driver) == controller.Exit.INCOMPLETE
    assert len(driver.github.comments) == 1  # the candidate's run comment, and no comparison


def test_a_malformed_request_is_refused_with_2_before_anything_is_connected() -> None:
    def never(_scope: controller.Scope) -> Any:
        raise AssertionError("connected")

    request = ["run", "--profile", "screen", "--pool", POOL, "--students", "8", "--instances", "1", "--for", "3h"]
    assert cli.main([*request, "--issue", "new", "--repository", "malformed"], connect=never) == 2
    with pytest.raises(SystemExit) as exited:
        cli.main(["compare", "--issue", "0", "reference", "candidate"], connect=never)
    assert exited.value.code == 2


# --- The live runtime ---


def held(**changes: Any) -> SimpleNamespace:
    """The instruments a preflight reads, for the phase tests' plan and pool."""
    aliases = tuple(f"canary-{at}" for at in range(lab.PLAN.canary_identities))
    found = {
        "gateway": SimpleNamespace(model=lab.POOL),
        "telemetry": SimpleNamespace(pool=lab.POOL, reader=lab_telemetry.clean(), engine=lab_telemetry.VLLM),
        "canaries": phases.Canaries(tuple(generate_key() for _ in aliases), aliases),
        "artifacts": SimpleNamespace(trial_image=lab.trial_image(lab.VERSIONS)),
    }
    return SimpleNamespace(**(found | changes))


def live(instruments: Any = None) -> controller.LiveRuntime:
    return controller.LiveRuntime(
        controller.Scope(lab.POOL, "example/repo"), instruments=instruments, token="fixture", session=lambda: None
    )


def test_the_live_preflight_reads_the_pool_s_telemetry_as_a_reading_does_and_the_cohort_the_plan_runs_as() -> None:
    class Scoped:
        def read_samples(self, query: str, *, at: float) -> list[Any]:
            if "[" not in query:  # an instant vector, which the workspace's reader refuses
                raise ValueError
            assert f'pool="{lab.POOL}"' in query
            return lab_telemetry.clean().read_samples(query, at=lab_telemetry.END)

    reader = SimpleNamespace(pool=lab.POOL, reader=Scoped(), engine=lab_telemetry.VLLM)
    assert live(held(telemetry=reader)).preflight(lab.PLAN) == []

    count = lab.PLAN.canary_identities
    one_alias = phases.Canaries(tuple(generate_key() for _ in range(count)), ("same",) * count)
    assert live(held(canaries=one_alias)).preflight(lab.PLAN) == [controller.Check.CANARIES]


def test_a_terminal_run_resolves_the_deployment_from_the_driver_s_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import boto3

    class Resolved(BaseException):
        pass

    def session(*_args: object, **_kwargs: object) -> Any:
        raise Resolved

    monkeypatch.setattr(boto3.session, "Session", session)
    monkeypatch.setattr(publisher, "HttpTransport", lambda *_args, **_kwargs: FakeGitHub())
    monkeypatch.setenv(controller.GITHUB_TOKEN_VARIABLE, "fixture")
    request = ["run", "--profile", "screen", "--pool", POOL, "--students", "8", "--instances", "1", "--for", "3h"]
    with pytest.raises(Resolved):
        cli.main([*request, "--issue", "new", "--repository", REPO, "--runs", str(tmp_path)])


def test_the_live_driver_reaches_the_pools_without_the_students_tables_or_the_whole_configuration() -> None:
    from botocore.exceptions import ClientError

    from rhi_admin import main as admin

    session = MagicMock()
    client = session.client.return_value
    looked_up: list[str] = []

    def resource(*, StackName: str, LogicalResourceId: str) -> Any:
        looked_up.append(StackName)
        return {"StackResourceDetail": {"PhysicalResourceId": "fixture"}}

    def parameter(*, Name: str) -> Any:
        if Name == "/rhi/config/session_max_hours":
            return {"Parameter": {"Value": "48"}}
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetParameter")

    denied = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Denied")
    client.describe_stack_resource.side_effect = resource
    client.get_parameter.side_effect = parameter
    client.get_parameters_by_path.side_effect = denied
    client.get_parameters.side_effect = denied
    client.describe_stacks.side_effect = denied
    client.get_function_configuration.return_value = {
        "Environment": {"Variables": {"RHI_SCALER_SETTINGS": json.dumps({"pools": {}})}}
    }
    runtime = controller.LiveRuntime(controller.Scope(POOL, REPO, 1), instruments=None, token="fixture", session=lambda: session)

    assert runtime.access().sessions.session_max_hours == 48
    assert runtime.preflight(PLAN) == [controller.Check.BINDING]  # the pool is not deployed

    assert looked_up and set(looked_up) == {admin.POOLS_STACK_NAME}
    assert not client.get_parameters_by_path.called


def checkpoints(bundle: Path) -> list[contracts.Checkpoint]:
    """The checkpoints a bundle holds, in order: the phase tests' plan is no suite's, which the
    report would read them against."""
    lines = (bundle / report.CHECKPOINTS).read_text(encoding="utf-8").splitlines()
    return [contracts.loads(line, contracts.Checkpoint) for line in lines]


def measured_live(bench: lab.Bench) -> Any:
    """The phase tests' bench, measured through `LiveRuntime.measure` on the bench's clock."""
    original = phases.run

    async def measure(plan: Plan, admitted: Any, gateway: Any, canaries: Any, source: Any, artifacts: Any, harnesses: Any, graders: Any, journal: Journal, *, origin_s: float, cancel: asyncio.Event, deadline_ms: int) -> Any:
        instruments = SimpleNamespace(gateway=gateway, canaries=canaries, telemetry=source, artifacts=artifacts, harnesses=harnesses, graders=graders)
        now = SimpleNamespace(time=lambda: origin_s + gateway.clock.now_ms() / 1000)
        with patch.object(controller, "time", now), patch.object(phases, "run", original):
            cells = list(range(len(plan.cells)))
            return await live(instruments).measure(plan, admitted, journal, cells, cancel=cancel, deadline_s=origin_s + 21_600)

    with patch.object(phases, "run", measure):
        return bench.run()


def test_cells_checkpointed_before_a_worker_error_stay_in_the_report(tmp_path: Path) -> None:
    bench = lab.Bench(tmp_path)

    def error(_at: int, _student: int) -> None:
        if lab.running(bench) == 2:
            raise OSError

    bench.gateway.on_arrival = error
    with pytest.raises(OSError):
        measured_live(bench)

    kept = {one.cell for one in bench.journal.attempts if one.reusable}
    assert 1 in kept
    assert live().write_bundle(bench.planned, bench.journal, tmp_path / "bundle")
    assert {one.record.cell for one in checkpoints(tmp_path / "bundle")} == kept


def test_a_resumed_booking_s_times_follow_the_earlier_booking_s_in_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = lab.Bench(tmp_path / "one")

    def stop(_at: int, _student: int) -> None:
        if lab.running(first) == 2:
            first.cancel.set()

    first.gateway.on_arrival = stop
    measured_live(first)
    # A later booking: a new run clock, from zero, a minute after the first one ended.
    monkeypatch.setattr(lab, "ORIGIN_S", lab.ORIGIN_S + first.clock.now_ms() / 1000 + 60)
    second = lab.Bench(tmp_path / "two", resumes=first.journal)
    measured_live(second)

    assert live().write_bundle(second.planned, second.journal, tmp_path / "bundle")
    kept = checkpoints(tmp_path / "bundle")
    assert len({one.record.cell for one in kept}) > 2
    # One timeline, as the report reads it: no record starts before the one before it ended.
    assert all(later.record.started_ms >= earlier.record.ended_ms for earlier, later in itertools.pairwise(kept))


def test_a_host_serves_the_release_of_its_latest_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    wanted, other, revision = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "f" * 40
    release = json.dumps({"engine_image": "engine@" + wanted, "engine_flags": [], "model_revision": revision})
    parameter = SimpleNamespace(get_parameter=lambda **_kwargs: {"Parameter": {"Value": release}})
    session = SimpleNamespace(region_name="test", get_credentials=lambda: None, client=lambda _service: parameter)
    keys = tuple(generate_key() for _ in range(PLAN.canary_identities))
    monkeypatch.setattr(lookups.admin_main, "pool_access", lambda _session: None)
    monkeypatch.setattr(lookups.admin_pools, "deployed", lambda _pools: {POOL: SimpleNamespace(instance_types=["g7e.2xlarge"], engine="vllm")})
    monkeypatch.setattr(lookups, "_output", lambda *_args: "https://example.com")
    monkeypatch.setattr(lookups.spike_keys, "read", lambda _secrets: SimpleNamespace(students=keys))
    monkeypatch.setattr(lookups.image, "prepare", lambda: SimpleNamespace(image_id=wanted))
    monkeypatch.setattr(lookups.workers, "Workers", lambda *_args, **_kwargs: contextlib.nullcontext(SimpleNamespace(start=None)))
    said: list[tuple[str, float]] = []

    def samples(_query: str, *, at: float) -> list[Any]:
        labels = {"instance": "host", "model_revision": revision}
        return [lookups.common.Series(labels={**labels, "image_digest": digest}, samples=((at - age, 1),)) for digest, age in said]

    monkeypatch.setattr(lookups.common, "Workspace", lambda *_args: SimpleNamespace(read_samples=samples))
    with contextlib.ExitStack() as stack:
        resolved = lookups.resolve(controller.Scope(POOL, REPO, 1), PLAN, session, {}, stack)
        # The host left the release: an older scrape of it, answered last, does not count.
        said[:] = [(other, 1), (wanted, 16)]
        assert resolved.releases() != [resolved.release]
        said[:] = [(wanted, 1), (other, 16)]
        assert resolved.releases() == [resolved.release]
        # One scrape that says two releases says none.
        said[:] = [(wanted, 1), (other, 1)]
        assert resolved.releases() == [lookups.AMBIGUOUS]


def test_a_gap_in_a_host_s_launch_series_is_no_whole_window() -> None:
    workspace = lab_telemetry.clean()
    series = workspace.find(lab_telemetry.LAUNCH, instance=lab_telemetry.HOST_A)[0]
    series.times = tuple(at for at in lab_telemetry.SCRAPES if not 1108 < at < 1168)

    hosts = controller._hosts(lab_telemetry.reading(workspace))

    assert [one.launch_whole_window for one in hosts].count(False) == 1


# --- The verdicts ---


def test_a_floor_missed_at_low_load_is_a_quality_miss(tmp_path: Path) -> None:
    summary = json.loads(report_lab.scorecard(build(tmp_path / "bundle", CANDIDATE)).summary)
    assert any(one["floor_outcome"] == "fail" for one in summary["quality"]["low_load"]["cells"])
    only_low = replace(CANDIDATE, slow_several_agents_repetition=None, fails=frozenset(f for f in CANDIDATE.fails if f[0] == "low_load"))
    summary = json.loads(report_lab.scorecard(build(tmp_path / "low", only_low)).summary)

    card = controller.scorecard(summary)

    assert controller.overall(card.quality) is controller.Verdict.MEASURED_MISS
    assert any("at low_load fail" in row for row in card.quality_rows)


def test_tasks_that_met_their_floors_without_the_class_load_are_inconclusive(
    bundles: dict[str, Path], tmp_path: Path
) -> None:
    unloaded = report_lab.class_load_changed(bundles["pass"], tmp_path / "unloaded", report_lab.without(report_lab.Traffic.BACKGROUND))

    card = controller.scorecard(json.loads(report_lab.scorecard(unloaded).summary))

    assert controller.overall(card.quality) is controller.Verdict.INCONCLUSIVE
    assert any("class load not shown" in row for row in card.quality_rows)
