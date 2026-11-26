"""The operator's driver (tools/evaluate/driver.py) and its client (`cli.main`), over a real Unix
socket, with the controller tests' fake services: GitHub in memory and the deployed pool over moto.
Each run goes through `controller.execute` as the terminal's does. No account, no network.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from evaluate import booking, cli, controller
from evaluate import driver as served
from evaluate import journal as journals
from evaluate.tests import test_controller as lab
from evaluate.tests.test_booking import POOL, pool  # noqa: F401 - `pool` is a fixture
from evaluate.tests.test_controller import WHOLE, Died, bundles, driver  # noqa: F401 - fixtures
from evaluate.tests.test_publisher import REPO

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Sequence

    from evaluate.plan import Plan

SKILL = Path(__file__).resolve().parents[3] / ".agents" / "skills" / "evaluate" / "SKILL.md"
RUN: dict[str, Any] = {
    "profile": "qualify",
    "pool": POOL,
    "students": 2,
    "instances": 2,
    "window_ms": WHOLE * 60_000,
    "issue": "new",
    "repository": REPO,
}


class Services:
    """The fake services behind the driver's `connect`: each scope it opened, and hooks that run
    when a scope is opened and in a runtime's preflight, with the count of scopes opened so far."""

    def __init__(self, fake: lab.Driver) -> None:
        self.fake, self.scopes = fake, []
        self.at_connect: Callable[[int], None] | None = None
        self.at_preflight: Callable[[int], None] | None = None

    @contextlib.contextmanager
    def __call__(self, scope: controller.Scope) -> Generator[lab.Runtime]:
        self.scopes.append(scope)
        if self.at_connect is not None:
            self.at_connect(len(self.scopes))
        yield Hooked(self, scope)


class Hooked(lab.Runtime):
    def __init__(self, services: Services, scope: controller.Scope) -> None:
        super().__init__(services.fake, scope)
        self.services = services

    def preflight(self, plan: Plan) -> Sequence[str]:
        if self.services.at_preflight is not None:
            self.services.at_preflight(len(self.services.scopes))
        return super().preflight(plan)


class Host:
    """The driver's runs root and socket, and every driver started on them, closed at the end."""

    def __init__(self, services: Services, runs: Path, socket_path: Path) -> None:
        self.services, self.runs, self.socket = services, runs, socket_path
        self.started: list[served.Driver] = []

    def start(self, **options: Any) -> served.Driver:
        one = served.Driver(self.runs, self.socket, connect=self.services, **options)
        one.start()
        self.started.append(one)
        return one

    def restart(self, **options: Any) -> served.Driver:
        self.started[-1].close()
        return self.start(**options)

    def call(self, **message: Any) -> dict[str, Any]:
        return served.call(self.socket, message, timeout=30)

    def settled(self, submission: str) -> dict[str, Any]:
        """The run's status once its driver recorded its exit."""
        for _ in range(600):
            shown = self.call(op="inspect", submission=submission)
            if shown.get("exit") is not None and not shown["running"]:
                return shown
            time.sleep(0.1)
        raise AssertionError("the run did not end")

    def counts(self) -> tuple[int, int, int]:
        """The runs journaled, the issues created and the bookings written."""
        fake = self.services.fake
        return len(journals.runs(self.runs)), len(fake.github.issues), fake.events.count("book")

    def admitted(self) -> list[Path]:
        return sorted((self.runs / served.SUBMISSIONS).glob("*.json"))


@pytest.fixture
def host(driver: lab.Driver) -> Iterator[Host]:  # noqa: F811 - the controller tests' fixture
    # A Unix socket's path is short: the folder is the system's temporary one, not pytest's.
    folder = Path(tempfile.mkdtemp(prefix="rhi-"))
    made = Host(Services(driver), driver.runs, folder / "sock" / "driver.sock")
    yield made
    for one in made.started:
        one.close()
    shutil.rmtree(folder, ignore_errors=True)


def submission() -> str:
    return secrets.token_hex(16)


def lose_answer(socket_path: Path, message: dict[str, Any]) -> None:
    """Sends a request and goes away without reading the answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(socket_path))
        connection.sendall(json.dumps(message).encode() + b"\n")


# --- One run, whatever the client and the driver lose ---


def test_a_submission_whose_answer_is_lost_is_sent_again_and_stays_one_run_issue_and_booking_across_a_restart(
    host: Host,
) -> None:
    host.start()
    sid = submission()
    lose_answer(host.socket, {"op": "submit", "submission": sid, "run": RUN})
    again = host.call(op="submit", submission=sid, run=RUN)
    assert again["ok"]
    shown = host.settled(sid)
    (number,) = host.services.fake.github.issues
    assert shown["run_id"] == again["run_id"]
    assert shown["exit"] == controller.Exit.PASS
    assert shown["issue"] == controller.issue_url(REPO, number)
    assert host.counts() == (1, 1, 1)

    host.restart()
    after = host.call(op="submit", submission=sid, run=RUN)
    by_run = host.call(op="inspect", run_id=shown["run_id"])
    assert after["run_id"] == by_run["run_id"] == shown["run_id"]
    assert by_run["exit"] == controller.Exit.PASS and by_run["issue"] == shown["issue"]
    assert host.counts() == (1, 1, 1)
    # The answer holds the allow-listed fields alone: no booking, no time, no marker.
    assert set(by_run) <= served.ANSWER_FIELDS


def test_a_driver_that_dies_between_the_mapping_and_the_journal_completes_that_admission_after_its_restart(
    host: Host,
) -> None:
    def die(_run_id: str) -> None:
        raise Died

    host.start(on_admitted=die)
    sid = submission()
    with pytest.raises(served.Unanswered):
        host.call(op="submit", submission=sid, run=RUN)
    (record,) = host.admitted()
    assert host.counts() == (0, 0, 0)

    host.restart()
    shown = host.settled(sid)
    assert shown["run_id"] == json.loads(record.read_text())["run_id"]
    assert host.call(op="submit", submission=sid, run=RUN)["run_id"] == shown["run_id"]
    assert shown["exit"] == controller.Exit.PASS
    assert host.counts() == (1, 1, 1)


def test_a_run_resumed_after_its_window_passed_keeps_the_window_of_its_admission_and_books_nothing(
    host: Host,
) -> None:
    def die(_run_id: str) -> None:
        raise Died

    host.start(on_admitted=die)
    sid = submission()
    with pytest.raises(served.Unanswered):
        host.call(op="submit", submission=sid, run=RUN)
    host.services.fake.pool.sleep(RUN["window_ms"] // 1000 + 60)  # the driver stays down past `--for`

    host.restart()
    assert host.settled(sid)["exit"] == controller.Exit.INCOMPLETE
    assert host.counts() == (1, 1, 0)


def test_a_driver_that_dies_after_releasing_its_booking_does_not_book_again_when_it_resumes_the_run(
    host: Host,
) -> None:
    fake = host.services.fake
    fake.script.fail_at = 2  # the run ends short, with cells left, and releases its booking

    def die() -> None:
        raise Died

    fake.github.on_comment = die  # then the driver dies before it records the run's end
    host.start()
    sid = submission()
    first = host.call(op="submit", submission=sid, run=RUN)
    for _ in range(600):
        if "stop" in fake.events and not host.call(op="inspect", submission=sid)["running"]:
            break
        time.sleep(0.1)
    assert host.call(op="inspect", submission=sid)["exit"] is None

    fake.github.on_comment, fake.script.fail_at = None, None
    host.restart()
    shown = host.settled(sid)
    assert shown["run_id"] == first["run_id"]
    assert shown["exit"] == controller.Exit.INCOMPLETE
    assert host.counts() == (1, 1, 1)


def test_a_restart_before_the_first_booking_write_makes_that_booking_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached = threading.Event()

    def interrupted(_self: booking.Capacity, *, minutes: int | None = None) -> booking.Outcome:
        reached.set()
        raise Died

    sid = submission()
    with monkeypatch.context() as patch:
        patch.setattr(booking.Capacity, "acquire", interrupted)
        first = host.start()
        assert host.call(op="submit", submission=sid, run=RUN)["ok"]
        assert reached.wait(10)
        first.close()
    assert host.counts() == (1, 1, 0)  # the booking was intended, never written

    host.start()
    assert host.settled(sid)["exit"] == controller.Exit.PASS
    assert host.counts() == (1, 1, 1)


def test_a_run_in_the_operator_s_session_resumes_in_that_session_untouched_after_a_restart(host: Host) -> None:
    fake = host.services.fake
    operator = lab.held_by_operator(fake)
    reached = threading.Event()

    def interrupted(_cell: int) -> None:
        reached.set()
        raise Died

    fake.script.before_cell = interrupted
    first = host.start()
    sid = submission()
    assert host.call(op="submit", submission=sid, run={**RUN, "session": "existing", "window_ms": None})["ok"]
    assert reached.wait(10)
    first.close()

    fake.script.before_cell = None
    host.start()
    assert host.settled(sid)["exit"] == controller.Exit.PASS
    assert lab.stored(fake.pool) == operator and host.counts() == (1, 1, 0)


@pytest.mark.parametrize("expired", [True, False], ids=["expired-then-booked-again", "replaced"])
def test_a_run_admitted_in_the_operator_s_session_never_measures_in_another_after_a_restart(
    host: Host, expired: bool
) -> None:
    fake = host.services.fake
    first = lab.held_by_operator(fake)

    def die(_run_id: str) -> None:
        raise Died

    host.start(on_admitted=die)
    sid = submission()
    with pytest.raises(served.Unanswered):
        host.call(op="submit", submission=sid, run={**RUN, "session": "existing", "window_ms": None})
    fake.pool.sleep(first.ends_at - fake.pool.now + 60 if expired else 60)
    replacement = lab.held_by_operator(fake)  # the same pool and hosts, booked again by hand
    assert replacement != first

    host.restart()
    assert host.settled(sid)["exit"] != controller.Exit.PASS
    assert fake.sent == [] and lab.stored(fake.pool) == replacement
    assert fake.events.count("book") == 0


def test_a_driver_closed_while_its_run_measures_keeps_the_runs_until_it_ends_and_its_result_stands(
    host: Host,
) -> None:
    reached, release = threading.Event(), threading.Event()

    def pause(_cell: int) -> None:
        if not reached.is_set():
            reached.set()
            release.wait(10)

    host.services.fake.script.before_cell = pause
    old = host.start()
    sid = submission()
    assert host.call(op="submit", submission=sid, run=RUN)["ok"]
    assert reached.wait(10)
    old.close(wait=0)
    # Its run still writes its journal: no other driver takes the runs meanwhile.
    with pytest.raises(served.InUse):
        host.start()
    release.set()
    old.close()

    host.start()
    assert host.settled(sid)["exit"] == controller.Exit.PASS
    assert host.counts() == (1, 1, 1)


def test_the_issue_a_later_publication_confirmed_is_shown(host: Host) -> None:
    host.services.fake.github.faults.append(("POST", "unsent"))
    host.start()
    sid = submission()
    run_id = host.call(op="submit", submission=sid, run=RUN)["run_id"]
    assert host.settled(sid)["issue"] is None

    assert controller.publish_resume(host.runs, run_id, connect=host.services, out=io.StringIO()) == 0
    (number,) = host.services.fake.github.issues
    assert host.call(op="inspect", run_id=run_id)["issue"] == controller.issue_url(REPO, number)


def test_an_issue_created_without_its_confirmation_is_pending_and_its_resume_shows_that_one_issue(host: Host) -> None:
    """GitHub creates the issue and the answer is lost: the run stops before booking, shows no
    issue and a pending publication, and the command the `evaluate` skill gives the operator for
    that state confirms that same issue."""
    github = host.services.fake.github
    github.faults.append(("POST", "lost-then-down"))
    host.start()
    sid = submission()
    run_id = host.call(op="submit", submission=sid, run=RUN)["run_id"]
    seen = host.settled(sid)
    assert (seen["issue"], seen["running"], seen["publication"]) == (None, False, "pending")
    assert seen["exit"] == controller.Exit.INCOMPLETE
    assert host.counts() == (1, 1, 0)
    assert len(github.issues) == 1  # GitHub has it; the driver could not confirm it

    given = re.findall(r"`scripts/evaluate\.sh (publish [^`]*<run-id>)`", SKILL.read_text(encoding="utf-8"))
    assert given, "the skill gives the operator a recovery for a pending publication"
    recovery = cli.parser().parse_args(shlex.split(given[0].replace("<run-id>", run_id)))
    github.down = False
    assert controller.publish_resume(host.runs, recovery.run_id, connect=host.services, out=io.StringIO()) == 0
    (number,) = github.issues
    assert host.call(op="inspect", run_id=run_id)["issue"] == controller.issue_url(REPO, number)
    assert host.counts() == (1, 1, 0)


def test_a_run_is_inspected_and_cancelled_while_another_submission_waits_on_its_admission(host: Host) -> None:
    measuring, admitting, release = threading.Event(), threading.Event(), threading.Event()

    def hold_the_batch(_cell: int) -> None:
        if not measuring.is_set():
            measuring.set()
            release.wait(15)

    def hold_the_second_admission(opened: int) -> None:
        if opened == 3:  # the first run's admission, then its invocation, then this one
            admitting.set()
            release.wait(15)

    host.services.fake.script.before_cell = hold_the_batch
    host.start()
    first = submission()
    assert host.call(op="submit", submission=first, run=RUN)["ok"]
    assert measuring.wait(10)
    host.services.at_connect = hold_the_second_admission
    with ThreadPoolExecutor(max_workers=1) as clients:
        second = clients.submit(host.call, op="submit", submission=submission(), run=RUN)
        try:
            assert admitting.wait(10)
            assert served.call(host.socket, {"op": "inspect", "submission": first}, timeout=2)["running"]
            reply = served.call(host.socket, {"op": "cancel", "submission": first}, timeout=2)
            assert reply["ok"] and reply["cancel_requested"]
        finally:
            release.set()
        assert second.result(timeout=30)["ok"]
    assert host.settled(first)["cancel_requested"]


def test_a_cancel_accepted_while_the_run_is_in_its_preflight_holds_and_nothing_is_created_or_booked(
    host: Host,
) -> None:
    sid = submission()
    answers: list[dict[str, Any]] = []

    def cancel(opened: int) -> None:
        if opened == 2:  # the run's own preflight: the first was its admission's
            answers.append(host.call(op="cancel", submission=sid))

    host.services.at_preflight = cancel
    host.start()
    assert host.call(op="submit", submission=sid, run=RUN)["ok"]
    shown = host.settled(sid)
    assert answers[0]["ok"] and shown["cancel_requested"]
    assert shown["exit"] == controller.Exit.INCOMPLETE and shown["issue"] is None
    assert host.counts() == (1, 0, 0)


def test_a_client_saves_its_submission_before_sending_it_and_the_same_id_with_other_fields_is_refused(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    outbox = host.runs.parent / "outbox"
    argv = ["submit", "--profile", "qualify", "--pool", POOL, "--students", "2", "--instances", "2"]
    argv += ["--for", f"{WHOLE}m", "--issue", "new", "--repository", REPO, "--outbox", str(outbox)]
    argv += ["--socket", str(host.socket)]
    # No driver listens yet: the answer is lost, the submission is kept.
    assert cli.main(argv) == 2
    (saved,) = outbox.glob("*.json")
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    sid = saved.stem
    assert json.loads(saved.read_text())["run"]["window_ms"] == WHOLE * 60_000

    host.start()
    capsys.readouterr()
    assert cli.main([*argv, "--submission", sid]) == 0
    run_id = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("run_id: "))
    assert cli.main([*argv, "--submission", sid]) == 0
    assert run_id in capsys.readouterr().out

    other = [*argv, "--submission", sid]
    other[other.index("--students") + 1] = "3"
    assert cli.main(other) == 2
    assert served.Refusal.SUBMISSION_CONFLICT.value in capsys.readouterr().err
    host.settled(sid)
    assert host.counts() == (1, 1, 1)


# --- What the driver refuses ---


def test_the_socket_is_its_owner_s_alone_and_another_user_is_refused_before_anything_is_read(host: Host) -> None:
    host.start(owner=os.getuid() + 1)
    assert stat.S_IMODE(host.socket.stat().st_mode) == 0o600
    assert stat.S_IMODE(host.socket.parent.stat().st_mode) == 0o700
    answer = host.call(op="submit", submission=submission(), run=RUN)
    assert answer == {"ok": False, "refused": served.Refusal.ACCESS_DENIED.value}
    assert host.services.scopes == [] and host.admitted() == [] and host.counts() == (0, 0, 0)


AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # built here, so that the file holds no key's shape


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ({"op": "exec", "argv": ["sh", "-c", "true"]}, served.Refusal.UNKNOWN_COMMAND),
        ({"op": "submit", "submission": "0" * 32, "run": RUN, "shell": "true"}, served.Refusal.UNKNOWN_FIELD),
        ({"op": "submit", "submission": "0" * 32, "run": {**RUN, "command": "true"}}, served.Refusal.UNKNOWN_FIELD),
        ({"op": "submit", "submission": "0" * 32, "run": {**RUN, "window_ms": None}}, served.Refusal.INVALID_BOOKING),
        ({"op": "submit", "submission": "0" * 32, "run": {**RUN, "window_ms": 0}}, served.Refusal.INVALID_BOOKING),
        (
            {"op": "submit", "submission": "0" * 32, "run": {**RUN, "window_ms": -60_000}},
            served.Refusal.INVALID_BOOKING,
        ),
        ({"op": "submit", "submission": "0" * 32, "run": {**RUN, "window_ms": 90_000}}, served.Refusal.INVALID_BOOKING),
        (
            {"op": "submit", "submission": "0" * 32, "run": {**RUN, "window_ms": served.MAX_WINDOW_MS + 60_000}},
            served.Refusal.INVALID_BOOKING,
        ),
        ({"op": "submit", "submission": "0" * 32, "run": {**RUN, "github_token": "x"}}, served.Refusal.CREDENTIAL),
        ({"op": "submit", "submission": "0" * 32, "run": {**RUN, "question": AWS_KEY}}, served.Refusal.CREDENTIAL),
        (
            {"op": "submit", "submission": "0" * 32, "run": {**RUN, "question": "Authorization: Bearer redacted"}},
            served.Refusal.CREDENTIAL,
        ),
        ({"op": "inspect", "submission": "0" * 32, "aws_secret_access_key": "x"}, served.Refusal.CREDENTIAL),
    ],
    ids=[
        "generic-command",
        "unknown-field",
        "unknown-run-field",
        "no-bound",
        "zero-bound",
        "negative-bound",
        "partial-minute",
        "over-the-ceiling",
        "credential-field",
        "credential-value",
        "authorization-header",
        "credential-in-inspect",
    ],
)
def test_a_request_outside_the_protocol_is_refused_before_anything_is_dispatched(
    host: Host, message: dict[str, Any], reason: served.Refusal
) -> None:
    host.start()
    answer = host.call(**message)
    assert answer["ok"] is False and answer["refused"] == reason.value
    assert host.services.scopes == [] and host.services.fake.events == []
    assert host.admitted() == [] and host.counts() == (0, 0, 0)


@pytest.mark.parametrize(
    "run",
    [{**RUN, "instances": 3}, {**RUN, "window_ms": (lab.SESSION_HOURS + 1) * 3_600_000}],
    ids=["more-hosts-than-the-pool", "longer-than-a-session"],
)
def test_a_booking_the_deployed_pool_cannot_hold_is_not_admitted(host: Host, run: dict[str, Any]) -> None:
    host.start()
    answer = host.call(op="submit", submission=submission(), run=run)
    assert answer["refused"] == served.Refusal.NOT_ADMITTED.value
    assert controller.Check.POOL_LIMITS.value in answer["detail"]
    assert "run_id" not in answer and host.admitted() == [] and host.counts() == (0, 0, 0)


def not_admitted(host: Host, run: dict[str, Any]) -> None:
    answer = host.call(op="submit", submission=submission(), run=run)
    assert answer["refused"] == served.Refusal.NOT_ADMITTED.value and "run_id" not in answer
    assert host.admitted() == [] and host.services.fake.events.count("book") == 0


def test_an_experiment_issue_that_is_not_there_or_records_another_workload_is_not_admitted(host: Host) -> None:
    host.start()
    not_admitted(host, {**RUN, "issue": 73})
    assert host.counts() == (0, 0, 0)

    sid = submission()
    host.call(op="submit", submission=sid, run=RUN)
    host.settled(sid)
    (number,) = host.services.fake.github.issues
    answer = host.call(op="submit", submission=submission(), run={**RUN, "issue": number, "input_seed": 41})
    assert answer["refused"] == served.Refusal.NOT_ADMITTED.value and "run_id" not in answer
    assert len(host.admitted()) == 1 and host.counts() == (1, 1, 1)


@pytest.mark.parametrize("issue", ["new", 1], ids=["new-issue", "selected-issue"])
def test_a_repository_github_does_not_have_for_the_driver_is_not_admitted(
    host: Host, monkeypatch: pytest.MonkeyPatch, issue: str | int
) -> None:
    github = host.services.fake.github
    routed = github._route

    def absent(method: str, path: str, body: Any) -> tuple[int, Any]:
        return (404, None) if path.startswith("/repos/") else routed(method, path, body)

    monkeypatch.setattr(github, "_route", absent)
    host.start()
    answer = host.call(op="submit", submission=submission(), run={**RUN, "issue": issue})
    assert answer["refused"] == served.Refusal.NOT_ADMITTED.value and "run_id" not in answer
    assert controller.Check.DESTINATION.value in answer["detail"]
    assert host.admitted() == [] and host.counts() == (0, 0, 0)


def test_a_repository_the_driver_reads_but_cannot_open_an_issue_in_is_admitted_and_ends_invalid_unbooked(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Admission only reads: a refused issue creation is the admitted run's, before its booking.
    github = host.services.fake.github
    routed = github._route
    monkeypatch.setattr(github, "_route", lambda method, path, body: (403, {}) if method == "POST" else routed(method, path, body))
    host.start()
    sid = submission()
    answer = host.call(op="submit", submission=sid, run=RUN)
    assert answer["ok"] and answer["run_id"]
    shown = host.settled(sid)
    assert shown["run_id"] == answer["run_id"] and shown["exit"] == controller.Exit.INCOMPLETE
    assert shown.get("issue") is None and host.counts() == (1, 0, 0)


@pytest.mark.parametrize("operator_hosts", [None, 1], ids=["no-session", "another-host-count"])
def test_a_designated_session_the_pool_does_not_hold_for_the_hosts_is_not_admitted(
    host: Host, operator_hosts: int | None
) -> None:
    if operator_hosts is not None:
        fake = host.services.fake
        session = lab.held_by_operator(fake)
        fake.pool.table.put_item(Item=dataclasses.replace(session, instances=operator_hosts).to_item())
    host.start()
    not_admitted(host, {**RUN, "session": "existing", "window_ms": None})
    assert host.counts() == (0, 0, 0)


def test_the_runtime_s_own_checks_run_only_once_the_submission_is_mapped_and_journaled(host: Host) -> None:
    # The live runtime's checks prepare the trial image and start its containers.
    seen: list[tuple[int, int]] = []
    host.services.at_preflight = lambda _opened: seen.append((len(host.admitted()), host.counts()[0]))
    host.start()
    sid = submission()
    assert host.call(op="submit", submission=sid, run=RUN)["ok"]
    host.settled(sid)
    assert seen == [(1, 1)]


def test_a_driver_never_takes_the_socket_another_driver_answers_on_and_replaces_a_stale_one(
    host: Host, tmp_path: Path
) -> None:
    host.start()
    sid = submission()
    run_id = host.call(op="submit", submission=sid, run=RUN)["run_id"]
    other = served.Driver(tmp_path / "other-runs", host.socket, connect=host.services)
    with pytest.raises(served.InUse):
        other.start()
    other.close()
    assert host.call(op="inspect", submission=sid)["run_id"] == run_id
    host.settled(sid)

    # A socket left at the path with nothing answering on it is the next driver's.
    host.started[-1].close()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as left:
        left.bind(str(host.socket))
    host.start()
    assert host.call(op="inspect", submission=sid)["run_id"] == run_id


def test_a_socket_directory_others_may_enter_is_refused_and_left_as_it_is(host: Host) -> None:
    host.socket.parent.mkdir(mode=0o700)
    host.socket.parent.chmod(0o755)
    with pytest.raises(PermissionError):
        host.start()
    assert stat.S_IMODE(host.socket.parent.stat().st_mode) == 0o755
    assert host.services.scopes == []
