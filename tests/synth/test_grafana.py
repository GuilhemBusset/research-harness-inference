"""Grafana in the observability stack (system design, sections 7.1 and 7.4): a Fargate task in the
private subnets, reached on port 3000 through one rule of the public load balancer's listener, at
priority 30, which authenticates against the admin's user pool before it forwards; an environment
that gives it the workspace's query address and the region; and a role that may query the one
workspace, read CloudWatch metrics and read the log groups of the gateway, the scaler, the issuer
and the pools, never a build project's. All of the load balancer's part only while the listener
exists.

The app of the session is synthesized from the example configuration, whose listener exists, with
cdk-nag: a finding that `tests/policy/acknowledged/observability_grafana.toml` does not acknowledge
fails every test here.
"""

from __future__ import annotations

import copy
import dataclasses
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

Template = dict[str, Any]
Templates = dict[str, Template]

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})
ACKNOWLEDGED = REPO / "tests" / "policy" / "acknowledged" / "observability_grafana.toml"
ELB = "AWS::ElasticLoadBalancingV2::"

PORT = 3000
PREFIX = "/grafana/"
QUERY_ACTIONS = {"aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"}
LOG_ACTIONS = {"logs:StartQuery", "logs:GetLogGroupFields", "logs:GetLogEvents"}
CLOUDWATCH_READS = {"cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "logs:GetQueryResults"}
SERVICE_LOG_GROUPS = {"/rhi/gateway", "/rhi/scaler", "/rhi/issuer"}
POOLS_LOG_GROUPS = "/rhi/pools/*"

# What the pseudo parameters of an ARN are read as, and the ARN of a log group so read: its name,
# then `:*` for the log group and its streams.
PSEUDO = {"AWS::Partition": "aws", "AWS::Region": "us-east-1", "AWS::AccountId": "123456789012"}
LOG_GROUPS = ":".join(
    ["arn", PSEUDO["AWS::Partition"], "logs", PSEUDO["AWS::Region"], PSEUDO["AWS::AccountId"], "log-group", ""]
)
LOG_GROUP_ARN = re.compile(re.escape(LOG_GROUPS) + r"(?P<name>[^:]+):\*")
BUILD_LOGS = LOG_GROUPS + "/aws/codebuild/"


def of_type(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


def everywhere(templates: Templates, kind: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every resource of a type, in whichever template: its template's path, its logical ID and
    its properties."""
    return [
        (path, name, properties)
        for path, template in templates.items()
        for name, properties in of_type(template, kind).items()
    ]


def stack_path(templates: Templates, stack: str) -> str:
    (path,) = [path for path in templates if Path(path).name.startswith(stack)]
    return path


def observability(templates: Templates) -> Template:
    return templates[stack_path(templates, "RhiObservability")]


def held(templates: Templates, reference: Any, path: str) -> tuple[str, str]:
    """The resource a reference of the template at `path` names, in whichever template holds it:
    the template's path and the resource's logical ID."""
    ((function, argument),) = reference.items()
    if function == "Fn::ImportValue":
        ((exporter, value),) = [
            (other, output["Value"])
            for other, template in templates.items()
            for output in template.get("Outputs", {}).values()
            if output.get("Export", {}).get("Name") == argument
        ]
        return held(templates, value, exporter)
    return path, argument if function == "Ref" else argument[0]


def kind(templates: Templates, resource: tuple[str, str]) -> str:
    return templates[resource[0]]["Resources"][resource[1]]["Type"]


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def imports(value: Any) -> set[str]:
    """The names of the exports a fragment of a template imports."""
    if isinstance(value, dict):
        found = {value["Fn::ImportValue"]} if isinstance(value.get("Fn::ImportValue"), str) else set()
        return found.union(*(imports(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(imports(item) for item in value))
    return set()


def task_role_statements(template: Template) -> list[dict[str, Any]]:
    """Every statement of the role of Grafana's task: those of the policies attached to it and of
    its inline policies. The role holds no managed policy, whose statements no template shows."""
    (task,) = of_type(template, "AWS::ECS::TaskDefinition").values()
    role_id = task["TaskRoleArn"]["Fn::GetAtt"][0]
    role = of_type(template, "AWS::IAM::Role")[role_id]
    assert "ManagedPolicyArns" not in role
    documents = [policy["PolicyDocument"] for policy in role.get("Policies", [])]
    for policy_kind in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        documents += [
            policy["PolicyDocument"]
            for policy in of_type(template, policy_kind).values()
            if {"Ref": role_id} in policy.get("Roles", [])
        ]
    return [statement for document in documents for statement in as_list(document["Statement"])]


def written(resource: Any) -> str | None:
    """A statement's resource as text, when the template writes it out with nothing but the
    pseudo parameters of an ARN, read as `PSEUDO`; None for anything else."""
    if isinstance(resource, str):
        return resource
    if isinstance(resource, dict) and PSEUDO.get(resource.get("Ref", "")):
        return PSEUDO[resource["Ref"]]
    if isinstance(resource, dict) and list(resource) == ["Fn::Join"] and resource["Fn::Join"][0] == "":
        parts = [written(part) for part in resource["Fn::Join"][1]]
        return None if None in parts else "".join(part for part in parts if part is not None)
    return None


def may_begin(pattern: str, prefix: str) -> bool:
    """Whether a pattern of a policy, in which `*` is any text and `?` any one character, takes
    some text that begins with `prefix`."""
    reached = {0}
    for character in prefix:
        following = set()
        for position in reached:
            while position < len(pattern) and pattern[position] == "*":
                following.add(position)
                position += 1
            if position < len(pattern) and pattern[position] in (character, "?"):
                following.add(position + 1)
        reached = following
    return bool(reached)


def build_logs_in_reach(template: Template) -> list[str]:
    """What lets the role of Grafana's task read a CodeBuild log group, `/aws/codebuild/...`: each
    action on logs, with its resource, that takes one. Empty when nothing does.

    The actions that CloudWatch grants on every resource only, which the module's acknowledgement
    file lists, read no log group by themselves and are not counted. A resource the template does
    not write out could be any log group."""
    on_every_resource = tomllib.loads(ACKNOWLEDGED.read_text(encoding="utf-8"))["wildcard_actions"]
    found = []
    for statement in task_role_statements(template):
        if statement["Effect"] != "Allow":
            continue
        if "Action" not in statement or "Resource" not in statement:
            found.append(f"a statement that names what it does not allow: {sorted(statement)}")
            continue
        for action in as_list(statement["Action"]):
            # An action is on logs when its name, or the pattern in its place, can begin so.
            if action in on_every_resource or not may_begin(action.lower(), "logs:"):
                continue
            for resource in as_list(statement["Resource"]):
                text = written(resource)
                if text is None or may_begin(text, BUILD_LOGS):
                    found.append(f"{action} on {text if text is not None else json.dumps(resource)}")
    return found


def grafana_target_groups(templates: Templates) -> list[tuple[str, str]]:
    """The target groups that take Grafana's port or check Grafana's health, in any template."""
    return [
        (path, name)
        for path, name, group in everywhere(templates, ELB + "TargetGroup")
        if group.get("Port") == PORT or str(group.get("HealthCheckPath", "")).startswith(PREFIX)
    ]


def grafana_rules(templates: Templates) -> list[tuple[str, str]]:
    """The listener rules that name a path of Grafana, sign in, or forward to a target group of
    Grafana, in any template."""
    groups = grafana_target_groups(templates)
    return [
        (path, name)
        for path, name, rule in everywhere(templates, ELB + "ListenerRule")
        if PREFIX.rstrip("/") in json.dumps(rule.get("Conditions", []))
        or any(action["Type"].startswith("authenticate") for action in rule["Actions"])
        or any(held(templates, a["TargetGroupArn"], path) in groups for a in rule["Actions"] if "TargetGroupArn" in a)
    ]


@pytest.fixture(scope="module")
def without_listener(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Templates:
    """Every template of the app when the configuration leaves the HTTPS listener off, with
    cdk-nag as the app runs it."""
    outdir = tmp_path_factory.mktemp("off")
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(dataclasses.replace(EXAMPLE, https_listener=False), outdir=str(outdir), context=context)
    app.synth()
    return {
        path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(outdir.rglob("*.template.json"))
    }


def test_one_task_runs_the_image_on_fargate_in_the_private_subnets(templates: Templates, cloud_assembly: Path) -> None:
    path = stack_path(templates, "RhiObservability")
    template = templates[path]
    (service,) = of_type(template, "AWS::ECS::Service").values()
    (task,) = of_type(template, "AWS::ECS::TaskDefinition").values()
    assert service["TaskDefinition"] == {"Ref": next(iter(of_type(template, "AWS::ECS::TaskDefinition")))}
    assert (service["LaunchType"], service["DesiredCount"]) == ("FARGATE", 1)
    assert task["RequiresCompatibilities"] == ["FARGATE"]

    # The one container is built from `services/grafana/`, for the task's architecture.
    (container,) = task["ContainerDefinitions"]
    assert [port["ContainerPort"] for port in container["PortMappings"]] == [PORT]
    assets = json.loads(
        (cloud_assembly / path).with_name(Path(path).name.replace(".template.", ".assets.")).read_text()
    )
    ((tag, image),) = assets["dockerImages"].items()
    assert tag in container["Image"]["Fn::Sub"]
    assert (image["source"]["platform"], task["RuntimePlatform"]["CpuArchitecture"]) == ("linux/arm64", "ARM64")
    built_from = (cloud_assembly / path).parent / image["source"]["directory"]
    for file in ("Dockerfile", "grafana.ini", "provisioning/datasources/datasources.yaml"):
        assert (built_from / file).read_bytes() == (REPO / "services" / "grafana" / file).read_bytes()

    # No public address, the network's private subnets, and Grafana's security group, which
    # admits one source on one port: the public load balancer's group on 3000.
    network = service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network["AssignPublicIp"] == "DISABLED"
    assert network["Subnets"]
    for subnet in network["Subnets"]:
        held_by, subnet_id = held(templates, subnet, path)
        tags = {tag["Key"]: tag["Value"] for tag in templates[held_by]["Resources"][subnet_id]["Properties"]["Tags"]}
        assert tags["aws-cdk:subnet-type"] == "Private"
    (group,) = network["SecurityGroups"]
    network_path, group_id = held(templates, group, path)
    (balancer,) = [
        b for b in of_type(templates[network_path], ELB + "LoadBalancer").values() if b["Scheme"] == "internet-facing"
    ]
    (admitted,) = [
        rule
        for rule in of_type(templates[network_path], "AWS::EC2::SecurityGroupIngress").values()
        if held(templates, rule["GroupId"], network_path) == (network_path, group_id)
    ]
    assert (admitted["IpProtocol"], admitted["FromPort"], admitted["ToPort"]) == ("tcp", PORT, PORT)
    assert [admitted["SourceSecurityGroupId"]] == balancer["SecurityGroups"]


def test_the_target_group_takes_the_task_on_port_3000(templates: Templates) -> None:
    path = stack_path(templates, "RhiObservability")
    template = templates[path]
    ((group_id, group),) = of_type(template, ELB + "TargetGroup").items()
    assert grafana_target_groups(templates) == [(path, group_id)]
    # A Fargate task is registered by its address.
    assert (group["Port"], group["Protocol"], group["TargetType"]) == (PORT, "HTTP", "ip")
    # Grafana's health route, under the prefix it is served from, on the port that takes the traffic.
    assert (group["HealthCheckProtocol"], group["HealthCheckPath"]) == ("HTTP", "/grafana/api/health")
    assert group.get("HealthCheckPort", "traffic-port") == "traffic-port"
    assert group["Matcher"] == {"HttpCode": "200"}

    (service,) = of_type(template, "AWS::ECS::Service").values()
    (task,) = of_type(template, "AWS::ECS::TaskDefinition").values()
    (container,) = task["ContainerDefinitions"]
    assert service["LoadBalancers"] == [
        {"ContainerName": container["Name"], "ContainerPort": PORT, "TargetGroupArn": {"Ref": group_id}}
    ]


def test_one_rule_at_priority_30_authenticates_against_the_user_pool_then_forwards(templates: Templates) -> None:
    path = stack_path(templates, "RhiObservability")
    access = stack_path(templates, "RhiAccess")
    (group_id,) = of_type(templates[path], ELB + "TargetGroup")
    ((rule_id, rule),) = of_type(templates[path], ELB + "ListenerRule").items()
    assert grafana_rules(templates) == [(path, rule_id)]
    assert rule["Priority"] == 30
    ((condition),) = rule["Conditions"]
    assert condition["Field"] == "path-pattern"
    # Grafana's paths, and the path the sign-in comes back to, which the load balancer answers.
    assert sorted(condition["PathPatternConfig"]["Values"]) == ["/grafana/*", "/oauth2/idpresponse"]

    # Two actions: the sign-in first, the forward second.
    sign_in, forward = sorted(rule["Actions"], key=lambda action: action["Order"])
    assert (sign_in["Order"], forward["Order"]) == (1, 2)
    assert sign_in["Type"] == "authenticate-cognito"
    assert forward == {"Type": "forward", "Order": 2, "TargetGroupArn": {"Ref": group_id}}
    # Against the access stack's user pool, through its client and its domain. Whoever has not
    # signed in is sent to the sign-in, and never on to Grafana.
    configured = sign_in["AuthenticateCognitoConfig"]
    assert configured["OnUnauthenticatedRequest"] == "authenticate"
    (pool_id,) = of_type(templates[access], "AWS::Cognito::UserPool")
    assert held(templates, configured["UserPoolArn"], path) == (access, pool_id)
    client = held(templates, configured["UserPoolClientId"], path)
    domain = held(templates, configured["UserPoolDomain"], path)
    assert (kind(templates, client), kind(templates, domain)) == (
        "AWS::Cognito::UserPoolClient",
        "AWS::Cognito::UserPoolDomain",
    )
    for resource in (client, domain):
        assert templates[access]["Resources"][resource[1]]["Properties"]["UserPoolId"] == {"Ref": pool_id}

    # The listener is the public load balancer's, on 443, and each of its rules has its own
    # priority: the gateway's 10, the issuer's 20 and this one.
    listener = held(templates, rule["ListenerArn"], path)
    properties = templates[listener[0]]["Resources"][listener[1]]["Properties"]
    assert properties["Port"] == 443
    balancer = templates[listener[0]]["Resources"][properties["LoadBalancerArn"]["Ref"]]["Properties"]
    assert balancer["Scheme"] == "internet-facing"
    priorities = sorted(
        other["Priority"]
        for other_path, _, other in everywhere(templates, ELB + "ListenerRule")
        if held(templates, other["ListenerArn"], other_path) == listener
    )
    assert priorities == [10, 20, 30]


def test_the_environment_gives_the_workspace_s_address_and_the_region(templates: Templates) -> None:
    template = observability(templates)
    (workspace_id,) = of_type(template, "AWS::APS::Workspace")
    (task,) = of_type(template, "AWS::ECS::TaskDefinition").values()
    (container,) = task["ContainerDefinitions"]
    # The address and the region are what CloudFormation gives at deployment, and the domain is
    # the example's: no template synthesized from the example holds a real address.
    assert {variable["Name"]: variable["Value"] for variable in container["Environment"]} == {
        "PROMETHEUS_URL": {"Fn::GetAtt": [workspace_id, "PrometheusEndpoint"]},
        "AWS_REGION": {"Ref": "AWS::Region"},
        "GF_SERVER_DOMAIN": "example.com",
    }
    assert "Secrets" not in container
    # The variables are those the image reads.
    read = (REPO / "services" / "grafana" / "provisioning" / "datasources" / "datasources.yaml").read_text()
    assert "${PROMETHEUS_URL}" in read and "${AWS_REGION}" in read
    # A workspace's address is a host of the service that holds it: none is written anywhere.
    assert not [path for path, written_out in templates.items() if "aps-workspaces" in json.dumps(written_out)]


def test_the_role_names_the_workspace_and_the_stage_s_log_groups_only(templates: Templates) -> None:
    template = observability(templates)
    (workspace_id,) = of_type(template, "AWS::APS::Workspace")
    by_resource = {json.dumps(s["Resource"], sort_keys=True): s for s in task_role_statements(template)}
    assert all(statement["Effect"] == "Allow" for statement in by_resource.values())

    # Three statements, and nothing else: the workspace, the log groups, and the reads that
    # CloudWatch grants on every resource only, each of which the module's file lists.
    workspace = by_resource.pop(json.dumps({"Fn::GetAtt": [workspace_id, "Arn"]}))
    assert set(workspace["Action"]) == QUERY_ACTIONS
    everything = by_resource.pop(json.dumps("*"))
    assert set(everything["Action"]) == CLOUDWATCH_READS
    assert CLOUDWATCH_READS <= set(tomllib.loads(ACKNOWLEDGED.read_text(encoding="utf-8"))["wildcard_actions"])
    (logs,) = by_resource.values()
    assert set(logs["Action"]) == LOG_ACTIONS

    # Each log group by its name, as text, in the stack's own partition, region and account: no
    # reference to the stack that creates it.
    named = set()
    for resource in logs["Resource"]:
        assert imports(resource) == set()
        matched = LOG_GROUP_ARN.fullmatch(written(resource) or "")
        assert matched, resource
        named.add(matched["name"])
    assert named == SERVICE_LOG_GROUPS | {POOLS_LOG_GROUPS}

    # What that reaches of the log groups the stage creates: the gateway's, the scaler's, the
    # issuer's and each configured pool's, and no other, Grafana's own included.
    created = {
        group["LogGroupName"]
        for path, _, group in everywhere(templates, "AWS::Logs::LogGroup")
        if "/" in path and isinstance(group.get("LogGroupName"), str)
    }
    assert "/rhi/grafana" in created
    assert [pool.name for pool in EXAMPLE.pools] == ["mock"]
    assert {name for name in created if any(fnmatchcase(name, pattern) for pattern in named)} == {
        "/rhi/gateway",
        "/rhi/scaler",
        "/rhi/issuer",
        "/rhi/pools/mock",
    }
    # Each name the role writes is one the stage creates, and the pattern takes nothing but the
    # pools' log groups.
    assert SERVICE_LOG_GROUPS <= created
    assert {name for name in created if fnmatchcase(name, POOLS_LOG_GROUPS)} == {"/rhi/pools/mock"}


def test_the_stack_depends_on_no_stack_that_depends_on_it(templates: Templates) -> None:
    path = stack_path(templates, "RhiObservability")
    exporters = {
        other
        for other, template in templates.items()
        for output in template.get("Outputs", {}).values()
        if output.get("Export", {}).get("Name") in imports(templates[path])
    }
    # The network and the sign-in, neither of which imports anything of this stack; not the pools
    # stack and not the gateway stack, which do.
    assert exporters == {stack_path(templates, "RhiNetwork"), stack_path(templates, "RhiAccess")}
    exported = {
        output["Export"]["Name"] for output in templates[path].get("Outputs", {}).values() if "Export" in output
    }
    assert exported
    for exporter in exporters:
        assert not imports(templates[exporter]) & exported
    assert imports(templates[stack_path(templates, "RhiPools")]) & exported


def allowing(template: Template, statement: dict[str, Any], *, inline: bool = False) -> Template:
    """A copy of the observability template in which the role of Grafana's task has one more
    statement, in the policy attached to it or in an inline policy of the role."""
    changed = copy.deepcopy(template)
    (task,) = of_type(changed, "AWS::ECS::TaskDefinition").values()
    role_id = task["TaskRoleArn"]["Fn::GetAtt"][0]
    if inline:
        document = {"Version": "2012-10-17", "Statement": [statement]}
        changed["Resources"][role_id]["Properties"]["Policies"] = [{"PolicyName": "More", "PolicyDocument": document}]
    else:
        (policy,) = [p for p in of_type(changed, "AWS::IAM::Policy").values() if {"Ref": role_id} in p["Roles"]]
        policy["PolicyDocument"]["Statement"].append(statement)
    return changed


def log_group_arn(name: str) -> dict[str, Any]:
    account = [{"Ref": "AWS::Region"}, ":", {"Ref": "AWS::AccountId"}]
    return {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":logs:", *account, f":log-group:{name}"]]}


@pytest.mark.parametrize(
    ("action", "resource", "reported"),
    [
        pytest.param(
            "logs:StartQuery", log_group_arn("/aws/codebuild/Synth:*"), "/aws/codebuild/Synth:*", id="one project"
        ),
        pytest.param("logs:GetLogEvents", log_group_arn("/aws/codebuild/*"), "/aws/codebuild/*", id="every project"),
        pytest.param("logs:StartQuery", log_group_arn("/aws/*"), "/aws/*", id="every log group of AWS's services"),
        pytest.param("logs:FilterLogEvents", log_group_arn("*"), "log-group:*", id="every log group"),
        pytest.param("logs:StartQuery", "*", "logs:StartQuery on *", id="every resource"),
        pytest.param("logs:Get*", "*", "logs:Get* on *", id="a group of actions"),
        pytest.param("*", "*", "* on *", id="every action"),
        pytest.param(
            "logs:StartQuery", {"Fn::ImportValue": "BuildLogs"}, "Fn::ImportValue", id="a log group of another stack"
        ),
    ],
)
@pytest.mark.parametrize("inline", [False, True], ids=["attached", "inline"])
def test_build_logs_are_out_of_reach(
    templates: Templates, action: str, resource: Any, reported: str, inline: bool
) -> None:
    template = observability(templates)
    # Nothing of the role the stage gives Grafana reaches a build project's log group.
    assert build_logs_in_reach(template) == []
    # The pipeline's build projects exist, and log where CodeBuild logs by default.
    projects = [project for _, _, project in everywhere(templates, "AWS::CodeBuild::Project")]
    assert projects and not [project for project in projects if "LogsConfig" in project]

    statement = {"Effect": "Allow", "Action": action, "Resource": resource}
    (found,) = build_logs_in_reach(allowing(template, statement, inline=inline))
    assert found.startswith(action) and reported in found


def test_reading_a_log_group_of_the_stage_is_not_reading_a_build_log(templates: Templates) -> None:
    more = {"Effect": "Allow", "Action": "logs:StartQuery", "Resource": log_group_arn("/rhi/grafana:*")}
    assert build_logs_in_reach(allowing(observability(templates), more)) == []


def test_no_listener_no_target_group(without_listener: Templates) -> None:
    assert not [
        listener
        for path, _, listener in everywhere(without_listener, ELB + "Listener")
        if without_listener[path]["Resources"][listener["LoadBalancerArn"]["Ref"]]["Properties"]["Scheme"]
        == "internet-facing"
    ]
    template = observability(without_listener)
    # The service is the one of the example configuration, less its load balancer.
    (service,) = of_type(template, "AWS::ECS::Service").values()
    assert (service["LaunchType"], service["DesiredCount"]) == ("FARGATE", 1)
    assert not service.get("LoadBalancers")
    assert "HealthCheckGracePeriodSeconds" not in service
    assert not [r["Type"] for r in template["Resources"].values() if r["Type"].startswith(ELB)]
    # No target group and no listener rule of Grafana in any template, the pipeline's included.
    assert len(without_listener) > len([path for path in without_listener if "/" in path]) > 0
    assert grafana_target_groups(without_listener) == []
    assert grafana_rules(without_listener) == []
    # The task and its role are as they are with the listener.
    assert build_logs_in_reach(template) == []
    assert len(task_role_statements(template)) == 3
