"""Where the results of an evaluation persist: one GitHub issue per experiment, a terminal comment
per run and a final comment per paired comparison (docs/design/agent-session-evaluation.md,
"Where results persist").

**What is published.** Only the fields of `Experiment`, `RunResult` and `Comparison`, each checked
before anything is rendered: a fixed vocabulary (`Ending`, `Reason`, `Verdict`, `Cleanup`,
`Relative` and the shapes, harnesses and profiles of `evaluate.contracts`), a count, a measured
number as it was measured, a digest, a commit, or the permalink of a reviewed bundle on GitHub. A
configuration is the public serving settings of `contracts.Settings`, each held to its own rule
(an image digest, a GPU's public name, ...), and the floors are the suite's `contracts.Objectives`,
signed where the suite's are. The experiment's question is the one free text, on one line and
without a marker or a mention. A field that is none of these refuses the whole record
(`Refusal.FIELD_REJECTED`) before anything is queued or sent. A completed run accounts for every
planned cell; a run that failed or was interrupted is published with its completed cells and the
fixed reason it ended for, its absent measurements read as not measured. Booking times, schedules,
identifiers of resources, tool output and prompts are not fields, so nothing renders them.

**Scanned before each write.** The public hygiene scanner (`tools/hygiene/scan.py`, loaded from
its file and never changed) reads every text a write sends, exactly as it goes into the request:
the issue's title and body, a comment's body with its marker. A finding refuses the write
(`Refusal.HYGIENE_FINDING`) and nothing is sent; a rendered comment is also scanned before it is
queued.

**The record first.** `open_experiment` creates the experiment's issue (a `CREATE_ISSUE` intent
of the journal, saved before it is sent, with the experiment's identity, the repository and the
driver's login) or selects the one the operator named: an issue, not a pull request, written by
the driver, whose body carries the identity of the same experiment. Only then is its number
journaled, and the journal refuses a booking until it is. If GitHub does not confirm it,
`Refusal.ISSUE_UNCONFIRMED` stops the run before it books. A run is bound to one experiment: once
its journal holds an issue, or a creation in doubt, another experiment is refused
(`Refusal.ISSUE_MISMATCH`). A creation saved while the driver's login was not known was never
sent: it is ruled out and saved again with the login before anything is sent.

**Comments in the journal.** `publish_run` queues the run's terminal comment and
`publish_comparison` the final comment of each pair the run is the candidate of, each a `DELIVER`
intent that holds the rendered body and its marker before anything is posted. A run resumed in a
later booking posts one comment for each invocation. A second comment for the same invocation of a
run, or the same pair, is refused (`Refusal.ALREADY_QUEUED`). They are delivered one at a time, in the order they were
queued, so a comparison ready while its run's comment is still in doubt is saved whole and posted
after it. A delivery that GitHub does not answer keeps its intent pending: the bundle stays where it
is, and the result counts it as waiting, apart from the run's verdicts.

**One destination.** Every write and every reconciliation of a run goes to the repository and as
the writer its journal saved with the issue. A publisher for another repository, or with another
login, sends nothing for that run (`Refusal.DESTINATION_MISMATCH`). A delivery whose saved payload
is not a comment this module queued is not sent either (`Refusal.UNRECOGNIZED_ENTRY`): it stays in
the journal, private, as it was.

**Reconciling.** A write whose answer is lost (no response, a cut response, or a server error) may
have happened.
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

import dataclasses
import hashlib
import http.client
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

from evaluate import contracts
from evaluate.contracts import ContractError, Harness, Objectives, Profile, Settings, Shape
from evaluate.journal import Action, Intent, IntentStatus, Journal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

HYGIENE_SCANNER = Path(__file__).resolve().parents[1] / "hygiene" / "scan.py"
PAGE_SIZE = 100

_LABEL = re.compile(r"[a-z0-9][a-z0-9._:+-]{0,63}")
_SETTINGS = tuple(one.name for one in dataclasses.fields(Settings))
_FLOORS = tuple(one.name for one in dataclasses.fields(Objectives))
_MAX_SEED = 2**32 - 1
_RUN_ID = re.compile(r"[0-9a-f]{16}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_PERMALINK = re.compile(
    r"https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/blob/[0-9a-f]{40}/[A-Za-z0-9._/-]{1,200}"
)
# What the question may not hold: a scanner exception, a marker, a mention, a link, markup.
_UNSAFE = re.compile(r"hygiene:|<!--|-->|@|://|[`<>\[\]|]", re.IGNORECASE)
_MAX_QUESTION = 300
_MAX_COUNT = contracts.MAX_COUNT


class Refusal(StrEnum):
    """Why the publisher declined: the whole vocabulary."""

    FIELD_REJECTED = "field-rejected"
    HYGIENE_FINDING = "hygiene-finding"
    # GitHub did not confirm the experiment's issue: nothing may be booked.
    ISSUE_UNCONFIRMED = "issue-unconfirmed"
    # The selected issue is not the driver's record of this experiment.
    ISSUE_MISMATCH = "issue-mismatch"
    # The run's comment, or the comparison of the same pair, is already queued.
    ALREADY_QUEUED = "already-queued"
    # The publisher writes to another repository, or as another login, than the run's record.
    DESTINATION_MISMATCH = "destination-mismatch"
    # A delivery in the journal is not a comment this module queued: it is not sent.
    UNRECOGNIZED_ENTRY = "unrecognized-entry"


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
    # Each target (`reference`, `candidate`, ...) and its requested public settings: a
    # `contracts.Settings`, or a mapping of some of its fields.
    targets: Mapping[str, Settings | Mapping[str, object]]
    seeds: Sequence[int]
    # The suite's objectives: a `contracts.Objectives`, or a mapping of all its fields.
    floors: Objectives | Mapping[str, float]


@dataclass(frozen=True)
class RunResult:
    run_id: str
    ending: Ending
    cells_completed: int
    cells_planned: int
    observed: Settings | Mapping[str, object]
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
    ttft_p95_ms: float | None = None
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


def _amount(value: object, low: float = 0.0, high: float = _MAX_COUNT) -> int | float:
    """A measured number, as it was given: never rounded."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _reject()
    if not math.isfinite(value) or not low <= value <= high:
        raise _reject()
    return value


def _part(passed: object, out_of: object) -> str:
    if _count(passed) > _count(out_of):
        raise _reject()
    return f"{passed}/{out_of}"


def _settings(values: object) -> dict[str, object]:
    """The declared public settings, each held to its own rule by `contracts.Settings`."""
    if isinstance(values, Settings):
        checked = values
    else:
        if not isinstance(values, Mapping) or not set(values) <= set(_SETTINGS):
            raise _reject()
        try:
            checked = Settings.from_json({name: values.get(name) for name in _SETTINGS}, "settings")
        except ContractError as error:
            raise _reject() from error
    return {name: value for name, value in checked.to_json().items() if value is not None}


def _floors(values: object) -> dict[str, int | float]:
    """The suite's objectives, each held to its own bounds by `contracts.Objectives`, as given."""
    if not isinstance(values, Objectives):
        if not isinstance(values, Mapping):
            raise _reject()
        try:
            Objectives.from_json(dict(values), "floors")
        except ContractError as error:
            raise _reject() from error
    given = values.to_json() if isinstance(values, Objectives) else values
    return {name: _amount(given[name], -_MAX_COUNT) for name in _FLOORS}


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
    # 5 and 5.0 are one floor: the identity reads each as a number.
    fields["floors"] = {name: float(value) + 0.0 for name, value in fields["floors"].items()}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _experiment_fields(experiment: Experiment) -> dict[str, Any]:
    targets, seeds = experiment.targets, experiment.seeds
    if not isinstance(targets, Mapping) or not isinstance(seeds, list | tuple):
        raise _reject()
    fields = {
        "question": _question(experiment.question),
        "source_commit": _matches(_COMMIT, experiment.source_commit),
        "suite": _matches(_LABEL, experiment.suite),
        "profile": str(_member(Profile, experiment.profile)),
        "workload": _matches(_DIGEST, experiment.workload),
        "targets": {_matches(_LABEL, name): _settings(settings) for name, settings in targets.items()},
        "seeds": [_seed(seed) for seed in seeds],
        "floors": _floors(experiment.floors),
    }
    if not fields["targets"] or not fields["seeds"]:
        raise _reject()
    return fields


def _seed(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SEED:
        raise _reject()
    return value


def _shown(value: object) -> str:
    """A checked value as it is written: a label as it is, a number as it was given."""
    return value if isinstance(value, str) else json.dumps(value)


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
        *(f"- `{name}`: {_settings_line(settings)}" for name, settings in sorted(fields["targets"].items())),
        "",
        "**Acceptance floors**",
        "",
        *(f"- `{name}`: {_shown(value)}" for name, value in sorted(fields["floors"].items())),
        "",
        _identity_line(experiment_identity(experiment)),
        "",
        "An evaluation record, outside the implementation backlog.",
    ]
    return f"Evaluation: {fields['question']}"[:256], "\n".join(lines) + "\n"


def _settings_line(settings: Mapping[str, object]) -> str:
    return ", ".join(f"{key}=`{_shown(value)}`" for key, value in sorted(settings.items())) or "none"


def _measured(value: str | None) -> str:
    return Verdict.NOT_MEASURED.value if value is None else value


def render_run(result: RunResult) -> str:
    """The run's terminal comment, from its checked fields."""
    ending = _member(Ending, result.ending)
    if (ending is Ending.COMPLETED) != (result.reason is None):
        raise _reject()  # a partial run has a fixed reason, and a completed one none
    completed = _part(result.cells_completed, result.cells_planned)
    if ending is Ending.COMPLETED and not 0 < result.cells_completed == result.cells_planned:
        raise _reject()  # a completed profile accounts for every planned cell
    if not isinstance(result.tasks, Mapping):
        raise _reject()
    ttft = None if result.ttft_p95_ms is None else f"{_shown(_amount(result.ttft_p95_ms))} ms"
    speeds = (result.speed_p50, result.speed_p10)
    speed = None if None in speeds else " / ".join(_shown(_amount(one)) for one in speeds) + " tokens/s"
    lines = [
        f"### Run `{_matches(_RUN_ID, result.run_id)}`: {ending}",
        "",
        *([] if result.reason is None else [f"- Reason: `{_member(Reason, result.reason)}`"]),
        f"- Completed cells: {completed}",
        f"- Observed configuration: {_settings_line(_settings(result.observed))}",
        f"- Hosts: {_count(result.requested_hosts)} requested, {_count(result.served_hosts)} served",
        f"- Capacity by shape: {_by_shape(result.capacity, Verdict)}",
        f"- Quality by shape: {_by_shape(result.quality, Verdict)}",
        f"- p95 first token: {_measured(ttft)}",
        f"- Stream speed p50 / p10: {_measured(speed)}",
        f"- Failures / attempts: {_part(result.failures, result.attempts)}",
        "- Task passes / attempts: "
        + (
            ", ".join(
                f"{_member(Harness, harness)} {_part(*counts)}" for harness, counts in sorted(result.tasks.items())
            )
            or "none"
        ),
        f"- Estimated host-hours: {_shown(_amount(result.host_hours))}",
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
        except (OSError, ValueError, http.client.HTTPException) as error:
            # No response, or one cut short (`IncompleteRead`) or unreadable: it may have happened.
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
            listed = self._call(
                "GET", f"{path}?{urllib.parse.urlencode({**query, 'per_page': PAGE_SIZE, 'page': page})}"
            )
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

    def _driver(self) -> str:
        """The driver's login, or `Refusal.ISSUE_UNCONFIRMED` when GitHub does not say it."""
        try:
            return self.github.login()
        except (Unanswered, Rejected) as error:
            raise PublishError(Refusal.ISSUE_UNCONFIRMED) from error

    def _same_destination(self, record: Mapping[str, Any] | None) -> None:
        """Refuses a publisher that is not the run's: another repository, or another login. A
        login GitHub does not give raises `Unanswered` or `Rejected`."""
        if record is None or record.get("repository") != self.github.repository:
            raise PublishError(Refusal.DESTINATION_MISMATCH)
        if record.get("driver") != self.github.login():
            raise PublishError(Refusal.DESTINATION_MISMATCH)

    # --- The experiment's issue ---

    def open_experiment(self, journal: Journal, experiment: Experiment, issue: int | None = None) -> int:
        """The number of the experiment's issue, journaled: created with `issue=None`, else the
        selected one once it shows to be the driver's record of this experiment. A run whose
        journal already holds an issue, or a creation in doubt, goes on with it only for the
        same experiment, in the same repository, as the same driver."""
        title, body = render_issue(experiment)
        identity = experiment_identity(experiment)
        if issue is not None and (type(issue) is not int or not 0 < issue <= _MAX_COUNT):
            raise _reject()
        held = _held(journal)
        if held is not None:
            record, creation = held
            if record.get("identity") != identity or issue not in (None, journal.issue_number):
                raise PublishError(Refusal.ISSUE_MISMATCH)
            if journal.issue_number is not None:
                try:
                    self._same_destination(record)
                except (Unanswered, Rejected) as error:
                    raise PublishError(Refusal.ISSUE_UNCONFIRMED) from error
                return journal.issue_number
            assert creation is not None  # no number yet: the creation is in doubt
            return self._settle_issue(journal, creation, sent_before=True)
        if journal.issue_number is not None:
            raise PublishError(Refusal.ISSUE_MISMATCH)  # journaled without a record of what it is
        if issue is not None:
            return self._select(journal, identity, issue)
        self._checked(title, body)
        try:
            driver: str | None = self.github.login()
        except (Unanswered, Rejected):
            driver = None  # saved unsent: the run stays bound to this experiment
        payload = {"title": title, "body": body, "identity": identity, "repository": self.github.repository}
        intent = journal.intend(Action.CREATE_ISSUE, {**payload, "driver": driver})
        if driver is None:
            raise PublishError(Refusal.ISSUE_UNCONFIRMED)
        return self._settle_issue(journal, intent, sent_before=False)

    def _select(self, journal: Journal, identity: str, number: int) -> int:
        try:
            found, driver = self.github.issue(number), self.github.login()
        except (Unanswered, Rejected) as error:
            raise PublishError(Refusal.ISSUE_UNCONFIRMED) from error
        mine = isinstance(found, dict) and "pull_request" not in found and _author(found) == driver
        if not mine or _identity_line(identity) not in str(found.get("body")).splitlines():
            raise PublishError(Refusal.ISSUE_MISMATCH)
        journal.adopt_issue(number, {"identity": identity, "repository": self.github.repository, "driver": driver})
        return number

    def _settle_issue(self, journal: Journal, intent: Intent, *, sent_before: bool) -> int:
        payload = intent.payload
        if payload.get("repository") != self.github.repository:
            raise PublishError(Refusal.DESTINATION_MISMATCH)
        driver = self._driver()
        if payload.get("driver") is None:
            # Saved before the driver was known, so never sent: sent now, as this driver.
            intent = journal.supersede(intent.ordinal, {**payload, "driver": driver})
            sent_before = False
        elif payload["driver"] != driver:
            raise PublishError(Refusal.DESTINATION_MISMATCH)

        def find() -> dict[str, Any] | None:
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
        if _identity_line(str(payload.get("identity"))) not in str(record.get("body")).splitlines():
            # The driver's record of this creation no longer describes this experiment: it stays
            # in doubt, and nothing is booked against it.
            raise PublishError(Refusal.ISSUE_MISMATCH)
        journal.confirm(intent.ordinal, {"number": record["number"]})
        return record["number"]

    # --- Comments ---

    def publish_run(self, journal: Journal, result: RunResult, *, invocation: int = 0) -> Outbox:
        """Queues the run's terminal comment, then delivers what the journal holds. A run resumed
        in a later booking ends each `invocation` with a comment of its own: the first is 0, and
        a second comment for the same invocation is refused."""
        body = render_run(result)
        if result.run_id != journal.run_id or type(invocation) is not int or not 0 <= invocation <= _MAX_COUNT:
            raise _reject()
        return self._queue(journal, Kind.RUN, body, invocation or None)

    def publish_comparison(self, journal: Journal, comparison: Comparison) -> Outbox:
        """Queues the pair's final comment in the candidate's journal, after its run's."""
        body = render_comparison(comparison)
        if comparison.candidate_run != journal.run_id or comparison.reference_run == journal.run_id:
            raise _reject()
        return self._queue(journal, Kind.COMPARISON, body, comparison.reference_run)

    def _queue(self, journal: Journal, kind: Kind, body: str, reference: str | int | None) -> Outbox:
        """Queues one comment of each invocation of a run, and one of each pair it is the
        candidate of."""
        for one in journal.intents:
            if one.action is Action.DELIVER and one.status is not IntentStatus.RULED_OUT:
                payload = one.payload
                if (payload.get("kind"), payload.get("reference")) == (kind.value, reference):
                    raise PublishError(Refusal.ALREADY_QUEUED)
        self._checked(body)
        journal.queue(Action.DELIVER, {"kind": kind.value, "body": body, "reference": reference})
        return self.flush(journal)

    def flush(self, journal: Journal) -> Outbox:
        """Delivers the journal's comments in order: the pending one, reconciled first, then each
        queued one. It stops at the first that GitHub does not confirm, which stays pending. It
        sends nothing to another repository, or as another login, than the run's."""
        if not any(_waiting(journal, Action.DELIVER)) and not any(
            _waiting(journal, Action.DELIVER, IntentStatus.QUEUED)
        ):
            return outbox(journal)
        held = _held(journal)
        try:
            self._same_destination(None if held is None else held[0])
        except (Unanswered, Rejected):
            return outbox(journal)  # no login: nothing is sent, and everything stays
        while True:
            intent = next(_waiting(journal, Action.DELIVER, IntentStatus.PENDING), None)
            sent_before = intent is not None
            if intent is None:
                queued = next(_waiting(journal, Action.DELIVER, IntentStatus.QUEUED), None)
                if queued is None:
                    break
                _entry(queued)
                intent = journal.dequeue(queued.ordinal)
            if not self._settle_comment(journal, intent, sent_before=sent_before):
                break
        return outbox(journal)

    def _settle_comment(self, journal: Journal, intent: Intent, *, sent_before: bool) -> bool:
        number = journal.issue_number
        assert number is not None  # the journal queues no delivery before its issue
        kind, text = _entry(intent)

        def find() -> dict[str, Any] | None:
            driver = self.github.login()
            mine = (one for one in self.github.comments(number) if _author(one) == driver)
            return next((one for one in mine if _carries(one.get("body"), intent.marker)), None)

        def send() -> dict[str, Any]:
            body = _with_marker(text, intent.marker)
            self._checked(body)
            return self.github.comment(number, body)

        record = self._settle(find, send, sent_before=sent_before)
        if record is None:
            return False
        journal.confirm(intent.ordinal, {"kind": kind.value, "comment": record.get("id")})
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


_HEADINGS = {Kind.RUN: "### Run `", Kind.COMPARISON: "### Comparison: "}


def _entry(intent: Intent) -> tuple[Kind, str]:
    """The kind and the body of a delivery this module queued, or `Refusal.UNRECOGNIZED_ENTRY`
    before anything is sent: any other payload stays private, in the journal, as it is."""
    payload = intent.payload
    kind, body = payload.get("kind"), payload.get("body")
    if set(payload) != {"kind", "body", "reference"} or kind not in set(Kind) or not isinstance(body, str):
        raise PublishError(Refusal.UNRECOGNIZED_ENTRY)
    if not body.startswith(_HEADINGS[Kind(kind)]):
        raise PublishError(Refusal.UNRECOGNIZED_ENTRY)
    return Kind(kind), body


def _held(journal: Journal) -> tuple[dict[str, Any], Intent | None] | None:
    """What the run's journal holds about its issue: the payload of its creation (confirmed, or
    in doubt) with that intent, or the record of the issue it adopted."""
    creations = [one for one in journal.intents if one.action is Action.CREATE_ISSUE]
    creation = next((one for one in reversed(creations) if one.status is not IntentStatus.RULED_OUT), None)
    if creation is not None:
        return creation.payload, creation
    record = journal.issue_record
    return None if record is None else (record, None)


def _waiting(journal: Journal, action: Action, status: IntentStatus = IntentStatus.PENDING) -> Iterator[Intent]:
    return (one for one in journal.intents if one.action is action and one.status is status)


def outbox(journal: Journal) -> Outbox:
    deliveries = [one for one in journal.intents if one.action is Action.DELIVER]
    delivered = sum(one.status is IntentStatus.CONFIRMED for one in deliveries)
    waiting = sum(one.status in (IntentStatus.PENDING, IntentStatus.QUEUED) for one in deliveries)
    return Outbox(journal.issue_number, delivered, waiting)


def resume_publication(root: Path, run_id: str, publisher: Publisher) -> Outbox:
    """Publish-only resume: the run's pending issue creation, then its pending and queued
    comments, from the journal alone. It measures nothing and sends nothing but to GitHub, in
    the run's repository and as the run's driver."""
    with Journal.open(root, run_id) as journal:
        creation = next(_waiting(journal, Action.CREATE_ISSUE), None)
        if creation is not None:
            publisher._settle_issue(journal, creation, sent_before=True)
        return publisher.flush(journal)
