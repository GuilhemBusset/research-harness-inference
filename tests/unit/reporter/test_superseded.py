"""Commits that a self-update of the pipeline left without an execution end with the verdict of the
execution that covers them (system design, section 8.4; evidence E20).

The pipeline runs queued, one execution per commit. `Pipeline` simulates it, as CodePipeline's API
shows it, and hands every event to the reporter's function in the order it happens: a push queues
an execution of its commit; the first queued execution runs; one that updates the pipeline ends
superseded, cancels every execution still queued, and the pipeline restarts on the latest commit.
The commits are made here.
"""

from __future__ import annotations

import itertools
import json
import secrets
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rhi_reporter import handler
from rhi_reporter.github import SECRET_VARIABLE
from rhi_reporter.status import Description, superseded_description

PIPELINE = "RhiPipeline-CodePipeline"
STAGE = "Rhi"
SECRET = "rhi/reporter/github-app"
DEPLOY, VERIFY = f"deploy/{STAGE}", f"verify/{STAGE}"
# How a run of an execution ends.
SUCCEEDS, FAILS_TO_DEPLOY, FAILS_TO_VERIFY, IS_STOPPED, UPDATES_THE_PIPELINE = (
    "succeeds",
    "fails to deploy",
    "fails to verify",
    "is stopped",
    "updates the pipeline",
)
RUNS = (SUCCEEDS, FAILS_TO_DEPLOY, FAILS_TO_VERIFY, IS_STOPPED, UPDATES_THE_PIPELINE)
PUSH = "push"


class Client:
    """The GitHub client: it records what it is asked to post."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str, str, str]] = []

    def post_status(self, commit: str, state: str, context: str, description: str) -> None:
        self.posted.append((commit, state, context, description))

    def last(self, commit: str, context: str) -> tuple[str, str] | None:
        """The state and description of the last status posted on the commit under the context."""
        found = [(state, text) for sha, state, under, text in self.posted if (sha, under) == (commit, context)]
        return found[-1] if found else None


class Pipeline:
    """CodePipeline and Secrets Manager, as the function reads them, for a pipeline in queued mode
    that deploys the stage `Rhi`. Its listing of executions comes two to a page."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.executions: dict[str, dict[str, Any]] = {}
        self.queue: list[str] = []
        self.commits: list[str] = []
        self.stage_states: dict[str, dict[str, str]] = {}
        # The executions an update of the pipeline cancelled, the one that updated it among them.
        self.cancelled: list[str] = []
        # Whether the next read of the executions fails.
        self.unreadable = False

    # What happens.
    def push(self) -> str:
        commit = secrets.token_hex(20)
        self.commits.append(commit)
        self.queue.append(self._execution(commit))
        return commit

    def run(self, outcome: str) -> str:
        """Runs the first queued execution to its end. Returns its identifier."""
        execution = self.queue.pop(0)
        self._event("Pipeline", execution, state="STARTED")
        if outcome == UPDATES_THE_PIPELINE:
            self._event("Action", execution, stage="UpdatePipeline", action="SelfMutate", state="SUCCEEDED")
            self._end(execution, "Superseded", "SUPERSEDED")
            cancelled, self.queue = self.queue, []
            self.cancelled += [execution, *cancelled]
            for other in cancelled:
                self._end(other, "Cancelled", "CANCELED")
            self.queue.append(self._execution(self.commits[-1]))
            return execution
        self._stage(execution, "InProgress", "STARTED")
        if outcome == FAILS_TO_DEPLOY:
            self._action(execution, "Network.Deploy", "Failed", "FAILED")
            self._stage(execution, "Failed", "FAILED")
            self._end(execution, "Failed", "FAILED")
            return execution
        if outcome == IS_STOPPED:
            self._stage(execution, "Stopped", "STOPPED")
            self._end(execution, "Stopped", "STOPPED")
            return execution
        self._action(execution, "Network.Deploy", "Succeeded", "SUCCEEDED")
        self._event("Action", execution, stage=STAGE, action="Verify", state="STARTED")
        verified = outcome == SUCCEEDS
        self._action(execution, "Verify", *(("Succeeded", "SUCCEEDED") if verified else ("Failed", "FAILED")))
        self._stage(execution, *(("Succeeded", "SUCCEEDED") if verified else ("Failed", "FAILED")))
        self._end(execution, *(("Succeeded", "SUCCEEDED") if verified else ("Failed", "FAILED")))
        return execution

    def _execution(self, commit: str) -> str:
        execution = f"execution-{len(self.executions)}"
        record = {"commit": commit, "status": "InProgress", "started": len(self.executions), "actions": []}
        self.executions[execution] = record
        return execution

    def _stage(self, execution: str, status: str, state: str) -> None:
        self.stage_states[STAGE] = {"pipelineExecutionId": execution, "status": status}
        self._event("Stage", execution, stage=STAGE, state=state)

    def _action(self, execution: str, action: str, status: str, state: str) -> None:
        record = self.executions[execution]
        detail = {"stageName": STAGE, "actionName": action, "status": status, "startTime": len(record["actions"])}
        record["actions"].append(detail | {"input": {"actionTypeId": {"provider": "CodeBuild"}}, "output": {}})
        self._event("Action", execution, stage=STAGE, action=action, state=state)

    def _end(self, execution: str, status: str, state: str) -> None:
        self.executions[execution]["status"] = status
        self._event("Pipeline", execution, state=state)

    def _event(self, kind: str, execution: str, **fields: str) -> None:
        detail: dict[str, Any] = {"pipeline": PIPELINE, "execution-id": execution, **fields}
        if "action" in detail:
            detail["type"] = {"category": "Build"}
        event = {"detail-type": f"CodePipeline {kind} Execution State Change", "detail": detail}
        aws = handler.Aws(codepipeline=self, cloudformation=self, s3=self, secrets=self)
        handler.handle(event, aws=aws, client=self.client, stages=[STAGE])

    # What the function reads.
    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        assert SecretId == SECRET
        return {"SecretString": json.dumps({"app_id": "1", "installation_id": "2", "private_key": "not a key"})}

    def get_pipeline_execution(self, pipelineName: str, pipelineExecutionId: str) -> dict[str, Any]:
        record = self.executions[pipelineExecutionId]
        revisions = [{"revisionId": record["commit"]}]
        return {"pipelineExecution": {"status": record["status"], "artifactRevisions": revisions}}

    def get_pipeline_state(self, name: str) -> dict[str, Any]:
        states = [{"stageName": stage, "latestExecution": latest} for stage, latest in self.stage_states.items()]
        return {"stageStates": states}

    def list_action_executions(self, pipelineName: str, filter: dict[str, str], **_: str) -> dict[str, Any]:
        return {"actionExecutionDetails": self.executions[filter["pipelineExecutionId"]]["actions"]}

    def list_pipeline_executions(self, pipelineName: str, maxResults: int, nextToken: str = "0") -> dict[str, Any]:
        assert pipelineName == PIPELINE
        if self.unreadable:
            self.unreadable = False
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ListExecutions")
        newest = sorted(self.executions.items(), key=lambda item: item[1]["started"], reverse=True)
        start = int(nextToken)
        summaries = [
            {
                "pipelineExecutionId": execution,
                "status": record["status"],
                "startTime": record["started"],
                "sourceRevisions": [{"actionName": "GitHub", "revisionId": record["commit"]}],
            }
            for execution, record in newest[start : start + 2]
        ]
        more = {"nextToken": str(start + 2)} if start + 2 < len(newest) else {}
        return {"pipelineExecutionSummaries": summaries, **more}

    def describe_stack_events(self, StackName: str, **_: str) -> dict[str, Any]:
        return {"StackEvents": []}


@pytest.fixture(autouse=True)
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_VARIABLE, SECRET)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (SUCCEEDS, {DEPLOY: ("success", Description.DEPLOYED), VERIFY: ("success", Description.VERIFIED)}),
        (
            FAILS_TO_VERIFY,
            {DEPLOY: ("failure", Description.DEPLOYMENT_FAILED), VERIFY: ("failure", Description.VERIFICATION_FAILED)},
        ),
        # No verification ran: the verification's context gets the execution's state, and claims none.
        (
            FAILS_TO_DEPLOY,
            {DEPLOY: ("failure", Description.DEPLOYMENT_FAILED), VERIFY: ("failure", Description.DEPLOYMENT_FAILED)},
        ),
    ],
)
def test_the_cancelled_commits_end_with_the_state_of_the_execution_that_covered_them(
    outcome: str, expected: dict[str, tuple[str, str]]
) -> None:
    client = Client()
    pipeline = Pipeline(client)
    first, second, third = pipeline.push(), pipeline.push(), pipeline.push()
    pipeline.run(UPDATES_THE_PIPELINE)
    # The restart is on the latest commit.
    (restart,) = pipeline.queue
    assert pipeline.executions[restart]["commit"] == third
    statuses = [(execution, record["status"]) for execution, record in pipeline.executions.items()]
    assert [status for _, status in statuses] == ["Superseded", "Cancelled", "Cancelled", "InProgress"]

    # While the restart runs, each cancelled commit is pending and names the covering commit.
    pipeline._event("Pipeline", restart, state="STARTED")
    pending = ("pending", superseded_description(third))
    for commit in (second, third):
        assert client.last(commit, DEPLOY) == pending
        assert client.last(commit, VERIFY) == pending

    pipeline.run(outcome)
    for commit in (first, second, third):
        for context, verdict in expected.items():
            assert client.last(commit, context) == verdict
    # Every pending status of a cancelled commit named the covering commit.
    superseded = {text for sha, state, _, text in client.posted if sha == second and state == "pending"}
    assert superseded == {superseded_description(third)}


def sequences(length: int) -> list[tuple[str, ...]]:
    """Every sequence of pushes and runs of at most `length` steps, in which a run always has an
    execution to run."""
    found = []
    for size in range(1, length + 1):
        for steps in itertools.product((PUSH, *RUNS), repeat=size):
            queued, valid = 0, True
            for step in steps:
                if step == PUSH:
                    queued += 1
                elif queued == 0:
                    valid = False
                    break
                else:
                    # An update cancels every queued execution and queues the restart.
                    queued = 1 if step == UPDATES_THE_PIPELINE else queued - 1
            if valid:
                found.append(steps)
    return found


def test_every_commit_ends_with_a_verdict() -> None:
    generated = sequences(6)
    assert any(steps.count(UPDATES_THE_PIPELINE) >= 2 for steps in generated)
    for steps in generated:
        client = Client()
        pipeline = Pipeline(client)
        for step in steps:
            if step == PUSH:
                pipeline.push()
            else:
                pipeline.run(step)
        # The pipeline runs what is still queued.
        while pipeline.queue:
            pipeline.run(SUCCEEDS)
        for commit in pipeline.commits:
            deployed, verified = client.last(commit, DEPLOY), client.last(commit, VERIFY)
            assert deployed is not None and deployed[0] != "pending", steps
            assert verified is None or verified[0] != "pending", steps


def test_a_cancelled_commit_waits_for_an_execution_that_covers_it() -> None:
    client = Client()
    pipeline = Pipeline(client)
    pipeline.push()
    queued = pipeline.push()
    # Stopped by hand while queued: no later execution exists yet to cover it.
    execution = pipeline.queue.pop()
    pipeline._end(execution, "Cancelled", "CANCELED")
    assert client.last(queued, DEPLOY) is None


def test_a_late_cancellation_leaves_the_covering_verdict() -> None:
    for steps in sequences(5):
        client = Client()
        pipeline = Pipeline(client)
        for step in steps:
            if step == PUSH:
                pipeline.push()
            else:
                pipeline.run(step)
        while pipeline.queue:
            pipeline.run(SUCCEEDS)
        verdicts = {
            (commit, context): client.last(commit, context)
            for commit in pipeline.commits
            for context in (DEPLOY, VERIFY)
        }
        # CodePipeline's events of the cancellation arrive after the covering execution has ended.
        for execution in pipeline.cancelled:
            pipeline._event("Action", execution, stage="Build", action="Synth", state="CANCELED")
            pipeline._event("Stage", execution, stage="Build", state="CANCELED")
            pipeline._event("Stage", execution, stage=STAGE, state="CANCELED")
        assert {key: client.last(*key) for key in verdicts} == verdicts, steps


def test_a_failed_read_of_the_executions_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client()
    pipeline = Pipeline(client)
    _, second, third = pipeline.push(), pipeline.push(), pipeline.push()
    pipeline.run(UPDATES_THE_PIPELINE)
    end = pipeline._end

    def end_unread(execution: str, status: str, state: str) -> None:
        pipeline.unreadable = True
        with pytest.raises(ClientError):
            end(execution, status, state)
        assert client.last(second, DEPLOY) == ("pending", superseded_description(third))
        # The function failed: Lambda invokes it again with the same event.
        pipeline._event("Pipeline", execution, state=state)

    monkeypatch.setattr(pipeline, "_end", end_unread)
    pipeline.run(SUCCEEDS)
    assert client.last(second, DEPLOY) == ("success", Description.DEPLOYED)
    assert client.last(second, VERIFY) == ("success", Description.VERIFIED)
