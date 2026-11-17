"""The reporter's function, on recorded sequences of CodePipeline's events, with stubs of the AWS
services it reads and of the GitHub client it posts with.

The events are shaped as EventBridge delivers them for the pipeline of `src/rhi/pipeline/stack.py`:
the steps `Build/Synth` before the stage `Rhi`, whose actions check widened permissions, wait for
an approval and deploy the stack `Rhi-Network`. The account is the documented one, and the commit
is made here.
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

from rhi_reporter import handler
from rhi_reporter.github import SECRET_VARIABLE

ACCOUNT = "123456789012"
REGION = "us-east-1"
PIPELINE = "RhiPipeline-CodePipeline"
STAGE = "Rhi"
STACK = "Rhi-Network"
SECRET = "rhi/reporter/github-app"
BUCKET = "example-artifacts"
SYNTH_KEY = "RhiPipeline-CodePi/Synth_Outp/assembly.zip"
STARTED = datetime.datetime(2026, 11, 2, 9, 30, tzinfo=datetime.UTC)
# What a stub says in its own words, which no log may repeat.
MARKER = "said-by-the-stub " + secrets.token_hex(8)

# The templates the synth step of the execution wrote, by their path in its output.
TEMPLATES = {
    "RhiPipeline.template.json": {"Resources": {"CodePipelineB74E5936": {}}},
    "assembly-Rhi/RhiNetwork779B463E.template.json": {"Resources": {"Vpc8378EB38": {}, "VpcFlowLog3A0C4F2A": {}}},
}

# The pipeline's own states, as `GetPipelineState` spells them, for a stage's event.
STAGE_STATES = {"STARTED": "InProgress", "SUCCEEDED": "Succeeded", "FAILED": "Failed"}


def stage_event(execution: str, stage: str, state: str) -> dict[str, Any]:
    detail = {"pipeline": PIPELINE, "execution-id": execution, "stage": stage, "state": state, "version": 3}
    return wrapped(handler.STAGE_CHANGE, detail)


def action_event(execution: str, stage: str, action: str, category: str, state: str) -> dict[str, Any]:
    detail = {
        "pipeline": PIPELINE,
        "execution-id": execution,
        "stage": stage,
        "action": action,
        "state": state,
        "region": REGION,
        "type": {"owner": "AWS", "category": category, "provider": "CodeBuild", "version": "1"},
        "version": 3,
    }
    return wrapped(handler.ACTION_CHANGE, detail)


def pipeline_event(execution: str, state: str) -> dict[str, Any]:
    detail = {"pipeline": PIPELINE, "execution-id": execution, "state": state, "version": 3}
    return wrapped("CodePipeline Pipeline Execution State Change", detail)


def wrapped(kind: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": kind,
        "source": "aws.codepipeline",
        "account": ACCOUNT,
        "time": "2026-11-02T09:31:00Z",
        "region": REGION,
        "resources": [f"arn:aws:codepipeline:{REGION}:{ACCOUNT}:{PIPELINE}"],
        "detail": detail,
    }


def succeeded(execution: str) -> list[dict[str, Any]]:
    return [
        pipeline_event(execution, "STARTED"),
        stage_event(execution, "Build", "STARTED"),
        action_event(execution, "Build", "Synth", "Build", "STARTED"),
        action_event(execution, "Build", "Synth", "Build", "SUCCEEDED"),
        stage_event(execution, "Build", "SUCCEEDED"),
        stage_event(execution, STAGE, "STARTED"),
        action_event(execution, STAGE, "Check.Confirm", "Approval", "STARTED"),
        action_event(execution, STAGE, "Check.Confirm", "Approval", "SUCCEEDED"),
        action_event(execution, STAGE, "Network.Deploy", "Deploy", "SUCCEEDED"),
        stage_event(execution, STAGE, "SUCCEEDED"),
        pipeline_event(execution, "SUCCEEDED"),
    ]


def failed(execution: str) -> list[dict[str, Any]]:
    return [
        pipeline_event(execution, "STARTED"),
        stage_event(execution, STAGE, "STARTED"),
        action_event(execution, STAGE, "Network.Prepare", "Deploy", "SUCCEEDED"),
        action_event(execution, STAGE, "Network.Deploy", "Deploy", "FAILED"),
        stage_event(execution, STAGE, "FAILED"),
        pipeline_event(execution, "FAILED"),
    ]


def assembly() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, template in TEMPLATES.items():
            archive.writestr(path, json.dumps(template))
        archive.writestr("manifest.json", json.dumps({"Resources": {"NotATemplate": {}}}))
    return buffer.getvalue()


class Stub:
    """CodePipeline, CloudFormation, S3 and Secrets Manager, for one execution of one commit."""

    def __init__(self, execution: str, commit: str) -> None:
        self.execution = execution
        self.commit = commit
        self.secret: dict[str, str] | None = {"app_id": "1", "installation_id": "2", "private_key": "not a key"}
        self.stage_states: dict[str, str] = {}
        self.objects_read: list[tuple[str, str]] = []

    # Secrets Manager.
    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        if self.secret is None or SecretId != SECRET:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": MARKER}}, "GetSecretValue")
        return {"SecretString": json.dumps(self.secret)}

    # CodePipeline.
    def get_pipeline_execution(self, pipelineName: str, pipelineExecutionId: str) -> dict[str, Any]:
        assert (pipelineName, pipelineExecutionId) == (PIPELINE, self.execution)
        revisions = [{"name": "example_owner_example_repository_Source", "revisionId": self.commit}]
        return {"pipelineExecution": {"pipelineExecutionId": self.execution, "artifactRevisions": revisions}}

    def get_pipeline_state(self, name: str) -> dict[str, Any]:
        assert name == PIPELINE
        return {
            "stageStates": [
                {"stageName": stage, "latestExecution": {"pipelineExecutionId": self.execution, "status": status}}
                for stage, status in self.stage_states.items()
            ]
        }

    def list_action_executions(self, pipelineName: str, filter: dict[str, str]) -> dict[str, Any]:
        assert (pipelineName, filter) == (PIPELINE, {"pipelineExecutionId": self.execution})
        synth = {
            "stageName": "Build",
            "actionName": "Synth",
            "status": "Succeeded",
            "startTime": STARTED - datetime.timedelta(minutes=10),
            "input": {"actionTypeId": {"category": "Build", "provider": "CodeBuild"}, "configuration": {}},
            "output": {
                "outputArtifacts": [{"name": "Synth_Output", "s3location": {"bucket": BUCKET, "key": SYNTH_KEY}}]
            },
        }
        deploy = {
            "stageName": STAGE,
            "actionName": "Network.Deploy",
            "status": "Failed",
            "startTime": STARTED,
            "input": {
                "actionTypeId": {"category": "Deploy", "provider": "CloudFormation"},
                "configuration": {"StackName": STACK, "ActionMode": "CHANGE_SET_EXECUTE"},
            },
            "output": {},
        }
        return {"actionExecutionDetails": [deploy, synth]}

    # S3: the artifact store.
    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.objects_read.append((Bucket, Key))
        return {"Body": io.BytesIO(assembly())}

    # CloudFormation: newest first, as the API gives them.
    def describe_stack_events(self, StackName: str) -> dict[str, Any]:
        assert StackName == STACK
        later = STARTED + datetime.timedelta(minutes=2)
        return {
            "StackEvents": [
                {"LogicalResourceId": STACK, "ResourceStatus": "UPDATE_ROLLBACK_COMPLETE", "Timestamp": later},
                {"LogicalResourceId": "NotInTheTemplates", "ResourceStatus": "UPDATE_FAILED", "Timestamp": later},
                {"LogicalResourceId": "VpcFlowLog3A0C4F2A", "ResourceStatus": "CREATE_FAILED", "Timestamp": later},
                {"LogicalResourceId": "Vpc8378EB38", "ResourceStatus": "UPDATE_COMPLETE", "Timestamp": STARTED},
                # A failure of an earlier deployment: not this one's.
                {
                    "LogicalResourceId": "Vpc8378EB38",
                    "ResourceStatus": "UPDATE_FAILED",
                    "Timestamp": STARTED - datetime.timedelta(days=1),
                },
            ]
        }


class Client:
    """The GitHub client: it records what it is asked to post."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str, str, str]] = []

    def post_status(self, commit: str, state: str, context: str, description: str) -> None:
        self.posted.append((commit, state, context, description))


@pytest.fixture
def commit() -> str:
    return secrets.token_hex(20)


@pytest.fixture
def stub(commit: str, monkeypatch: pytest.MonkeyPatch) -> Stub:
    monkeypatch.setenv(SECRET_VARIABLE, SECRET)
    return Stub(str(uuid.uuid4()), commit)


def run(events: list[dict[str, Any]], stub: Stub, client: Client) -> None:
    """Hands each event to the function, as CodePipeline's state stands when it is sent."""
    aws = handler.Aws(codepipeline=stub, cloudformation=stub, s3=stub, secrets=stub)
    for event in events:
        detail = event["detail"]
        if event["detail-type"] == handler.STAGE_CHANGE:
            stub.stage_states[detail["stage"]] = STAGE_STATES[detail["state"]]
        assert handler.handle(event, aws=aws, client=client, stages=[STAGE]) is None


DEPLOY = f"deploy/{STAGE}"


def test_a_successful_execution_ends_in_the_deployment_s_success(stub: Stub, commit: str) -> None:
    client = Client()
    run(succeeded(stub.execution), stub, client)
    assert client.posted == [
        (commit, "pending", DEPLOY, "The deployment is running."),
        (commit, "pending", DEPLOY, "The deployment is running."),
        (commit, "pending", DEPLOY, "The deployment is running."),
        (commit, "pending", DEPLOY, "The deployment waits for the owner's approval in the pipeline."),
        (commit, "pending", DEPLOY, "The deployment is running."),
        (commit, "pending", DEPLOY, "The deployment is running."),
        (commit, "success", DEPLOY, "The deployment succeeded."),
    ]
    assert stub.objects_read == []


def test_a_failed_execution_reports_only_resources_of_its_templates(stub: Stub, commit: str) -> None:
    client = Client()
    run(failed(stub.execution), stub, client)
    assert client.posted == [
        (commit, "pending", DEPLOY, "The deployment is running."),
        (commit, "pending", DEPLOY, "The deployment is running."),
        (commit, "failure", DEPLOY, "The deployment failed."),
        (commit, "failure", f"{DEPLOY}/action", f"{STAGE} Network.Deploy"),
        (commit, "failure", f"{DEPLOY}/resource/1", "VpcFlowLog3A0C4F2A CREATE_FAILED"),
        (commit, "failure", f"{DEPLOY}/dropped", "1"),
        (commit, "failure", DEPLOY, "The deployment failed."),
    ]
    # The templates were read from the output of the same execution's synth step.
    assert stub.objects_read == [(BUCKET, SYNTH_KEY)]
    known = {logical_id for template in TEMPLATES.values() for logical_id in template["Resources"]}
    named = [description.split()[0] for _, _, context, description in client.posted if "/resource/" in context]
    assert named and set(named) <= known


def test_a_failed_step_before_the_stage_fails_its_deployment(stub: Stub, commit: str) -> None:
    client = Client()
    events = [
        action_event(stub.execution, "Build", "Synth", "Build", "FAILED"),
        stage_event(stub.execution, "Build", "FAILED"),
    ]
    run(events, stub, client)
    assert client.posted == [
        (commit, "failure", DEPLOY, "The deployment failed."),
        (commit, "failure", f"{DEPLOY}/action", f"{STAGE} Synth"),
    ]


def test_a_late_pending_status_does_not_hide_the_verdict(stub: Stub) -> None:
    client = Client()
    events = succeeded(stub.execution)
    # The last action's event arrives after the stage's own.
    events[8], events[9] = events[9], events[8]
    run(events, stub, client)
    assert client.posted[-1][1:3] == ("success", DEPLOY)


@pytest.mark.parametrize("secret", [None, {"app_id": "1", "installation_id": "2"}], ids=["absent", "no private_key"])
def test_no_secret_no_status(stub: Stub, secret: dict[str, str] | None, caplog: pytest.LogCaptureFixture) -> None:
    stub.secret = secret
    client = Client()
    with caplog.at_level(logging.DEBUG):
        run(failed(stub.execution)[3:4], stub, client)
    assert client.posted == []
    assert [record.getMessage() for record in caplog.records] == [handler.NO_CREDENTIAL]
    assert MARKER not in caplog.text
