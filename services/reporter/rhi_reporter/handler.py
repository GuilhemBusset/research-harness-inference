"""The reporter's function: CodePipeline's events in, commit statuses out (system design, sections
8.2, step 7, and 8.4).

The rule of `src/rhi/pipeline/reporter.py` invokes `handler` with each state change of this
pipeline's executions, stages and actions. `handle` turns one into statuses, through
`rhi_reporter.status`, adds the report of a failure, through `rhi_reporter.report`, and posts them
in that order with `rhi_reporter.github`.

**Which events post what.**

- An event of a stage the pipeline deploys (`RHI_REPORTER_STAGES`), the stage's own or one of its
  actions', is the status `status_for` gives it.
- A verification that ended in success is first asked whether it ran its tests: the step leaves
  the mark `not-run`, with its reason, when it ran none (below). With the mark, the status is a
  success whose description says that no test ran and why (`status.as_it_ran`), never
  `VERIFIED_DESCRIPTION`. The same holds for the verification of a covering execution, whose
  state a superseded commit takes. When the marks cannot be read, the function fails after
  logging `NO_NOT_RUN` and posts no such success, so that the event is invoked again: a
  verification that may have run nothing is not reported as one that passed.
- A verification that ended in success and ran its tests is then asked whether its GPU tests ran:
  the step leaves the mark `gpu-not-run` when it booked the coursework pool and no instance was
  launched during its wait. With the mark of the run that succeeded (below), the status is a
  success whose description says that the mock pool's tests passed and that the GPU tests did not
  run for lack of capacity (`status.without_gpu_tests`), never `VERIFIED_DESCRIPTION`. It is read
  with the mark `not-run`, for the same statuses, and a mark that cannot be read, or whose run
  cannot be, posts no success in the same way.
- An action of a step that runs before those stages (source, synthesis, self-update, assets)
  reports under each of them, as one of their actions, so that a failure there ends the stage's
  `deploy/<stage>` status as a failure, and its success never reads as a deployment's. The own
  events of those steps' stages post nothing: their actions say it all.
- An execution's own event, at its start and at its end, gives the statuses of the commits left
  without an execution that deploys them (`rhi_reporter.superseded`): an execution ends
  `Superseded` when a newer one overtakes it while it waits between stages, not only when the
  pipeline updates itself, and an update cancels the others. They are those this execution
  covers, and its own commit when this execution was cancelled or superseded and a later one
  covers it. They are read from the pipeline's executions, newest first, down to the first
  execution older than this one that ended with a verdict. Under each stage, such a commit is pending, naming the covering
  commit, while the covering execution runs, and gets its final state once it has ended, with
  the state of its verification when that ran.
- An event of a stage or an action of an execution that was cancelled or superseded (the
  execution ended `Cancelled` or `Superseded`, by a self-update or overtaken by a newer one, or
  the event's own state is `CANCELED`, which CodePipeline gives only when an update of the
  pipeline cancels an execution) posts none of its own: its commit's verdict is the covering
  execution's. It posts that commit's superseded statuses again, as the execution's own end does,
  so that a cancellation that arrives late, after the covering execution has ended, leaves its
  verdict as it stands.
- When the pipeline's executions cannot be read for a superseded commit, the function fails after
  logging `NO_EXECUTIONS`, so that the event is invoked again: returning would leave the commit
  pending for good.
- A pending status is not posted once the stage has ended for the same execution, once one of
  the execution's stages has stopped short (failed, stopped or cancelled), or once the execution
  itself has ended: events arrive in no guaranteed order, and a late pending status, of the stage
  or of a step before it, would hide the verdict already posted.
- A pending status of `deploy/<stage>` follows the stage's manual approvals as CodePipeline's
  records of the event's own execution show them now, not as the events tell them: an approval
  waits when the latest run of an approval action of that stage, by its start, is `InProgress`. A
  record that omits its action's type is an approval's when the event names that action as its
  approval, or when the pipeline's state shows that action's run holding an open approval request.
  While one waits, a status that would say the deployment runs says instead that it waits for the
  approval, so that a late or out-of-order event of the same execution cannot hide the wait. An
  approval's start that arrives once its own run has ended, and none waits, posts nothing, so that
  it cannot bring the wait back; the start of an approval of another stage is, for this stage, a
  step that runs. An approval of another execution changes nothing. When the records cannot be
  read, no pending status is posted, after logging `NO_APPROVALS`: the last one stays in place.

**The commit** is the source revision of the execution, read from CodePipeline: an event does not
carry it. An execution that names none yet posts nothing.

**The report of a failure** follows the failed action's own status. Its parts:

- the failed resources of the stack the failed action deploys, read from CloudFormation's events
  of that stack since the action started;
- `logical_ids`: the names of the resources of every template that the same execution's synth
  step wrote, read from its output in the pipeline's artifact store. A resource is named only
  when one of them holds it;
- for a failed verification, the action named `Verify`, the smoke tests that failed: the lines of
  the mark `failed-smoke-tests` of the execution (below). The report keeps a line only when it is
  in `smoke_tests`, the allow-list of smoke test IDs, and counts every other line as dropped, so
  nothing a test printed or held can reach a status. The allow-list is the list of
  `tests/smoke/ids.py`, which the function's construct reads at synthesis and gives in
  `RHI_SMOKE_TEST_IDS`, comma-separated (`smoke_test_ids`). No mark, or no variable, names no test;
- for a failed verification too, the last lifecycle stage each instance reached: the lines of the
  mark `last-stages` of the execution. The report keeps a line only when it is in
  `lifecycle_stages`, the allow-list of stages, and counts every other line as dropped: nothing
  else of an instance's row, and no text that is not a stage's name, reaches a status. The
  allow-list is the stages of a start, the lifecycle agent's enumeration (`rhi_lifecycle.stages`),
  and the stage serving, under which an instance in service writes its row
  (`rhi_lifecycle.serving`), which the function's construct reads at synthesis and gives in
  `RHI_LIFECYCLE_STAGES`, comma-separated (`lifecycle_stages`): a report names an instance that
  was serving. No mark names no stage, and no variable is an empty allow-list.

What cannot be read for the report leaves it out: a resource then counts as dropped, and marks
that cannot be read name no test, after logging `NO_FAILED_TESTS`, and no stage, after logging
`NO_LAST_STAGES`.

**The verification's marks.** A CodeBuild action that fails may leave no output artifact, so the
verification step writes what it has to say itself, in its post-build phase, to the pipeline's
artifact bucket (`src/rhi/pipeline/verify.py`), which `RHI_REPORTER_ARTIFACTS` names. The marks of
an execution are the objects under `verification/<execution ID>/`, and the event carries that ID:

- `failed-smoke-tests`: the IDs of the smoke tests that failed, one per line;
- `last-stages`: the last lifecycle stage each instance of the tested pools reached, one per line,
  left when the tests failed;
- `not-run`: one line, the reason no test ran: `canary-missing` or `listener-off`, which the
  verification script leaves, or `parked`, which the step leaves while the system is parked;
- `gpu-not-run`: one line, the reason the GPU tests did not run, `no-capacity`, which the
  verification script leaves when no instance of the coursework pool was launched during its wait.
  The mark alone says so: its line is not read, only the listing of its key, which also says when
  it was written.

**The mark of an earlier attempt.** The step leaves `gpu-not-run` from a build that fails too, on
the mock pool's tests say, and a retry of the stage keeps the execution's ID: the mark of the
attempt that failed is still there when the retry succeeds, with its GPU tests run or not. So the
mark counts for a success only when it was written, as S3's listing says, at or after the start of
the latest run of the action `Verify` of that stage in the execution, as CodePipeline's records
say: the run that succeeded. A mark written before that start is an earlier attempt's, and says
nothing of this success. While the records show no such run, or the listing no time, the mark
counts: a success is not given `VERIFIED_DESCRIPTION` on a mark that cannot be dated.

A mark is first looked for by listing its key, then read: the function's role may list that
prefix, so a mark that is not there is told from one that cannot be read. At most `MARK_BYTES` of
one are read, and nothing of a mark is logged. While no bucket is named, no mark is read.

**The owner's message.** After the statuses of an action's event are posted, an action that
ended without success, a failure or an error, has its details sent to the owner's topic, the one
`RHI_REPORTER_TOPIC` names (`rhi_reporter.notify`): once per event, whatever number of stages it
reports under. They are read from the same records as the report, the action's latest run that
failed or was abandoned and its stack's events since it started, and are read once for both.
Sending is the last thing the function does for an event, and nothing it raises reaches the
statuses: a failure to publish is logged, `NOT_SENT`, and the function returns normally. While the
variable is unset, nothing is sent.

**The credential.** Before anything is posted, the secret is checked as the client reads it.
While it is absent or lacks one of its fields, nothing is posted, `NO_CREDENTIAL` is logged, and
the function returns normally, so that EventBridge does not retry: the owner creates the secret
after the first deployment.

**A measurement.** The rule on the measurement bus invokes `handler` with the events of source
`measure.SOURCE`, each one run of a spike, the canary or the dry run, which a job sent because it
cannot post a status itself (sections 8.4 and 12). Such an event takes no other path. Its statuses
are the ones `measure.read_event` gives, posted in the order of their parts on the commit the event
names, the one the job ran from. When the measure module refuses any part of it, nothing is posted
and `NOT_A_MEASUREMENT` is logged, without the event's content.

Before it posts, the function reads the commit's statuses, and posts only what the commit does not
hold yet (`measure.still_to_post`): when every part of the run label's newest posting there is the
event's part of that number, in state and description, only the event's other parts, in order;
otherwise every part from part 1. So an invocation again of an event whose posting stopped
part-way completes that posting rather than begin another, which the record would read as two
postings that overlap. When a part is left unposted, by a refusal of GitHub's, a commit not in the
repository among other reasons, by a credential that stops signing, by a read of the statuses that
fails, or by the deadline, the function logs at ERROR `NOT_ALL_POSTED`, with the measurement's id,
its run label, how many of its parts the commit holds and how many parts it has, and fails, so that
Lambda invokes it again.

**The deadline.** Every status of an invocation is posted with one deadline, the Lambda context's
remaining time less `DEADLINE_MARGIN_SECONDS`: until then the client waits out GitHub's temporary
refusals (`rhi_reporter.github`), and a refusal it cannot wait out by then fails the posting. A
longer wait is Lambda's own retry of the invocation's event.
"""

from __future__ import annotations

import functools
import io
import json
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rhi_reporter import github, measure, notify, superseded
from rhi_reporter.report import build_report, statuses_of
from rhi_reporter.status import (
    APPROVAL_CATEGORY,
    APPROVAL_DESCRIPTION,
    DEPLOY_CONTEXT,
    VERIFIED_DESCRIPTION,
    VERIFY_ACTION,
    Description,
    State,
    as_it_ran,
    status_for,
    without_gpu_tests,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from rhi_reporter.status import CommitStatus

STAGES_VARIABLE = "RHI_REPORTER_STAGES"
TOPIC_VARIABLE = "RHI_REPORTER_TOPIC"
SMOKE_TESTS_VARIABLE = "RHI_SMOKE_TEST_IDS"
LIFECYCLE_STAGES_VARIABLE = "RHI_LIFECYCLE_STAGES"
ARTIFACTS_VARIABLE = "RHI_REPORTER_ARTIFACTS"

PIPELINE_CHANGE = "CodePipeline Pipeline Execution State Change"
STAGE_CHANGE = "CodePipeline Stage Execution State Change"
ACTION_CHANGE = "CodePipeline Action Execution State Change"

# The pipeline's synth step (`src/rhi/pipeline/stack.py`): its output is the execution's cloud
# assembly, which holds every template the execution deploys.
SYNTH_STAGE = "Build"
SYNTH_ACTION = "Synth"
TEMPLATE_SUFFIX = ".template.json"
# What CodePipeline names the provider of a stack's deployment.
CLOUDFORMATION = "CloudFormation"
# The states CodePipeline gives a stage that has ended, in its API's own spelling, and among them
# those of a stage that stopped short, which ends every deployment of the execution.
ENDED = frozenset({"Succeeded", "Failed", "Stopped", "Cancelled"})
STOPPED_SHORT = frozenset({"Failed", "Stopped", "Cancelled"})
# The states CodePipeline gives an execution that has ended, or is ending.
EXECUTION_ENDED = frozenset({"Succeeded", "Failed", "Stopped", "Stopping", "Cancelled", "Superseded"})
# The state of a stage's or an action's event when an update of the pipeline cancelled its execution.
CANCELED = "CANCELED"
# At most this many pages of a stack's events are read: 100 events each.
EVENT_PAGES = 10
# At most this many pages of the pipeline's executions are read: 100 executions each.
EXECUTION_PAGES = 10
# The statuses CodePipeline gives a run of an action that ended without success.
UNSUCCESSFUL_RUNS = frozenset({"Failed", "Abandoned"})
# The status CodePipeline gives a run of an action that has not ended: for an approval, one that waits.
ACTIVE_RUN = "InProgress"
# The verification's marks (`src/rhi/pipeline/verify.py`): the prefix of their keys in the artifact
# bucket, before the execution's ID, and the name of each.
MARKS_PREFIX = "verification"
FAILED_TESTS_MARK = "failed-smoke-tests"
LAST_STAGES_MARK = "last-stages"
NOT_RUN_MARK = "not-run"
GPU_NOT_RUN_MARK = "gpu-not-run"
# At most this much of a mark is read: every smoke test's ID fits in it many times over, and so
# does a stage for each instance of a fleet at its cap.
MARK_BYTES = 65536

NO_CREDENTIAL = "No commit status was posted: the reporter's GitHub credential is not in its secret."
NO_COMMIT = "No commit status was posted: the pipeline's execution names no commit yet."
NO_FACTS = "The report of a failure leaves out its resources: the pipeline's records could not be read."
NO_APPROVALS = "A pending status was not posted: the pipeline's approvals could not be read."
NO_EXECUTIONS = "No superseded commit was reported yet: the pipeline's executions could not be read."
NO_FAILED_TESTS = "The report of a failed verification names no smoke test: the verification's marks could not be read."
NO_LAST_STAGES = (
    "The report of a failed verification names no lifecycle stage: the verification's marks could not be read."
)
NO_NOT_RUN = "A verification's success was not posted: whether it ran its tests could not be read from its marks."
NO_VERIFICATION = "A superseded commit's verification takes the execution's state: its actions could not be read."
NOT_SENT = "The details of a failure were not sent to the owner: the topic refused them or could not be reached."
NOT_A_MEASUREMENT = "No commit status was posted: the measurement event is not one the measure module accepts."
NOT_ALL_POSTED = "A measurement's commit statuses were not all posted:"
# The time an invocation keeps for itself after its last post may begin: one call's timeout.
DEADLINE_MARGIN_SECONDS = 10

_SHA = re.compile(r"[0-9a-f]{40}")

logger = logging.getLogger(__name__)


class NotAllPostedError(Exception):
    """A measurement's statuses were not all posted. The text is fixed."""

    def __init__(self) -> None:
        super().__init__("a measurement's commit statuses were not all posted")


@dataclass(frozen=True)
class Aws:
    """The clients the function reads AWS with: the SDK's, or stubs with the same methods."""

    codepipeline: Any
    cloudformation: Any
    s3: Any
    secrets: Any
    # The owner's topic is published to with it. None sends nothing.
    sns: Any = None


@dataclass
class _Records:
    """What the pipeline's records hold of one failed action, read once for the report and the
    owner's message."""

    executions: list[Mapping[str, Any]]
    # The action's latest run that ended without success, None when there is none.
    run: Mapping[str, Any] | None
    resource_events: list[Mapping[str, object]]
    read: bool


@dataclass
class _Marks:
    """What a failed verification left in its marks, each beside the allow-list the report names
    it by: the failed smoke tests, and the last lifecycle stage each instance reached."""

    smoke_tests: Sequence[str]
    lifecycle_stages: Sequence[str]
    failed_tests: Sequence[str] = ()
    last_stages: Sequence[str] = ()


@functools.cache
def _clients() -> tuple[Aws, github.StatusClient]:
    """The clients of one execution environment, made by its first event."""
    secrets = boto3.client("secretsmanager")
    aws = Aws(
        codepipeline=boto3.client("codepipeline"),
        cloudformation=boto3.client("cloudformation"),
        s3=boto3.client("s3"),
        secrets=secrets,
        sns=boto3.client("sns"),
    )
    return aws, github.StatusClient(secrets)


def handler(event: Mapping[str, Any], context: Any) -> None:
    """The Lambda function's entry point."""
    aws, client = _clients()
    stages = tuple(stage for stage in os.environ.get(STAGES_VARIABLE, "").split(",") if stage)
    handle(
        event,
        aws=aws,
        client=client,
        stages=stages,
        topic=os.environ.get(TOPIC_VARIABLE) or None,
        smoke_tests=smoke_test_ids(os.environ),
        lifecycle_stages=lifecycle_stages(os.environ),
        artifacts=os.environ.get(ARTIFACTS_VARIABLE) or None,
        deadline=deadline_of(context, time.time()),
    )


def deadline_of(context: Any, now: float) -> float | None:
    """The deadline of every post of the invocation `context` is Lambda's context of, on the clock
    that reads `now`: its remaining time less `DEADLINE_MARGIN_SECONDS`. None, no wait, without a
    context that tells its remaining time."""
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if remaining is None:
        return None
    return now + remaining() / 1000 - DEADLINE_MARGIN_SECONDS


def smoke_test_ids(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The allow-list of smoke test IDs that `environ` gives the function: the IDs of
    `tests/smoke/ids.py`, as the function's construct wrote them into `RHI_SMOKE_TEST_IDS`. Empty
    without the variable."""
    return tuple(test_id for test_id in environ.get(SMOKE_TESTS_VARIABLE, "").split(",") if test_id)


def lifecycle_stages(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The allow-list of lifecycle stages that `environ` gives the function: the stages of the
    lifecycle agent's enumeration, as the function's construct wrote them into
    `RHI_LIFECYCLE_STAGES`. Empty without the variable."""
    return tuple(stage for stage in environ.get(LIFECYCLE_STAGES_VARIABLE, "").split(",") if stage)


def handle(
    event: Mapping[str, Any],
    *,
    aws: Aws,
    client: Any,
    stages: Sequence[str],
    topic: str | None = None,
    smoke_tests: Sequence[str] = (),
    artifacts: str | None = None,
    lifecycle_stages: Sequence[str] = (),
    deadline: float | None = None,
) -> None:
    """Posts the statuses of one of CodePipeline's events with `client`, which has `post_status`
    and `statuses` as `github.StatusClient` has them, then sends the details of an action that ended
    without success to `topic`, the owner's. `stages` are the stages the pipeline deploys,
    `smoke_tests` the IDs a report of a failure may name a smoke test by, `lifecycle_stages` the
    names it may name a lifecycle stage by, and `artifacts` the pipeline's artifact bucket, where
    a verification leaves its marks. Every call of `client` is given `deadline`, a time on its
    clock: None waits out no refusal. An event of a measurement posts its statuses and nothing
    else."""
    post = functools.partial(_post, client, deadline=deadline)
    if event.get("source") == measure.SOURCE:
        _measurement(event, aws=aws, client=client, deadline=deadline)
        return
    kind, detail = event.get("detail-type"), event.get("detail")
    if kind == PIPELINE_CHANGE and isinstance(detail, dict):
        _supersede(detail, aws=aws, post=post, stages=stages, artifacts=artifacts)
        return
    if kind not in (STAGE_CHANGE, ACTION_CHANGE) or not isinstance(detail, dict):
        return
    stage, action = detail.get("stage"), detail.get("action") if kind == ACTION_CHANGE else None
    if stage not in stages and action is None:
        return
    if not github.has_credential(aws.secrets):
        logger.warning(NO_CREDENTIAL)
        return
    pipeline, execution = detail.get("pipeline"), detail.get("execution-id")
    commit, execution_status = _execution(aws.codepipeline, pipeline, execution)
    if execution_status in superseded.SUPERSEDED_STATUSES or detail.get("state") == CANCELED:
        _reconcile(detail, aws=aws, post=post, stages=stages, artifacts=artifacts)
        return
    if commit is None:
        logger.warning(NO_COMMIT)
        return
    kind_of = detail.get("type")
    category = kind_of.get("category") if isinstance(kind_of, dict) else None
    ended: tuple[set[str], bool, set[tuple[object, object]]] | None = None
    records: list[_Records] = []
    listed: list[list[Mapping[str, Any]]] = []

    def runs() -> list[Mapping[str, Any]]:
        """The execution's action runs, listed once for the event."""
        if not listed:
            listed.append(list(_action_executions(aws.codepipeline, pipeline, execution)))
        return listed[0]

    def read() -> _Records:
        if not records:
            records.append(_read(aws, action_runs=runs, stage=stage, action=action))
        return records[0]

    def verification_started() -> object:
        return _started(_latest_verification(runs(), stage))

    try:
        for under in [stage] if stage in stages else stages:
            facts = {"stage": under, "action": action, "category": category, "state": detail.get("state")}
            facts["commit"] = commit
            (status,) = _as_they_ran(aws, artifacts, execution, [status_for(facts)], verification_started)
            if status.state is State.PENDING:
                if execution_status in EXECUTION_ENDED:
                    continue
                ended = _ended(aws.codepipeline, pipeline, execution) if ended is None else ended
                stages_ended, stopped_short, open_approvals = ended
                if under in stages_ended or stopped_short:
                    continue
                own = action if category == APPROVAL_CATEGORY and stage == under else None
                known = {name for at, name in open_approvals if at == under} | ({own} - {None})
                status = _as_approvals_stand(status, runs, under, own, known)
                if status is None:
                    continue
            post(status)
            if status.state is State.FAILURE and action is not None:
                marks = _Marks(smoke_tests=smoke_tests, lifecycle_stages=lifecycle_stages)
                if action == VERIFY_ACTION:
                    marks.failed_tests = _failed_tests(aws, artifacts, execution)
                    marks.last_stages = _last_stages(aws, artifacts, execution)
                for fact in _report(aws, facts, read(), marks):
                    post(fact)
    except github.CredentialError:
        logger.warning(NO_CREDENTIAL)
    if action is not None and topic is not None and aws.sns is not None:
        own = {"stage": stage, "action": action, "category": category, "state": detail.get("state"), "commit": commit}
        _send(aws, topic, own, execution=execution, read=read)


def _measurement(event: Mapping[str, Any], *, aws: Aws, client: Any, deadline: float | None) -> None:
    """Posts the statuses of one run that a job sent that its commit does not hold yet, on the
    commit the event names, or nothing when the measure module refuses any part of the event.
    Raises `NotAllPostedError`, after logging `NOT_ALL_POSTED`, when a part is left unposted."""
    try:
        statuses = measure.read_event(event)
    except ValueError:
        logger.warning(NOT_A_MEASUREMENT)
        return
    if not github.has_credential(aws.secrets):
        logger.warning(NO_CREDENTIAL)
        return
    measurement, label, _ = measure.context_parts(statuses[0].context)
    held = 0
    try:
        rest = measure.still_to_post(statuses, client.statuses(statuses[0].commit, deadline=deadline))
        held = len(statuses) - len(rest)
        for status in rest:
            _post(client, status, deadline=deadline)
            held += 1
    except (github.RefusedError, github.CredentialError):
        # Fixed text, and names of the measure module's own shapes: nothing GitHub or AWS said.
        logger.error(
            "%s measurement %s, run %s, %d of %d parts on the commit",
            NOT_ALL_POSTED,
            measurement,
            label,
            held,
            len(statuses),
        )
        raise NotAllPostedError from None


def _supersede(
    detail: Mapping[str, Any],
    *,
    aws: Aws,
    post: Callable[[CommitStatus], None],
    stages: Sequence[str],
    artifacts: str | None,
) -> None:
    """Posts the statuses of the superseded commits that the execution of `detail` covers, or
    whose execution it is."""
    if not stages:
        return
    if not github.has_credential(aws.secrets):
        logger.warning(NO_CREDENTIAL)
        return
    _reconcile(detail, aws=aws, post=post, stages=stages, artifacts=artifacts)


def _reconcile(
    detail: Mapping[str, Any],
    *,
    aws: Aws,
    post: Callable[[CommitStatus], None],
    stages: Sequence[str],
    artifacts: str | None,
) -> None:
    """Posts the statuses of the superseded commits that the execution of `detail` covers, or
    whose execution it is, as the pipeline's executions stand now. Raises when they, or the marks
    of a covering execution's verification, cannot be read, so that the event is invoked again."""
    pipeline, execution = detail.get("pipeline"), detail.get("execution-id")
    try:
        executions = _executions(aws.codepipeline, pipeline, execution)
    except (BotoCoreError, ClientError):
        logger.warning(NO_EXECUTIONS)
        raise
    try:
        for cover in superseded.covers(executions):
            if execution not in (cover.superseded.execution_id, cover.covering.execution_id):
                continue
            for stage in stages:
                latest = _verification(aws.codepipeline, pipeline, cover, stage) if cover.ended else None
                state = latest.get("status") if latest is not None else None
                statuses = superseded.statuses_for(cover, stage, state if isinstance(state, str) else None)
                started = functools.partial(_started, latest)
                for status in _as_they_ran(aws, artifacts, cover.covering.execution_id, statuses, started):
                    post(status)
    except github.CredentialError:
        logger.warning(NO_CREDENTIAL)


def _executions(codepipeline: Any, pipeline: object, execution: object) -> list[superseded.Execution]:
    """The pipeline's executions, newest first, down to the first one older than `execution`
    that did not end superseded: no older one is covered by, or covers, `execution`."""
    found: list[superseded.Execution] = []
    seen = False
    token: str | None = None
    for _ in range(EXECUTION_PAGES):
        page = {"nextToken": token} if token else {}
        answer = codepipeline.list_pipeline_executions(pipelineName=pipeline, maxResults=100, **page)
        for summary in answer.get("pipelineExecutionSummaries", []):
            record = superseded.execution_of(summary)
            if record is None:
                continue
            found.append(record)
            if record.execution_id == execution:
                seen = True
            elif seen and record.status not in superseded.SUPERSEDED_STATUSES:
                return found
        token = answer.get("nextToken")
        if not token:
            break
    return found


def _verification(codepipeline: Any, pipeline: object, cover: superseded.Cover, stage: str) -> Mapping[str, Any] | None:
    """The latest run of the action named Verify of `stage` in the covering execution, as
    CodePipeline records it. None when it did not run, or cannot be read."""
    try:
        return _latest_verification(_action_executions(codepipeline, pipeline, cover.covering.execution_id), stage)
    except (BotoCoreError, ClientError, KeyError, TypeError):
        logger.warning(NO_VERIFICATION)
        return None


def _latest_verification(runs: Iterable[Mapping[str, Any]], stage: object) -> Mapping[str, Any] | None:
    """The latest run, by its start, of the action named Verify of `stage` among `runs`, an
    execution's action runs: a stage that is retried runs the action again. None without one."""
    found = [detail for detail in runs if (detail.get("stageName"), detail.get("actionName")) == (stage, VERIFY_ACTION)]
    return max(found, key=lambda detail: detail["startTime"], default=None)


def _started(run: Mapping[str, Any] | None) -> object:
    """When `run`, an action's run, started, as CodePipeline records it. None without a run."""
    return run["startTime"] if run is not None else None


def _post(client: Any, status: CommitStatus, *, deadline: float | None) -> None:
    client.post_status(status.commit, status.state.value, status.context, status.description, deadline=deadline)


def _listed(s3: Any, bucket: str | None, execution: object, name: str) -> Mapping[str, Any] | None:
    """The mark `name` that the verification of `execution` left in `bucket`, as the listing of
    its key gives it: its key, and when it was last written. None when it left no such mark, or
    when no bucket or no execution is named. What S3 raises is raised: a mark that cannot be read
    is not a mark that is not there."""
    if bucket is None or not isinstance(execution, str) or not execution:
        return None
    key = f"{MARKS_PREFIX}/{execution}/{name}"
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=key)
    return next((entry for entry in listed.get("Contents", []) if entry.get("Key") == key), None)


def _mark(s3: Any, bucket: str | None, execution: object, name: str) -> list[str] | None:
    """The lines of the mark `name` that the verification of `execution` left in `bucket`, without
    the blank ones. None when it left no such mark (`_listed`). What S3 raises is raised."""
    entry = _listed(s3, bucket, execution, name)
    if entry is None:
        return None
    text = s3.get_object(Bucket=bucket, Key=entry["Key"])["Body"].read(MARK_BYTES).decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _gpu_not_run(aws: Aws, artifacts: str | None, execution: object, started: Callable[[], object]) -> bool:
    """Whether the run of the verification of `execution` that started at `started()` left the
    mark `gpu-not-run`: the mark is there, and was not written before that run started. A mark
    written before is an earlier attempt's, which a retry of the stage does not remove. `started`
    is asked only when the mark is there; while it gives None, no run in the records, or the
    listing gives no time, the mark counts. What S3 or `started` raises is raised."""
    entry = _listed(aws.s3, artifacts, execution, GPU_NOT_RUN_MARK)
    if entry is None:
        return False
    written, start = entry.get("LastModified"), started()
    return written is None or start is None or written >= start


def _failed_tests(aws: Aws, artifacts: str | None, execution: object) -> list[str]:
    """What the failed verification of `execution` left as its failed smoke tests, one per line:
    the report keeps those that are IDs. Empty when it left none, and, after logging
    `NO_FAILED_TESTS`, when its marks cannot be read."""
    try:
        return _mark(aws.s3, artifacts, execution, FAILED_TESTS_MARK) or []
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
        logger.warning(NO_FAILED_TESTS)
        return []


def _last_stages(aws: Aws, artifacts: str | None, execution: object) -> list[str]:
    """What the failed verification of `execution` left as the last lifecycle stage each instance
    reached, one per line: the report keeps those that are stages. Empty when it left none, and,
    after logging `NO_LAST_STAGES`, when its marks cannot be read."""
    try:
        return _mark(aws.s3, artifacts, execution, LAST_STAGES_MARK) or []
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
        logger.warning(NO_LAST_STAGES)
        return []


def _as_they_ran(
    aws: Aws,
    artifacts: str | None,
    execution: object,
    statuses: Sequence[CommitStatus],
    started: Callable[[], object],
) -> list[CommitStatus]:
    """`statuses` as the verification of `execution` ran. When one of them says that the
    verification's tests ran and passed, the mark `not-run` of that execution is read first, and
    with it that status says instead that no test ran, and why. Without it, the mark `gpu-not-run`
    is looked for, and with the one of the verification's latest run, which `started` gives the
    start of (`_gpu_not_run`), that status says instead that the mock pool's tests passed and that
    the GPU tests did not run for lack of capacity. Raises, after logging `NO_NOT_RUN`, when the
    marks or that start cannot be read: no status then claims a verification that may have run
    less than its tests."""
    if not any(status.description == VERIFIED_DESCRIPTION for status in statuses):
        return list(statuses)
    try:
        lines = _mark(aws.s3, artifacts, execution, NOT_RUN_MARK)
        gpu_not_run = lines is None and _gpu_not_run(aws, artifacts, execution, started)
    except Exception:
        logger.warning(NO_NOT_RUN)
        raise
    if gpu_not_run:
        # The mark alone says that the GPU tests did not run, whatever it holds.
        return [without_gpu_tests(status) for status in statuses]
    # The mark alone says that no test ran, whatever it holds: an empty one is a reason not known.
    reason = None if lines is None else next(iter(lines), "")
    return [as_it_ran(status, reason) for status in statuses]


def _execution(codepipeline: Any, pipeline: object, execution: object) -> tuple[str | None, object]:
    """The full SHA of the commit the execution deploys, None when it names none, and the
    execution's status as CodePipeline gives it now."""
    answer = codepipeline.get_pipeline_execution(pipelineName=pipeline, pipelineExecutionId=execution)
    record = answer.get("pipelineExecution", {})
    for revision in record.get("artifactRevisions", []):
        sha = revision.get("revisionId")
        if isinstance(sha, str) and _SHA.fullmatch(sha):
            return sha, record.get("status")
    return None, record.get("status")


def _ended(codepipeline: Any, pipeline: object, execution: object) -> tuple[set[str], bool, set[tuple[object, object]]]:
    """The stages whose latest execution is this one and has ended, whether one of them stopped
    short: no stage of the execution runs on after that, and the approvals this execution leaves
    open, by stage and action: those whose latest run carries the token of an approval request."""
    answer = codepipeline.get_pipeline_state(name=pipeline)
    ended: set[str] = set()
    stopped_short = False
    open_approvals: set[tuple[object, object]] = set()
    for state in answer.get("stageStates", []):
        latest = state.get("latestExecution", {})
        if latest.get("pipelineExecutionId") != execution:
            continue
        if latest.get("status") in ENDED:
            ended.add(state.get("stageName"))
            stopped_short = stopped_short or latest.get("status") in STOPPED_SHORT
        for action in state.get("actionStates", []):
            run = action.get("latestExecution", {})
            if run.get("token") and run.get("status") == ACTIVE_RUN:
                open_approvals.add((state.get("stageName"), action.get("actionName")))
    return ended, stopped_short, open_approvals


def _as_approvals_stand(
    status: CommitStatus,
    runs: Callable[[], Sequence[Mapping[str, Any]]],
    stage: str,
    own: object,
    known: set[object],
) -> CommitStatus | None:
    """The pending status `status` of `stage`, as the stage's manual approvals stand in `runs`, the
    action runs of the event's own execution. A run is an approval as its record's type says; a
    record that says no type is one when its action is among `known`, the approvals of `stage` the
    event or the pipeline's state names. While one waits, a deployment that runs is one that
    waits for it. None, nothing to post, for the start of `own`, the event's approval of `stage`,
    once its own run has ended and none waits. The start of an approval of another stage (`own`
    None) is a step that runs. None too, after logging `NO_APPROVALS`, when the runs cannot be
    read: the status last posted stays. Any other status is returned as it is."""
    if not status.context.startswith(DEPLOY_CONTEXT) or status.description not in (
        Description.DEPLOYING,
        APPROVAL_DESCRIPTION,
    ):
        return status
    try:
        latest: dict[object, Mapping[str, Any]] = {}
        for run in runs():
            # `input` is optional in a record: without it, an approval is known by its name alone.
            category = (run.get("input") or {}).get("actionTypeId", {}).get("category")
            name = run.get("actionName")
            approval = category == APPROVAL_CATEGORY if category is not None else name is not None and name in known
            if run.get("stageName") != stage or not approval:
                continue
            # A stage that is retried runs its approval again: the latest run is the one that counts.
            if name not in latest or run["startTime"] > latest[name]["startTime"]:
                latest[name] = run
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, AttributeError):
        logger.warning(NO_APPROVALS)
        return None
    if any(run.get("status") == ACTIVE_RUN for run in latest.values()):
        return replace(status, description=APPROVAL_DESCRIPTION)
    if status.description != APPROVAL_DESCRIPTION:
        return status
    if own is None:
        return replace(status, description=Description.DEPLOYING)
    # The event's own approval, not yet in the records, waits as the event tells.
    return None if own in latest else status


def _read(
    aws: Aws, *, action_runs: Callable[[], Sequence[Mapping[str, Any]]], stage: object, action: object
) -> _Records:
    """The records of the action `stage` and `action` name as the pipeline does: the execution's
    action runs, as `action_runs` lists them, the action's latest run that ended without success,
    and the events since it started of the stack it deploys. What cannot be read is left empty,
    and `NO_FACTS` logged."""
    records = _Records(executions=[], run=None, resource_events=[], read=True)
    try:
        records.executions = list(action_runs())
        runs = [
            detail
            for detail in records.executions
            if (detail.get("stageName"), detail.get("actionName")) == (stage, action)
            and detail.get("status") in UNSUCCESSFUL_RUNS
        ]
        if runs:
            # A stage that is retried runs the action again: the latest run is the one that failed now.
            records.run = max(runs, key=lambda detail: detail["startTime"])
            records.resource_events = _resource_events(aws.cloudformation, records.run)
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
        logger.warning(NO_FACTS)
        records.read = False
    return records


def _report(aws: Aws, facts: Mapping[str, object], records: _Records, marks: _Marks) -> list[CommitStatus]:
    """The report of the failure `facts` tell, as statuses, from the failed action's records and
    from what a failed verification left, `marks`: the report names its failed smoke tests and
    its instances' last stages only by their allow-lists."""
    failed = records.run is not None and records.run.get("status") == "Failed"
    resource_events = records.resource_events if failed else []
    logical_ids: set[str] = set()
    try:
        if resource_events:
            logical_ids = _logical_ids(aws.s3, records.executions)
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
        logger.warning(NO_FACTS)
    report = build_report(
        facts,
        resource_events,
        marks.failed_tests,
        marks.last_stages,
        logical_ids=logical_ids,
        smoke_test_ids=marks.smoke_tests,
        lifecycle_stages=marks.lifecycle_stages,
    )
    return statuses_of(report)


def _send(
    aws: Aws, topic: str, facts: Mapping[str, object], *, execution: object, read: Callable[[], _Records]
) -> None:
    """Publishes to `topic` the owner's message of the action's event `facts` tell, when it ended
    without success. Nothing raised here goes further: it is logged as `NOT_SENT`, without its
    text."""
    try:
        if notify.message_for(facts) is None:
            return
        records = read()
        message = notify.message_for(
            facts,
            execution=execution,
            action_execution=records.run,
            resource_events=records.resource_events,
            read=records.read,
        )
        if message is not None:
            aws.sns.publish(TopicArn=topic, Subject=message.subject, Message=message.body)
    except Exception:  # noqa: BLE001 - whatever the topic or a record raises, the posted statuses stand.
        logger.warning(NOT_SENT)


def _action_executions(codepipeline: Any, pipeline: object, execution: object) -> Iterator[Mapping[str, Any]]:
    """Every action execution of the pipeline's execution."""
    token: str | None = None
    while True:
        page = {"nextToken": token} if token else {}
        answer = codepipeline.list_action_executions(
            pipelineName=pipeline, filter={"pipelineExecutionId": execution}, **page
        )
        yield from answer.get("actionExecutionDetails", [])
        token = answer.get("nextToken")
        if not token:
            return


def _resource_events(cloudformation: Any, detail: Mapping[str, Any]) -> list[Mapping[str, object]]:
    """CloudFormation's events of the stack a deployment action names, since the action started.
    Empty for an action that deploys no stack."""
    action_input = detail.get("input", {})
    stack = action_input.get("configuration", {}).get("StackName")
    if action_input.get("actionTypeId", {}).get("provider") != CLOUDFORMATION or not isinstance(stack, str):
        return []
    started = detail["startTime"]
    found: list[Mapping[str, object]] = []
    token: str | None = None
    for _ in range(EVENT_PAGES):
        page = {"NextToken": token} if token else {}
        answer = cloudformation.describe_stack_events(StackName=stack, **page)
        events = answer.get("StackEvents", [])
        # Newest first: the events before the action belong to an earlier deployment.
        found += [event for event in events if event["Timestamp"] >= started]
        token = answer.get("NextToken")
        if not token or any(event["Timestamp"] < started for event in events):
            break
    return found


def _logical_ids(s3: Any, executions: Sequence[Mapping[str, Any]]) -> set[str]:
    """The logical IDs of every template of the synth step's output, in the artifact store."""
    found: set[str] = set()
    for detail in executions:
        if detail.get("stageName") != SYNTH_STAGE or detail.get("actionName") != SYNTH_ACTION:
            continue
        for artifact in detail.get("output", {}).get("outputArtifacts", []):
            location = artifact["s3location"]
            body = s3.get_object(Bucket=location["bucket"], Key=location["key"])["Body"].read()
            with zipfile.ZipFile(io.BytesIO(body)) as assembly:
                for name in assembly.namelist():
                    if name.endswith(TEMPLATE_SUFFIX):
                        resources = json.loads(assembly.read(name)).get("Resources", {})
                        found.update(logical_id for logical_id in resources if isinstance(logical_id, str))
    return found
