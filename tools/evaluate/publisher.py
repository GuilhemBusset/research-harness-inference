"""Where the results of an evaluation persist: one GitHub issue per experiment, a terminal comment
per run and a final comment per paired comparison (docs/design/agent-session-evaluation.md,
"Where results persist").

**What is published.** Only the fields of `Experiment`, `RunResult` and `Comparison`, each checked
before anything is rendered: a fixed vocabulary (`Ending`, `Reason`, `Verdict`, `Cleanup`,
`Relative` and the shapes, harnesses and profiles of `evaluate.contracts`), a bounded label, a
count, a relative duration, a digest, a commit, or the permalink of a reviewed bundle on GitHub.
The experiment's question is the one free text, on one line and without a marker or a mention. A
field that is none of these refuses the whole record (`Refusal.FIELD_REJECTED`) before anything is
queued or sent. A run that failed or was interrupted is published with its completed cells and the
fixed reason it ended for; its absent measurements read as not measured. Booking times, schedules,
identifiers of resources, tool output and prompts are not fields, so nothing renders them.

**Scanned before each write.** The public hygiene scanner (`tools/hygiene/scan.py`, loaded from
its file and never changed) reads every text a write sends, exactly as it goes into the request:
the issue's title and body, a comment's body with its marker. A finding refuses the write
(`Refusal.HYGIENE_FINDING`) and nothing is sent; a rendered comment is also scanned before it is
queued.

**The record first.** `open_experiment` creates the experiment's issue (a `CREATE_ISSUE` intent
of the journal, saved before it is sent) or selects the one the operator named: an issue, not a
pull request, written by the driver, whose body carries the identity of the same experiment. Only
then is its number journaled, and the journal refuses a booking until it is. If GitHub does not
confirm it, `Refusal.ISSUE_UNCONFIRMED` stops the run before it books.

**Comments in the journal.** `publish_run` queues the run's terminal comment and
`publish_comparison` the paired comparison's, each a `DELIVER` intent that holds the rendered body
and its marker before anything is posted. They are delivered one at a time, in the order they were
queued, so a comparison ready while its run's comment is still in doubt is saved whole and posted
after it. A delivery that GitHub does not answer keeps its intent pending: the bundle stays where it
is, and the result counts it as waiting, apart from the run's verdicts.

**Reconciling.** A write whose answer is lost (no response, or a server error) may have happened.
The intent's marker is a line of what was sent. The publisher then reads the driver's own records,
the issues the driver created or the comments on the experiment's issue, and confirms the first one
written by the driver whose text carries that marker line. A record of anyone else with the same
marker is not the driver's and is never confirmed. If none of the driver's carries it, the same
intent, with the same marker and the same body, is sent again by the next flush or resume: the
retry and its own lost answer are reconciled against that one marker, so the run ends with one
record of each kind.

**Publish-only resume.** `resume_publication` opens a run's journal and replays what is pending or
queued in it: the issue's creation, then the deliveries. This module needs the standard library,
`evaluate.journal` and the scanner's file: no pool, no cloud client, no load generator, nothing
that sends to the endpoint. A refusal is one member of `Refusal`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from evaluate.contracts import Harness, Profile, Shape
from evaluate.journal import Action, Intent, IntentStatus, Journal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

HYGIENE_SCANNER = Path(__file__).resolve().parents[1] / "hygiene" / "scan.py"
PAGE_SIZE = 100

_LABEL = re.compile(r"[a-z0-9][a-z0-9._:+-]{0,63}")
_RUN_ID = re.compile(r"[0-9a-f]{16}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_PERMALINK = re.compile(r"https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/blob/[0-9a-f]{40}/[A-Za-z0-9._/-]{1,200}")
# What the question may not hold: a scanner exception, a marker, a mention, a link, markup.
_UNSAFE = re.compile(r"hygiene:|<!--|-->|@|://|[`<>\[\]|]", re.IGNORECASE)
_MAX_QUESTION = 300
_MAX_COUNT = 10**9


class Refusal(StrEnum):
    """Why the publisher declined: the whole vocabulary."""

    FIELD_REJECTED = "field-rejected"
    HYGIENE_FINDING = "hygiene-finding"
    # GitHub did not confirm the experiment's issue: nothing may be booked.
    ISSUE_UNCONFIRMED = "issue-unconfirmed"
    # The selected issue is not the driver's record of this experiment.
    ISSUE_MISMATCH = "issue-mismatch"
    # The run's comment, or its comparison, is already queued.
    ALREADY_QUEUED = "already-queued"


class PublishError(Exception):
    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Ending(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class Reason(StrEnum):
    """Why a run ended without its whole profile."""

    POOL_ALREADY_BOOKED = "pool-already-booked"
    CAPACITY_UNAVAILABLE = "capacity-unavailable"
    BOOKING_CHANGED = "booking-changed"
    WINDOW_EXHAUSTED = "window-exhausted"
    OPERATOR_CANCELLED = "operator-cancelled"
    DRIVER_INTERRUPTED = "driver-interrupted"
    DRIVER_ERROR = "driver-error"
    INVALID_EVIDENCE = "invalid-evidence"


class Verdict(StrEnum):
    PASS = "pass"
    MEASURED_MISS = "measured-miss"
    INCONCLUSIVE = "inconclusive"
    NOT_MEASURED = "not-measured"


class Relative(StrEnum):
    """A candidate against its reference."""

    BETTER = "candidate-better"
    NO_DIFFERENCE = "no-difference"
    WORSE = "candidate-worse"
    INCONCLUSIVE = "inconclusive"


class Cleanup(StrEnum):
    """What became of the run's booking: the values of `evaluate.booking.Cleanup`, which the
    publish-only path does not import."""

    HELD = "held"
    RELEASED = "released"
    YIELDED = "yielded"
    ENDED = "ended"
    NOT_OWNED = "not-owned"
    UNCONFIRMED = "unconfirmed"


class Kind(StrEnum):
    RUN = "run"
    COMPARISON = "comparison"


@dataclass(frozen=True)
class Experiment:
    question: str
    source_commit: str
    suite: str
    profile: Profile
    workload: str
    # Each target (`reference`, `candidate`, ...) and its requested settings.
    targets: Mapping[str, Mapping[str, str]]
    seeds: Sequence[int]
    # Each floor and its value, in the unit its name says.
    floors: Mapping[str, float]


@dataclass(frozen=True)
class RunResult:
    run_id: str
    ending: Ending
    cells_completed: int
    cells_planned: int
    observed: Mapping[str, str]
    requested_hosts: int
    served_hosts: int
    capacity: Mapping[Shape, Verdict]
    quality: Mapping[Shape, Verdict]
    failures: int
    attempts: int
    # Each harness: its passed tasks and its attempted tasks.
    tasks: Mapping[Harness, tuple[int, int]]
    host_hours: float
    cleanup: Cleanup
    bundle_digest: str
    reason: Reason | None = None
    ttft_p95_ms: int | None = None
    speed_p50: float | None = None
    speed_p10: float | None = None
    permalink: str | None = None


@dataclass(frozen=True)
class Comparison:
    reference_run: str
    candidate_run: str
    capacity: Mapping[Shape, Relative]
    quality: Mapping[Shape, Relative]
    bundle_digest: str
    permalink: str | None = None


@dataclass(frozen=True)
class Outbox:
    """Where a run's publication stands: its issue, the comments confirmed, and those waiting
    (pending or queued), which a resume delivers."""

    issue: int | None
    delivered: int
    waiting: int


# --- Checking the fields ---------------------------------------------------------------------


def _reject() -> PublishError:
    return PublishError(Refusal.FIELD_REJECTED)


def _matches(pattern: re.Pattern[str], value: object) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _reject()
    return value


def _member[E: StrEnum](vocabulary: type[E], value: object) -> E:
    if not isinstance(value, vocabulary):
        raise _reject()
    return value


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise _reject()
    return value


def _amount(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise _reject()
    return float(value)


def _part(passed: object, out_of: object) -> str:
    if _count(passed) > _count(out_of):
        raise _reject()
    return f"{passed}/{out_of}"


def _labels(values: object) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise _reject()
    return {_matches(_LABEL, key): _matches(_LABEL, value) for key, value in values.items()}


def _by_shape[E: StrEnum](values: object, vocabulary: type[E]) -> str:
    if not isinstance(values, Mapping):
        raise _reject()
    shown = [f"{_member(Shape, shape)} {_member(vocabulary, verdict)}" for shape, verdict in sorted(values.items())]
    return ", ".join(shown) or "none"


def _question(value: object) -> str:
    if not isinstance(value, str) or not value.isprintable() or _UNSAFE.search(value):
        raise _reject()
    if not 0 < len(value.strip()) <= _MAX_QUESTION:
        raise _reject()
    return value.strip()


def _permalink(value: object) -> str | None:
    return None if value is None else _matches(_PERMALINK, value)


# --- Rendering -------------------------------------------------------------------------------


def experiment_identity(experiment: Experiment) -> str:
    """The digest of the experiment's public fields: what a selected issue must carry."""
    fields = _experiment_fields(experiment)
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _experiment_fields(experiment: Experiment) -> dict[str, Any]:
    targets, floors, seeds = experiment.targets, experiment.floors, experiment.seeds
    if not isinstance(targets, Mapping) or not isinstance(floors, Mapping) or not isinstance(seeds, list | tuple):
        raise _reject()
    fields = {
        "question": _question(experiment.question),
        "source_commit": _matches(_COMMIT, experiment.source_commit),
        "suite": _matches(_LABEL, experiment.suite),
        "profile": str(_member(Profile, experiment.profile)),
        "workload": _matches(_DIGEST, experiment.workload),
        "targets": {_matches(_LABEL, name): _labels(settings) for name, settings in targets.items()},
        "seeds": [_count(seed) for seed in seeds],
        "floors": {_matches(_LABEL, name): _amount(value) for name, value in floors.items()},
    }
    if not fields["targets"] or not fields["floors"] or not fields["seeds"]:
        raise _reject()
    return fields


def _identity_line(identity: str) -> str:
    return f"Experiment identity: `{identity}`"


def render_issue(experiment: Experiment) -> tuple[str, str]:
    """The issue's title and body, from the experiment's checked fields."""
    fields = _experiment_fields(experiment)
    lines = [
        f"**Question:** {fields['question']}",
        "",
        f"- Source commit: `{fields['source_commit']}`",
        f"- Suite / profile: `{fields['suite']}` / `{fields['profile']}`",
        f"- Workload: `{fields['workload']}`",
        f"- Seeds: {', '.join(str(seed) for seed in fields['seeds'])}",
        "",
        "**Target configurations**",
        "",
        *(f"- `{name}`: {_settings(settings)}" for name, settings in sorted(fields["targets"].items())),
        "",
        "**Acceptance floors**",
        "",
        *(f"- `{name}`: {value:g}" for name, value in sorted(fields["floors"].items())),
        "",
        _identity_line(experiment_identity(experiment)),
        "",
        "An evaluation record, outside the implementation backlog.",
    ]
    return f"Evaluation: {fields['question']}"[:256], "\n".join(lines) + "\n"


def _settings(settings: Mapping[str, str]) -> str:
    return ", ".join(f"{key}=`{value}`" for key, value in sorted(settings.items())) or "none"


def _measured(value: str | None) -> str:
    return Verdict.NOT_MEASURED.value if value is None else value


def render_run(result: RunResult) -> str:
    """The run's terminal comment, from its checked fields."""
    ending = _member(Ending, result.ending)
    if (ending is Ending.COMPLETED) != (result.reason is None):
        raise _reject()  # a partial run has a fixed reason, and a completed one none
    if not isinstance(result.tasks, Mapping):
        raise _reject()
    ttft = None if result.ttft_p95_ms is None else f"{_count(result.ttft_p95_ms)} ms"
    speeds = (result.speed_p50, result.speed_p10)
    speed = None if None in speeds else " / ".join(f"{_amount(one):.1f}" for one in speeds) + " tokens/s"
    lines = [
        f"### Run `{_matches(_RUN_ID, result.run_id)}`: {ending}",
        "",
        *([] if result.reason is None else [f"- Reason: `{_member(Reason, result.reason)}`"]),
        f"- Completed cells: {_part(result.cells_completed, result.cells_planned)}",
        f"- Observed configuration: {_settings(_labels(result.observed))}",
        f"- Hosts: {_count(result.requested_hosts)} requested, {_count(result.served_hosts)} served",
        f"- Capacity by shape: {_by_shape(result.capacity, Verdict)}",
        f"- Quality by shape: {_by_shape(result.quality, Verdict)}",
        f"- p95 first token: {_measured(ttft)}",
        f"- Stream speed p50 / p10: {_measured(speed)}",
        f"- Failures / attempts: {_part(result.failures, result.attempts)}",
        "- Task passes / attempts: "
        + (", ".join(f"{_member(Harness, harness)} {_part(*counts)}" for harness, counts in sorted(result.tasks.items())) or "none"),
        f"- Estimated host-hours: {_amount(result.host_hours):.2f}",
        f"- Cleanup: `{_member(Cleanup, result.cleanup)}`",
        *_bundle(result.bundle_digest, result.permalink),
    ]
    return "\n".join(lines) + "\n"


def render_comparison(comparison: Comparison) -> str:
    """The paired comparison's final comment, from its checked fields."""
    lines = [
        (
            f"### Comparison: candidate `{_matches(_RUN_ID, comparison.candidate_run)}`"
            f" against reference `{_matches(_RUN_ID, comparison.reference_run)}`"
        ),
        "",
        f"- Capacity by shape: {_by_shape(comparison.capacity, Relative)}",
        f"- Quality by shape: {_by_shape(comparison.quality, Relative)}",
        *_bundle(comparison.bundle_digest, comparison.permalink),
    ]
    return "\n".join(lines) + "\n"


def _bundle(digest: str, permalink: str | None) -> list[str]:
    link = _permalink(permalink)
    return [
        f"- Bundle digest: `{_matches(_DIGEST, digest)}`",
        *([] if link is None else [f"- Reviewed bundle: {link}"]),
    ]


def marker_line(marker: str) -> str:
    """The line of a sent text that names its intent."""
    return f"<!-- {marker} -->"


def _with_marker(body: str, marker: str) -> str:
    return f"{body}\n{marker_line(marker)}\n"


def _carries(text: object, marker: str) -> bool:
    return isinstance(text, str) and marker_line(marker) in text.splitlines()


# --- GitHub ----------------------------------------------------------------------------------


class Unanswered(Exception):
    """No answer that says what happened: the request may or may not have been applied."""


class Rejected(Exception):
    """GitHub declined the request: nothing was applied."""


class Transport(Protocol):
    def __call__(self, method: str, path: str, body: Mapping[str, object] | None) -> tuple[int, Any]:
        """Sends one request of GitHub's REST API and gives its status and decoded JSON. Raises
        `Unanswered` when no response came."""
        ...


@dataclass
class HttpTransport:
    """GitHub's REST API over HTTPS, as the driver's identity (its token)."""

    token: str = field(repr=False)
    base: str = "https://api.github.com"
    timeout: float = 30.0

    def __call__(self, method: str, path: str, body: Mapping[str, object] | None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(dict(body)).encode()
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}"}
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, None
        except (OSError, ValueError) as error:
            raise Unanswered from error


class GitHub:
    """The REST calls the publisher makes, in one repository, as one identity."""

    def __init__(self, transport: Transport, repository: str) -> None:
        self._send = transport
        self.repository = _matches(_REPOSITORY, repository)
        self._login: str | None = None

    def _call(self, method: str, path: str, body: Mapping[str, object] | None = None) -> Any:
        status, answer = self._send(method, path, body)
        if status >= 500:
            raise Unanswered
        if not 200 <= status < 300:
            raise Rejected
        return answer

    def login(self) -> str:
        """The driver's identity, which writes and owns its records."""
        if self._login is None:
            answer = self._call("GET", "/user")
            if not isinstance(answer, dict) or not isinstance(answer.get("login"), str):
                raise Unanswered
            self._login = answer["login"]
        return self._login

    def _pages(self, path: str, query: Mapping[str, object]) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            listed = self._call("GET", f"{path}?{urllib.parse.urlencode({**query, 'per_page': PAGE_SIZE, 'page': page})}")
            if not isinstance(listed, list):
                raise Unanswered
            yield from (one for one in listed if isinstance(one, dict))
            if len(listed) < PAGE_SIZE:
                return
            page += 1

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        return self._call("POST", f"/repos/{self.repository}/issues", {"title": title, "body": body})

    def issue(self, number: int) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.repository}/issues/{number}")

    def issues_by(self, login: str) -> Iterator[dict[str, Any]]:
        return self._pages(f"/repos/{self.repository}/issues", {"creator": login, "state": "all"})

    def comment(self, number: int, body: str) -> dict[str, Any]:
        return self._call("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body})

    def comments(self, number: int) -> Iterator[dict[str, Any]]:
        return self._pages(f"/repos/{self.repository}/issues/{number}/comments", {})


def _author(record: Mapping[str, Any]) -> object:
    user = record.get("user")
    return user.get("login") if isinstance(user, dict) else None


# --- Publishing ------------------------------------------------------------------------------

type Scan = Callable[[str, str], Sequence[object]]


def hygiene_scan() -> Scan:
    """The public hygiene scanner's `scan_text`, loaded from its file and never changed."""
    name = "_evaluate_hygiene_scan"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, HYGIENE_SCANNER)
        if spec is None or spec.loader is None:
            raise ImportError("the hygiene scanner")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module.scan_text


class Publisher:
    def __init__(self, github: GitHub, scan: Scan | None = None) -> None:
        self.github = github
        self._scan = hygiene_scan() if scan is None else scan

    def _checked(self, *texts: str) -> None:
        """Refuses texts the public scanner finds anything in. Called on the exact text of a
        write, right before it is sent."""
        if any(self._scan(text, "evaluate-publication.md") for text in texts):
            raise PublishError(Refusal.HYGIENE_FINDING)

    # --- The experiment's issue ---

    def open_experiment(self, journal: Journal, experiment: Experiment, issue: int | None = None) -> int:
        """The number of the experiment's issue, journaled: created with `issue=None`, else the
        selected one once it shows to be the driver's record of this experiment."""
        title, body = render_issue(experiment)
        if issue is not None:
            return self._select(journal, experiment, issue)
        if journal.issue_number is not None:
            return journal.issue_number
        intent = next((one for one in _waiting(journal, Action.CREATE_ISSUE)), None)
        sent_before = intent is not None
        if intent is None:
            self._checked(title, body)
            intent = journal.intend(Action.CREATE_ISSUE, {"title": title, "body": body})
        return self._settle_issue(journal, intent, sent_before=sent_before)

    def _select(self, journal: Journal, experiment: Experiment, number: int) -> int:
        if journal.issue_number is not None:
            if journal.issue_number != number:
                raise PublishError(Refusal.ISSUE_MISMATCH)
            return number
        _count(number)
        try:
            found, driver = self.github.issue(number), self.github.login()
        except (Unanswered, Rejected) as error:
            raise PublishError(Refusal.ISSUE_UNCONFIRMED) from error
        identity = _identity_line(experiment_identity(experiment))
        mine = isinstance(found, dict) and "pull_request" not in found and _author(found) == driver
        if not mine or identity not in str(found.get("body")).splitlines():
            raise PublishError(Refusal.ISSUE_MISMATCH)
        journal.adopt_issue(number)
        return number

    def _settle_issue(self, journal: Journal, intent: Intent, *, sent_before: bool) -> int:
        def find() -> dict[str, Any] | None:
            driver = self.github.login()
            return next(
                (
                    one
                    for one in self.github.issues_by(driver)
                    if "pull_request" not in one and _author(one) == driver and _carries(one.get("body"), intent.marker)
                ),
                None,
            )

        def send() -> dict[str, Any]:
            title, body = intent.payload["title"], _with_marker(intent.payload["body"], intent.marker)
            self._checked(title, body)
            return self.github.create_issue(title, body)

        record = self._settle(find, send, sent_before=sent_before)
        if record is None or type(record.get("number")) is not int:
            raise PublishError(Refusal.ISSUE_UNCONFIRMED)
        journal.confirm(intent.ordinal, {"number": record["number"]})
        return record["number"]

    # --- Comments ---

    def publish_run(self, journal: Journal, result: RunResult) -> Outbox:
        """Queues the run's terminal comment, then delivers what the journal holds."""
        body = render_run(result)
        if result.run_id != journal.run_id:
            raise _reject()
        return self._queue(journal, Kind.RUN, body)

    def publish_comparison(self, journal: Journal, comparison: Comparison) -> Outbox:
        """Queues the comparison's final comment in the candidate's journal, after its run's."""
        body = render_comparison(comparison)
        if comparison.candidate_run != journal.run_id or comparison.reference_run == journal.run_id:
            raise _reject()
        return self._queue(journal, Kind.COMPARISON, body)

    def _queue(self, journal: Journal, kind: Kind, body: str) -> Outbox:
        if any(_kind(one) == kind for one in journal.intents if one.status is not IntentStatus.RULED_OUT):
            raise PublishError(Refusal.ALREADY_QUEUED)
        self._checked(body)
        journal.queue(Action.DELIVER, {"kind": kind.value, "body": body})
        return self.flush(journal)

    def flush(self, journal: Journal) -> Outbox:
        """Delivers the journal's comments in order: the pending one, reconciled first, then each
        queued one. It stops at the first that GitHub does not confirm, which stays pending."""
        while True:
            intent = next(_waiting(journal, Action.DELIVER, IntentStatus.PENDING), None)
            sent_before = intent is not None
            if intent is None:
                queued = next(_waiting(journal, Action.DELIVER, IntentStatus.QUEUED), None)
                if queued is None:
                    break
                intent = journal.dequeue(queued.ordinal)
            if not self._settle_comment(journal, intent, sent_before=sent_before):
                break
        return outbox(journal)

    def _settle_comment(self, journal: Journal, intent: Intent, *, sent_before: bool) -> bool:
        number = journal.issue_number
        assert number is not None  # the journal queues no delivery before its issue

        def find() -> dict[str, Any] | None:
            driver = self.github.login()
            mine = (one for one in self.github.comments(number) if _author(one) == driver)
            return next((one for one in mine if _carries(one.get("body"), intent.marker)), None)

        def send() -> dict[str, Any]:
            body = _with_marker(intent.payload["body"], intent.marker)
            self._checked(body)
            return self.github.comment(number, body)

        record = self._settle(find, send, sent_before=sent_before)
        if record is None:
            return False
        journal.confirm(intent.ordinal, {"kind": intent.payload["kind"], "comment": record.get("id")})
        return True

    @staticmethod
    def _settle(
        find: Callable[[], dict[str, Any] | None], send: Callable[[], dict[str, Any]], *, sent_before: bool
    ) -> dict[str, Any] | None:
        """The driver's record of an intent, or None while it is not confirmed. One that may have
        been sent is looked for first; one whose write got no answer is looked for after it."""
        try:
            if sent_before and (found := find()) is not None:
                return found
            try:
                sent = send()
            except Unanswered:
                return find()
            return sent if isinstance(sent, dict) else find()
        except (Unanswered, Rejected):
            return None


def _kind(intent: Intent) -> str | None:
    return intent.payload.get("kind") if intent.action is Action.DELIVER else None


def _waiting(journal: Journal, action: Action, status: IntentStatus = IntentStatus.PENDING) -> Iterator[Intent]:
    return (one for one in journal.intents if one.action is action and one.status is status)


def outbox(journal: Journal) -> Outbox:
    deliveries = [one for one in journal.intents if one.action is Action.DELIVER]
    delivered = sum(one.status is IntentStatus.CONFIRMED for one in deliveries)
    waiting = sum(one.status in (IntentStatus.PENDING, IntentStatus.QUEUED) for one in deliveries)
    return Outbox(journal.issue_number, delivered, waiting)


def resume_publication(root: Path, run_id: str, publisher: Publisher) -> Outbox:
    """Publish-only resume: the run's pending issue creation, then its pending and queued
    comments, from the journal alone. It measures nothing and sends nothing but to GitHub."""
    with Journal.open(root, run_id) as journal:
        creation = next(_waiting(journal, Action.CREATE_ISSUE), None)
        if creation is not None:
            publisher._settle_issue(journal, creation, sent_before=True)
        return publisher.flush(journal)
