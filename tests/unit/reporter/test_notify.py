"""The owner's message of a failure (system design, section 8.4): built from a recorded failed
execution, sent after the statuses, and never in their way.

The records are shaped as CodePipeline and CloudFormation give them for the pipeline of
`src/rhi/pipeline/stack.py`, with the documented account and a commit made here.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import secrets
import uuid
import zipfile
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rhi_reporter import handler, notify
from rhi_reporter.github import SECRET_VARIABLE

ACCOUNT = "123456789012"
PIPELINE = "RhiPipeline-CodePipeline"
STAGE = "Rhi"
STACK = "Rhi-Network"
SECRET = "rhi/reporter/github-app"
TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:RhiPipeline-OwnerTopic"
STARTED = datetime.datetime(2026, 11, 2, 9, 30, tzinfo=datetime.UTC)
# What AWS says of the failure: the message carries it as given, and no status or log does.
ERROR = "Stack Rhi-Network failed: " + secrets.token_hex(8)
REASONS = {
    "VpcFlowLog3A0C4F2A": "Resource handler returned message: " + secrets.token_hex(8),
    "Vpc8378EB38": "Resource update cancelled " + secrets.token_hex(8),
}


def action_event(execution: str, stage: str, action: str, state: str) -> dict[str, Any]:
    detail = {
        "pipeline": PIPELINE,
        "execution-id": execution,
        "stage": stage,
        "action": action,
        "state": state,
        "type": {"owner": "AWS", "category": "Deploy", "provider": "CloudFormation", "version": "1"},
    }
    return {"detail-type": handler.ACTION_CHANGE, "source": "aws.codepipeline", "account": ACCOUNT, "detail": detail}


def stage_event(execution: str, state: str) -> dict[str, Any]:
    detail = {"pipeline": PIPELINE, "execution-id": execution, "stage": STAGE, "state": state}
    return {"detail-type": handler.STAGE_CHANGE, "source": "aws.codepipeline", "account": ACCOUNT, "detail": detail}


class Stub:
    """CodePipeline, CloudFormation, S3, Secrets Manager and SNS for one execution of one commit,
    and the GitHub client: every call they are asked to make, in order, is in `calls`."""

    def __init__(self, commit: str, run_status: str = "Failed", refuse: bool = False) -> None:
        self.execution = str(uuid.uuid4())
        self.commit = commit
        self.run_status = run_status
        self.refuse = refuse
        self.calls: list[tuple[str, Any]] = []

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        return {"SecretString": json.dumps({"app_id": "1", "installation_id": "2", "private_key": "not a key"})}

    def get_pipeline_execution(self, pipelineName: str, pipelineExecutionId: str) -> dict[str, Any]:
        revisions = [{"name": "Source", "revisionId": self.commit}]
        return {"pipelineExecution": {"status": "InProgress", "artifactRevisions": revisions}}

    def get_pipeline_state(self, name: str) -> dict[str, Any]:
        return {"stageStates": []}

    def list_action_executions(self, pipelineName: str, filter: dict[str, str]) -> dict[str, Any]:
        deploy = {
            "stageName": STAGE,
            "actionName": "Network.Deploy",
            "status": self.run_status,
            "startTime": STARTED,
            "input": {
                "actionTypeId": {"category": "Deploy", "provider": "CloudFormation"},
                "configuration": {"StackName": STACK, "ActionMode": "CHANGE_SET_EXECUTE"},
            },
            "output": {
                "executionResult": {
                    "externalExecutionSummary": ERROR,
                    "externalExecutionUrl": "https://example.com/not-in-the-message",
                    "errorDetails": {"code": "JobFailed", "message": ERROR},
                }
            },
        }
        synth = {
            "stageName": "Build",
            "actionName": "Synth",
            "status": "Succeeded",
            "startTime": STARTED - datetime.timedelta(minutes=10),
            "output": {"outputArtifacts": [{"s3location": {"bucket": "example-artifacts", "key": "assembly.zip"}}]},
        }
        return {"actionExecutionDetails": [deploy, synth]}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Rhi.template.json", json.dumps({"Resources": {logical_id: {} for logical_id in REASONS}}))
        return {"Body": io.BytesIO(buffer.getvalue())}

    def describe_stack_events(self, StackName: str) -> dict[str, Any]:
        later = STARTED + datetime.timedelta(minutes=2)
        return {
            "StackEvents": [
                {"LogicalResourceId": STACK, "ResourceStatus": "UPDATE_ROLLBACK_COMPLETE", "Timestamp": later},
                *(
                    {
                        "LogicalResourceId": logical_id,
                        "ResourceStatus": "UPDATE_FAILED",
                        "ResourceStatusReason": reason,
                        "Timestamp": later,
                    }
                    for logical_id, reason in REASONS.items()
                ),
                {"LogicalResourceId": "Vpc8378EB38", "ResourceStatus": "UPDATE_IN_PROGRESS", "Timestamp": STARTED},
            ]
        }

    def publish(self, TopicArn: str, Subject: str, Message: str) -> dict[str, str]:
        self.calls.append(("publish", (TopicArn, Subject, Message)))
        if self.refuse:
            raise ClientError({"Error": {"Code": "AuthorizationError", "Message": ERROR}}, "Publish")
        return {"MessageId": str(uuid.uuid4())}

    def post_status(
        self, commit: str, state: str, context: str, description: str, *, deadline: float | None = None
    ) -> None:
        self.calls.append(("status", (commit, state, context, description)))

    def run(self, events: list[dict[str, Any]]) -> None:
        aws = handler.Aws(codepipeline=self, cloudformation=self, s3=self, secrets=self, sns=self)
        for event in events:
            assert handler.handle(event, aws=aws, client=self, stages=[STAGE], topic=TOPIC) is None

    def published(self) -> list[tuple[str, str, str]]:
        return [arguments for kind, arguments in self.calls if kind == "publish"]

    def statuses(self) -> list[tuple[str, str, str, str]]:
        return [arguments for kind, arguments in self.calls if kind == "status"]


@pytest.fixture
def commit(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(SECRET_VARIABLE, SECRET)
    return secrets.token_hex(20)


def failed(execution: str) -> list[dict[str, Any]]:
    return [
        stage_event(execution, "STARTED"),
        action_event(execution, STAGE, "Network.Deploy", "STARTED"),
        action_event(execution, STAGE, "Network.Deploy", "FAILED"),
        stage_event(execution, "FAILED"),
    ]


@pytest.mark.parametrize(
    ("state", "run_status"),
    [("FAILED", "Failed"), ("ABANDONED", "Abandoned")],
    ids=["a failed action", "an errored action"],
)
def test_a_failed_execution_gives_one_message_with_the_error_and_the_reasons(
    commit: str, state: str, run_status: str
) -> None:
    stub = Stub(commit, run_status=run_status)
    events = failed(stub.execution)
    events[2]["detail"]["state"] = state
    stub.run(events[:3])
    # The message follows every status of the action's event.
    assert stub.calls[-1][0] == "publish"
    stub.run(events[3:])

    ((topic, subject, body),) = stub.published()
    assert topic == TOPIC
    assert len(subject) <= notify.SUBJECT_LIMIT and ERROR not in subject
    for fact in [commit, stub.execution, STAGE, "Network.Deploy", "JobFailed", ERROR, *REASONS.values()]:
        assert fact in body
    for logical_id in REASONS:
        assert f"{logical_id} UPDATE_FAILED" in body
    assert "UPDATE_ROLLBACK_COMPLETE" not in body and "example.com" not in body
    # No status carries AWS's text.
    assert stub.statuses() and all(ERROR not in description for *_, description in stub.statuses())


def test_a_failed_step_before_the_stage_gives_its_own_stage(commit: str) -> None:
    stub = Stub(commit)
    stub.run([action_event(stub.execution, "Build", "Synth", "FAILED")])
    ((_, _, body),) = stub.published()
    assert "Stage: Build" in body and "Action: Synth" in body


def test_a_successful_execution_gives_none(commit: str) -> None:
    stub = Stub(commit, run_status="Succeeded")
    execution = stub.execution
    stub.run(
        [
            stage_event(execution, "STARTED"),
            action_event(execution, STAGE, "Network.Deploy", "STARTED"),
            action_event(execution, STAGE, "Network.Deploy", "SUCCEEDED"),
            stage_event(execution, "SUCCEEDED"),
        ]
    )
    assert stub.published() == []
    assert stub.statuses()[-1][1] == "success"
    facts = {"stage": STAGE, "action": "Network.Deploy", "state": "SUCCEEDED", "commit": commit}
    assert notify.message_for(facts) is None


def test_a_topic_that_refuses_the_message_leaves_the_statuses_posted(
    commit: str, caplog: pytest.LogCaptureFixture
) -> None:
    sent = Stub(commit)
    sent.run(failed(sent.execution))
    refused = Stub(commit, refuse=True)
    refused.execution = sent.execution
    with caplog.at_level(logging.WARNING):
        refused.run(failed(refused.execution))
    assert len(refused.published()) == 1
    assert refused.statuses() == sent.statuses()
    assert ("failure", f"deploy/{STAGE}") in {(state, context) for _, state, context, _ in refused.statuses()}
    assert handler.NOT_SENT in [record.getMessage() for record in caplog.records]
    assert ERROR not in caplog.text
