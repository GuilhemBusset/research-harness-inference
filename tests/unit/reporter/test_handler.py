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

from rhi_lifecycle import serving
from rhi_lifecycle.stages import Stage
from rhi_reporter import handler, measure
from rhi_reporter.github import SECRET_VARIABLE, CredentialError, RefusedError
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
HOUR = datetime.timedelta(hours=1)
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
        self.written: dict[str, datetime.datetime] = {}
        self.refuses_to_list = False
        # The runs of the stage's action `Verify` in this execution: their status and start.
        self.verifications: list[tuple[str, datetime.datetime]] = []

    def mark(self, name: str, held: str, execution: str | None = None, written: datetime.datetime = STARTED) -> None:
        """Leaves a mark at the time `written`, as the verification step of an execution does:
        this one's unless named."""
        key = f"verification/{execution or self.execution}/{name}"
        self.marks[key], self.written[key] = held.encode(), written

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
        """The stages' latest executions, all this one's; an approval's latest run of this execution
        holds the token of its request while it waits."""
        assert name == PIPELINE
        runs = self.approvals.get(self.execution, [])
        stages = self.stage_states | ({STAGE: self.stage_states.get(STAGE, "InProgress")} if runs else {})
        states = []
        for stage, status in stages.items():
            state: dict[str, Any] = {
                "stageName": stage,
                "latestExecution": {"pipelineExecutionId": self.execution, "status": status},
            }
            if stage == STAGE and runs:
                latest, _ = max(runs, key=lambda run: run[1])
                token = {"token": str(uuid.uuid4())} if latest == "InProgress" else {}
                state["actionStates"] = [{"actionName": "Check.Confirm", "latestExecution": {"status": latest} | token}]
            states.append(state)
        return {"stageStates": states}

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
        verifications = [
            {"stageName": STAGE, "actionName": "Verify", "status": status, "startTime": started}
            for status, started in self.verifications
        ]
        return {"actionExecutionDetails": [deploy, *approvals, *verifications, synth]}

    # S3: the artifact store. It lists under verification/ only, as the function's role may, and
    # answers a read of an object that is not there as it answers a role that may not list it.
    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, Any]:
        assert Bucket == BUCKET and Prefix.startswith("verification/")
        if self.refuses_to_list:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": MARKER}}, "ListObjectsV2")
        listed = [
            {"Key": key, "LastModified": self.written[key]} for key in sorted(self.marks) if key.startswith(Prefix)
        ]
        return {"Contents": listed}

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
    """The GitHub client: it records what it is asked to post, and lists what a commit holds,
    `held` before the posts, newest first, as GitHub does. `refuse` refuses the post of that
    number, counted from 1, with `refusal`; `deadlines` are the deadlines it was given."""

    def __init__(self, held: list[dict[str, Any]] | None = None) -> None:
        self.posted: list[tuple[str, str, str, str]] = []
        self.held = list(held or [])
        self.deadlines: list[float | None] = []
        self.newer_than: list[int | None] = []
        self.refuse: int | None = None
        self.refusal: Exception = RefusedError(None, None)

    def post_status(
        self,
        commit: str,
        state: str,
        context: str,
        description: str,
        *,
        deadline: float | None = None,
        newer_than: int | None = None,
    ) -> None:
        self.deadlines.append(deadline)
        self.newer_than.append(newer_than)
        if self.refuse == len(self.posted) + 1:
            raise self.refusal
        self.posted.append((commit, state, context, description))

    def statuses(self, commit: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        self.deadlines.append(deadline)
        return self.held[::-1]


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


def test_a_failed_verification_names_an_instance_in_service_by_the_stage_serving(stub: Stub, commit: str) -> None:
    # One instance in service and one that stopped at a stage of a start, under the function's
    # allow-list: the stages of a start and the stage serving (`src/rhi/pipeline/reporter.py`).
    stub.mark("last-stages", f"{serving.STAGE}\nweights\n")
    client = Client()
    run(
        verification(stub.execution, "FAILED"),
        stub,
        client,
        lifecycle_stages=(*(stage.value for stage in Stage), serving.STAGE),
    )
    named = {description for _, _, context, description in client.posted if "/lifecycle/" in context}
    assert named == {serving.STAGE, "weights"}
    assert not [context for _, _, context, _ in client.posted if context.endswith("/dropped")]


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


def test_a_verification_whose_gpu_tests_did_not_run_for_lack_of_capacity_says_so(
    stub: Stub, commit: str, caplog: pytest.LogCaptureFixture
) -> None:
    def posted(by: Stub) -> list[tuple[str, str, str, str]]:
        client = Client()
        run(verification(by.execution, "SUCCEEDED")[1:], by, client)
        return client.posted

    # The step booked the coursework pool, no instance was launched, and the mock pool's tests passed.
    stub.mark("gpu-not-run", "no-capacity\n")
    assert posted(stub) == [(commit, "success", VERIFY, Description.VERIFIED_WITHOUT_GPU)]
    # Not the description the implement loop discharges a smoke test's item on.
    assert Description.VERIFIED_WITHOUT_GPU != VERIFIED_DESCRIPTION
    # The mark of another execution says nothing of this one.
    stub.marks.clear()
    stub.mark("gpu-not-run", "no-capacity\n", execution=str(uuid.uuid4()))
    assert posted(stub) == [(commit, "success", VERIFY, VERIFIED_DESCRIPTION)]

    class RefusesTheMark(Stub):
        """The artifact bucket, which lists the mark `not-run` and refuses to list `gpu-not-run`."""

        def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, Any]:
            if Prefix.endswith("/gpu-not-run"):
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": MARKER}}, "ListObjectsV2")
            return super().list_objects_v2(Bucket, Prefix)

    # While that mark cannot be read, no success is posted: the GPU tests may not have run.
    with caplog.at_level(logging.DEBUG), pytest.raises(ClientError):
        posted(RefusesTheMark(stub.execution, commit))
    assert [record.getMessage() for record in caplog.records] == [handler.NO_NOT_RUN]
    assert MARKER not in caplog.text


def test_the_gpu_mark_of_a_failed_attempt_says_nothing_of_the_retry_that_passes(
    stub: Stub, commit: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def described() -> str:
        client = Client()
        run(verification(stub.execution, "SUCCEEDED")[1:], stub, client)
        ((posted_on, state, context, description),) = client.posted
        assert (posted_on, state, context) == (commit, "success", VERIFY)
        return description

    # The first attempt found no capacity and failed on the mock pool's tests: the step left its
    # mark. The stage was retried, under the same execution ID, and the retry passed.
    retried = STARTED + datetime.timedelta(hours=2)
    stub.verifications = [("Failed", STARTED), ("Succeeded", retried)]
    stub.mark("gpu-not-run", "no-capacity\n", written=STARTED + datetime.timedelta(hours=1))
    # The retry left no mark: its GPU tests ran, and its success says so.
    assert described() == VERIFIED_DESCRIPTION
    # The retry found no capacity either, and wrote the mark again.
    stub.mark("gpu-not-run", "no-capacity\n", written=retried + datetime.timedelta(hours=1))
    assert described() == Description.VERIFIED_WITHOUT_GPU

    # While the runs cannot be read, the mark cannot be dated, and no success is posted.
    def throttled(**_: object) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": MARKER}}, "ListActionExecutions")

    monkeypatch.setattr(stub, "list_action_executions", throttled)
    client = Client()
    with caplog.at_level(logging.DEBUG), pytest.raises(ClientError):
        run(verification(stub.execution, "SUCCEEDED")[1:], stub, client)
    assert client.posted == [] and MARKER not in caplog.text
    assert [record.getMessage() for record in caplog.records] == [handler.NO_NOT_RUN]


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
            failed = {"stageName": STAGE, "actionName": "Verify", "status": "Failed", "startTime": STARTED - HOUR}
            verify = {"stageName": STAGE, "actionName": "Verify", "status": "Succeeded", "startTime": STARTED}
            return {"actionExecutionDetails": [failed, verify]}

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

    def verified_as(written: datetime.datetime) -> str:
        """The description of the superseded commit's verification when the covering execution
        left, at that time, the mark that its GPU tests did not run."""
        covering.marks.clear()
        covering.mark("gpu-not-run", "no-capacity\n", written=written)
        client = Client()
        run([pipeline_event(covering.execution, "SUCCEEDED")], covering, client)
        return client.posted[-1][3]

    # So does the mark of GPU tests that did not run, when the run that succeeded left it, not the
    # attempt that failed before it.
    assert verified_as(STARTED + HOUR) == Description.VERIFIED_WITHOUT_GPU
    assert verified_as(STARTED - HOUR / 2) == VERIFIED_DESCRIPTION


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


def without_input(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    """CodePipeline's records may omit `input`: the stub's records say no action's type."""
    listed = stub.list_action_executions

    def listed_without_input(pipelineName: str, filter: dict[str, str]) -> dict[str, Any]:
        details = listed(pipelineName, filter)["actionExecutionDetails"]
        return {"actionExecutionDetails": [{k: v for k, v in run.items() if k != "input"} for run in details]}

    monkeypatch.setattr(stub, "list_action_executions", listed_without_input)


@pytest.mark.parametrize("start_first", [True, False], ids=["the approval's start first", "the approval's start last"])
def test_a_waiting_approval_without_its_input_stays_visible(
    stub: Stub, commit: str, start_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    without_input(stub, monkeypatch)
    stub.approvals[stub.execution] = [("InProgress", STARTED)]
    events = progress(stub.execution)
    events.insert(0 if start_first else len(events), approval(stub.execution, "STARTED"))
    client = Client()
    deliver(events, stub, client)
    assert deployment(client, commit) == [WAITING] * 4


def test_a_resolved_approval_without_its_input_does_not_restore_the_wait(
    stub: Stub, commit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    without_input(stub, monkeypatch)
    stub.approvals[stub.execution] = [("Succeeded", STARTED)]
    client = Client()
    deliver([action_event(stub.execution, STAGE, "Network.Deploy", "Deploy", "STARTED")], stub, client)
    deliver([approval(stub.execution, "STARTED")], stub, client)
    assert deployment(client, commit) == [RUNNING]


def test_a_step_that_runs_unnamed_is_not_an_approval(stub: Stub, commit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = stub.list_action_executions

    def with_unnamed_step(pipelineName: str, filter: dict[str, str]) -> dict[str, Any]:
        # `actionName` is optional in a record too.
        kind = {"actionTypeId": {"category": "Build", "provider": "CodeBuild"}}
        record = {"stageName": STAGE, "status": "InProgress", "startTime": LATER, "input": kind}
        return {"actionExecutionDetails": [*listed(pipelineName, filter)["actionExecutionDetails"], record]}

    monkeypatch.setattr(stub, "list_action_executions", with_unnamed_step)
    stub.approvals[stub.execution] = [("Succeeded", STARTED)]
    client = Client()
    deliver([stage_event(stub.execution, STAGE, "STARTED")], stub, client)
    deliver([action_event(stub.execution, STAGE, "CopyWeights", "Build", "STARTED")], stub, client)
    assert deployment(client, commit) == [RUNNING] * 2


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


# A run of S5's size: its 163 numbers encode as 61 parts, 41 of three numbers then 20 of two.
WIDE = {f"short_{at:03}_".ljust(41, "x"): 1000 + at for at in range(123)} | {
    f"long_{at:03}_".ljust(55, "x"): 1000 + at for at in range(40)
}
WIDE_LABEL = "4bit-draft-head-1"


def wide(commit: str) -> dict[str, Any]:
    """The event EventBridge delivers for a run of 163 numbers that a job sent from `commit`."""
    entry = measure.measurement_event(
        "S5", WIDE_LABEL, measure.Verdict.PASS, WIDE, names=list(WIDE), commit=commit, bus="rhi-measurements"
    )
    return wrapped(entry["DetailType"], json.loads(entry["Detail"])) | {"source": entry["Source"], "resources": []}


def as_held(posted: list[tuple[str, str, str, str]], first_id: int = 1) -> list[dict[str, Any]]:
    """Posted statuses as GitHub lists them, each with its id, in the order they were posted."""
    return [
        {"id": first_id + at, "context": context, "state": state, "description": text}
        for at, (_, state, context, text) in enumerate(posted)
    ]


def parts(client: Client) -> list[int]:
    return [measure.context_parts(context)[2] for _, _, context, _ in client.posted]


@pytest.mark.parametrize(
    "refusal",
    [RefusedError(403, None), RefusedError(429, None), RefusedError(502, None), CredentialError()],
    ids=["403", "429", "502", "credential"],
)
def test_a_measurement_cut_short_is_logged_once_and_fails(
    stub: Stub, caplog: pytest.LogCaptureFixture, refusal: Exception
) -> None:
    event = wide(secrets.token_hex(20))
    assert len(measure.read_event(event)) == 61
    client = Client()
    client.refuse, client.refusal = 43, refusal
    aws = handler.Aws(codepipeline=stub, cloudformation=stub, s3=stub, secrets=stub)
    with caplog.at_level(logging.DEBUG), pytest.raises(handler.NotAllPostedError):
        handler.handle(event, aws=aws, client=client, stages=[STAGE], deadline=1000.0)
    assert parts(client) == list(range(1, 43))
    (record,) = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert message.startswith(handler.NOT_ALL_POSTED)
    assert {"S5", WIDE_LABEL, "42", "61"} <= set(message.replace(",", " ").split())
    # Every call of the invocation had its deadline.
    assert set(client.deadlines) == {1000.0}


def test_a_failed_read_of_the_statuses_posts_nothing_and_fails(stub: Stub, caplog: pytest.LogCaptureFixture) -> None:
    class Unreadable(Client):
        def statuses(self, commit: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
            raise RefusedError(503, None)

    client = Unreadable()
    with caplog.at_level(logging.DEBUG), pytest.raises(handler.NotAllPostedError):
        run([wide(secrets.token_hex(20))], stub, client)
    assert client.posted == []
    assert [record.levelno for record in caplog.records] == [logging.ERROR]


def test_a_measurement_invoked_again_completes_the_posting_cut_short(stub: Stub) -> None:
    event = wide(secrets.token_hex(20))
    first = Client()
    first.refuse = 43
    with pytest.raises(handler.NotAllPostedError):
        run([event], stub, first)

    again = Client(held=as_held(first.posted))
    run([event], stub, again)
    assert parts(again) == list(range(43, 62))
    # Each part is posted as one the commit must not hold twice: none newer than its statuses.
    assert set(again.newer_than) == {42}
    # Once whole, a third invocation posts nothing.
    whole = Client(held=as_held(first.posted + again.posted))
    run([event], stub, whole)
    assert whole.posted == []


def test_a_newest_posting_that_differs_is_posted_again_from_part_1(stub: Stub) -> None:
    event = wide(secrets.token_hex(20))
    first = Client()
    first.refuse = 43
    with pytest.raises(handler.NotAllPostedError):
        run([event], stub, first)
    commit, state, context, text = first.posted[20]
    changed = [*first.posted[:20], (commit, state, context, text.replace("=1", "=2", 1)), *first.posted[21:]]

    again = Client(held=as_held(changed))
    run([event], stub, again)
    assert parts(again) == list(range(1, 62))
    # An older posting that differs says nothing: only the newest counts.
    older = Client(held=as_held(changed) + as_held(first.posted, first_id=100))
    run([event], stub, older)
    assert parts(older) == list(range(43, 62))
    # Nor is a posting completed that holds a part twice: the record reads it as two postings.
    twice = Client(held=as_held([*first.posted, first.posted[-1]]))
    run([event], stub, twice)
    assert parts(twice) == list(range(1, 62))


def test_the_deadline_is_the_context_s_remaining_time_less_ten_seconds() -> None:
    class Context:
        def get_remaining_time_in_millis(self) -> int:
            return 60_000

    assert handler.deadline_of(Context(), 1_000.0) == 1_050.0
