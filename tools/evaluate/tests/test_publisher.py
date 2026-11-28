"""The publication of an evaluation (`evaluate.publisher`): its experiment issue, the terminal
comment of each run and the comparison's, against a fake GitHub endpoint in memory
(docs/design/agent-session-evaluation.md, "Where results persist"). No network, no account.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tomllib
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from evaluate import journal as journals
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
    HttpTransport,
    Publisher,
    PublishError,
    Reason,
    Refusal,
    Relative,
    RunResult,
    Unanswered,
    Verdict,
    marker_line,
    render_comparison,
    render_issue,
    render_run,
    resume_publication,
)

REPOSITORY = Path(__file__).resolve().parents[3]
REPO = "example-org/experiments"
DRIVER, OTHER = "evaluation-driver", "someone-else"
SUITE = tomllib.loads((REPOSITORY / "tools/evaluate/suites/course-v1.toml").read_text(encoding="utf-8"))
# The public settings of the checked-in report's pool: its image digest, its GPU's public name.
SETTINGS = json.loads((REPOSITORY / "tools/evaluate/fixtures/report/manifest.json").read_text(encoding="utf-8"))
SETTINGS = {name: value for name, value in SETTINGS["observed"].items() if value is not None}
EXPERIMENT = Experiment(
    question="Does the candidate release keep class-size capacity on two hosts?",
    source_commit="a" * 40,
    suite="course-v1",
    profile=Profile.QUALIFY,
    workload="sha256:" + "b" * 64,
    targets={
        "reference": {"host_count": 2, "kv_dtype": "bits_16"},
        "candidate": {"host_count": 2, "kv_dtype": "bits_8"},
    },
    seeds=(11, 12, 13),
    floors=SUITE["objectives"],
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
        if route == f"/repos/{REPO}":
            return 200, {"full_name": REPO, "has_issues": True, "archived": False}
        rest = route.removeprefix(f"/repos/{REPO}/issues").strip("/").split("/")
        if rest == [""] and method == "POST":
            return 201, self.issues[self.post_issue(DRIVER, body["body"])]
        if rest == [""]:
            return 200, self._page(
                [one for one in self.issues.values() if one["user"]["login"] == params["creator"]], params
            )
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
        return [
            one for one in every if one["user"]["login"] == login and marker_line(marker) in one["body"].splitlines()
        ]


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
        observed={"instance_type": "g7e.2xlarge", "gpu_type": "RTX PRO 6000", "host_count": 2},
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
        compared = driver.publish_comparison(
            candidate, replace(comparison(candidate.run_id), reference_run=reference.run_id)
        )

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


def test_a_saved_creation_survives_a_stop_during_its_recovery(tmp_path: Path, fake: FakeGitHub) -> None:
    with Journal.create(tmp_path) as journal:
        fake.down = True  # no login: the creation is saved, unsent
        with pytest.raises(PublishError):
            Publisher(GitHub(fake, REPO)).open_experiment(journal, EXPERIMENT)
    fake.down = False
    write = journals._write_whole

    def stop_after_the_write(path: Path, text: str) -> None:
        write(path, text)
        raise InterruptedError

    with patch.object(journals, "_write_whole", stop_after_the_write), pytest.raises(InterruptedError):
        resume_publication(tmp_path, journal.run_id, Publisher(GitHub(fake, REPO)))
    recovered = resume_publication(tmp_path, journal.run_id, Publisher(GitHub(fake, REPO)))
    assert recovered.issue is not None and list(fake.issues) == [recovered.issue]


def test_a_recovered_issue_is_confirmed_only_for_its_experiment(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    journal = Journal.create(tmp_path)
    fake.faults.append(("POST", "lost-then-down"))
    with pytest.raises(PublishError):
        driver.open_experiment(journal, EXPERIMENT)
    ((number, issue),) = fake.issues.items()
    (creation,) = journal.intents
    # Edited, before the recovery, into another experiment's record that keeps the marker.
    issue["body"] = render_issue(replace(EXPERIMENT, seeds=(88,)))[1] + marker_line(creation.marker) + "\n"
    fake.down = False
    with pytest.raises(PublishError) as raised:
        driver.open_experiment(journal, EXPERIMENT)
    assert raised.value.reason is Refusal.ISSUE_MISMATCH
    refused_to_book(journal)
    assert list(fake.issues) == [number]


def test_a_cut_response_is_an_answer_lost() -> None:
    reply = MagicMock(status=201)
    reply.read.side_effect = http.client.IncompleteRead(b"{", 10)
    with patch("urllib.request.urlopen") as opener, pytest.raises(Unanswered):
        opener.return_value.__enter__.return_value = reply
        HttpTransport("")("POST", f"/repos/{REPO}/issues", {})


# --- One record, one destination -------------------------------------------------------------


@pytest.mark.parametrize("changed", ["repository", "writer"])
def test_a_resume_writes_only_to_the_runs_repository_as_its_driver(
    tmp_path: Path, fake: FakeGitHub, changed: str
) -> None:
    with Journal.create(tmp_path) as journal:
        number = Publisher(GitHub(fake, REPO)).open_experiment(journal, EXPERIMENT)
        fake.faults.append(("POST", "lost-then-down"))  # the comment is posted, its answer lost
        assert Publisher(GitHub(fake, REPO)).publish_run(journal, run(journal.run_id)).waiting == 1
    fake.down = False
    elsewhere = FakeGitHub()
    elsewhere.post_issue(DRIVER, "Another experiment")  # the same number, in another repository

    def send(method: str, path: str, body: Any) -> tuple[int, Any]:
        if changed == "repository":
            return elsewhere(method, path.replace("example-org/other", REPO), body)
        if path == "/user":
            return 200, {"login": "second-driver"}
        return fake(method, path, body)

    repository = "example-org/other" if changed == "repository" else REPO
    with pytest.raises(PublishError) as raised:
        resume_publication(tmp_path, journal.run_id, Publisher(GitHub(send, repository)))
    assert raised.value.reason is Refusal.DESTINATION_MISMATCH
    assert len(fake.comments) == 1 and fake.comments[0]["issue"] == number
    assert not elsewhere.comments and not elsewhere.writes()
    assert resume_publication(tmp_path, journal.run_id, Publisher(GitHub(fake, REPO))) == publisher.Outbox(number, 1, 0)
    assert len(fake.comments) == 1  # the run's own driver finds it by its marker


@pytest.mark.parametrize("in_doubt", [True, False])
def test_a_run_stays_bound_to_its_experiment(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher, in_doubt: bool
) -> None:
    journal = Journal.create(tmp_path)
    fake.down = in_doubt
    if in_doubt:
        with pytest.raises(PublishError):
            driver.open_experiment(journal, EXPERIMENT)
    else:
        driver.open_experiment(journal, EXPERIMENT)
    fake.down = False
    for changed in (replace(EXPERIMENT, seeds=(19,)), replace(EXPERIMENT, targets={"reference": {"host_count": 3}})):
        with pytest.raises(PublishError) as raised:
            driver.open_experiment(journal, changed)
        assert raised.value.reason is Refusal.ISSUE_MISMATCH
    if in_doubt:
        refused_to_book(journal)
        assert fake.writes() == []
    number = driver.open_experiment(journal, EXPERIMENT)  # the same experiment goes on with its issue
    assert (number, len(fake.issues)) == (journal.issue_number, 1)


def test_each_distinct_pair_has_one_final_comment(tmp_path: Path, fake: FakeGitHub, driver: Publisher) -> None:
    with Journal.create(tmp_path) as candidate:
        number = driver.open_experiment(candidate, EXPERIMENT)
        driver.publish_run(candidate, run(candidate.run_id))
        pairs = []
        for _ in range(2):
            with Journal.create(tmp_path) as reference:
                driver.open_experiment(reference, EXPERIMENT, issue=number)
                driver.publish_run(reference, run(reference.run_id))
                pairs.append(replace(comparison(candidate.run_id), reference_run=reference.run_id))
                driver.publish_comparison(candidate, pairs[-1])
        with pytest.raises(PublishError) as raised:
            driver.publish_comparison(candidate, pairs[0])  # a retry of the same pair
        assert raised.value.reason is Refusal.ALREADY_QUEUED
    assert len(fake.comments) == 5
    assert [comment["issue"] for comment in fake.comments] == [number] * 5


def test_a_delivery_the_publisher_did_not_queue_stays_private(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    with Journal.create(tmp_path) as journal:
        driver.open_experiment(journal, EXPERIMENT)
        legacy = journal.intend(Action.DELIVER, {"body": "Private diagnostic note."})
    with pytest.raises(PublishError) as raised:
        resume_publication(tmp_path, journal.run_id, driver)
    assert raised.value.reason is Refusal.UNRECOGNIZED_ENTRY
    assert fake.comments == []
    with Journal.open(tmp_path, journal.run_id) as again:
        assert again.intents[legacy.ordinal] == legacy  # kept, pending, as it was


# --- What is published -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"bundle_digest": "md5:" + "c" * 32},
        {"permalink": "https://example.com/bundle.json"},
        {"observed": {"class_schedule": "private-canary"}},  # not a public setting
        {"observed": {"gpu_type": "a free name"}},
        {"ending": Ending.INTERRUPTED},  # a partial run without its fixed reason
        {"reason": Reason.CAPACITY_UNAVAILABLE},  # a completed run with one
        {"cells_completed": 0},  # completed, with cells missing
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


def test_public_settings_floors_and_measurements_render_as_given(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    seed = 2**32 - 1
    experiment = replace(EXPERIMENT, targets={"reference": SETTINGS}, seeds=(seed,))
    with Journal.create(tmp_path) as journal:
        number = driver.open_experiment(journal, experiment)
        driver.publish_run(journal, run(journal.run_id, observed=SETTINGS, ttft_p95_ms=850.5))
    issue, (comment,) = fake.issues[number]["body"], fake.comments
    for value in (SETTINGS["engine_image_digest"], SETTINGS["gpu_type"]):
        assert value in issue and value in comment["body"]
    assert str(seed) in issue and "850.5 ms" in comment["body"]
    assert f"`paired_margin_points`: {SUITE['objectives']['paired_margin_points']}" in issue
    assert render_issue(experiment) == render_issue(experiment)  # a checked record renders the same
    assert "850.5" in render_run(run("a" * 16, ttft_p95_ms=850.5))


def test_a_comparison_shows_each_shape_s_limiting_rows_or_the_evidence_missing() -> None:
    from evaluate import capacity, quality, report

    # The mirrored vocabularies are those the report grades and the paired decisions read.
    assert {one.value for one in publisher.Floor} == {one.value for one in capacity.Floor}
    assert {one.value for one in publisher.Undecided} == {one.value for one in quality.Reason} | {report.LOAD_UNSHOWN}
    missed = publisher.Limit(publisher.Floor.SPEED_P10, missed=True, headroom=-0.5, repetition=1)
    shown = render_comparison(
        replace(
            comparison("a" * 16),
            limits={Shape.SEVERAL_AGENTS: (None, missed)},
            undecided={Shape.HANDS_ON: publisher.Undecided.MISSING_PAIRS},
        )
    )
    several = next(line for line in shown.splitlines() if "several_agents limiting row" in line)
    assert "reference no floor read" in several and "`speed_p10` missed (headroom -0.5, repetition 1)" in several
    assert "hands_on limiting row: reference no floor read; candidate no floor read" in shown
    assert "`missing-pairs`" in shown
    for limits in ({Shape.HANDS_ON: (replace(missed, missed="yes"), None)}, {Shape.HANDS_ON: (missed,)}):
        with pytest.raises(PublishError):
            render_comparison(replace(comparison("a" * 16), limits=limits))


def test_a_comparison_names_its_treatment_label_and_the_levers_it_verified() -> None:
    from evaluate import report

    # The mirrored vocabularies are those of the treatment diff.
    assert {one.value for one in publisher.Lever} == {one.value for one in report.Lever}
    others = {lever.value for lever in report.Lever if lever not in report.FLAGS}
    assert {one.value for one in publisher.Label} == {"repeat", "combined", "flags_only"} | others
    flags_only = replace(
        comparison("a" * 16), label=publisher.Label.FLAGS_ONLY, verified=(publisher.Lever.LAUNCH_SETTINGS,)
    )
    assert "- Treatment: `flags_only`; verified levers: `launch_settings`" in render_comparison(flags_only)
    for refused in ({"label": "flags only"}, {"verified": ("launch_settings",)}, {"label": None, "verified": ("x",)}):
        with pytest.raises(PublishError):
            render_comparison(replace(flags_only, **refused))


def test_a_rejected_question_or_a_scan_finding_sends_nothing(
    tmp_path: Path, fake: FakeGitHub, driver: Publisher
) -> None:
    journal = Journal.create(tmp_path)
    for question in ("Kept? hygiene: allow account-id (no)", "Ask @someone", "Read https://example.com"):
        with pytest.raises(PublishError) as raised:
            driver.open_experiment(journal, replace(EXPERIMENT, question=question))
        assert raised.value.reason is Refusal.FIELD_REJECTED

    resource = "vpc-" + "0123456789abcdef0"  # what the real scanner finds, built here
    for scanned, question in (
        (driver, f"Does {resource} hold?"),
        (Publisher(GitHub(fake, REPO), scan=lambda _t, _p: ["x"]), None),
    ):
        with pytest.raises(PublishError) as raised:
            scanned.open_experiment(journal, EXPERIMENT if question is None else replace(EXPERIMENT, question=question))
        assert raised.value.reason is Refusal.HYGIENE_FINDING
    assert (fake.writes(), journal.intents) == ([], ())

    driver.open_experiment(journal, EXPERIMENT)
    comments_found = Publisher(GitHub(fake, REPO), scan=lambda text, _path: ["x"] if text.startswith("### Run") else [])
    with pytest.raises(PublishError) as raised:
        comments_found.publish_run(journal, run(journal.run_id))
    assert raised.value.reason is Refusal.HYGIENE_FINDING
    assert (len(fake.writes()), deliveries(journal)) == (1, [])  # the issue, and nothing else


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
    resumed = Publisher(GitHub(fake, REPO))  # a new process: publish-only
    assert resume_publication(tmp_path, journal.run_id, resumed) == publisher.Outbox(number, 2, 0)
    run_comment, compared = fake.comments
    assert run_comment["body"] == pending.payload["body"] + "\n" + marker_line(pending.marker) + "\n"
    assert compared["body"] == queued.payload["body"] + "\n" + marker_line(queued.marker) + "\n"
    # Publish-only: the driver's identity and the experiment's issue, and nothing else.
    assert {path.partition("?")[0] for _method, path in fake.log} <= {
        "/user",
        f"/repos/{REPO}/issues/{number}/comments",
    }


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
