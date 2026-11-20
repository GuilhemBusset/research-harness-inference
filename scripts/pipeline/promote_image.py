"""Promotes a machine image for a GPU pool, only after a canary start on it passes (system design,
sections 4.3 and 5.1).

The job takes a pool, a candidate image ID and the instance families the candidate's release notes
list. In this order it:

1. finds the pool's Auto Scaling group by its tag `rhi-pool` (`rhi.pools.group`), and reads the
   group's launch template, its subnets and its instance types;
2. refuses the pool when the candidate does not list the family of every one of those types (the
   part of the type before the dot, compared whole and in any letter case: G7 does not list
   g7e), and when the candidate is not an available image. Nothing has started yet;
3. starts one instance, the canary, with EC2's own call: from the group's launch template, at the
   group's version, with the candidate in place of the template's image, the pool's first instance
   type, in one of the group's subnets (the next one when a zone has no capacity of that type), with
   the template's tags `rhi-pool` and `rhi-release-parameter`, which the boot unit reads, and the
   job's own tag `JOB_TAG`. Auto Scaling does not know the instance: the job never changes a
   group's capacity, which the scaler alone sets;
4. reads the canary's serial console until its lifecycle agent writes its line
   (`rhi_lifecycle.canary`): `rhi-canary served ...` or `rhi-canary failed stage=<stage>`, until
   the instance stops on its own, or until the time limit;
5. terminates the canary, whatever came of it;
6. only when the line said the engine served, writes the candidate's ID to the pool's image
   parameter, `/rhi/images/<pool>` (`rhi.pools.image`), and then, only once that write has
   succeeded, starts an execution of this project's pipeline, so that the next deployment takes
   the image and refreshes the pool.

Any other end writes nothing, starts nothing, and exits non-zero with a cause from `Cause`. The job
prints the pool, the image and the cause, and never a message of AWS.

Run by the project of `rhi.pipeline.image_promotion`, which the owner starts by hand and nothing
else starts, with `RHI_PROMOTE_PIPELINE` naming the pipeline:

    uv run --locked python scripts/pipeline/promote_image.py \\
        --pool coursework --image ami-<candidate> --families G6e,G7e,P5,P5e,P5en,P6
"""

from __future__ import annotations

import argparse
import enum
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

NAME = "promote_image"

# The tags a pool's group and launch template carry (`rhi.pools.group`), which this job reads, and
# the parameter each pool's image is written to (`rhi.pools.image`). That module imports the CDK,
# which this job does not install; tests/unit/test_promote_image.py holds the values equal.
POOL_TAG = "rhi-pool"
RELEASE_PARAMETER_TAG = "rhi-release-parameter"
IMAGE_PARAMETER_PREFIX = "/rhi/images/"
# The job's own tag on the canary: its role may read the console of, and terminate, only an
# instance that carries it (`rhi.pipeline.image_promotion`).
JOB_TAG = "rhi-promotion"
JOB_TAG_VALUE = "canary"
# The word that opens the canary's console line, and the line's two forms (`rhi_lifecycle.canary`).
MARK = "rhi-canary"
LINE = re.compile(rf"{MARK} (served|failed)(?: stage=([a-z-]+))?\b")
# The variable that names this project's pipeline.
PIPELINE_VARIABLE = "RHI_PROMOTE_PIPELINE"

# How long the job waits for the canary's line. The canary keeps a deadline of 15 minutes, or
# twice its pool's measured start, from the moment its agent starts; the instance boots first.
TIMEOUT_SECONDS = 40 * 60
# How often it reads the console. EC2 keeps the latest output of a Nitro instance's console.
POLL_SECONDS = 20.0

# The states in which an instance can no longer write its line.
STOPPED = frozenset({"shutting-down", "terminated", "stopping", "stopped"})
# What EC2 answers when a zone has no capacity of the type asked for: the next subnet is tried.
NO_CAPACITY = frozenset({"InsufficientInstanceCapacity", "Unsupported"})


class Cause(enum.Enum):
    """Why the job wrote no image. The value is what the job prints."""

    NO_GROUP = "no-group"
    FAMILY_NOT_LISTED = "family-not-listed"
    IMAGE_UNAVAILABLE = "image-unavailable"
    TEMPLATE_UNTAGGED = "template-untagged"
    NOT_STARTED = "canary-not-started"
    CANARY_FAILED = "canary-failed"
    CANARY_STOPPED = "canary-stopped"
    TIMED_OUT = "canary-timed-out"
    NOT_TERMINATED = "canary-not-terminated"
    NOT_WRITTEN = "parameter-not-written"
    PIPELINE_NOT_STARTED = "pipeline-not-started"


class Refused(Exception):
    """The job's end without a promotion, by its cause."""

    def __init__(self, cause: Cause, detail: str = "") -> None:
        super().__init__(cause.value)
        self.cause = cause
        self.detail = detail


@dataclass(frozen=True)
class Group:
    """What the job reads of a pool's group."""

    template_id: str
    version: str
    subnets: list[str]
    instance_types: list[str]


@dataclass
class Clients:
    """The AWS SDK's clients the job calls: Auto Scaling, EC2, SSM and CodePipeline."""

    autoscaling: Any
    ec2: Any
    ssm: Any
    codepipeline: Any


@dataclass
class Promotion:
    """One promotion. `clock` and `sleep` are arguments so that a test waits for nothing."""

    pool: str
    image: str
    families: list[str]
    pipeline: str
    clients: Clients
    timeout: float = TIMEOUT_SECONDS
    poll: float = POLL_SECONDS
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep
    said: list[str] = field(default_factory=list)

    def run(self) -> None:
        """Promote the candidate, or raise `Refused`."""
        group = self.find_group()
        listed = {family.strip().lower() for family in self.families if family.strip()}
        unlisted = sorted({family_of(kind) for kind in group.instance_types} - listed)
        if unlisted:
            raise Refused(Cause.FAMILY_NOT_LISTED, ",".join(unlisted))
        self.check_image()
        tags = self.template_tags(group)
        instance = self.start(group, tags)
        try:
            served, stage = self.watch(instance)
        finally:
            self.terminate(instance)
        if not served:
            raise Refused(Cause.CANARY_FAILED, f"stage {stage}")
        self.write()
        self.start_pipeline()

    def call(self, cause: Cause, method: Callable[..., Any], **arguments: Any) -> Any:
        """A call to AWS whose failure ends the job with `cause`, without AWS's message."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return method(**arguments)
        except (BotoCoreError, ClientError):
            raise Refused(cause) from None

    def find_group(self) -> Group:
        pages = self.call(
            Cause.NO_GROUP,
            self.clients.autoscaling.describe_auto_scaling_groups,
            Filters=[{"Name": f"tag:{POOL_TAG}", "Values": [self.pool]}],
        )
        groups = pages.get("AutoScalingGroups", [])
        if len(groups) != 1:
            raise Refused(Cause.NO_GROUP)
        (group,) = groups
        mixed = group.get("MixedInstancesPolicy", {}).get("LaunchTemplate", {})
        specification = mixed.get("LaunchTemplateSpecification") or group.get("LaunchTemplate") or {}
        kinds = [override["InstanceType"] for override in mixed.get("Overrides", []) if "InstanceType" in override]
        subnets = [subnet for subnet in group.get("VPCZoneIdentifier", "").split(",") if subnet]
        if "LaunchTemplateId" not in specification or not kinds or not subnets:
            raise Refused(Cause.NO_GROUP)
        version = str(specification.get("Version", "$Default"))
        return Group(specification["LaunchTemplateId"], version, subnets, kinds)

    def check_image(self) -> None:
        found = self.call(Cause.IMAGE_UNAVAILABLE, self.clients.ec2.describe_images, ImageIds=[self.image])
        images = found.get("Images", [])
        if [image.get("State") for image in images] != ["available"]:
            raise Refused(Cause.IMAGE_UNAVAILABLE)

    def template_tags(self, group: Group) -> dict[str, str]:
        """The tags `rhi-pool` and `rhi-release-parameter` the launch template gives an instance."""
        found = self.call(
            Cause.TEMPLATE_UNTAGGED,
            self.clients.ec2.describe_launch_template_versions,
            LaunchTemplateId=group.template_id,
            Versions=[group.version],
        )
        versions = found.get("LaunchTemplateVersions", [])
        specifications = versions[0].get("LaunchTemplateData", {}).get("TagSpecifications", []) if versions else []
        tags = {
            tag["Key"]: tag["Value"]
            for specification in specifications
            if specification.get("ResourceType") == "instance"
            for tag in specification.get("Tags", [])
        }
        if tags.get(POOL_TAG) != self.pool or not tags.get(RELEASE_PARAMETER_TAG):
            raise Refused(Cause.TEMPLATE_UNTAGGED)
        return {POOL_TAG: tags[POOL_TAG], RELEASE_PARAMETER_TAG: tags[RELEASE_PARAMETER_TAG]}

    def start(self, group: Group, tags: dict[str, str]) -> str:
        """Start the canary, in the first of the group's subnets with capacity of the pool's first
        type. Its ID."""
        from botocore.exceptions import BotoCoreError, ClientError

        tagged = [{"Key": key, "Value": value} for key, value in {**tags, JOB_TAG: JOB_TAG_VALUE}.items()]
        for subnet in group.subnets:
            try:
                started = self.clients.ec2.run_instances(
                    LaunchTemplate={"LaunchTemplateId": group.template_id, "Version": group.version},
                    ImageId=self.image,
                    InstanceType=group.instance_types[0],
                    SubnetId=subnet,
                    MinCount=1,
                    MaxCount=1,
                    TagSpecifications=[{"ResourceType": "instance", "Tags": tagged}],
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in NO_CAPACITY:
                    continue
                raise Refused(Cause.NOT_STARTED) from None
            except BotoCoreError:
                raise Refused(Cause.NOT_STARTED) from None
            (instance,) = started["Instances"]
            self.said.append("canary started")
            return instance["InstanceId"]
        raise Refused(Cause.NOT_STARTED)

    def watch(self, instance: str) -> tuple[bool, str | None]:
        """Whether the canary's line says its engine served, and the stage it failed at. Raises `Refused` when the instance
        stops before it writes a line, or when the time limit passes first."""
        from botocore.exceptions import BotoCoreError, ClientError

        end = self.clock() + self.timeout
        while True:
            try:
                described = self.clients.ec2.describe_instances(InstanceIds=[instance])
                states = [
                    found.get("State", {}).get("Name")
                    for reservation in described.get("Reservations", [])
                    for found in reservation.get("Instances", [])
                ]
                output = self.clients.ec2.get_console_output(InstanceId=instance, Latest=True).get("Output")
            except (BotoCoreError, ClientError):
                # A read that fails is tried again until the time limit: the instance may be new.
                states, output = [], None
            outcome = line_of(output)
            if outcome is not None:
                return outcome
            if any(state in STOPPED for state in states):
                raise Refused(Cause.CANARY_STOPPED)
            if self.clock() >= end:
                raise Refused(Cause.TIMED_OUT)
            self.sleep(self.poll)

    def terminate(self, instance: str) -> None:
        self.call(Cause.NOT_TERMINATED, self.clients.ec2.terminate_instances, InstanceIds=[instance])
        self.said.append("canary terminated")

    def write(self) -> None:
        self.call(
            Cause.NOT_WRITTEN,
            self.clients.ssm.put_parameter,
            Name=f"{IMAGE_PARAMETER_PREFIX}{self.pool}",
            Value=self.image,
            Type="String",
            Overwrite=True,
        )
        self.said.append(f"{IMAGE_PARAMETER_PREFIX}{self.pool} is {self.image}")

    def start_pipeline(self) -> None:
        self.call(Cause.PIPELINE_NOT_STARTED, self.clients.codepipeline.start_pipeline_execution, name=self.pipeline)
        self.said.append("pipeline started")


def family_of(instance_type: str) -> str:
    """An instance type's family, in lower case: `g7e` for `g7e.2xlarge`."""
    return instance_type.partition(".")[0].lower()


def line_of(output: str | None) -> tuple[bool, str | None] | None:
    """The canary's line in its console output: whether it served, and the stage it failed at. None
    while there is no line. The last line counts. EC2 answers in base64, and boto3 decodes it
    before it returns (botocore's `decode_console_output`), so the text arrives decoded."""
    if not output:
        return None
    found = LINE.findall(output)
    if not found:
        return None
    word, stage = found[-1]
    return word == "served", stage or None


def arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=NAME, description="Promote a machine image after a canary start on it.")
    parser.add_argument("--pool", required=True, help="the GPU pool, by its name")
    parser.add_argument("--image", required=True, help="the candidate image's ID")
    parser.add_argument("--families", required=True, help="the instance families the candidate lists, comma-separated")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS, help="seconds to wait for the canary's line")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    clients: Clients | None = None,
    environ: dict[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Run the job. 0 once the image is written and the pipeline started, 1 otherwise."""
    given = arguments(sys.argv[1:] if argv is None else argv)
    environ = dict(os.environ) if environ is None else environ
    pipeline = environ.get(PIPELINE_VARIABLE, "")
    if not pipeline:
        print(f"{NAME}: {PIPELINE_VARIABLE} names no pipeline", file=sys.stderr)
        return 1
    if clients is None:
        import boto3

        clients = Clients(*(boto3.client(name) for name in ("autoscaling", "ec2", "ssm", "codepipeline")))
    promotion = Promotion(
        pool=given.pool,
        image=given.image,
        families=given.families.split(","),
        pipeline=pipeline,
        clients=clients,
        timeout=given.timeout,
        clock=clock,
        sleep=sleep,
    )
    print(f"{NAME}: pool {given.pool}, candidate {given.image}")
    try:
        promotion.run()
    except Refused as refused:
        for said in promotion.said:
            print(f"{NAME}: {said}")
        detail = f" ({refused.detail})" if refused.detail else ""
        print(f"{NAME}: not promoted: {refused.cause.value}{detail}", file=sys.stderr)
        return 1
    for said in promotion.said:
        print(f"{NAME}: {said}")
    print(f"{NAME}: promoted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
