"""The publication of an evaluation (`evaluate.publisher`): its experiment issue, the terminal
comment of each run and the comparison's, against a fake GitHub endpoint in memory
(docs/design/agent-session-evaluation.md, "Where results persist"). No network, no account.
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evaluate import publisher
from evaluate.contracts import Harness, Profile, Shape
from evaluate.journal import Action, IntentStatus, Journal, JournalError
from evaluate.journal import Refusal as JournalRefusal
from evaluate.publisher import (
    Cleanup,
    Comparison,
    Ending,
    Experiment,
    GitHub,
    Publisher,
    PublishError,
    Reason,
    Refusal,
    Relative,
    RunResult,
    Unanswered,
    Verdict,
    marker_line,
    resume_publication,
)

REPOSITORY = Path(__file__).resolve().parents[3]
REPO = "example-org/experiments"
DRIVER, OTHER = "evaluation-driver", "someone-else"
EXPERIMENT = Experiment(
    question="Does the candidate release keep class-size capacity on two hosts?",
    source_commit="a" * 40,
    suite="course-v1",
    profile=Profile.QUALIFY,
    workload="sha256:" + "b" * 64,
    targets={"reference": {"release": "r41", "hosts": "2"}, "candidate": {"release": "r42", "hosts": "2"}},
    seeds=(11, 12, 13),
    floors={"ttft_p95_ms": 2000, "speed_p10": 8.0},
)
REFERENCE_RUN = "f" * 16


class FakeGitHub:
    """The REST routes the publisher calls, in memory, with a page of two records. A fault is the
    next request of a method that gets no answer: `unsent` before it is applied, `lost` after,
    `lost-then-down` after, and the endpoint answers nothing more until `down` is cleared."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: list[dict[str, Any]] = []
        self.log: list[tuple[str, str]] = []
        self.faults: list[tuple[str, str]] = []
        self.down = False
        self._ids = 0

    def _next(self) -> int:
        self._ids += 1
        return self._ids

    def post_issue(self, login: str, body: str) -> int:
        number = self._next()
        self.issues[number] = {"number": number, "title": "An experiment", "body": body, "user": {"login": login}}
        return number

    def post_comment(self, login: str, number: int, body: str) -> int:
        comment = {"id": self._next(), "issue": number, "body": body, "user": {"login": login}}
        self.comments.append(comment)
        return comment["id"]

    def __call__(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        self.log.append((method, path))
        if self.down:
            raise Unanswered
        fault = next((one for one in self.faults if one[0] == method), None)
        if fault is not None:
            self.faults.remove(fault)
            if fault[1] == "unsent":
                raise Unanswered
        answer = self._route(method, path, body)
        if fault is not None:
            self.down = fault[1] == "lost-then-down"
            raise Unanswered
        return answer

    def _route(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        route, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if route == "/user":
            return 200, {"login": DRIVER}
        rest = route.removeprefix(f"/repos/{REPO}/issues").strip("/").split("/")
        if rest == [""] and method == "POST":
            return 201, self.issues[self.post_issue(DRIVER, body["body"])]
        if rest == [""]:
            return 200, self._page([one for one in self.issues.values() if one["user"]["login"] == params["creator"]], params)
        number = int(rest[0])
        if rest == [str(number)]:
            return (200, self.issues[number]) if number in self.issues else (404, None)
        if method == "POST":
            return 201, {"id": self.post_comment(DRIVER, number, body["body"]), "user": {"login": DRIVER}}
        return 200, self._page([one for one in self.comments if one["issue"] == number], params)

    @staticmethod
    def _page(records: list[dict[str, Any]], params: dict[str, str]) -> list[dict[str, Any]]:
        size, page = int(params["per_page"]), int(params["page"])
        return records[(page - 1) * size : page * size]

    def writes(self) -> list[tuple[str, str]]:
        return [one for one in self.log if one[0] == "POST"]

    def records(self, marker: str, login: str = DRIVER) -> list[dict[str, Any]]:
        """The records of `login`, issues and comments, that carry the marker's line."""
        every = [*self.issues.values(), *self.comments]
        return [one for one in every if one["user"]["login"] == login and marker_line(marker) in one["body"].splitlines()]


@pytest.fixture(autouse=True)
def pages_of_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publisher, "PAGE_SIZE", 2)  # records spread over pages


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def driver(fake: FakeGitHub) -> Publisher:
    return Publisher(GitHub(fake, REPO))


def run(run_id: str, **changes: Any) -> RunResult:
    measured = RunResult(
        run_id=run_id,
        ending=Ending.COMPLETED,
        cells_completed=12,
        cells_planned=12,
        observed={"release": "r42", "instance-type": "g6e.xlarge"},
        requested_hosts=2,
        served_hosts=2,
        capacity={Shape.HANDS_ON: Verdict.PASS, Shape.SEVERAL_AGENTS: Verdict.MEASURED_MISS},
        quality={Shape.HANDS_ON: Verdict.PASS, Shape.SEVERAL_AGENTS: Verdict.PASS},
        failures=3,
        attempts=400,
        tasks={Harness.CLAUDE_CODE: (10, 12), Harness.CODEX: (9, 12)},
        host_hours=1.5,
        cleanup=Cleanup.RELEASED,
        bundle_digest="sha256:" + "c" * 64,
        ttft_p95_ms=850,
        speed_p50=42.0,
        speed_p10=18.5,
    )
    return replace(measured, **changes)


def comparison(candidate: str) -> Comparison:
    return Comparison(
        reference_run=REFERENCE_RUN,
        candidate_run=candidate,
        capacity={Shape.HANDS_ON: Relative.NO_DIFFERENCE, Shape.SEVERAL_AGENTS: Relative.WORSE},
        quality={Shape.HANDS_ON: Relative.INCONCLUSIVE},
        bundle_digest="sha256:" + "d" * 64,
        permalink=f"https://github.com/{REPO}/blob/{'e' * 40}/docs/evaluations/pair/summary.json",
    )


def deliveries(journal: Journal) -> list[Any]:
    return [one for one in journal.intents if one.action is Action.DELIVER]


def refused_to_book(journal: Journal) -> None:
    with pytest.raises(JournalError) as raised:
        journal.intend(Action.BOOK, {"instances": 2})
    assert raised.value.reason is JournalRefusal.ISSUE_NOT_JOURNALED


# --- The record, before booking --------------------------------------------------------------


def test_the_experiment_issue_is_confirmed_before_anything_is_booked(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    journal = Journal.create(tmp_path)
    fake.down = True
    with pytest.raises(PublishError) as raised:
        driver.open_experiment(journal, EXPERIMENT)
    assert raised.value.reason is Refusal.ISSUE_UNCONFIRMED
    refused_to_book(journal)

    # A selected issue is the driver's, an issue, and the record of this same experiment.
    fake.down = False
    _title, body = publisher.render_issue(EXPERIMENT)
    other = publisher.render_issue(replace(EXPERIMENT, seeds=(1,)))[1]
    selections = {
        fake.post_issue(OTHER, body): Refusal.ISSUE_MISMATCH,
        fake.post_issue(DRIVER, other): Refusal.ISSUE_MISMATCH,
        999: Refusal.ISSUE_UNCONFIRMED,
    }
    for number, reason in selections.items():
        with Journal.create(tmp_path) as candidate:
            with pytest.raises(PublishError) as raised:
                driver.open_experiment(candidate, EXPERIMENT, issue=number)
            assert raised.value.reason is reason
            refused_to_book(candidate)


def test_candidate_and_reference_comments_and_their_comparison_attach_to_one_experiment(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    with Journal.create(tmp_path) as reference:
        number = driver.open_experiment(reference, EXPERIMENT)
        assert reference.issue_number == number
        reference_comment = driver.publish_run(reference, run(reference.run_id))
    with Journal.create(tmp_path) as candidate:
        assert driver.open_experiment(candidate, EXPERIMENT, issue=number) == number
        candidate.intend(Action.BOOK, {"instances": 2})  # the booking comes once the issue is known
        candidate_comment = driver.publish_run(candidate, run(candidate.run_id))
        compared = driver.publish_comparison(candidate, replace(comparison(candidate.run_id), reference_run=reference.run_id))

    assert len(fake.issues) == 1
    assert [comment["issue"] for comment in fake.comments] == [number] * 3
    assert reference_comment == publisher.Outbox(number, 1, 0)
    assert (candidate_comment, compared) == (publisher.Outbox(number, 1, 0), publisher.Outbox(number, 2, 0))
    with Journal.open(tmp_path, candidate.run_id) as again, pytest.raises(PublishError) as raised:
        driver.publish_run(again, run(candidate.run_id))  # one terminal comment per run
    assert raised.value.reason is Refusal.ALREADY_QUEUED


# --- Ambiguous answers -----------------------------------------------------------------------


def test_a_lost_answer_is_recovered_as_the_drivers_one_record_of_each_kind(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    journal = Journal.create(tmp_path)
    # Each write is applied and its answer lost; someone else already posted each marker.
    fake.post_issue(OTHER, marker_line(f"evaluate/{journal.run_id}/0"))
    fake.faults.append(("POST", "lost"))
    number = driver.open_experiment(journal, EXPERIMENT)
    for ordinal in (1, 2):
        fake.post_comment(OTHER, number, marker_line(f"evaluate/{journal.run_id}/{ordinal}"))
    fake.faults.append(("POST", "lost"))
    driver.publish_run(journal, run(journal.run_id))
    fake.faults.append(("POST", "lost"))
    driver.publish_comparison(journal, comparison(journal.run_id))

    assert [intent.status for intent in journal.intents] == [IntentStatus.CONFIRMED] * 3
    issue, run_comment, compared = journal.intents
    assert [record["number"] for record in fake.records(issue.marker)] == [number] == [journal.issue_number]
    for intent in (run_comment, compared):
        (own,) = fake.records(intent.marker)
        assert intent.result == {"kind": intent.payload["kind"], "comment": own["id"]}
        assert len(fake.records(intent.marker, OTHER)) == 1  # left alone, and never confirmed


def test_a_retry_keeps_its_marker_until_the_drivers_record_is_found(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    journal = Journal.create(tmp_path)
    number = driver.open_experiment(journal, EXPERIMENT)
    marker = f"evaluate/{journal.run_id}/1"
    fake.post_comment(OTHER, number, marker_line(marker))
    fake.faults.append(("POST", "unsent"))  # never applied: only the other author's copy is there
    assert driver.publish_run(journal, run(journal.run_id)).waiting == 1
    assert fake.records(marker) == []
    journal.close()

    # The resume sends the same intent again; it is applied, then GitHub is gone.
    fake.faults.append(("POST", "lost-then-down"))
    assert resume_publication(tmp_path, journal.run_id, driver).waiting == 1
    fake.down = False
    writes = len(fake.writes())
    assert resume_publication(tmp_path, journal.run_id, driver) == publisher.Outbox(number, 1, 0)

    assert len(fake.writes()) == writes  # found by its marker, not sent a third time
    (own,) = fake.records(marker)
    with Journal.open(tmp_path, journal.run_id) as again:
        (delivery,) = deliveries(again)
        assert (delivery.marker, delivery.result) == (marker, {"kind": "run", "comment": own["id"]})


# --- What is published -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"bundle_digest": "md5:" + "c" * 32},
        {"permalink": "https://example.com/bundle.json"},
        {"observed": {"release": "Free text, with spaces"}},
        {"ending": Ending.INTERRUPTED},  # a partial run without its fixed reason
        {"reason": Reason.CAPACITY_UNAVAILABLE},  # a completed run with one
        {"failures": 401},
        {"cleanup": "stopped"},
    ],
)
def test_a_rejected_field_sends_and_queues_nothing(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher, changes: dict[str, Any]
) -> None:
    journal = Journal.create(tmp_path)
    driver.open_experiment(journal, EXPERIMENT)
    intents, writes = journal.intents, fake.writes()
    with pytest.raises(PublishError) as raised:
        driver.publish_run(journal, run(journal.run_id, **changes))
    assert raised.value.reason is Refusal.FIELD_REJECTED
    assert (journal.intents, fake.writes()) == (intents, writes)


def test_a_rejected_question_or_a_scan_finding_sends_nothing(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    journal = Journal.create(tmp_path)
    for question in ("Kept? hygiene: allow account-id (no)", "Ask @someone", "Read https://example.com"):
        with pytest.raises(PublishError) as raised:
            driver.open_experiment(journal, replace(EXPERIMENT, question=question))
        assert raised.value.reason is Refusal.FIELD_REJECTED

    finding = Publisher(GitHub(fake, REPO), scan=lambda _text, _path: ["finding"])
    with pytest.raises(PublishError) as raised:
        finding.open_experiment(journal, EXPERIMENT)
    assert raised.value.reason is Refusal.HYGIENE_FINDING

    driver.open_experiment(journal, EXPERIMENT)
    writes = fake.writes()
    resource = "vpc-" + "0123456789abcdef0"  # a label the real scanner finds, built here
    with pytest.raises(PublishError) as raised:
        driver.publish_run(journal, run(journal.run_id, observed={"network": resource}))
    assert raised.value.reason is Refusal.HYGIENE_FINDING
    assert (fake.writes(), deliveries(journal)) == (writes, [])
    assert len(writes) == 1  # the issue, and nothing else


def test_a_post_run_outage_keeps_the_comment_and_its_comparison_for_publish_only_resume(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    journal = Journal.create(tmp_path)
    number = driver.open_experiment(journal, EXPERIMENT)
    fake.down = True
    partial = run(journal.run_id, ending=Ending.INTERRUPTED, reason=Reason.WINDOW_EXHAUSTED, cells_completed=7)
    assert driver.publish_run(journal, partial).waiting == 1
    assert driver.publish_comparison(journal, comparison(journal.run_id)) == publisher.Outbox(number, 0, 2)
    pending, queued = deliveries(journal)
    assert (pending.status, queued.status) == (IntentStatus.PENDING, IntentStatus.QUEUED)
    for value in (Verdict.MEASURED_MISS, Cleanup.RELEASED, Reason.WINDOW_EXHAUSTED, "7/12"):
        assert value in pending.payload["body"]
    journal.close()  # the driver exits with GitHub still down

    fake.down = False
    fake.log.clear()
    assert resume_publication(tmp_path, journal.run_id, driver) == publisher.Outbox(number, 2, 0)
    run_comment, compared = fake.comments
    assert run_comment["body"] == pending.payload["body"] + "\n" + marker_line(pending.marker) + "\n"
    assert compared["body"] == queued.payload["body"] + "\n" + marker_line(queued.marker) + "\n"
    # Publish-only: the driver's identity and the experiment's issue, and nothing else.
    assert {path.partition("?")[0] for _method, path in fake.log} <= {"/user", f"/repos/{REPO}/issues/{number}/comments"}


def test_publish_only_resume_loads_no_benchmark_or_cloud_client() -> None:
    probe = (
        "import sys, evaluate.publisher\n"
        "loaded = sorted(name for name in sys.modules if name.split('.')[0] in {"
        "'boto3', 'botocore', 'loadgen', 'rhi_gateway', 'rhi_admin', 'rhi_common', 'booking', 'common', "
        "'openai', 'anthropic'} or name in {"
        "'evaluate.booking', 'evaluate.streams', 'evaluate.replay', 'evaluate.workers', 'evaluate.trials', "
        "'evaluate.forwarding', 'evaluate.adapters', 'evaluate.telemetry', 'evaluate.capacity', 'evaluate.report'})\n"
        "print(loaded)\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(REPOSITORY / "tools")}
    probed = subprocess.run([sys.executable, "-c", probe], env=environment, capture_output=True, text=True, check=True)
    assert probed.stdout.strip() == "[]"
