"""The gateway's service in the gateway stack (system design, sections 2, 5.3, 6.2 and 11.2): as
many Fargate tasks as `gateway_tasks`, in private subnets; an environment the gateway's settings
reader accepts, with a pool or with none; a task role that may do what the gateway and its
collector do and no more; and a collector beside the gateway, both logging to /rhi/gateway. How
the public load balancer reaches the service is `test_gateway_routing.py`'s.

Some values of the environment are references that CloudFormation resolves at deployment.
`Deployment` gives each a placeholder of the form the deployment would give, and remembers which
resource of which template the placeholder stands for.

The tests synthesize the stage themselves, with cdk-nag, from the example configuration and from
changes of it. The CDK is imported inside `synthesize`, after `app_module` has started jsii (see
conftest.py).
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest

from rhi import config
from rhi_common import tables
from rhi_gateway import settings as gateway_settings

if TYPE_CHECKING:
    from types import ModuleType

    import aws_cdk as cdk

Template = dict[str, Any]
Templates = dict[str, Template]
# A resource of the stage: its template's path, and its logical ID there.
Resource = tuple[str, str]

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})

REGION = "us-east-1"
ARN = f"arn:aws:{{service}}:{REGION}:123456789012:{{resource}}"
PSEUDO = {"AWS::Partition": "aws", "AWS::Region": REGION, "AWS::AccountId": "123456789012"}
# What CloudFormation gives for a reference to a resource, by its type and the attribute read: the
# test's placeholder, of the same form, from the resource's logical ID and its properties.
FORMS = {
    ("AWS::DynamoDB::Table", "Ref"): lambda name, _: f"table-{name}",
    ("AWS::DynamoDB::Table", "Arn"): lambda name, _: ARN.format(service="dynamodb", resource=f"table/table-{name}"),
    ("AWS::Lambda::Function", "Ref"): lambda name, _: f"function-{name}",
    ("AWS::Lambda::Function", "Arn"): lambda name, _: ARN.format(
        service="lambda", resource=f"function:function-{name}"
    ),
    ("AWS::ElasticLoadBalancingV2::TargetGroup", "Ref"): lambda name, _: ARN.format(
        service="elasticloadbalancing",
        resource=f"targetgroup/{name}/{hashlib.sha256(name.encode()).hexdigest()[:16]}",
    ),
    ("AWS::ElasticLoadBalancingV2::LoadBalancer", "DNSName"): lambda name, _: f"internal-{name.lower()}.example.com",
    # Secrets Manager ends a secret's ARN with six characters of its own.
    ("AWS::SecretsManager::Secret", "Ref"): lambda _, properties: ARN.format(
        service="secretsmanager", resource=f"secret:{properties['Name']}-AbCdEf"
    ),
    ("AWS::APS::Workspace", "Arn"): lambda name, _: ARN.format(service="aps", resource=f"workspace/ws-{name.lower()}"),
    ("AWS::APS::Workspace", "PrometheusEndpoint"): lambda name, _: f"https://aps.example.com/workspaces/ws-{name}/",
}

READS = {"dynamodb:BatchGetItem", "dynamodb:ConditionCheckItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"}
WRITES = {"dynamodb:BatchWriteItem", "dynamodb:DeleteItem", "dynamodb:PutItem", "dynamodb:UpdateItem"}
SECRET_READS = {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}
# Managed Prometheus gives a workspace's endpoint with a closing slash; remote write is below it.
REMOTE_WRITE_PATH = "api/v1/remote_write"


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def find(templates: Templates, prefix: str) -> tuple[str, Template]:
    (found,) = [(path, template) for path, template in templates.items() if Path(path).name.startswith(prefix)]
    return found


def of_type(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


class Deployment:
    """The stage's templates, read as a deployment would resolve them."""

    def __init__(self, templates: Templates) -> None:
        self.templates = templates
        # The resource each placeholder stands for.
        self.stands_for: dict[str, Resource] = {}
        self._exports = {
            output["Export"]["Name"]: (path, output["Value"])
            for path, template in templates.items()
            for output in template.get("Outputs", {}).values()
            if "Export" in output
        }

    def origin(self, value: Any, path: str) -> tuple[str, str, str]:
        """The resource a reference of the template at `path` reads, in whichever template holds
        it, and the attribute read (`Ref` for the resource's own reference)."""
        ((function, argument),) = value.items()
        if function == "Fn::ImportValue":
            return self.origin(self._exports[argument][1], self._exports[argument][0])
        if function == "Ref":
            return path, argument, "Ref"
        assert function == "Fn::GetAtt", f"not a reference the test reads: {value}"
        return path, argument[0], argument[1]

    def text(self, value: Any, path: str) -> str:
        """A value of the template at `path` as the text a deployment makes of it."""
        if isinstance(value, str):
            return value
        ((function, argument),) = value.items()
        if function == "Fn::Join":
            return argument[0].join(self.text(part, path) for part in argument[1])
        if function == "Fn::Sub":
            body, given = argument if isinstance(argument, list) else (argument, {})
            return re.sub(r"\$\{([^}]*)\}", lambda name: self.text(given.get(name[1], {"Ref": name[1]}), path), body)
        if function == "Ref" and argument in PSEUDO:
            return PSEUDO[argument]
        held_by, logical_id, attribute = self.origin(value, path)
        resource = self.templates[held_by]["Resources"][logical_id]
        placeholder = FORMS[resource["Type"], attribute](logical_id, resource.get("Properties", {}))
        self.stands_for[placeholder] = (held_by, logical_id)
        return placeholder


class Gateway:
    """The gateway stack's template among the stage's, and what the tests read of it."""

    def __init__(self, outdir: Path) -> None:
        self.outdir = outdir
        templates = {
            path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(outdir.rglob("*.template.json"))
        }
        self.deployment = Deployment(templates)
        self.path, self.template = find(templates, "RhiGateway")
        ((_, self.service),) = of_type(self.template, "AWS::ECS::Service").items()
        ((_, self.task),) = of_type(self.template, "AWS::ECS::TaskDefinition").items()
        # The gateway is the container that listens; the collector is the other one.
        (self.container,) = [c for c in self.task["ContainerDefinitions"] if c.get("PortMappings")]
        (self.collector,) = [c for c in self.task["ContainerDefinitions"] if not c.get("PortMappings")]

    def environment(self, container: dict[str, Any]) -> dict[str, str]:
        return {entry["Name"]: self.deployment.text(entry["Value"], self.path) for entry in container["Environment"]}

    def origin(self, value: Any) -> tuple[str, str, str]:
        return self.deployment.origin(value, self.path)


def synthesize(app_module: ModuleType, outdir: Path, change: Any = None) -> Gateway:
    """The gateway of the stage built from the example configuration's data once `change` has
    changed it: with the context of `cdk.json`, nothing bundled, and cdk-nag as the app runs it,
    which fails the synthesis on a finding that the file of the resource's module does not
    acknowledge."""
    import aws_cdk as cdk

    from rhi.stage import EndpointStage

    data = tomllib.loads((REPO / config.EXAMPLE_FILE).read_text(encoding="utf-8"))
    if change is not None:
        change(data)
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = cdk.App(outdir=str(outdir), context=context, analytics_reporting=False)
    EndpointStage(app, "Rhi", config=config.parse(data, "test"))
    app_module.add_nag(app)
    app.synth()
    return Gateway(outdir)


@pytest.fixture(scope="module")
def gateway(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Gateway:
    """The gateway under the example configuration."""
    return synthesize(app_module, tmp_path_factory.mktemp("example"))


# Six numbers that are neither the configuration's defaults nor the settings reader's.
OTHER_NUMBERS = {
    "request_body_limit_bytes": 1_048_576,
    "longest_request_seconds": 900,
    "keepalive_seconds": 7,
    "retry_after_warming_seconds": 31,
    "retry_after_capacity_unavailable_seconds": 61,
    "retry_after_concurrency_limit_seconds": 6,
}
# Two floors of the objectives between the gateway's own boundaries, and one left at its default.
OTHER_FLOORS = {
    "target_ttft_p95_seconds": 6,
    "target_stream_median_tokens_per_second": 16.5,
    "target_stream_p10_tokens_per_second": 10,
}


@pytest.fixture(scope="module")
def one_task(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Gateway:
    """The gateway under a configuration of one task and of other numeric settings."""
    return synthesize(
        app_module, tmp_path_factory.mktemp("one-task"), lambda data: data.update(OTHER_NUMBERS, **OTHER_FLOORS, gateway_tasks=1)
    )


def test_the_service_runs_on_fargate_in_private_subnets(gateway: Gateway) -> None:
    assert gateway.service["LaunchType"] == "FARGATE"
    assert gateway.task["RequiresCompatibilities"] == ["FARGATE"]
    # 0.5 vCPU, in ECS's units, and 1 GB.
    assert (gateway.task["Cpu"], gateway.task["Memory"]) == ("512", "1024")
    assert [port["ContainerPort"] for port in gateway.container["PortMappings"]] == [8080]

    network = gateway.service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network["AssignPublicIp"] == "DISABLED"
    # Every subnet of the tasks is one of the network's whose route table has no route to an
    # internet gateway.
    subnets = [gateway.origin(subnet) for subnet in network["Subnets"]]
    assert len(subnets) == 3
    for path, subnet, _ in subnets:
        held_by = gateway.deployment.templates[path]
        assert held_by["Resources"][subnet]["Type"] == "AWS::EC2::Subnet"
        (table,) = [
            association["RouteTableId"]
            for association in of_type(held_by, "AWS::EC2::SubnetRouteTableAssociation").values()
            if association["SubnetId"] == {"Ref": subnet}
        ]
        routes = [route for route in of_type(held_by, "AWS::EC2::Route").values() if route["RouteTableId"] == table]
        assert routes and not [route for route in routes if "GatewayId" in route]

    # The tasks' one security group is the one the internal load balancer's group admits.
    (path, group, _) = gateway.origin(*network["SecurityGroups"])
    pools_path, pools = find(gateway.deployment.templates, "RhiPools")
    ((_, upstream),) = of_type(pools, "AWS::ElasticLoadBalancingV2::LoadBalancer").items()
    (_, upstream_group, _) = gateway.deployment.origin(*upstream["SecurityGroups"], pools_path)
    admitted = [
        rule["SourceSecurityGroupId"]
        for rule in of_type(gateway.deployment.templates[path], "AWS::EC2::SecurityGroupIngress").values()
        if rule["GroupId"] == {"Fn::GetAtt": [upstream_group, "GroupId"]}
    ]
    assert admitted == [{"Fn::GetAtt": [group, "GroupId"]}]


def test_the_image_is_built_from_the_gateway_s_context_for_the_tasks_architecture(gateway: Gateway) -> None:
    (assets,) = (gateway.outdir / gateway.path).parent.glob("RhiGateway*.assets.json")
    ((tag, image),) = json.loads(assets.read_text(encoding="utf-8"))["dockerImages"].items()
    assert gateway.container["Image"]["Fn::Sub"].endswith(f":{tag}")
    assert gateway.task["RuntimePlatform"] == {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"}
    assert image["source"]["platform"] == "linux/arm64"
    # The context `rhi.packaging` gives a Python service, and the gateway's Dockerfile.
    context = (assets.parent / image["source"]["directory"]).resolve()
    assert {entry.name for entry in context.iterdir()} == {
        "Dockerfile",
        "requirements.txt",
        "rhi_common",
        "rhi_gateway",
    }
    assert (context / "Dockerfile").read_bytes() == (REPO / "services" / "gateway" / "Dockerfile").read_bytes()


def test_the_tasks_are_as_many_as_gateway_tasks(gateway: Gateway, one_task: Gateway) -> None:
    assert EXAMPLE.gateway_tasks == 2
    assert gateway.service["DesiredCount"] == 2
    assert one_task.service["DesiredCount"] == 1


def test_the_environment_is_what_the_gateway_reads(gateway: Gateway) -> None:
    environment = gateway.environment(gateway.container)
    assert set(gateway_settings.REQUIRED) <= set(environment)
    read = gateway_settings.read_settings(environment)
    assert read.pools == EXAMPLE.pools
    # The pool `claude-*` model names reach (section 6.2, step 4).
    assert read.default_pool is not None and read.default_pool.name == EXAMPLE.default_pool == "coursework"

    stands_for = gateway.deployment.stands_for
    pools_path, pools = find(gateway.deployment.templates, "RhiPools")
    # The upstream is the internal load balancer, on its listener's port.
    upstream = urlsplit(read.upstream_url)
    path, balancer = stands_for[str(upstream.hostname)]
    assert path == pools_path and pools["Resources"][balancer]["Properties"]["Scheme"] == "internal"
    ((listener, listens),) = of_type(pools, "AWS::ElasticLoadBalancingV2::Listener").items()
    assert listens["LoadBalancerArn"] == {"Ref": balancer}
    assert (upstream.scheme, upstream.port) == (listens["Protocol"].lower(), listens["Port"])
    # The mock pool's target group is the one a rule of that listener forwards the pool's requests to.
    path, target_group = stands_for[read.target_groups["mock"]]
    assert path == pools_path
    forwarding = [
        rule
        for rule in of_type(pools, "AWS::ElasticLoadBalancingV2::ListenerRule").values()
        if rule["ListenerArn"] == {"Ref": listener}
        and {"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": "X-Rhi-Pool", "Values": ["mock"]}}
        in rule["Conditions"]
    ]
    assert forwarding
    for rule in forwarding:
        assert rule["Actions"] == [{"Type": "forward", "TargetGroupArn": {"Ref": target_group}}]

    # The tables are the four the gateway names, each by its keys, and the function is the scaler.
    for name, schema in (
        (read.keys_table, tables.KEYS),
        (read.usage_table, tables.USAGE),
        (read.heartbeat_table, tables.HEARTBEATS),
        (read.pool_state_table, tables.POOL_STATE),
    ):
        path, table = stands_for[name]
        keys = [
            key["AttributeName"]
            for key in gateway.deployment.templates[path]["Resources"][table]["Properties"]["KeySchema"]
        ]
        assert keys == [key.name for key in (schema.partition_key, schema.sort_key) if key is not None]
    path, function = stands_for[read.scaler_function]
    assert (
        gateway.deployment.templates[path]["Resources"][function]["Properties"]["Handler"]
        == "rhi_scaler.handler.handler"
    )


def test_the_numeric_settings_are_the_configuration_s(gateway: Gateway, one_task: Gateway) -> None:
    # Each is written, also where it is the reader's own default.
    assert set(gateway_settings.NUMERIC_DEFAULTS) <= set(gateway.environment(gateway.container))
    read = gateway_settings.read_settings(one_task.environment(one_task.container))
    assert {
        "request_body_limit_bytes": read.body_limit_bytes,
        "longest_request_seconds": read.longest_request_seconds,
        "keepalive_seconds": read.keepalive_seconds,
        "retry_after_warming_seconds": read.retry_after_warming,
        "retry_after_capacity_unavailable_seconds": read.retry_after_capacity_unavailable,
        "retry_after_concurrency_limit_seconds": read.retry_after_concurrency_limit,
    } == OTHER_NUMBERS
    # The floors are the configuration's, and boundaries of the histograms they are read in.
    assert set(gateway_settings.FLOOR_DEFAULTS) <= set(gateway.environment(gateway.container))
    assert {
        "target_ttft_p95_seconds": read.target_ttft_p95_seconds,
        "target_stream_median_tokens_per_second": read.target_stream_median_tokens_per_second,
        "target_stream_p10_tokens_per_second": read.target_stream_p10_tokens_per_second,
    } == OTHER_FLOORS
    assert 6 in read.wait_buckets and 16.5 in read.speed_buckets


def test_each_pool_s_secret_is_given_by_reference_never_by_value(gateway: Gateway) -> None:
    read = gateway_settings.read_settings(gateway.environment(gateway.container))
    path, secret = gateway.deployment.stands_for[read.pool_secrets["mock"]]
    held = gateway.deployment.templates[path]["Resources"][secret]
    assert (held["Type"], held["Properties"]["Name"]) == ("AWS::SecretsManager::Secret", "/rhi/pools/mock/bearer")
    # ECS is given no secret to put in a variable, and the template resolves none itself.
    assert not [container for container in gateway.task["ContainerDefinitions"] if "Secrets" in container]
    assert "{{resolve:" not in json.dumps(gateway.template)


def test_the_task_role_may_do_what_the_gateway_does_and_nothing_else(gateway: Gateway) -> None:
    (_, role_id, _) = gateway.origin(gateway.task["TaskRoleArn"])
    assert gateway.task["ExecutionRoleArn"] != gateway.task["TaskRoleArn"]
    role = gateway.template["Resources"][role_id]["Properties"]
    assert [s["Principal"] for s in role["AssumeRolePolicyDocument"]["Statement"]] == [
        {"Service": "ecs-tasks.amazonaws.com"}
    ]
    # Everything the role holds is in the policies of this template that name it: it attaches no
    # managed policy, and no other stack can name it, since this stack exports nothing.
    assert not role.get("ManagedPolicyArns") and not role.get("Policies")
    assert not any("Export" in output for output in gateway.template.get("Outputs", {}).values())
    statements = [
        statement
        for policy in of_type(gateway.template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in policy["Roles"]
        for statement in as_list(policy["PolicyDocument"]["Statement"])
    ]
    allowed: set[tuple[str, Resource | str]] = set()
    for statement in statements:
        assert set(statement) == {"Action", "Effect", "Resource"} and statement["Effect"] == "Allow"
        for resource in as_list(statement["Resource"]):
            text = gateway.deployment.text(resource, gateway.path)
            allowed |= {
                (action, gateway.deployment.stands_for.get(text, text)) for action in as_list(statement["Action"])
            }

    read = gateway_settings.read_settings(gateway.environment(gateway.container))
    stands_for = gateway.deployment.stands_for
    # The workspace is the one the collector is told to write to.
    endpoint = gateway.environment(gateway.collector)["RHI_WORKSPACE_URL"].removesuffix(REMOTE_WRITE_PATH)
    expected: set[tuple[str, Resource | str]] = {
        (action, stands_for[table]) for action in READS for table in (read.keys_table, read.pool_state_table)
    }
    expected |= {
        (action, stands_for[table]) for action in READS | WRITES for table in (read.usage_table, read.heartbeat_table)
    }
    # A pool's secret by its name, whatever six characters Secrets Manager ends its ARN with.
    expected |= {(action, arn[:-6] + "??????") for action in SECRET_READS for arn in read.pool_secrets.values()}
    expected |= {
        ("elasticloadbalancing:DescribeTargetHealth", "*"),
        ("lambda:InvokeFunction", stands_for[read.scaler_function]),
        ("aps:RemoteWrite", stands_for[endpoint]),
    }
    assert allowed == expected


def test_an_action_on_every_resource_is_one_the_module_s_file_lists(gateway: Gateway) -> None:
    file = REPO / "tests" / "policy" / "acknowledged" / "gateway_service.toml"
    listed = tomllib.loads(file.read_text(encoding="utf-8"))["wildcard_actions"]
    on_every_resource = {
        action
        for policy in of_type(gateway.template, "AWS::IAM::Policy").values()
        for statement in as_list(policy["PolicyDocument"]["Statement"])
        if "*" in as_list(statement["Resource"])
        for action in as_list(statement["Action"])
    }
    assert on_every_resource == set(listed)


def test_a_collector_pinned_by_digest_runs_beside_the_gateway_with_its_configuration(gateway: Gateway) -> None:
    assert len(gateway.task["ContainerDefinitions"]) == 2
    assert re.fullmatch(r"[a-z0-9./-]+:[0-9.]+@sha256:[0-9a-f]{64}", gateway.collector["Image"])
    environment = gateway.environment(gateway.collector)
    # The collector reads its configuration, the file's own text, from the variable that holds it.
    written = (REPO / "services" / "gateway" / "collector" / "config.yaml").read_text(encoding="utf-8")
    (variable,) = [name for name, value in environment.items() if value == written]
    assert gateway.collector["Command"] == [f"--config=env:{variable}"]
    # What that configuration reads of its environment: the remote-write address and the region.
    assert set(re.findall(r"\$\{env:([A-Z_]+)\}", written)) == set(environment) - {variable}
    assert environment["RHI_WORKSPACE_URL"].endswith(REMOTE_WRITE_PATH)
    endpoint = environment["RHI_WORKSPACE_URL"].removesuffix(REMOTE_WRITE_PATH)
    path, workspace = gateway.deployment.stands_for[endpoint]
    assert gateway.deployment.templates[path]["Resources"][workspace]["Type"] == "AWS::APS::Workspace"
    assert environment["RHI_REGION"] == REGION


def test_both_containers_log_to_rhi_gateway_which_keeps_90_days(gateway: Gateway) -> None:
    ((log_group, properties),) = of_type(gateway.template, "AWS::Logs::LogGroup").items()
    assert (properties["LogGroupName"], properties["RetentionInDays"]) == ("/rhi/gateway", 90)
    for container in (gateway.container, gateway.collector):
        logging = container["LogConfiguration"]
        assert (logging["LogDriver"], logging["Options"]["awslogs-group"]) == ("awslogs", {"Ref": log_group})


def test_the_admin_finds_the_log_group_by_its_logical_id(app: cdk.App) -> None:
    from rhi_admin import main

    stack = app.node.find_child("Rhi").gateway  # ty: ignore[unresolved-attribute]
    assert stack.stack_name == main.GATEWAY_STACK_NAME
    logical_id = stack.resolve(stack.get_logical_id(stack.service.log_group.node.default_child))
    assert logical_id == main.GATEWAY_LOG_GROUP_LOGICAL_ID


def test_the_endpoint_s_output_is_the_domain_or_none_while_there_is_no_listener(
    app_module: ModuleType, gateway: Gateway, tmp_path: Path
) -> None:
    assert EXAMPLE.https_listener
    assert gateway.template["Outputs"]["Endpoint"]["Value"] == EXAMPLE.domain
    # With no listener the output is still declared: what reads it never changes shape.
    without = synthesize(app_module, tmp_path, lambda data: data.update(https_listener=False))
    assert list(without.template["Outputs"]) == list(gateway.template["Outputs"]) == ["Endpoint"]
    assert without.template["Outputs"]["Endpoint"]["Value"] == "none"


def test_a_configuration_with_no_pool(app_module: ModuleType, tmp_path: Path) -> None:
    def leave_out_the_pools(data: dict[str, Any]) -> None:
        data["pools"] = {}
        del data["https_listener"], data["default_pool"]

    gateway = synthesize(app_module, tmp_path, leave_out_the_pools)
    environment = gateway.environment(gateway.container)
    read = gateway_settings.read_settings(environment)
    assert read.pools == () and read.default_pool is None
    assert gateway_settings.DEFAULT_POOL not in environment
    assert (environment["RHI_POOLS"], environment["RHI_TARGET_GROUPS"], environment["RHI_POOL_SECRETS"]) == ("{}",) * 3
    assert gateway.service["DesiredCount"] == 2
