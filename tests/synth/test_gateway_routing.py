"""The gateway behind the public load balancer (system design, sections 5.3, 6.2 and 8.5): one rule
of the HTTPS listener, at priority 10, forwards the paths of the gateway's route table to the
service's target group, which keeps a deregistering task for 1800 seconds and checks its health
with a keyless `GET /v1/models` that answers 401. All of it only while the listener exists: without
it the service has no load balancer.

The tests synthesize the stage themselves, with its gateway stack and with cdk-nag, which fails the
synthesis on a finding that the file of the resource's module does not acknowledge: the app does
not ask the stage for that stack yet (`rhi.stage.EndpointStage`). The CDK is imported inside
`synthesize`, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config
from rhi_gateway import routes

if TYPE_CHECKING:
    from types import ModuleType

Template = dict[str, Any]

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})
PATTERNS = ["/v1/*", "/status", "/wake", "/api/hello"]
ELB = "AWS::ElasticLoadBalancingV2::"


def of_type(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


@dataclasses.dataclass(frozen=True)
class Stage:
    """The templates of the stage's stacks, by their paths, and the gateway stack's among them."""

    templates: dict[str, Template]

    @property
    def gateway_path(self) -> str:
        (path,) = [path for path in self.templates if Path(path).name.startswith("RhiGateway")]
        return path

    @property
    def gateway(self) -> Template:
        return self.templates[self.gateway_path]

    @property
    def service(self) -> dict[str, Any]:
        (service,) = of_type(self.gateway, "AWS::ECS::Service").values()
        return service

    def everywhere(self, kind: str) -> list[tuple[str, str, dict[str, Any]]]:
        """Every resource of a type, in whichever template: its template's path, its logical ID
        and its properties."""
        return [
            (path, name, properties)
            for path, template in self.templates.items()
            for name, properties in of_type(template, kind).items()
        ]

    def held(self, reference: Any, path: str) -> tuple[str, str]:
        """The resource a reference of the template at `path` names, in whichever template holds
        it: the template's path and the resource's logical ID."""
        ((function, argument),) = reference.items()
        if function == "Fn::ImportValue":
            ((exporter, value),) = [
                (other, output["Value"])
                for other, template in self.templates.items()
                for output in template.get("Outputs", {}).values()
                if output.get("Export", {}).get("Name") == argument
            ]
            return self.held(value, exporter)
        return path, argument if function == "Ref" else argument[0]

    def public_listeners(self) -> list[tuple[str, str]]:
        """The listeners of an internet-facing load balancer."""
        return [
            (path, name)
            for path, name, listener in self.everywhere(ELB + "Listener")
            if self.templates[path]["Resources"][listener["LoadBalancerArn"]["Ref"]]["Properties"]["Scheme"]
            == "internet-facing"
        ]


def synthesize(app_module: ModuleType, outdir: Path, *, https_listener: bool) -> Stage:
    """The stage, with its gateway stack, under the example configuration with the listener on or
    off: with the context of `cdk.json`, nothing bundled, and cdk-nag as the app runs it."""
    import aws_cdk as cdk

    from rhi.stage import EndpointStage

    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = cdk.App(outdir=str(outdir), context=context, analytics_reporting=False)
    EndpointStage(app, "Rhi", config=dataclasses.replace(EXAMPLE, https_listener=https_listener), gateway=True)
    app_module.add_nag(app)
    app.synth()
    return Stage(
        {
            path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(outdir.rglob("*.template.json"))
        }
    )


@pytest.fixture(scope="module")
def stage(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Stage:
    """The stage under the example configuration, whose listener exists."""
    assert EXAMPLE.https_listener is True
    return synthesize(app_module, tmp_path_factory.mktemp("listener"), https_listener=True)


def test_the_service_is_attached_on_port_8080_to_a_target_group_that_drains_for_1800_seconds(stage: Stage) -> None:
    ((group_id, group),) = of_type(stage.gateway, ELB + "TargetGroup").items()
    # Fargate tasks are registered by their addresses.
    assert (group["Port"], group["Protocol"], group["TargetType"]) == (8080, "HTTP", "ip")
    attributes = {attribute["Key"]: attribute["Value"] for attribute in group["TargetGroupAttributes"]}
    assert attributes["deregistration_delay.timeout_seconds"] == "1800"

    (task,) = of_type(stage.gateway, "AWS::ECS::TaskDefinition").values()
    (listening,) = [container for container in task["ContainerDefinitions"] if container.get("PortMappings")]
    assert [port["ContainerPort"] for port in listening["PortMappings"]] == [8080]
    assert stage.service["LoadBalancers"] == [
        {"ContainerName": listening["Name"], "ContainerPort": 8080, "TargetGroupArn": {"Ref": group_id}}
    ]

    # The attachment adds no security group rule: the tasks' group still admits one source on one
    # port, the public load balancer's group on 8080.
    assert not [kind for kind in (r["Type"] for r in stage.gateway["Resources"].values()) if "SecurityGroup" in kind]
    (tasks_group,) = stage.service["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"]
    network_path, tasks_group_id = stage.held(tasks_group, stage.gateway_path)
    network = stage.templates[network_path]
    (balancer,) = [b for b in of_type(network, ELB + "LoadBalancer").values() if b["Scheme"] == "internet-facing"]
    (admitted,) = [
        rule
        for rule in of_type(network, "AWS::EC2::SecurityGroupIngress").values()
        if stage.held(rule["GroupId"], network_path) == (network_path, tasks_group_id)
    ]
    assert (admitted["IpProtocol"], admitted["FromPort"], admitted["ToPort"]) == ("tcp", 8080, 8080)
    assert [admitted["SourceSecurityGroupId"]] == balancer["SecurityGroups"]


def test_the_health_check_gets_v1_models_and_counts_401_as_healthy(stage: Stage) -> None:
    (group,) = of_type(stage.gateway, ELB + "TargetGroup").values()
    # A load balancer's HTTP check is a GET that carries no header, so no key, and it goes to the
    # port that takes the traffic.
    assert (group["HealthCheckProtocol"], group["HealthCheckPath"]) == ("HTTP", "/v1/models")
    assert group.get("HealthCheckPort", "traffic-port") == "traffic-port"
    assert group["Matcher"] == {"HttpCode": "401"}
    # The path is a route of the gateway's table that answers GET: the check adds no route.
    assert routes.match("GET", group["HealthCheckPath"].encode()) is routes.MODELS


def test_one_rule_of_the_public_listener_at_priority_10_forwards_the_gateway_s_paths(stage: Stage) -> None:
    (group_id,) = of_type(stage.gateway, ELB + "TargetGroup")
    forward = [{"Type": "forward", "TargetGroupArn": {"Ref": group_id}}]
    ((path, _, rule),) = [found for found in stage.everywhere(ELB + "ListenerRule") if found[2]["Actions"] == forward]
    assert path == stage.gateway_path
    assert rule["Priority"] == 10
    ((condition),) = rule["Conditions"]
    assert condition["Field"] == "path-pattern"
    assert sorted(condition["PathPatternConfig"]["Values"]) == sorted(PATTERNS)

    # Every route of the gateway's table is under one of the patterns.
    def takes(pattern: str, route: str) -> bool:
        return route.startswith(pattern[:-1]) if pattern.endswith("*") else route == pattern

    for route in routes.TABLE:
        assert [pattern for pattern in PATTERNS if takes(pattern, route.path)], route

    # The listener is the public load balancer's one listener, on 443, and each of its rules has
    # its own priority.
    listener = stage.held(rule["ListenerArn"], path)
    assert [listener] == stage.public_listeners()
    assert stage.templates[listener[0]]["Resources"][listener[1]]["Properties"]["Port"] == 443
    priorities = [
        other["Priority"]
        for other_path, _, other in stage.everywhere(ELB + "ListenerRule")
        if stage.held(other["ListenerArn"], other_path) == listener
    ]
    assert 10 in priorities and len(priorities) == len(set(priorities))


def test_no_listener_no_target_group(app_module: ModuleType, tmp_path: Path) -> None:
    stage = synthesize(app_module, tmp_path, https_listener=False)
    assert stage.public_listeners() == []
    # The service is the one of the example configuration, less its load balancer.
    assert stage.service["DesiredCount"] == EXAMPLE.gateway_tasks
    assert not stage.service.get("LoadBalancers")
    assert "HealthCheckGracePeriodSeconds" not in stage.service
    assert not [r["Type"] for r in stage.gateway["Resources"].values() if r["Type"].startswith(ELB)]

    # What is left in the stage's templates is the pools': target groups of instances on the
    # engine's port, and rules of the internal load balancer's listener. None takes a task by its
    # address or port 8080, and none is on a listener the internet reaches.
    groups = stage.everywhere(ELB + "TargetGroup")
    assert groups
    for path, _, group in groups:
        assert Path(path).name.startswith("RhiPools")
        assert (group["TargetType"], group["Port"]) == ("instance", 8000)
    rules = stage.everywhere(ELB + "ListenerRule")
    assert rules
    for path, _, rule in rules:
        held_by, listener = stage.held(rule["ListenerArn"], path)
        balancer = stage.templates[held_by]["Resources"][listener]["Properties"]["LoadBalancerArn"]["Ref"]
        assert stage.templates[held_by]["Resources"][balancer]["Properties"]["Scheme"] == "internal"
