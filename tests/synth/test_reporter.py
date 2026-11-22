"""The pipeline's reporter, in the app's synthesized templates (system design, section 8.2, step 7):
a rule on this pipeline's state changes and nothing else, the function it invokes, the secret
the function reads, which the stack refers to and never creates, and the owner's topic, where the
function sends what a public status may not carry (section 8.4); the allow-list of smoke test IDs,
the allow-list of lifecycle stages and the artifact bucket the function is told, and its role's
read of the verification's marks in that bucket. Beside them, the measurement bus,
a rule on it for a job's measurements that invokes the same function, and the grant a job's role
is given to put events on that bus (sections 8.4 and 12).
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

from rhi import config
from rhi_lifecycle.stages import Stage
from rhi_reporter import handler, measure

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
# The secret's fixed name, which the owner creates (section 8.6, step 3).
SECRET_NAME = "rhi/reporter/github-app"
MEASUREMENT_BUS = "rhi-measurements"
# The ID of every smoke test, where the smoke tests list them: the reporter's allow-list.
SMOKE_TEST_IDS: tuple[str, ...] = runpy.run_path(str(Path(__file__).resolve().parents[1] / "smoke/ids.py"))[
    "SMOKE_TEST_IDS"
]


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource.get("Properties", {})
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == kind
    }


def reporter_functions(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The functions of a template that run the reporter's handler."""
    return {
        logical_id: properties
        for logical_id, properties in of_type(template, "AWS::Lambda::Function").items()
        if properties.get("Handler") == "rhi_reporter.handler.handler"
    }


def test_the_rule_matches_this_pipeline_s_state_changes_only(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (pipeline_id,) = of_type(template, "AWS::CodePipeline::Pipeline")
    # The one rule on the default bus that matches events and invokes no log group: the other is on
    # the measurement bus, a schedule matches none, and the rule that keeps the same events targets
    # the pipeline's events log group (`tests/synth/test_pipeline_events.py`).
    (rule,) = [
        rule
        for template in templates.values()
        for rule in of_type(template, "AWS::Events::Rule").values()
        if "EventBusName" not in rule
        and "EventPattern" in rule
        and not any(":log-group:" in json.dumps(target["Arn"]) for target in rule["Targets"])
    ]
    pattern = rule["EventPattern"]
    assert pattern["source"] == ["aws.codepipeline"]
    assert sorted(pattern["detail-type"]) == [
        "CodePipeline Action Execution State Change",
        "CodePipeline Pipeline Execution State Change",
        "CodePipeline Stage Execution State Change",
    ]
    # This pipeline's ARN, and no other resource.
    (arn,) = pattern["resources"]
    parts = arn["Fn::Join"][1]
    assert arn["Fn::Join"][0] == "" and parts[-1] == {"Ref": pipeline_id}
    assert [part for part in parts if isinstance(part, str)] == ["arn:", ":codepipeline:", ":", ":"]
    assert set(pattern) == {"source", "detail-type", "resources"}

    (target,) = rule["Targets"]
    (function_id,) = reporter_functions(template)
    assert target["Arn"] == {"Fn::GetAtt": [function_id, "Arn"]}


def test_the_measurement_rule_matches_the_measurement_bus_only(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    buses = [(path, bus) for path, t in templates.items() for bus in of_type(t, "AWS::Events::EventBus").items()]
    ((path, (bus_id, bus)),) = buses
    assert path == PIPELINE_TEMPLATE and bus["Name"] == MEASUREMENT_BUS
    rules = [rule for t in templates.values() for rule in of_type(t, "AWS::Events::Rule").values()]
    (rule,) = [rule for rule in rules if "EventBusName" in rule]
    assert rule["EventBusName"] == {"Ref": bus_id}
    # A job's events, whatever they are, and the job and the reporter agree on their source.
    assert rule["EventPattern"] == {"source": [measure.SOURCE]} == {"source": ["rhi.measurement"]}
    (target,) = rule["Targets"]
    (function_id,) = reporter_functions(template)
    assert target["Arn"] == {"Fn::GetAtt": [function_id, "Arn"]}


def test_a_job_given_the_grant_may_put_events_on_the_bus_and_read_no_secret(app: Any) -> None:
    # Imported once the fixtures have started the CDK as the sandbox needs it (`conftest.py`).
    import aws_cdk as cdk
    from aws_cdk import assertions

    from rhi.pipeline import reporter

    stack = cdk.Stack(cdk.App(), "Job")
    codebuild = cdk.aws_iam.ServicePrincipal("codebuild.amazonaws.com")
    # ty reads the CDK's generated classes as unlike the protocols they implement (`src/rhi/pipeline/reporter.py`).
    role = cdk.aws_iam.Role(stack, "Role", assumed_by=codebuild)  # ty: ignore[invalid-argument-type]
    environment = {"OTHER": "kept"}
    reporter.measuring_job(role, environment)

    assert environment == {"OTHER": "kept", measure.BUS_VARIABLE: MEASUREMENT_BUS}
    template = dict(assertions.Template.from_stack(stack).to_json())
    (policy,) = of_type(template, "AWS::IAM::Policy").values()
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert {key: statement[key] for key in ("Effect", "Action")} == {"Effect": "Allow", "Action": "events:PutEvents"}
    parts = statement["Resource"]["Fn::Join"][1]
    texts = [part for part in parts if isinstance(part, str)]
    assert texts == ["arn:", ":events:", ":", f":event-bus/{MEASUREMENT_BUS}"]
    assert "secretsmanager" not in str(template)


def test_the_function_is_told_the_secret_and_the_repository(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (function,) = reporter_functions(template).values()
    (topic_id,) = of_type(template, "AWS::SNS::Topic")
    (pipeline,) = of_type(template, "AWS::CodePipeline::Pipeline").values()
    example = config.load({"RHI_CONFIG": "example"})
    assert function["Environment"]["Variables"] == {
        "RHI_REPORTER_SECRET": SECRET_NAME,
        "RHI_REPORTER_REPOSITORY": f"{example.github_owner}/{example.github_repo}",
        handler.STAGES_VARIABLE: "Rhi",
        handler.TOPIC_VARIABLE: {"Ref": topic_id},
        "RHI_SMOKE_TEST_IDS": ",".join(SMOKE_TEST_IDS),
        "RHI_LIFECYCLE_STAGES": ",".join(stage.value for stage in Stage),
        # The bucket the verification step writes its marks to: the pipeline's artifact bucket.
        handler.ARTIFACTS_VARIABLE: pipeline["ArtifactStore"]["Location"],
    }
    # The allow-list as the function reads it: every ID the smoke tests list, and nothing else.
    assert handler.SMOKE_TESTS_VARIABLE == "RHI_SMOKE_TEST_IDS"
    assert handler.smoke_test_ids(function["Environment"]["Variables"]) == SMOKE_TEST_IDS != ()


def test_the_function_s_lifecycle_stages_are_the_members_of_the_stages_enumeration(
    templates: dict[str, dict[str, Any]],
) -> None:
    (function,) = reporter_functions(templates[PIPELINE_TEMPLATE]).values()
    # Each member by its value, the name a launch writes into its instance's heartbeat row: what
    # the function reads back from the variable is the enumeration, whole and nothing else.
    members = tuple(stage.value for stage in Stage)
    assert handler.LIFECYCLE_STAGES_VARIABLE == "RHI_LIFECYCLE_STAGES"
    assert handler.lifecycle_stages(function["Environment"]["Variables"]) == members
    assert "weights" in members and len(set(members)) == len(members)


def test_one_encrypted_topic_that_refuses_unencrypted_transport(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (topic_id, topic), *others = [
        (logical_id, properties)
        for template in templates.values()
        for logical_id, properties in of_type(template, "AWS::SNS::Topic").items()
    ]
    assert others == [] and topic_id in template["Resources"]
    assert topic["KmsMasterKeyId"] == "alias/aws/sns"
    (policy,) = [p for p in of_type(template, "AWS::SNS::TopicPolicy").values() if {"Ref": topic_id} in p["Topics"]]
    assert {
        "Effect": "Deny",
        "Principal": "*",
        "Action": "sns:Publish",
        "Resource": {"Ref": topic_id},
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }.items() <= next(iter(policy["PolicyDocument"]["Statement"])).items()


def test_one_e_mail_subscription_to_the_admin_address(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (topic_id,) = of_type(template, "AWS::SNS::Topic")
    (subscription,) = of_type(template, "AWS::SNS::Subscription").values()
    example = config.load({"RHI_CONFIG": "example"})
    assert subscription == {"Protocol": "email", "Endpoint": example.admin_email, "TopicArn": {"Ref": topic_id}}


def test_the_stack_creates_no_secret(templates: dict[str, dict[str, Any]]) -> None:
    # Imported once the fixtures have started the CDK as the sandbox needs it (`conftest.py`).
    from rhi.pipeline import reporter

    assert reporter.SECRET_NAME == SECRET_NAME
    assert all(
        secret.get("Name") != SECRET_NAME
        for template in templates.values()
        for secret in of_type(template, "AWS::SecretsManager::Secret").values()
    )


def test_the_synth_step_the_handler_reads_is_the_pipeline_s(templates: dict[str, dict[str, Any]]) -> None:
    (pipeline,) = of_type(templates[PIPELINE_TEMPLATE], "AWS::CodePipeline::Pipeline").values()
    outputs = {
        (stage["Name"], action["Name"]): action.get("OutputArtifacts", [])
        for stage in pipeline["Stages"]
        for action in stage["Actions"]
    }
    assert outputs[handler.SYNTH_STAGE, handler.SYNTH_ACTION]


# What the function's role may do: each action with what it is allowed on.
ROLE_ACTIONS = {
    "secretsmanager:GetSecretValue",
    "codepipeline:GetPipelineExecution",
    "codepipeline:GetPipelineState",
    "codepipeline:ListActionExecutions",
    "codepipeline:ListPipelineExecutions",
    "cloudformation:DescribeStackEvents",
    "s3:GetObject",
    "s3:ListBucket",
    "sns:Publish",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def role_statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Every statement of the policies attached to the reporter's role."""
    (function,) = reporter_functions(template).values()
    role = function["Role"]["Fn::GetAtt"][0]
    return [
        statement
        for policy in of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Roles"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]


def test_the_role_may_list_this_pipeline_s_executions(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (pipeline_id,) = of_type(template, "AWS::CodePipeline::Pipeline")
    statements = role_statements(template)
    # As narrow as before the function read the instances' last stages: that mark is one more
    # object under verification/, which the role already reads and lists.
    actions = [action for statement in statements for action in as_list(statement["Action"])]
    assert sorted(actions) == sorted(ROLE_ACTIONS)
    (listing,) = [s for s in statements if "codepipeline:ListPipelineExecutions" in as_list(s["Action"])]
    assert listing["Effect"] == "Allow"
    # This pipeline's ARN, and no other resource.
    parts = listing["Resource"]["Fn::Join"][1]
    assert parts[-1] == {"Ref": pipeline_id}
    assert [part for part in parts if isinstance(part, str)] == ["arn:", ":codepipeline:", ":", ":"]


def test_the_role_reads_the_verification_s_marks_and_lists_no_other_key(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (pipeline,) = of_type(template, "AWS::CodePipeline::Pipeline").values()
    bucket = {"Fn::GetAtt": [pipeline["ArtifactStore"]["Location"]["Ref"], "Arn"]}
    on_s3 = [s for s in role_statements(template) if any(a.startswith("s3:") for a in as_list(s["Action"]))]
    # The objects of the artifact bucket, the marks under verification/ among them, as before; and
    # a listing of that bucket that is shown the keys under the marks' prefix and no other, which
    # is the prefix the function lists and the one the verification step writes under.
    marks = f"{handler.MARKS_PREFIX}/*"
    assert marks == "verification/*"
    assert sorted(on_s3, key=lambda statement: statement["Action"]) == [
        {"Action": "s3:GetObject", "Effect": "Allow", "Resource": {"Fn::Join": ["", [bucket, "/*"]]}},
        {
            "Action": "s3:ListBucket",
            "Effect": "Allow",
            "Resource": bucket,
            "Condition": {"StringLike": {"s3:prefix": marks}},
        },
    ]


def test_the_role_may_publish_to_the_owner_s_topic_only(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (topic_id,) = of_type(template, "AWS::SNS::Topic")
    publishing = [s for s in role_statements(template) if any(a.startswith("sns:") for a in as_list(s["Action"]))]
    assert publishing == [{"Action": "sns:Publish", "Effect": "Allow", "Resource": {"Ref": topic_id}}]
