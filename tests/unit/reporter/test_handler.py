"""The reporter's function, on recorded sequences of CodePipeline's events and on the measurement
events of a job, with stubs of the AWS services it reads and of the GitHub client it posts with.

The events are shaped as EventBridge delivers them for the pipeline of `src/rhi/pipeline/stack.py`:
the steps `Build/Synth` before the stage `Rhi`, whose actions check widened permissions, wait for
an approval and deploy the stack `Rhi-Network`, and whose verification, the action `Verify`, leaves
its marks in the artifact bucket under `verification/<execution ID>/`. The account is the
documented one, and the commit is made here.
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

from rhi_reporter import handler, measure
from rhi_reporter.github import SECRET_VARIABLE, RefusedError
from rhi_reporter.status import VERIFIED_DESCRIPTION, CommitStatus, Description, State

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


def verification(execution: str, state: str) -> list[dict[str, Any]]:
    """The events of the stage's verification, which ends in `state`."""
    return [
        action_event(execution, STAGE, "Verify", "Build", "STARTED"),
        action_event(execution, STAGE, "Verify", "Build", state),
    ]


def assembly() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, template in TEMPLATES.items():
            archive.writestr(path, json.dumps(template))
        archive.writestr("manifest.json", json.dumps({"Resources": {"NotATemplate": {}}}))
    return buffer.getvalue()


class Stub:
    """CodePipeline, CloudFormation, S3 and Secrets Manager, for one execution of one commit, and
    the manual approvals of any execution it is told of."""

    def __init__(self, execution: str, commit: str) -> None:
        self.execution = execution
        self.commit = commit
        # Other executions, by their ID, with their commit.
        self.others: dict[str, str] = {}
        # The runs of the stage's approval `Check.Confirm`, by execution: their status and start.
        self.approvals: dict[str, list[tuple[str, datetime.datetime]]] = {}
        self.secret: dict[str, str] | None = {"app_id": "1", "installation_id": "2", "private_key": "not a key"}
        self.stage_states: dict[str, str] = {}
        self.status = "InProgress"
        self.objects_read: list[tuple[str, str]] = []
        # The verification's marks, by their key in the artifact bucket, and whether S3 refuses to
        # list them.
        self.marks: dict[str, bytes] = {}
        self.refuses_to_list = False

    def mark(self, name: str, held: str, execution: str | None = None) -> None:
        """Leaves a mark, as the verification step of an execution does: this one's unless named."""
        self.marks[f"verification/{execution or self.execution}/{name}"] = held.encode()

    # Secrets Manager.
    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        if self.secret is None or SecretId != SECRET:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": MARKER}}, "GetSecretValue")
        return {"SecretString": json.dumps(self.secret)}

    # CodePipeline.
    def get_pipeline_execution(self, pipelineName: str, pipelineExecutionId: str) -> dict[str, Any]:
        assert pipelineName == PIPELINE and pipelineExecutionId in {self.execution, *self.others}
        commit = self.others.get(pipelineExecutionId, self.commit)
        revisions = [{"name": "example_owner_example_repository_Source", "revisionId": commit}]
        record = {"pipelineExecutionId": pipelineExecutionId, "status": self.status, "artifactRevisions": revisions}
        return {"pipelineExecution": record}

    def get_pipeline_state(self, name: str) -> dict[str, Any]:
        assert name == PIPELINE
        return {
            "stageStates": [
                {"stageName": stage, "latestExecution": {"pipelineExecutionId": self.execution, "status": status}}
                for stage, status in self.stage_states.items()
            ]
        }

    def list_pipeline_executions(self, pipelineName: str, maxResults: int) -> dict[str, Any]:
        assert pipelineName == PIPELINE
        revisions = [{"actionName": "GitHub", "revisionId": self.commit}]
        summary = {"pipelineExecutionId": self.execution, "status": self.status, "startTime": STARTED}
        return {"pipelineExecutionSummaries": [summary | {"sourceRevisions": revisions}]}

    def list_action_executions(self, pipelineName: str, filter: dict[str, str]) -> dict[str, Any]:
        assert pipelineName == PIPELINE and set(filter) == {"pipelineExecutionId"}
        execution = filter["pipelineExecutionId"]
        approvals = [
            {
                "stageName": STAGE,
                "actionName": "Check.Confirm",
                "status": status,
                "startTime": started,
                "input": {"actionTypeId": {"category": "Approval", "provider": "Manual"}, "configuration": {}},
                "output": {},
            }
            for status, started in self.approvals.get(execution, [])
        ]
        if execution != self.execution:
            assert execution in self.others
            return {"actionExecutionDetails": approvals}
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
        return {"actionExecutionDetails": [deploy, *approvals, synth]}

    # S3: the artifact store. It lists under verification/ only, as the function's role may, and
    # answers a read of an object that is not there as it answers a role that may not list it.
    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, Any]:
        assert Bucket == BUCKET and Prefix.startswith("verification/")
        if self.refuses_to_list:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": MARKER}}, "ListObjectsV2")
        return {"Contents": [{"Key": key} for key in sorted(self.marks) if key.startswith(Prefix)]}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == BUCKET
        self.objects_read.append((Bucket, Key))
        if Key not in {SYNTH_KEY, *self.marks}:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": MARKER}}, "GetObject")
        return {"Body": io.BytesIO(self.marks[Key] if Key in self.marks else assembly())}

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


def run(
    events: list[dict[str, Any]],
    stub: Stub,
    client: Client,
    smoke_tests: tuple[str, ...] = (),
    lifecycle_stages: tuple[str, ...] = (),
) -> None:
    """Hands each event to the function, as CodePipeline's state stands when it is sent, with
    `smoke_tests` as the allow-list of smoke test IDs and `lifecycle_stages` as that of stages."""
    aws = handler.Aws(codepipeline=stub, cloudformation=stub, s3=stub, secrets=stub)
    for event in events:
        detail = event["detail"]
        if event["detail-type"] == handler.STAGE_CHANGE:
            stub.stage_states[detail["stage"]] = STAGE_STATES[detail["state"]]
        told = handler.handle(
            event,
            aws=aws,
            client=client,
            stages=[STAGE],
            smoke_tests=smoke_tests,
            artifacts=BUCKET,
            lifecycle_stages=lifecycle_stages,
        )
        assert told is None


DEPLOY = f"deploy/{STAGE}"
VERIFY = f"verify/{STAGE}"


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


def test_a_failed_verification_names_its_failed_smoke_tests_by_their_listed_ids(stub: Stub, commit: str) -> None:
    # What the step left: two IDs of the allow-list, and a line that is none, as a test's name with
    # what it printed would be.
    unlisted = f"tests/smoke/test_smoke.py::test_hello {MARKER}"
    stub.mark("failed-smoke-tests", f"no-key\n{unlisted}\nhello\n")
    client = Client()
    run(verification(stub.execution, "FAILED"), stub, client, ("hello", "no-key", "environment"))
    assert client.posted == [
        (commit, "pending", VERIFY, "The verification is running."),
        (commit, "failure", VERIFY, "The verification failed."),
        (commit, "failure", f"{VERIFY}/action", f"{STAGE} Verify"),
        (commit, "failure", f"{VERIFY}/test/1", "hello"),
        (commit, "failure", f"{VERIFY}/test/2", "no-key"),
        # The unlisted line, as a count and nothing else.
        (commit, "failure", f"{VERIFY}/dropped", "1"),
    ]
    assert MARKER not in str(client.posted)
    # The mark was read from the folder of the event's own execution.
    assert stub.objects_read == [(BUCKET, f"verification/{stub.execution}/failed-smoke-tests")]


def test_a_failed_verification_names_the_last_stage_an_instance_reached(
    stub: Stub, commit: str, caplog: pytest.LogCaptureFixture
) -> None:
    # What the step left: the stage of two instances that never served, and a line that is no
    # stage of the allow-list, as anything else of a heartbeat's row would be.
    unlisted = f"instance {MARKER}"
    stub.mark("last-stages", f"weights\n{unlisted}\nweights\n")
    client = Client()
    with caplog.at_level(logging.DEBUG):
        run(verification(stub.execution, "FAILED"), stub, client, lifecycle_stages=("boot", "weights", "warm-up"))
    assert client.posted == [
        (commit, "pending", VERIFY, "The verification is running."),
        (commit, "failure", VERIFY, "The verification failed."),
        (commit, "failure", f"{VERIFY}/action", f"{STAGE} Verify"),
        (commit, "failure", f"{VERIFY}/lifecycle/1", "weights"),
        # The unlisted line, as a count and nothing else.
        (commit, "failure", f"{VERIFY}/dropped", "1"),
    ]
    assert MARKER not in str(client.posted) and MARKER not in caplog.text
    # The mark was read from the folder of the event's own execution.
    assert stub.objects_read == [(BUCKET, f"verification/{stub.execution}/last-stages")]


@pytest.mark.parametrize("left", ["nothing", "another execution's", "marks that cannot be listed"])
def test_a_failed_verification_with_no_marks_to_read_names_no_test_and_no_stage(
    stub: Stub, commit: str, left: str, caplog: pytest.LogCaptureFixture
) -> None:
    marks = {"failed-smoke-tests": "hello\n", "last-stages": "weights\n"}
    if left == "another execution's":
        other = str(uuid.uuid4())
        for name, held in marks.items():
            stub.mark(name, held, execution=other)
    # Marks of this execution that S3 does not let the function list.
    stub.refuses_to_list = left == "marks that cannot be listed"
    if stub.refuses_to_list:
        for name, held in marks.items():
            stub.mark(name, held)
    client = Client()
    with caplog.at_level(logging.DEBUG):
        run(verification(stub.execution, "FAILED")[1:], stub, client, ("hello", "no-key"), ("weights",))
    # The failure and its action, and no status of a smoke test or of a stage, not even a count.
    assert client.posted == [
        (commit, "failure", VERIFY, "The verification failed."),
        (commit, "failure", f"{VERIFY}/action", f"{STAGE} Verify"),
    ]
    assert stub.objects_read == []
    assert [record.getMessage() for record in caplog.records] == (
        [handler.NO_FAILED_TESTS, handler.NO_LAST_STAGES] if stub.refuses_to_list else []
    )
    assert MARKER not in caplog.text


def test_the_function_reads_its_allow_list_and_its_bucket_from_its_environment(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.mark("failed-smoke-tests", "hello\nno-key\nenvironment\n")
    aws, client = handler.Aws(codepipeline=stub, cloudformation=stub, s3=stub, secrets=stub), Client()
    monkeypatch.setattr(handler, "_clients", lambda: (aws, client))
    monkeypatch.setenv(handler.STAGES_VARIABLE, STAGE)
    (event,) = verification(stub.execution, "FAILED")[1:]

    def reported() -> list[tuple[str, str]]:
        """The contexts and descriptions of the report the function posts for the event."""
        client.posted.clear()
        handler.handler(event, None)
        return [(context, description) for _, _, context, description in client.posted[1:]]

    # The variables as the function's construct writes them. Without the allow-list no test is
    # named, and without the bucket no mark is read.
    monkeypatch.delenv("RHI_SMOKE_TEST_IDS", raising=False)
    monkeypatch.setenv("RHI_REPORTER_ARTIFACTS", BUCKET)
    assert reported() == [(f"{VERIFY}/action", f"{STAGE} Verify"), (f"{VERIFY}/dropped", "3")]
    monkeypatch.setenv("RHI_SMOKE_TEST_IDS", "hello,no-key")
    assert reported() == [
        (f"{VERIFY}/action", f"{STAGE} Verify"),
        (f"{VERIFY}/test/1", "hello"),
        (f"{VERIFY}/test/2", "no-key"),
        (f"{VERIFY}/dropped", "1"),
    ]
    monkeypatch.delenv("RHI_REPORTER_ARTIFACTS")
    assert reported() == [(f"{VERIFY}/action", f"{STAGE} Verify")]


def test_the_function_reads_its_lifecycle_stages_from_its_environment(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.mark("last-stages", "weights\nnot-a-stage\nboot\n")
    aws, client = handler.Aws(codepipeline=stub, cloudformation=stub, s3=stub, secrets=stub), Client()
    monkeypatch.setattr(handler, "_clients", lambda: (aws, client))
    monkeypatch.setenv(handler.STAGES_VARIABLE, STAGE)
    monkeypatch.setenv("RHI_REPORTER_ARTIFACTS", BUCKET)
    (event,) = verification(stub.execution, "FAILED")[1:]

    def reported() -> list[tuple[str, str]]:
        """The contexts and descriptions of the report the function posts for the event."""
        client.posted.clear()
        handler.handler(event, None)
        return [(context, description) for _, _, context, description in client.posted[1:]]

    # The variable as the function's construct writes it. Without it the allow-list is empty, and
    # no stage is named.
    monkeypatch.delenv("RHI_LIFECYCLE_STAGES", raising=False)
    assert reported() == [(f"{VERIFY}/action", f"{STAGE} Verify"), (f"{VERIFY}/dropped", "3")]
    monkeypatch.setenv("RHI_LIFECYCLE_STAGES", "boot,release-check,weights")
    assert reported() == [
        (f"{VERIFY}/action", f"{STAGE} Verify"),
        (f"{VERIFY}/lifecycle/1", "boot"),
        (f"{VERIFY}/lifecycle/2", "weights"),
        (f"{VERIFY}/dropped", "1"),
    ]


def test_a_verification_that_ran_nothing_is_not_verified(stub: Stub, commit: str) -> None:
    def described(reason: str | None) -> str:
        """The description posted for a verification that passed and left `reason` as its
        not-run mark, or no such mark."""
        stub.marks.clear()
        if reason is not None:
            stub.mark("not-run", f"{reason}\n")
        client = Client()
        run(verification(stub.execution, "SUCCEEDED")[1:], stub, client)
        ((posted_on, state, context, description),) = client.posted
        # A success of the verification's context either way: the step did not fail.
        assert (posted_on, state, context) == (commit, "success", VERIFY)
        return description

    reasons = ("canary-missing", "listener-off", "parked", "dormant")
    canary, listener, parked, unknown = (described(reason) for reason in reasons)
    # One description of the vocabulary for each known reason, and one for a reason not known yet.
    assert (canary, listener, parked, unknown) == (
        Description.NOT_RUN_CANARY_MISSING,
        Description.NOT_RUN_LISTENER_OFF,
        Description.NOT_RUN_PARKED,
        Description.NOT_RUN_OTHER,
    )
    assert len({canary, listener, parked, unknown}) == 4
    assert VERIFIED_DESCRIPTION not in (canary, listener, parked, unknown)
    # A mark that holds nothing still says that no test ran.
    assert described("") == Description.NOT_RUN_OTHER
    # Only a verification that left no such mark ran its tests.
    assert described(None) == VERIFIED_DESCRIPTION


def test_a_success_is_not_posted_while_the_marks_cannot_be_read(stub: Stub, caplog: pytest.LogCaptureFixture) -> None:
    stub.refuses_to_list = True
    client = Client()
    # The function fails, so that the event is invoked again: the verification may have run nothing.
    with caplog.at_level(logging.DEBUG), pytest.raises(ClientError):
        run(verification(stub.execution, "SUCCEEDED")[1:], stub, client)
    assert client.posted == []
    assert [record.getMessage() for record in caplog.records] == [handler.NO_NOT_RUN]
    assert MARKER not in caplog.text


def test_a_superseded_commit_is_not_verified_by_a_verification_that_ran_nothing(stub: Stub, commit: str) -> None:
    earlier, superseded_commit = str(uuid.uuid4()), secrets.token_hex(20)

    class Covering(Stub):
        """The stub's execution after an earlier one that a self-update superseded: it ended in
        success, and so did its verification."""

        def list_pipeline_executions(self, pipelineName: str, maxResults: int) -> dict[str, Any]:
            own, before = STARTED, STARTED - datetime.timedelta(hours=1)
            return {
                "pipelineExecutionSummaries": [
                    summary(self.execution, "Succeeded", own, self.commit),
                    summary(earlier, "Superseded", before, superseded_commit),
                ]
            }

        def list_action_executions(self, pipelineName: str, filter: dict[str, str]) -> dict[str, Any]:
            assert filter == {"pipelineExecutionId": self.execution}
            verify = {"stageName": STAGE, "actionName": "Verify", "status": "Succeeded", "startTime": STARTED}
            return {"actionExecutionDetails": [verify]}

    def summary(execution: str, status: str, started: datetime.datetime, deploys: str) -> dict[str, Any]:
        revisions = [{"actionName": "GitHub", "revisionId": deploys}]
        return {"pipelineExecutionId": execution, "status": status, "startTime": started, "sourceRevisions": revisions}

    covering = Covering(stub.execution, commit)
    covering.mark("not-run", "listener-off\n")
    client = Client()
    run([pipeline_event(covering.execution, "SUCCEEDED")], covering, client)
    # The superseded commit takes the covering execution's verdict, and its verification's as it ran.
    assert client.posted == [
        (superseded_commit, "success", DEPLOY, Description.DEPLOYED),
        (superseded_commit, "success", VERIFY, Description.NOT_RUN_LISTENER_OFF),
    ]


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


@pytest.mark.parametrize(
    ("execution_status", "stage_failed"),
    [("Failed", False), ("InProgress", True)],
    ids=["the execution ended", "a stage before failed"],
)
def test_a_late_step_before_the_stage_does_not_hide_its_failure(
    stub: Stub, execution_status: str, stage_failed: bool
) -> None:
    stub.status = execution_status
    client = Client()
    events = [action_event(stub.execution, "Build", "Synth", "Build", "FAILED")]
    if stage_failed:
        events.append(stage_event(stub.execution, "Build", "FAILED"))
    # The source step's success arrives last; the stage `Rhi` never started in this execution.
    events.append(action_event(stub.execution, "Source", "GitHub", "Source", "SUCCEEDED"))
    run(events, stub, client)
    assert client.posted[0][1:3] == ("failure", DEPLOY)
    assert all(state == "failure" for _, state, _, _ in client.posted)


def deliver(events: list[dict[str, Any]], stub: Stub, client: Client) -> None:
    """Hands each event to the function as it arrives, late or out of order: CodePipeline's state
    is the one the test set, not the one the event tells."""
    aws = handler.Aws(codepipeline=stub, cloudformation=stub, s3=stub, secrets=stub)
    for event in events:
        assert handler.handle(event, aws=aws, client=client, stages=[STAGE], artifacts=BUCKET) is None


def deployment(client: Client, commit: str) -> list[tuple[str, str]]:
    """The states and descriptions posted to the deployment's context of `commit`, in order."""
    return [(state, text) for on, state, context, text in client.posted if (on, context) == (commit, DEPLOY)]


def approval(execution: str, state: str) -> dict[str, Any]:
    return action_event(execution, STAGE, "Check.Confirm", "Approval", state)


def progress(execution: str) -> list[dict[str, Any]]:
    """Ordinary pending events of an execution whose stage reached its approval."""
    return [
        stage_event(execution, STAGE, "STARTED"),
        action_event(execution, STAGE, "Check.Check", "Build", "SUCCEEDED"),
        action_event(execution, "Build", "Synth", "Build", "SUCCEEDED"),
    ]


LATER = STARTED + datetime.timedelta(minutes=5)
WAITING = ("pending", Description.APPROVAL_WAITING)
RUNNING = ("pending", Description.DEPLOYING)


@pytest.mark.parametrize("start_first", [True, False], ids=["the approval's start first", "the approval's start last"])
@pytest.mark.parametrize(
    "runs",
    [[("InProgress", STARTED)], [("InProgress", LATER), ("Failed", STARTED)]],
    ids=["its first run", "its run after a rejection and a retry"],
)
def test_ordinary_progress_leaves_the_approval_visible_while_it_waits(
    stub: Stub, commit: str, start_first: bool, runs: list[tuple[str, datetime.datetime]]
) -> None:
    stub.approvals[stub.execution] = runs
    events = progress(stub.execution)
    events.insert(0 if start_first else len(events), approval(stub.execution, "STARTED"))
    client = Client()
    deliver(events, stub, client)
    assert deployment(client, commit) == [WAITING] * 4


@pytest.mark.parametrize(
    "runs",
    [[("Succeeded", STARTED)], [("Failed", STARTED), ("Succeeded", LATER)]],
    ids=["its first run", "its run after a rejection and a retry"],
)
def test_progress_resumes_once_the_approval_resolves(
    stub: Stub, commit: str, runs: list[tuple[str, datetime.datetime]]
) -> None:
    (*before, (_, started)) = runs
    stub.approvals[stub.execution] = [*before, ("InProgress", started)]
    client = Client()
    deliver([approval(stub.execution, "STARTED")], stub, client)
    stub.approvals[stub.execution] = runs
    events = [
        approval(stub.execution, "SUCCEEDED"),
        action_event(stub.execution, STAGE, "Network.Deploy", "Deploy", "STARTED"),
    ]
    # The resolved attempt's start, delayed, before and after the progress that followed it.
    deliver([approval(stub.execution, "STARTED"), *events, approval(stub.execution, "STARTED")], stub, client)
    stub.stage_states[STAGE] = "Succeeded"
    deliver([stage_event(stub.execution, STAGE, "SUCCEEDED"), approval(stub.execution, "STARTED")], stub, client)
    assert deployment(client, commit) == [WAITING, RUNNING, RUNNING, ("success", Description.DEPLOYED)]


def test_an_approval_of_another_execution_changes_nothing(stub: Stub, commit: str) -> None:
    other, other_commit = str(uuid.uuid4()), secrets.token_hex(20)
    stub.others[other] = other_commit
    stub.approvals[other] = [("InProgress", LATER)]
    # This execution's own approval was resolved.
    stub.approvals[stub.execution] = [("Succeeded", STARTED)]
    client = Client()
    deliver([approval(other, "STARTED"), *progress(stub.execution), approval(stub.execution, "STARTED")], stub, client)
    assert deployment(client, commit) == [RUNNING] * 3
    # The other execution's wait is posted on its own commit only.
    assert deployment(client, other_commit) == [WAITING]


def test_an_approval_of_another_stage_is_a_step_that_runs(
    stub: Stub, commit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = stub.list_action_executions

    def with_preflight(pipelineName: str, filter: dict[str, str]) -> dict[str, Any]:
        answer = listed(pipelineName, filter)
        kind = {"actionTypeId": {"category": "Approval", "provider": "Manual"}}
        record = {"stageName": "Preflight", "actionName": "Confirm", "status": "InProgress", "startTime": STARTED}
        return {"actionExecutionDetails": [*answer["actionExecutionDetails"], record | {"input": kind}]}

    monkeypatch.setattr(stub, "list_action_executions", with_preflight)
    client = Client()
    deliver([action_event(stub.execution, "Preflight", "Confirm", "Approval", "STARTED")], stub, client)
    assert deployment(client, commit) == [RUNNING]


@pytest.mark.parametrize("resolved", [False, True], ids=["while it waits", "once it resolved"])
def test_unreadable_approvals_leave_the_last_status_in_place(
    stub: Stub, commit: str, resolved: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stub.approvals[stub.execution] = [("Succeeded" if resolved else "InProgress", STARTED)]
    client = Client()
    deliver([stage_event(stub.execution, STAGE, "STARTED")], stub, client)

    def throttled(**_: object) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": MARKER}}, "ListActionExecutions")

    monkeypatch.setattr(stub, "list_action_executions", throttled)
    with caplog.at_level(logging.WARNING):
        deliver([approval(stub.execution, "STARTED"), *progress(stub.execution)], stub, client)
    assert deployment(client, commit) == [RUNNING if resolved else WAITING]
    assert handler.NO_APPROVALS in caplog.messages and MARKER not in caplog.text


@pytest.mark.parametrize("secret", [None, {"app_id": "1", "installation_id": "2"}], ids=["absent", "no private_key"])
def test_no_secret_no_status(stub: Stub, secret: dict[str, str] | None, caplog: pytest.LogCaptureFixture) -> None:
    stub.secret = secret
    client = Client()
    with caplog.at_level(logging.DEBUG):
        run(failed(stub.execution)[3:4], stub, client)
    assert client.posted == []
    assert [record.getMessage() for record in caplog.records] == [handler.NO_CREDENTIAL]
    assert MARKER not in caplog.text


# A matrix of S5's kind: more numbers than one description holds.
SPEEDS = {f"output_speed_s{streams}_p50": streams * 10.25 for streams in (1, 2, 4, 8, 16, 32, 64)}


def measurement(commit: str, label: str) -> dict[str, Any]:
    """The event EventBridge delivers for one run that a job sent from `commit`."""
    entry = measure.measurement_event(
        "S5", label, measure.Verdict.PASS, SPEEDS, names=list(SPEEDS), commit=commit, bus="rhi-measurements"
    )
    return wrapped(entry["DetailType"], json.loads(entry["Detail"])) | {"source": entry["Source"], "resources": []}


def test_a_measurement_posts_its_statuses_on_the_job_s_commit(stub: Stub) -> None:
    # Not the commit of the pipeline's execution: the one the event names.
    job_commit = secrets.token_hex(20)
    for label in ("8bit", "4bit"):
        client = Client()
        run([measurement(job_commit, label)], stub, client)
        assert [context for _, _, context, _ in client.posted] == [f"measure/S5/{label}/1", f"measure/S5/{label}/2"]
        assert {posted_on for posted_on, _, _, _ in client.posted} == {job_commit}
        statuses = [CommitStatus(c, State(state), context, text) for c, state, context, text in client.posted]
        (run_read,) = measure.decode(statuses)
        assert (run_read.label, run_read.verdict, dict(run_read.numbers)) == (label, measure.Verdict.PASS, SPEEDS)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"numbers": SPEEDS | {"undeclared": 1}}, id="a name outside the declared enumeration"),
        pytest.param({"measurement": "S99"}, id="an unknown id"),
        pytest.param({"label": "8/bit"}, id="a run label with a slash"),
        pytest.param({"commit": "not-a-sha"}, id="a commit that is not a SHA"),
        pytest.param({"numbers": SPEEDS | {"output_speed_s1_p50": "12"}}, id="text where a number belongs"),
    ],
)
def test_a_bad_measurement_posts_nothing(
    stub: Stub, commit: str, change: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    event = measurement(commit, "8bit")
    event["detail"] |= change
    client = Client()
    with caplog.at_level(logging.DEBUG):
        run([event], stub, client)
    assert client.posted == []
    assert [record.getMessage() for record in caplog.records] == [handler.NOT_A_MEASUREMENT]


def test_a_refused_measurement_is_logged_and_not_retried(stub: Stub, caplog: pytest.LogCaptureFixture) -> None:
    class Refusing(Client):
        def post_status(self, commit: str, state: str, context: str, description: str) -> None:
            super().post_status(commit, state, context, description)
            raise RefusedError(422, "unprocessable")

    client = Refusing()
    with caplog.at_level(logging.DEBUG):
        run([measurement(secrets.token_hex(20), "8bit")], stub, client)
    assert len(client.posted) == 1
    assert [record.getMessage() for record in caplog.records] == [
        f"{handler.REFUSED} GitHub refused the call: HTTP 422, code unprocessable"
    ]
