"""The weekly GPU canary of `src/rhi/pipeline/canary.py` (system design, sections 8.2, step 6, and
14): a job of the pipeline's stack that runs the coursework pool's smoke tests against the deployed
endpoint, a schedule on Sundays at 04:00 UTC whose only target is that job, and a role that reads
the canary's secret, queries the workspace, reads the pools' release parameters, queries the
coursework pool's heartbeat rows and sends the measurement event, and nothing else.

The tests read the app's own pipeline, built from the example configuration. One also builds the
app parked: no schedule, and no job for one to start, only the job's log group.
"""

from __future__ import annotations

import json
import re
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
CANARY = "rhi-gpu-canary"
WEEKLY = "cron(0 4 ? * SUN *)"
# Every action the job's role is allowed, and nothing else.
JOB_ACTIONS = {
    "secretsmanager:GetSecretValue",
    "aps:QueryMetrics",
    "ssm:GetParameter",
    "dynamodb:Query",
    "events:PutEvents",
    "codeconnections:GetConnection",
    "codeconnections:GetConnectionToken",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """A value of the template as text, `<>` in place of each pseudo parameter and `<Name>` or
    `<Name.Attribute>` in place of each resource."""
    if isinstance(value, str):
        return value
    ((function, argument),) = value.items()
    if function == "Ref":
        return "<>" if argument.startswith("AWS::") else f"<{argument}>"
    if function == "Fn::GetAtt":
        return f"<{'.'.join(argument)}>"
    assert function == "Fn::Join", f"a value built with {function}"
    return argument[0].join(text(part) for part in argument[1])


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource["Properties"]
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == kind
    }


def statements(template: dict[str, Any], role: str) -> list[dict[str, Any]]:
    """Every statement of the policies attached to a role, which holds none of its own."""
    assert "Policies" not in template["Resources"][role]["Properties"]
    assert "ManagedPolicyArns" not in template["Resources"][role]["Properties"]
    return [
        statement
        for policy in of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Roles"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]


@pytest.fixture(scope="module")
def pipeline(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return templates[PIPELINE_TEMPLATE]


@pytest.fixture(scope="module")
def project(pipeline: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The canary's project, by its name: there is one."""
    (found,) = [item for item in of_type(pipeline, "AWS::CodeBuild::Project").items() if item[1].get("Name") == CANARY]
    return found


def test_the_job_runs_the_canary_script_on_main_with_no_webhook(project: tuple[str, dict[str, Any]]) -> None:
    _, properties = project
    assert properties["Triggers"] == {"Webhook": False}
    assert properties["Source"]["Type"] == "GITHUB"
    assert properties["SourceVersion"] == "main"
    assert properties["Environment"]["PrivilegedMode"] is False
    build_spec = json.loads(properties["Source"]["BuildSpec"])
    commands = [command for phase in build_spec["phases"].values() for command in phase["commands"]]
    assert "uv run --locked python scripts/pipeline/canary_result.py" in commands
    assert not any("codepipeline" in command for command in commands)


def test_the_job_is_told_the_endpoint_and_the_release_parameters(
    project: tuple[str, dict[str, Any]],
) -> None:
    example = config.load({"RHI_CONFIG": "example"})
    _, properties = project
    told = {variable["Name"]: variable["Value"] for variable in properties["Environment"]["EnvironmentVariables"]}
    assert told["RHI_SMOKE_ENDPOINT"] == (example.domain if example.https_listener else "none")
    pools = {pool.name: f"/rhi/pools/{pool.name}/release" for pool in example.pools}
    assert json.loads(told["RHI_SMOKE_RELEASE_PARAMETERS"]) == pools
    assert told["RHI_MEASUREMENT_BUS"] == "rhi-measurements"


def test_one_weekly_schedule_whose_only_target_is_the_job(
    pipeline: dict[str, Any], project: tuple[str, dict[str, Any]]
) -> None:
    logical_id, _ = project
    targeting = [
        properties
        for properties in of_type(pipeline, "AWS::Events::Rule").values()
        if any(target["Arn"] == {"Fn::GetAtt": [logical_id, "Arn"]} for target in properties.get("Targets", []))
    ]
    (schedule,) = targeting
    assert schedule["Name"] == CANARY
    assert schedule["ScheduleExpression"] == WEEKLY
    assert schedule.get("State", "ENABLED") == "ENABLED"
    assert "EventPattern" not in schedule
    (target,) = schedule["Targets"]
    # The schedule's role may start a build of the job, and nothing else.
    role = target["RoleArn"]["Fn::GetAtt"][0]
    assert [(as_list(s["Action"]), text(s["Resource"])) for s in statements(pipeline, role)] == [
        (["codebuild:StartBuild"], f"<{logical_id}.Arn>")
    ]


def test_the_job_s_role_reads_the_canary_s_inputs_and_sends_the_measurement_only(
    pipeline: dict[str, Any], project: tuple[str, dict[str, Any]]
) -> None:
    _, properties = project
    role = properties["ServiceRole"]["Fn::GetAtt"][0]
    allowed = statements(pipeline, role)
    assert {action for statement in allowed for action in as_list(statement["Action"])} == JOB_ACTIONS
    assert all(statement["Effect"] == "Allow" and "NotAction" not in statement for statement in allowed)

    def resources_of(action: str) -> set[str]:
        return {
            text(resource)
            for statement in allowed
            if action in as_list(statement["Action"])
            for resource in as_list(statement["Resource"])
        }

    assert resources_of("secretsmanager:GetSecretValue") == {"arn:<>:secretsmanager:<>:<>:secret:rhi/canary-key-??????"}
    example = config.load({"RHI_CONFIG": "example"})
    assert resources_of("ssm:GetParameter") == {
        f"arn:<>:ssm:<>:<>:parameter/rhi/pools/{pool.name}/release" for pool in example.pools
    }
    assert resources_of("events:PutEvents") == {"arn:<>:events:<>:<>:event-bus/rhi-measurements"}
    # The workspace is queried, by the stage's tag, never listed or described: its address is in
    # the coursework pool's release parameter.
    (query,) = [statement for statement in allowed if "aps:QueryMetrics" in as_list(statement["Action"])]
    assert as_list(query["Action"]) == ["aps:QueryMetrics"]
    assert text(query["Resource"]) == "arn:<>:aps:<>:<>:workspace/*"
    assert query["Condition"] == {"StringEquals": {"aws:ResourceTag/rhi-stage": "Rhi"}}


def test_the_job_s_role_queries_the_coursework_pool_s_heartbeat_rows_only(
    templates: dict[str, dict[str, Any]], pipeline: dict[str, Any], project: tuple[str, dict[str, Any]]
) -> None:
    _, properties = project
    role = properties["ServiceRole"]["Fn::GetAtt"][0]
    mine = statements(pipeline, role)
    # Exactly one statement of DynamoDB, by any pattern, and its only action is the query.
    (query,) = [
        statement
        for statement in mine
        if any(fnmatchcase(action.lower(), "dynamodb:*") for action in as_list(statement["Action"]))
    ]
    assert as_list(query["Action"]) == ["dynamodb:Query"]
    # Its one resource is the verification's pattern of the stage's heartbeats table: the pools
    # stack's name, the logical ID of a table of that stack, and the wildcard of the suffix.
    (resource,) = as_list(query["Resource"])
    # The verification step's role, the one that lists the pipeline's executions.
    verifying = [
        policy["PolicyDocument"]["Statement"]
        for policy in of_type(pipeline, "AWS::IAM::Policy").values()
        if any(
            "codepipeline:ListPipelineExecutions" in as_list(s["Action"]) for s in policy["PolicyDocument"]["Statement"]
        )
    ]
    verification = {
        text(resource)
        for policy in verifying
        for statement in policy
        if "dynamodb:Query" in as_list(statement["Action"])
        for resource in as_list(statement["Resource"])
    }
    assert verification == {text(resource)}
    shape = re.fullmatch(r"arn:<>:dynamodb:<>:<>:table/Rhi-Pools-(TablesHeartbeats\w+)-\*", text(resource))
    assert shape is not None
    pools = [t for path, t in templates.items() if path.rsplit("/", 1)[-1].startswith("RhiPools")]
    assert any(shape[1] in of_type(t, "AWS::DynamoDB::Table") for t in pools)
    # The coursework pool's partition alone.
    assert query["Condition"] == {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["coursework"]}}


def test_parked_the_canary_has_no_schedule_no_job_and_no_role(
    app_module: ModuleType, pipeline: dict[str, Any], project: tuple[str, dict[str, Any]], tmp_path: Path
) -> None:
    # The app as `app.py` builds it, cdk-nag included, from the example configuration parked.
    data = tomllib.loads((REPO / config.EXAMPLE_FILE).read_text(encoding="utf-8"))
    data["parked"] = True
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app_module.build(config.parse(data, "test"), outdir=str(tmp_path), context=context).synth()
    parked = json.loads((tmp_path / PIPELINE_TEMPLATE).read_text(encoding="utf-8"))

    # No rule or schedule of the canary's name, and no job for one to start.
    for kind in ("AWS::Events::Rule", "AWS::Scheduler::Schedule"):
        assert not [p for p in of_type(parked, kind).values() if p.get("Name") == CANARY]
    assert not [p for p in of_type(parked, "AWS::CodeBuild::Project").values() if p.get("Name") == CANARY]
    # Nor the job's role, nor the schedule's, under the logical IDs they have unparked, nor a policy
    # attached to either.
    _, properties = project
    (schedule,) = [p for p in of_type(pipeline, "AWS::Events::Rule").values() if p.get("Name") == CANARY]
    roles = {properties["ServiceRole"]["Fn::GetAtt"][0], schedule["Targets"][0]["RoleArn"]["Fn::GetAtt"][0]}
    assert not roles & set(of_type(parked, "AWS::IAM::Role"))
    assert not [
        p for p in of_type(parked, "AWS::IAM::Policy").values() if any(r.get("Ref") in roles for r in p["Roles"])
    ]
    # The job's log group stays, under its logical ID, with the logs of the runs before.
    groups = {
        k: p
        for k, p in of_type(pipeline, "AWS::Logs::LogGroup").items()
        if p.get("LogGroupName") == f"/aws/codebuild/{CANARY}"
    }
    assert groups and all(of_type(parked, "AWS::Logs::LogGroup").get(k) == p for k, p in groups.items())
