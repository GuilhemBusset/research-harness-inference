"""The image promotion job (scripts/pipeline/promote_image.py): a canary started outside the pool's
group from its launch template with the candidate image, terminated whatever comes of it, the
pool's image parameter written only after the canary served, and the pipeline started only after
that write.

Stand-ins answer for Auto Scaling, EC2, SSM and CodePipeline. Each records its calls, in one log
shared by all four, and refuses a call it was not given an answer for, so no request leaves the
process and a call the job should not make fails the test. The clock and the sleep are the test's.
"""

from __future__ import annotations

import ast
import base64
import hashlib
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

import promote_image
from rhi import config

if TYPE_CHECKING:
    from collections.abc import Callable

POOL = "coursework"
PIPELINE = "the-pipeline"
FAMILIES = "G6e,G7e,P5,P5e,P5en,P6"
# Stand-in IDs, made when the tests run: each has the shape of EC2's and names nothing.
CANDIDATE = "ami-" + hashlib.sha256(b"candidate").hexdigest()[:17]
TEMPLATE = "lt-" + hashlib.sha256(b"template").hexdigest()[:17]
INSTANCE = "i-" + hashlib.sha256(b"canary").hexdigest()[:17]
SUBNETS = ["subnet-" + hashlib.sha256(name).hexdigest()[:17] for name in (b"a", b"b")]
RELEASE_PARAMETER = f"/rhi/pools/{POOL}/release"

SERVED = "rhi-canary served image_digest=sha256:" + "0" * 64 + " model_revision=" + "1" * 40
FAILED = "rhi-canary failed stage=weights"
# What changes a group's size or membership: the job makes none of these calls.
CAPACITY_CALLS = {
    "set_desired_capacity",
    "update_auto_scaling_group",
    "attach_instances",
    "detach_instances",
    "terminate_instance_in_auto_scaling_group",
    "put_scaling_policy",
    "execute_policy",
}


class Stub:
    """One service's stand-in: each answer by its method's name, a value, an exception or a
    function of the call's arguments."""

    def __init__(self, service: str, log: list[tuple[str, str, dict[str, Any]]], **answers: Any) -> None:
        self.service, self.log, self.answers = service, log, answers

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in self.answers:
            raise AssertionError(f"the job called {self.service}.{name}, which it should not")

        def call(**arguments: Any) -> Any:
            self.log.append((self.service, name, arguments))
            answer = self.answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer(**arguments) if callable(answer) else answer

        return call


def console(*lines: str) -> dict[str, str]:
    """EC2's answer to a read of the console: its output, in base64."""
    return {"Output": base64.b64encode("\n".join(["[  OK  ] Started boot.", *lines]).encode()).decode()}


def failure(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "AWS's own words"}}, "Operation")


def a_group(instance_type: str = "g7e.2xlarge") -> dict[str, Any]:
    specification = {"LaunchTemplateId": TEMPLATE, "Version": "3"}
    return {
        "AutoScalingGroups": [
            {
                "AutoScalingGroupName": "the-group",
                "MixedInstancesPolicy": {
                    "LaunchTemplate": {
                        "LaunchTemplateSpecification": specification,
                        "Overrides": [{"InstanceType": instance_type}],
                    }
                },
                "VPCZoneIdentifier": ",".join(SUBNETS),
                "DesiredCapacity": 0,
            }
        ]
    }


TAGGED = {
    "LaunchTemplateVersions": [
        {
            "LaunchTemplateData": {
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "rhi-pool", "Value": POOL},
                            {"Key": "rhi-engine", "Value": "vllm"},
                            {"Key": "rhi-release-parameter", "Value": RELEASE_PARAMETER},
                        ],
                    }
                ]
            }
        }
    ]
}


class Run:
    """One run of the job against the stand-ins: its exit code, its calls and its output."""

    def __init__(
        self,
        capsys: pytest.CaptureFixture[str],
        *,
        lines: list[dict[str, str]] | None = None,
        families: str = FAMILIES,
        instance_type: str = "g7e.2xlarge",
        start: Any = None,
        state: str = "running",
        put: Any = None,
        terminate: Any = None,
    ) -> None:
        self.log: list[tuple[str, str, dict[str, Any]]] = []
        reads = iter(lines or [console()])
        last: dict[str, Any] = {}
        now = [0.0]

        def read(**_: Any) -> dict[str, str]:
            last["output"] = next(reads, last.get("output", {}))
            return last["output"]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        clients = promote_image.Clients(
            autoscaling=Stub("autoscaling", self.log, describe_auto_scaling_groups=a_group(instance_type)),
            ec2=Stub(
                "ec2",
                self.log,
                describe_images={"Images": [{"ImageId": CANDIDATE, "State": "available"}]},
                describe_launch_template_versions=TAGGED,
                run_instances=start or {"Instances": [{"InstanceId": INSTANCE}]},
                describe_instances={"Reservations": [{"Instances": [{"State": {"Name": state}}]}]},
                get_console_output=read,
                terminate_instances=terminate or {},
            ),
            ssm=Stub("ssm", self.log, put_parameter=put or {"Version": 2}),
            codepipeline=Stub("codepipeline", self.log, start_pipeline_execution={"pipelineExecutionId": "run"}),
        )
        argv = ["--pool", POOL, "--image", CANDIDATE, "--families", families, "--timeout", "600"]
        environ = {promote_image.PIPELINE_VARIABLE: PIPELINE}
        self.code = promote_image.main(argv, clients=clients, environ=environ, clock=lambda: now[0], sleep=sleep)
        self.out, self.err = capsys.readouterr()

    def calls(self, name: str) -> list[dict[str, Any]]:
        return [arguments for _, called, arguments in self.log if called == name]

    def order(self) -> list[str]:
        return [name for _, name, _ in self.log]


def constants(module: str) -> dict[str, Any]:
    """The constants a module of the app assigns, read from its source: importing it starts the CDK."""
    tree = ast.parse((config.REPO_ROOT / module).read_text(encoding="utf-8"))
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def test_the_job_reads_the_tags_and_names_the_parameter_as_the_pools_give_them() -> None:
    group, image = constants("src/rhi/pools/group.py"), constants("src/rhi/pools/image.py")
    promotion = constants("src/rhi/pipeline/image_promotion.py")
    assert promote_image.POOL_TAG == group["POOL_TAG"]
    assert promote_image.RELEASE_PARAMETER_TAG == group["RELEASE_PARAMETER_TAG"]
    assert promote_image.IMAGE_PARAMETER_PREFIX == image["IMAGE_PARAMETER_PREFIX"]
    # The tag the job's role requires of the instances it reads and terminates.
    assert (promote_image.JOB_TAG, promote_image.JOB_TAG_VALUE) == (promotion["JOB_TAG"], promotion["JOB_TAG_VALUE"])
    assert promote_image.PIPELINE_VARIABLE == promotion["PIPELINE_VARIABLE"]


def test_a_serving_canary_writes_the_parameter_then_starts_the_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(capsys, lines=[{}, console(), console("rhi-canary-agent: starting"), console(SERVED)])
    assert run.code == 0
    # The group is found by its pool's tag.
    (described,) = run.calls("describe_auto_scaling_groups")
    assert described["Filters"] == [{"Name": "tag:rhi-pool", "Values": [POOL]}]
    # One instance, from the group's launch template, with the candidate, in one of its subnets,
    # with the template's two tags and the job's own.
    (started,) = run.calls("run_instances")
    assert started["LaunchTemplate"] == {"LaunchTemplateId": TEMPLATE, "Version": "3"}
    assert started["ImageId"] == CANDIDATE and started["InstanceType"] == "g7e.2xlarge"
    assert started["SubnetId"] in SUBNETS and started["MinCount"] == started["MaxCount"] == 1
    (tags,) = started["TagSpecifications"]
    assert tags["ResourceType"] == "instance"
    assert {tag["Key"]: tag["Value"] for tag in tags["Tags"]} == {
        "rhi-pool": POOL,
        "rhi-release-parameter": RELEASE_PARAMETER,
        promote_image.JOB_TAG: promote_image.JOB_TAG_VALUE,
    }
    assert run.calls("terminate_instances") == [{"InstanceIds": [INSTANCE]}]
    assert run.calls("put_parameter") == [
        {"Name": f"/rhi/images/{POOL}", "Value": CANDIDATE, "Type": "String", "Overwrite": True}
    ]
    assert run.calls("start_pipeline_execution") == [{"name": PIPELINE}]
    order = run.order()
    assert order.index("terminate_instances") < order.index("put_parameter") < order.index("start_pipeline_execution")
    assert order[-1] == "start_pipeline_execution"


@pytest.mark.parametrize(
    ("changes", "cause"),
    [
        pytest.param({"lines": [console(FAILED)]}, promote_image.Cause.CANARY_FAILED, id="failed"),
        pytest.param({"lines": [console()]}, promote_image.Cause.TIMED_OUT, id="timed out"),
        pytest.param({"state": "terminated"}, promote_image.Cause.CANARY_STOPPED, id="stopped before its line"),
    ],
)
def test_a_canary_that_does_not_serve_is_terminated_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], changes: dict[str, Any], cause: promote_image.Cause
) -> None:
    run = Run(capsys, **changes)
    assert run.code != 0
    assert f"not promoted: {cause.value}" in run.err
    assert len(run.calls("run_instances")) == 1
    assert run.calls("terminate_instances") == [{"InstanceIds": [INSTANCE]}]
    assert run.calls("put_parameter") == [] and run.calls("start_pipeline_execution") == []


def test_a_failed_write_starts_no_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(capsys, lines=[console(SERVED)], put=failure("AccessDeniedException"))
    assert run.code != 0 and f"not promoted: {promote_image.Cause.NOT_WRITTEN.value}" in run.err
    assert run.calls("terminate_instances") == [{"InstanceIds": [INSTANCE]}]
    assert run.calls("start_pipeline_execution") == []
    assert "AWS's own words" not in run.out + run.err


def test_a_canary_that_is_not_terminated_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(capsys, lines=[console(SERVED)], terminate=failure("UnauthorizedOperation"))
    assert run.code != 0 and f"not promoted: {promote_image.Cause.NOT_TERMINATED.value}" in run.err
    assert run.calls("put_parameter") == [] and run.calls("start_pipeline_execution") == []


def test_a_zone_without_capacity_moves_the_canary_to_the_next_subnet(capsys: pytest.CaptureFixture[str]) -> None:
    attempts: list[str] = []

    def start(**arguments: Any) -> dict[str, Any]:
        attempts.append(arguments["SubnetId"])
        if len(attempts) == 1:
            raise failure("InsufficientInstanceCapacity")
        return {"Instances": [{"InstanceId": INSTANCE}]}

    run = Run(capsys, lines=[console(SERVED)], start=start)
    assert run.code == 0 and attempts == SUBNETS


@pytest.mark.parametrize(
    ("families", "instance_type"),
    [
        pytest.param("G6e,G7,P5,P6", "g7e.2xlarge", id="the single CUDA image, for a G7e pool"),
        pytest.param("g7e", "p5.48xlarge", id="another family"),
    ],
)
def test_a_pool_whose_family_is_not_listed_is_refused_before_anything_starts(
    capsys: pytest.CaptureFixture[str], families: str, instance_type: str
) -> None:
    run = Run(capsys, families=families, instance_type=instance_type)
    assert run.code != 0
    assert f"not promoted: {promote_image.Cause.FAMILY_NOT_LISTED.value}" in run.err
    # Nothing but the description of the group, which says the pool's family.
    assert run.order() == ["describe_auto_scaling_groups"]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"lines": [console(SERVED)]}, id="served"),
        pytest.param({"lines": [console(FAILED)]}, id="failed"),
        pytest.param({"lines": [console()]}, id="timed out"),
        pytest.param({"families": "G6e,G7,P5,P6"}, id="refused"),
    ],
)
def test_no_call_changes_a_group_s_capacity(capsys: pytest.CaptureFixture[str], changes: dict[str, Any]) -> None:
    run = Run(capsys, **changes)
    # The Auto Scaling stand-in answers its description only, and refuses any other call.
    assert {name for service, name, _ in run.log if service == "autoscaling"} == {"describe_auto_scaling_groups"}
    assert not {name for _, name, _ in run.log} & CAPACITY_CALLS
