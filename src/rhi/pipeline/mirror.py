"""The private mirror of the images the pipeline's builds pull, and the step that fills it (system
design, section 8.2).

The builds pull five images from Docker Hub, each pinned by its digest: the emulator and BuildKit
of `IMAGE_BUILDER` in `rhi.pipeline.stack`, on every run of an image project, and the bases of the
services' FROM lines, when an image is rebuilt. Docker Hub limits anonymous pulls per address, and
CodeBuild's addresses are shared, so the account keeps its own copy of each.

**The repositories.** `MirrorRepositories` creates one private ECR repository for each image,
`rhi-mirror/` followed by the image's path on Docker Hub, where an official image's path starts
with `library/` (`REPOSITORIES`). BuildKit asks a mirror for an image at its Docker Hub path under
the mirror's own path, so the Dockerfiles keep their FROM lines. Each repository is retained when
the stack is deleted: it holds copies only, but a deletion should not empty what the builds read.
No pull-through cache rule is created. Each repository's policy lets the bootstrap's
image-publishing role of the deploying account pull its images (`IMAGE_PUBLISHING_ROLE`), and
nothing more: `cdk-assets` logs in with that role before it builds, so BuildKit's pulls of the base
images carry its login, and that role's own policy reaches only the bootstrap's asset repository.

**The asset step** pulls from the mirror alone (`IMAGE_BUILDER` of `rhi.pipeline.stack`). The role
CDK Pipelines shares among the projects that publish images may get an ECR authorization token and
pull from the emulator's and BuildKit's repositories (`MirrorRepositories.grant_the_image_builder`):
the Docker daemon pulls those two with it, before `cdk-assets` runs.

**The step**, named `ACTION`, runs in the stage Build, before the synthesis, which depends on it
(`rhi.pipeline.stack`): it is the first action of every execution, before the Assets stage that
pulls the images. It runs `scripts/pipeline/mirror_images.py` on the source, in a locked
environment, as the hold runs its script. The stage Build runs before the self-update, so the
step's project is the one the previous deployment defined, but it reads the pins from the commit
being deployed: a merge that bumps a pin has the new digest copied before its own Assets stage.
The script copies with crane of go-containerregistry, pinned by its version and checked against
its release's checksum before it is unpacked (`CRANE_*`), and finds it at the path
`RHI_MIRROR_CRANE` names. It reads Docker Hub, anonymously, only for a digest the mirror lacks:
once for each new pin, and never on an ordinary run.

**The role** may get an ECR authorization token, which AWS grants only on every resource, to log
crane in to the registry, and pull, push and describe images in the repositories whose names start
with `rhi-mirror/`, in the deploying account and region (`MIRROR_ACTIONS`). The prefix, not the
five repositories, so that a repository a later change adds is in reach before the role is
redeployed, and a missing one reads as missing rather than as denied. It may create no repository.
Beside that it holds what CDK Pipelines gives every project of the pipeline: its own logs, its own
test reports and a read of its input in the artifact bucket. `tests/synth/test_pipeline_mirror.py`
holds it to that.

The step is a step: CDK Pipelines creates its project and role inside the pipeline construct of
`rhi.pipeline.stack`, whose acknowledgement file covers them.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import pipelines
from constructs import Construct

from rhi.pipeline.synth import UV_VERSION

# The name of the step's action, and the script it runs.
ACTION = "Mirror"
SCRIPT = "scripts/pipeline/mirror_images.py"
# Every repository of the mirror is named under this path.
PREFIX = "rhi-mirror"
# One repository for each image the builds pull, by its path on Docker Hub.
REPOSITORIES = (
    f"{PREFIX}/library/python",
    f"{PREFIX}/grafana/grafana-oss",
    f"{PREFIX}/vllm/vllm-openai",
    f"{PREFIX}/tonistiigi/binfmt",
    f"{PREFIX}/moby/buildkit",
)

# crane, of go-containerregistry, for the x86_64 build host, checked against its release's
# checksum. The release had been out a week when it was pinned (sections 9 and 17).
CRANE_VERSION = "0.22.1"
CRANE_ARCHIVE = "go-containerregistry_Linux_x86_64.tar.gz"
CRANE_SHA256 = "0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0"
CRANE_URL = f"https://github.com/google/go-containerregistry/releases/download/v{CRANE_VERSION}/{CRANE_ARCHIVE}"
# Where the step unpacks it, outside the source, and the variable that names it to the script.
CRANE_FOLDER = "/tmp/rhi-crane"
CRANE = f"{CRANE_FOLDER}/crane"
CRANE_VARIABLE = "RHI_MIRROR_CRANE"

INSTALL_COMMANDS = [
    f"python3 -m pip install uv=={UV_VERSION}",
    f'curl -fsSL --create-dirs -o "{CRANE_FOLDER}/{CRANE_ARCHIVE}" {CRANE_URL}',
    f'echo "{CRANE_SHA256}  {CRANE_FOLDER}/{CRANE_ARCHIVE}" | sha256sum -c -',
    f'tar -xzf "{CRANE_FOLDER}/{CRANE_ARCHIVE}" -C "{CRANE_FOLDER}" crane',
]
COMMANDS = ["uv sync --locked", f"uv run --locked python {SCRIPT}"]

# What the role may do in the mirror's repositories: pull, push and describe images.
MIRROR_ACTIONS = [
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchCheckLayerAvailability",
    "ecr:InitiateLayerUpload",
    "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload",
    "ecr:PutImage",
    "ecr:DescribeImages",
]


# What a reader of the mirror may do in a repository: pull images.
PULL_ACTIONS = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
# The bootstrap's image-publishing role of the deploying account and region, by its name, with
# the default qualifier: `cdk-assets` logs in with it before it builds, and BuildKit's pulls of the
# base images carry that login.
IMAGE_PUBLISHING_ROLE = cdk.DefaultStackSynthesizer.DEFAULT_IMAGE_ASSET_PUBLISHING_ROLE_ARN.replace(
    "${Qualifier}", cdk.DefaultStackSynthesizer.DEFAULT_QUALIFIER
)
# The two repositories the Docker daemon of an image project pulls from, with the project's role.
IMAGE_BUILDER_REPOSITORIES = (f"{PREFIX}/tonistiigi/binfmt", f"{PREFIX}/moby/buildkit")


class MirrorRepositories(Construct):
    """One private ECR repository for each name of `REPOSITORIES`, retained when the stack is
    deleted, whose policy lets the bootstrap's image-publishing role pull its images, and nothing
    else: within one account, a repository's policy is enough, and the bootstrap's own policies
    stay as they are."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.repositories = {
            name: ecr.Repository(
                self,
                name.removeprefix(f"{PREFIX}/").replace("/", "-").title().replace("-", ""),
                repository_name=name,
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )
            for name in REPOSITORIES
        }
        publishing_role = iam.ArnPrincipal(cdk.Fn.sub(IMAGE_PUBLISHING_ROLE))
        for repository in self.repositories.values():
            repository.add_to_resource_policy(
                iam.PolicyStatement(
                    actions=PULL_ACTIONS,
                    # ty reads the CDK's generated principal as unlike its protocol: jsii renames a parameter.
                    principals=[publishing_role],  # ty: ignore[invalid-argument-type]
                )
            )

    def grant_the_image_builder(self, role: iam.Role) -> None:
        """Lets `role`, that of the projects that publish images, get an ECR authorization token
        and pull the emulator and BuildKit from their repositories: the Docker daemon pulls those
        two with it, before `cdk-assets` logs in."""
        # AWS grants the token only on every resource.
        role.add_to_principal_policy(iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=PULL_ACTIONS,
                resources=[self.repositories[name].repository_arn for name in IMAGE_BUILDER_REPOSITORIES],
            )
        )


def mirror_step(scope: cdk.Stack, source: pipelines.IFileSetProducer) -> pipelines.CodeBuildStep:
    """The step of the pipeline of `scope` that copies into the mirror, from `source`, each pinned
    image whose digest the mirror lacks."""
    repositories = scope.format_arn(service="ecr", resource="repository", resource_name=f"{PREFIX}/*")
    return pipelines.CodeBuildStep(
        ACTION,
        input=source,
        install_commands=INSTALL_COMMANDS,
        commands=COMMANDS,
        env={CRANE_VARIABLE: CRANE},
        role_policy_statements=[
            # The token crane logs in to the registry with: AWS grants it only on every resource.
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),
            # The mirror's repositories, by their prefix, in the deploying account and region.
            iam.PolicyStatement(actions=MIRROR_ACTIONS, resources=[repositories]),
        ],
    )
