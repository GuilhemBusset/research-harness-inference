"""The operator's driver (tools/evaluate/driver.py) and its client (`cli.main`), over a real Unix
socket, with the controller tests' fake services: GitHub in memory and the deployed pool over moto.
Each run goes through `controller.execute` as the terminal's does. No account, no network.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import socket
import stat
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from evaluate import cli, controller
from evaluate import driver as served
from evaluate import journal as journals
from evaluate.tests import test_controller as lab
from evaluate.tests.test_booking import POOL, pool  # noqa: F401 - `pool` is a fixture
from evaluate.tests.test_controller import WHOLE, Died, bundles, driver  # noqa: F401 - fixtures
from evaluate.tests.test_publisher import REPO

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Sequence

    from evaluate.plan import Plan

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
    """The fake services behind the driver's `connect`: each scope it opened, and a hook that runs
    in a runtime's preflight with the count of scopes opened so far."""

    def __init__(self, fake: lab.Driver) -> None:
        self.fake, self.scopes = fake, []
        self.at_preflight: Callable[[int], None] | None = None

    @contextlib.contextmanager
    def __call__(self, scope: controller.Scope) -> Generator[lab.Runtime]:
        self.scopes.append(scope)
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
