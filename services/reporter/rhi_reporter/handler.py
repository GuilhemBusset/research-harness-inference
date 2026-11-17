"""The reporter's function: CodePipeline's events in, commit statuses out (system design, sections
8.2, step 7, and 8.4).

The rule of `src/rhi/pipeline/reporter.py` invokes `handler` with each state change of this
pipeline's executions, stages and actions. `handle` turns one into statuses, through
`rhi_reporter.status`, adds the report of a failure, through `rhi_reporter.report`, and posts them
in that order with `rhi_reporter.github`.

**Which events post what.**

- An event of a stage the pipeline deploys (`RHI_REPORTER_STAGES`), the stage's own or one of its
  actions', is the status `status_for` gives it.
- An action of a step that runs before those stages (source, synthesis, self-update, assets)
  reports under each of them, as one of their actions, so that a failure there ends the stage's
  `deploy/<stage>` status as a failure, and its success never reads as a deployment's. The own
  events of those steps' stages post nothing: their actions say it all.
- An execution's own event, at its start and at its end, gives the statuses of the commits a
  self-update left without an execution (`rhi_reporter.superseded`): those this execution covers,
  and its own commit when this execution was cancelled and a later one covers it. They are read
  from the pipeline's executions, newest first, down to the first execution older than this one
  that ended with a verdict. Under each stage, such a commit is pending, naming the covering
  commit, while the covering execution runs, and gets its final state once it has ended, with
  the state of its verification when that ran.
- An event of a stage or an action of an execution that a self-update cancelled (the execution
  ended `Cancelled` or `Superseded`, or the event's own state is `CANCELED`, which CodePipeline
  gives only when an update of the pipeline cancels an execution) posts none of its own: its
  commit's verdict is the covering execution's. It posts that commit's superseded statuses again,
  as the execution's own end does, so that a cancellation that arrives late, after the covering
  execution has ended, leaves its verdict as it stands.
- When the pipeline's executions cannot be read for a superseded commit, the function fails after
  logging `NO_EXECUTIONS`, so that the event is invoked again: returning would leave the commit
  pending for good.
- A pending status is not posted once the stage has ended for the same execution, once one of
  the execution's stages has stopped short (failed, stopped or cancelled), or once the execution
  itself has ended: events arrive in no guaranteed order, and a late pending status, of the stage
  or of a step before it, would hide the verdict already posted.

**The commit** is the source revision of the execution, read from CodePipeline: an event does not
carry it. An execution that names none yet posts nothing.

**The report of a failure** follows the failed action's own status. Its parts:

- the failed resources of the stack the failed action deploys, read from CloudFormation's events
  of that stack since the action started;
- `logical_ids`: the names of the resources of every template that the same execution's synth
  step wrote, read from its output in the pipeline's artifact store. A resource is named only
  when one of them holds it;
- empty allow-lists of smoke test IDs and lifecycle stages: the steps that report them come later
  (issues E0-21 and E0-26).

What cannot be read for the report leaves it out: a resource then counts as dropped.

**The credential.** Before anything is posted, the secret is checked as the client reads it.
While it is absent or lacks one of its fields, nothing is posted, `NO_CREDENTIAL` is logged, and
the function returns normally, so that EventBridge does not retry: the owner creates the secret
after the first deployment.
"""

from __future__ import annotations

import functools
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rhi_reporter import github, superseded
from rhi_reporter.report import build_report, statuses_of
from rhi_reporter.status import VERIFY_ACTION, State, status_for

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from rhi_reporter.status import CommitStatus

STAGES_VARIABLE = "RHI_REPORTER_STAGES"

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

NO_CREDENTIAL = "No commit status was posted: the reporter's GitHub credential is not in its secret."
NO_COMMIT = "No commit status was posted: the pipeline's execution names no commit yet."
NO_FACTS = "The report of a failure leaves out its resources: the pipeline's records could not be read."
NO_EXECUTIONS = "No superseded commit was reported yet: the pipeline's executions could not be read."
NO_VERIFICATION = "A superseded commit's verification takes the execution's state: its actions could not be read."

_SHA = re.compile(r"[0-9a-f]{40}")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Aws:
    """The clients the function reads AWS with: the SDK's, or stubs with the same methods."""

    codepipeline: Any
    cloudformation: Any
    s3: Any
    secrets: Any


@functools.cache
def _clients() -> tuple[Aws, github.StatusClient]:
    """The clients of one execution environment, made by its first event."""
    secrets = boto3.client("secretsmanager")
    aws = Aws(
        codepipeline=boto3.client("codepipeline"),
        cloudformation=boto3.client("cloudformation"),
        s3=boto3.client("s3"),
        secrets=secrets,
    )
    return aws, github.StatusClient(secrets)


def handler(event: Mapping[str, Any], _context: object) -> None:
    """The Lambda function's entry point."""
    aws, client = _clients()
    stages = tuple(stage for stage in os.environ.get(STAGES_VARIABLE, "").split(",") if stage)
    handle(event, aws=aws, client=client, stages=stages)


def handle(event: Mapping[str, Any], *, aws: Aws, client: Any, stages: Sequence[str]) -> None:
    """Posts the statuses of one of CodePipeline's events with `client`, which has `post_status`
    as `github.StatusClient` has it. `stages` are the stages the pipeline deploys."""
    kind, detail = event.get("detail-type"), event.get("detail")
    if kind == PIPELINE_CHANGE and isinstance(detail, dict):
        _supersede(detail, aws=aws, client=client, stages=stages)
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
        _reconcile(detail, aws=aws, client=client, stages=stages)
        return
    if commit is None:
        logger.warning(NO_COMMIT)
        return
    kind_of = detail.get("type")
    category = kind_of.get("category") if isinstance(kind_of, dict) else None
    ended: tuple[set[str], bool] | None = None
    try:
        for under in [stage] if stage in stages else stages:
            facts = {"stage": under, "action": action, "category": category, "state": detail.get("state")}
            facts["commit"] = commit
            status = status_for(facts)
            if status.state is State.PENDING:
                if execution_status in EXECUTION_ENDED:
                    continue
                ended = _ended(aws.codepipeline, pipeline, execution) if ended is None else ended
                stages_ended, stopped_short = ended
                if under in stages_ended or stopped_short:
                    continue
            _post(client, status)
            if status.state is State.FAILURE and action is not None:
                for fact in _report(aws, facts, pipeline=pipeline, execution=execution, stage=stage, action=action):
                    _post(client, fact)
    except github.CredentialError:
        logger.warning(NO_CREDENTIAL)


def _supersede(detail: Mapping[str, Any], *, aws: Aws, client: Any, stages: Sequence[str]) -> None:
    """Posts the statuses of the superseded commits that the execution of `detail` covers, or
    whose execution it is."""
    if not stages:
        return
    if not github.has_credential(aws.secrets):
        logger.warning(NO_CREDENTIAL)
        return
    _reconcile(detail, aws=aws, client=client, stages=stages)


def _reconcile(detail: Mapping[str, Any], *, aws: Aws, client: Any, stages: Sequence[str]) -> None:
    """Posts the statuses of the superseded commits that the execution of `detail` covers, or
    whose execution it is, as the pipeline's executions stand now. Raises when they cannot be
    read, so that the event is invoked again."""
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
                verification = _verification(aws.codepipeline, pipeline, cover, stage) if cover.ended else None
                for status in superseded.statuses_for(cover, stage, verification):
                    _post(client, status)
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


def _verification(codepipeline: Any, pipeline: object, cover: superseded.Cover, stage: str) -> str | None:
    """The status of the latest run of the action named Verify of `stage` in the covering
    execution, as CodePipeline spells it. None when it did not run, or cannot be read."""
    try:
        runs = [
            detail
            for detail in _action_executions(codepipeline, pipeline, cover.covering.execution_id)
            if (detail.get("stageName"), detail.get("actionName")) == (stage, VERIFY_ACTION)
        ]
        latest = max(runs, key=lambda detail: detail["startTime"], default=None)
    except (BotoCoreError, ClientError, KeyError, TypeError):
        logger.warning(NO_VERIFICATION)
        return None
    status = latest.get("status") if latest is not None else None
    return status if isinstance(status, str) else None


def _post(client: Any, status: CommitStatus) -> None:
    client.post_status(status.commit, status.state.value, status.context, status.description)


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


def _ended(codepipeline: Any, pipeline: object, execution: object) -> tuple[set[str], bool]:
    """The stages whose latest execution is this one and has ended, and whether one of them
    stopped short: no stage of the execution runs on after that."""
    answer = codepipeline.get_pipeline_state(name=pipeline)
    ended: set[str] = set()
    stopped_short = False
    for state in answer.get("stageStates", []):
        latest = state.get("latestExecution", {})
        if latest.get("pipelineExecutionId") == execution and latest.get("status") in ENDED:
            ended.add(state.get("stageName"))
            stopped_short = stopped_short or latest.get("status") in STOPPED_SHORT
    return ended, stopped_short


def _report(
    aws: Aws, facts: Mapping[str, object], *, pipeline: object, execution: object, stage: str, action: str
) -> list[CommitStatus]:
    """The report of the failure `facts` tell, as statuses. `stage` and `action` name the failed
    action as the pipeline does."""
    resource_events: list[Mapping[str, object]] = []
    logical_ids: set[str] = set()
    try:
        executions = list(_action_executions(aws.codepipeline, pipeline, execution))
        failed = [
            detail
            for detail in executions
            if (detail.get("stageName"), detail.get("actionName"), detail.get("status")) == (stage, action, "Failed")
        ]
        if failed:
            # A stage that is retried runs the action again: the latest run is the one that failed now.
            latest = max(failed, key=lambda detail: detail["startTime"])
            resource_events = _resource_events(aws.cloudformation, latest)
        if resource_events:
            logical_ids = _logical_ids(aws.s3, executions)
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
        logger.warning(NO_FACTS)
    report = build_report(facts, resource_events, logical_ids=logical_ids, smoke_test_ids=(), lifecycle_stages=())
    return statuses_of(report)


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
