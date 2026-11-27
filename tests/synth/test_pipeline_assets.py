"""The asset step builds an image for linux/arm64 as well as one for linux/amd64 in the same run
(system design, sections 4.2, 4.3 and 8.2, step 4).

The app's stage holds no image yet, so these tests give the pipeline stack a stage of their own:
one image for each platform and one file. CodeBuild's build hosts are x86_64, so a project that
publishes an image installs an emulator for arm64 and a builder that can target either platform,
each pinned by a digest or by a version and its checksum, and its publishing builds with them.

It pulls nothing from Docker Hub: it logs in to the deploying account's registry, whose host it
reads on the build host, runs the emulator and the builder from the mirror there, and gives the
builder that mirror as its one mirror for Docker Hub.
"""

from __future__ import annotations

import copy
import json
import re
import runpy
import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
ASSET_STAGE = "Assets"
PUBLISHES = re.compile(r"cdk-assets .* publish .*")
# What CDK Pipelines installs in every asset project, file or image: its publisher. The asset step
# installs the rest to build images.
INSTALLS_THE_PUBLISHER = re.compile(r"npm install -g cdk-assets@\S+")

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")
EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")
CHECKSUM = re.compile(r"\b[0-9a-f]{64}\b")

# The deploying account and region as a stand-in `aws` and CodeBuild give them on the build host,
# and the registry's host they make.
ACCOUNT, REGION = "123456789012", "eu-west-1"
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
# The mirror, as the build specification writes it, the registry's host read in the shell.
MIRROR = "$RHI_REGISTRY/rhi-mirror"
DOCKER_HUB = {"docker.io", "index.docker.io", "registry-1.docker.io"}


def synthesize(folder: Path) -> dict[str, Any]:
    """The pipeline stack's template, for a stage with an arm64 image, an amd64 image and a file.
    A caller asks for `app_module` first: it imports the CDK the way the sandbox needs
    (`conftest.py`)."""
    import aws_cdk as cdk
    from aws_cdk import aws_ecr_assets as ecr_assets
    from aws_cdk import aws_s3_assets as s3_assets

    from rhi.pipeline.stack import PipelineStack

    (folder / "image").mkdir(parents=True)
    (folder / "image" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (folder / "file.txt").write_text("A file.\n", encoding="utf-8")
    context = {"aws:cdk:bundling-stacks": []}
    app = cdk.App(outdir=str(folder / "cdk.out"), context=context, analytics_reporting=False)
    stage = cdk.Stage(app, "Rhi")
    stack = cdk.Stack(stage, "Assets")
    for name, platform in (("Arm64", ecr_assets.Platform.LINUX_ARM64), ("Amd64", ecr_assets.Platform.LINUX_AMD64)):
        ecr_assets.DockerImageAsset(stack, name, directory=str(folder / "image"), platform=platform)
    s3_assets.Asset(stack, "File", path=str(folder / "file.txt"))
    PipelineStack(app, "RhiPipeline", config=config.load({"RHI_CONFIG": "example"}), stage=stage)
    outdir = Path(app.synth().directory)
    return json.loads((outdir / PIPELINE_TEMPLATE).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def template(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return synthesize(tmp_path_factory.mktemp("assets"))


def asset_projects(template: dict[str, Any]) -> list[dict[str, Any]]:
    """The projects the actions of the asset stage run."""
    (pipeline,) = [r["Properties"] for r in template["Resources"].values() if r["Type"] == "AWS::CodePipeline::Pipeline"]
    (stage,) = [stage for stage in pipeline["Stages"] if stage["Name"] == ASSET_STAGE]
    return [template["Resources"][a["Configuration"]["ProjectName"]["Ref"]]["Properties"] for a in stage["Actions"]]


def image_projects(template: dict[str, Any]) -> list[dict[str, Any]]:
    """The asset projects that run Docker: CDK Pipelines makes privileged those that publish images."""
    return [project for project in asset_projects(template) if project["Environment"]["PrivilegedMode"]]


def build_spec(project: dict[str, Any]) -> dict[str, Any]:
    return json.loads(project["Source"]["BuildSpec"])


def installed(template: dict[str, Any]) -> list[str]:
    """What the image projects install beyond CDK Pipelines' publisher."""
    return [
        command
        for project in image_projects(template)
        for command in build_spec(project)["phases"]["install"]["commands"]
        if not INSTALLS_THE_PUBLISHER.fullmatch(command)
    ]


def commands_of(project: dict[str, Any]) -> list[str]:
    """Every command a project runs: those of each phase, and those of each phase's `finally`."""
    phases = build_spec(project)["phases"].values()
    return [command for phase in phases for key in ("commands", "finally") for command in phase.get(key, [])]


# The Docker commands that start, create or pull a container image, each in its short form and
# under its group: `docker run` is `docker container run`, `docker pull` is `docker image pull`.
TAKES_AN_IMAGE = [
    *(["docker", verb] for verb in ("run", "create", "pull")),
    *(["docker", "container", verb] for verb in ("run", "create")),
    ["docker", "image", "pull"],
]
# The Docker commands that name no image among their words: the login names a registry, and the
# creation of a builder names its image in an `image=` option.
NAMES_NO_IMAGE = [["docker", "login"], ["docker", "buildx", "create"]]


def words_of(command: str) -> list[str]:
    """A command's words as the shell reads them, its operators (`|`, `&&`, `;`, `>`, ...) apart."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def images_of(words: list[str]) -> list[str]:
    """The container images a command names: an `image=` option, as its own word or attached to
    its option (`--driver-opt=image=...`), and in each simple command of Docker's, what one of
    `TAKES_AN_IMAGE` starts, creates or pulls (options written as one word), and, in any other but
    those of `NAMES_NO_IMAGE` (a manifest's lookup, a tag, a push), each of its words: what this
    does not know how to read is taken to name an image."""
    values = [word.split("=", 1)[1] if word.startswith("-") and "=" in word else word for word in words]
    found = [part.split("=", 1)[1] for value in values for part in value.split(",") if part.startswith("image=")]
    simple: list[list[str]] = [[]]
    for word in words:
        if word and set(word) <= set("();<>|&"):
            simple.append([])
        else:
            simple[-1].append(word)
    for command in simple:
        if command[:1] != ["docker"] or any(command[: len(verb)] == verb for verb in NAMES_NO_IMAGE):
            continue
        verb = next((verb for verb in TAKES_AN_IMAGE if command[: len(verb)] == verb), ["docker"])
        arguments = [word for word in command[len(verb) :] if not word.startswith("-")]
        found += arguments[:1] if verb in TAKES_AN_IMAGE else arguments
    return found


def unpinned(commands: list[str]) -> list[str]:
    """The tools that install commands name without a version or a digest: an image without its
    digest, a download without an exact version or whose file no command checks against a
    checksum, a package without an exact version."""
    found = []
    for command in commands:
        words = words_of(command)
        found += [image for image in images_of(words) if not DIGEST.search(image)]
        for url in [word for word in words if word.startswith(("https://", "http://"))]:
            written = words[words.index("-o") + 1] if "-o" in words else url.rsplit("/", 1)[-1]
            checked = any("sha256sum -c" in other and written in other and CHECKSUM.search(other) for other in commands)
            if "latest" in url or not EXACT_VERSION.search(url) or not checked:
                found.append(url)
        if words[:2] in (["npm", "install"], ["pip", "install"]):
            packages = [word for word in words[2:] if not word.startswith("-")]
            found += [package for package in packages if not re.search(r"(@|==)\d+\.\d+\.\d+$", package)]
    return found


def on_docker_hub(image: str) -> bool:
    """Whether an image reference is one of Docker Hub's: its first part names no registry host,
    or names Docker Hub's."""
    first, _, rest = image.partition("/")
    names_a_host = bool(rest) and ("." in first or ":" in first or first == "localhost")
    return not names_a_host or first in DOCKER_HUB


def in_the_shell(commands: list[str], variables: dict[str, str], folder: Path) -> str:
    """What `commands`, then printing `$RHI_REGISTRY`, write, run in one shell as CodeBuild runs a
    build specification, with `variables` and a stand-in `aws` that names `ACCOUNT` as the caller's."""
    (folder / "aws").write_text(f"#!/bin/sh\necho {ACCOUNT}\n", encoding="utf-8")
    (folder / "aws").chmod(0o755)
    environment = {"PATH": f"{folder}:/usr/bin:/bin", "AWS_REGION": REGION, **variables}
    script = "\n".join([*commands, 'printf %s "$RHI_REGISTRY"'])
    return subprocess.run(
        ["/bin/sh", "-ec", script], env=environment, capture_output=True, text=True, check=True
    ).stdout


def test_each_image_project_installs_the_emulator_and_the_builder_from_the_mirror_and_builds_with_them(
    template: dict[str, Any], tmp_path: Path
) -> None:
    from rhi.pipeline import stack

    projects = image_projects(template)
    assert len(projects) == 2
    for number, project in enumerate(projects):
        spec = build_spec(project)
        install, build = spec["phases"]["install"]["commands"], spec["phases"]["build"]["commands"]
        words = [words_of(command) for command in install]
        # The registry's host, read on the build host: the caller's account and CodeBuild's region.
        (finds,) = [at for at, w in enumerate(words) if w[:1] == ["export"] and w[1].startswith("RHI_REGISTRY=")]
        # The login to that registry, with the project's own role.
        (login,) = [at for at, w in enumerate(words) if w[:3] == ["aws", "ecr", "get-login-password"]]
        assert words[login][3:] == ["|", "docker", "login", "--username", "AWS", "--password-stdin", "$RHI_REGISTRY"]
        # The emulator: QEMU's arm64 handler, registered with the build host's kernel, from the
        # mirror at the digest the stack pins.
        (emulator,) = [at for at, w in enumerate(words) if images_of(w) and w[-2:] == ["--install", "arm64"]]
        assert images_of(words[emulator]) == [f"{MIRROR}/{stack.EMULATOR_IMAGE}"]
        # The builder's configuration: one mirror, for Docker Hub, the mirror's path in the registry.
        (configures,) = [at for at, w in enumerate(words) if w[:1] == ["printf"] and "mirrors" in w[1]]
        written = words[configures][words[configures].index(">") + 1]
        # Written once the registry's host is known, and run here in the order CodeBuild runs them.
        assert finds < configures
        folder = tmp_path / str(number)
        folder.mkdir(exist_ok=True)
        to_folder = {configures: install[configures].replace(written, str(folder / "buildkitd.toml"))}
        commands = [to_folder.get(at, install[at]) for at in sorted((finds, configures))]
        assert in_the_shell(commands, spec["env"]["variables"], folder) == REGISTRY
        configuration = tomllib.loads((folder / "buildkitd.toml").read_text(encoding="utf-8"))
        assert configuration == {"registry": {"docker.io": {"mirrors": [f"{REGISTRY}/rhi-mirror"]}}}
        # The builder: BuildKit in a container, from the mirror at the digest the stack pins, with
        # that configuration, loading what it builds into Docker, and selected.
        (create,) = [at for at, w in enumerate(words) if w[:3] == ["docker", "buildx", "create"]]
        builder = words[create]
        assert images_of(builder) == [f"{MIRROR}/{stack.BUILDKIT_IMAGE}"]
        assert builder[builder.index("--buildkitd-config") + 1] == written
        assert {"--driver", "docker-container", "--use"} <= set(builder)
        assert "default-load=true" in builder
        assert spec["env"]["variables"]["BUILDX_BUILDER"] == builder[builder.index("--name") + 1]
        assert finds < login < emulator < create and configures < create
        # BuildKit names a base image by its Docker Hub reference in its progress, even one it
        # pulls from the mirror: the builds keep it quiet.
        assert spec["env"]["variables"]["BUILDKIT_PROGRESS"] == "quiet"
        # Installed before the build phase, which publishes, and so builds the image.
        assert build and all(PUBLISHES.fullmatch(command) for command in build)
        assert not any(PUBLISHES.fullmatch(command) for command in install)


def test_no_command_of_an_image_project_names_an_image_on_docker_hub(template: dict[str, Any]) -> None:
    commands = [command for project in image_projects(template) for command in commands_of(project)]
    images = [image.replace("$RHI_REGISTRY", REGISTRY) for c in commands for image in images_of(words_of(c))]
    assert len(images) == 4
    assert not [image for image in images if on_docker_hub(image)]
    # CodeBuild writes each command it runs to the build's log: none names Docker Hub's hosts.
    assert not [command for command in commands if any(host in command for host in DOCKER_HUB)]


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("tonistiigi/binfmt@sha256:" + "0" * 64, True),
        ("python:3.13-slim", True),
        ("docker.io/moby/buildkit@sha256:" + "0" * 64, True),
        ("registry-1.docker.io/library/python", True),
        (f"{REGISTRY}/rhi-mirror/moby/buildkit@sha256:" + "0" * 64, False),
    ],
)
def test_an_image_on_docker_hub_is_found(image: str, expected: bool) -> None:
    assert on_docker_hub(image) is expected


@pytest.mark.parametrize(
    ("section", "command"),
    [
        pytest.param("finally", "docker pull docker.io/library/python", id="a pull in a finally block"),
        pytest.param("commands", "docker image pull python", id="a pull under the image group"),
        pytest.param("commands", "docker container run --rm python", id="a run under the container group"),
        pytest.param("commands", "docker manifest inspect python:3.13-slim", id="a manifest's lookup"),
        pytest.param("commands", "true && docker pull python", id="a pull after an operator"),
        pytest.param(
            "commands",
            "docker buildx create --name second --driver docker-container --driver-opt=image=moby/buildkit --bootstrap",
            id="a builder's image attached to its option",
        ),
    ],
)
def test_a_command_that_names_an_image_on_docker_hub_is_found(
    template: dict[str, Any], section: str, command: str
) -> None:
    changed = copy.deepcopy(template)
    project = image_projects(changed)[0]
    spec = build_spec(project)
    spec["phases"]["install"].setdefault(section, []).append(command)
    project["Source"]["BuildSpec"] = json.dumps(spec)
    with pytest.raises(AssertionError):
        test_no_command_of_an_image_project_names_an_image_on_docker_hub(changed)


def configured_first(template: dict[str, Any]) -> dict[str, Any]:
    """`template` with the builder's configuration of its first image project written before the
    registry's host is found, while the build's variables already name another registry."""
    changed = copy.deepcopy(template)
    project = image_projects(changed)[0]
    spec = build_spec(project)
    install = spec["phases"]["install"]["commands"]
    (configures,) = [command for command in install if command.startswith("printf ")]
    install.remove(configures)
    install.insert(0, configures)
    spec["env"]["variables"]["RHI_REGISTRY"] = "example.com"
    project["Source"]["BuildSpec"] = json.dumps(spec)
    return changed


def test_a_configuration_written_before_the_registry_is_known_is_found(
    template: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(AssertionError):
        test_each_image_project_installs_the_emulator_and_the_builder_from_the_mirror_and_builds_with_them(
            configured_first(template), tmp_path
        )


def test_the_pipeline_policy_leaves_out_an_image_project_only_while_its_mirror_is_for_docker_hub(
    template: dict[str, Any],
) -> None:
    """The policy of `tests/policy/test_pipeline.py` leaves out each image project as the stack
    writes it, and lists one whose builder's configuration mirrors another registry, or is
    written before the project finds the deploying account's registry."""
    policy = runpy.run_path(str(Path(__file__).resolve().parents[1] / "policy" / "test_pipeline.py"))
    (pipeline,) = [r["Properties"] for r in template["Resources"].values() if r["Type"] == "AWS::CodePipeline::Pipeline"]
    (stage,) = [stage for stage in pipeline["Stages"] if stage["Name"] == ASSET_STAGE]
    for action in stage["Actions"]:
        changed = copy.deepcopy(template)
        project = changed["Resources"][action["Configuration"]["ProjectName"]["Ref"]]["Properties"]
        assert policy["publishes_assets"](action, changed)
        if not project["Environment"]["PrivilegedMode"]:
            continue
        spec = build_spec(project)
        spec["env"]["variables"]["RHI_MIRRORED_REGISTRY"] = "example.com"
        project["Source"]["BuildSpec"] = json.dumps(spec)
        assert not policy["publishes_assets"](action, changed)
    changed = configured_first(template)
    reordered = [a for a in stage["Actions"] if policy["build_spec_of"](a, changed) != policy["build_spec_of"](a, template)]
    assert len(reordered) == 1
    assert not policy["publishes_assets"](reordered[0], changed)


def test_the_file_project_is_left_as_cdk_pipelines_writes_it(template: dict[str, Any]) -> None:
    (project,) = [p for p in asset_projects(template) if not p["Environment"]["PrivilegedMode"]]
    spec = build_spec(project)
    assert "env" not in spec
    assert all(INSTALLS_THE_PUBLISHER.fullmatch(command) for command in spec["phases"]["install"]["commands"])


def test_tools_are_pinned(template: dict[str, Any]) -> None:
    commands = installed(template)
    assert commands
    assert unpinned(commands) == []


PINNED_BUILDX = [
    "curl -fsSL -o docker-buildx https://github.com/docker/buildx/releases/download/v0.38.2/buildx-v0.38.2.linux-amd64",
    f'echo "{"0" * 64}  docker-buildx" | sha256sum -c -',
]


@pytest.mark.parametrize(
    "commands",
    [
        pytest.param(["docker run --privileged --rm tonistiigi/binfmt:latest --install arm64"], id="an image by tag"),
        pytest.param(["docker run --privileged --rm tonistiigi/binfmt --install arm64"], id="an image by name"),
        pytest.param(
            ["docker buildx create --driver docker-container --driver-opt image=moby/buildkit --use"],
            id="an image option by name",
        ),
        pytest.param(
            ["curl -fsSL -o docker-buildx https://github.com/docker/buildx/releases/latest/download/buildx.linux-amd64"]
            + PINNED_BUILDX[1:],
            id="the latest download",
        ),
        pytest.param(PINNED_BUILDX[:1], id="a download no checksum checks"),
        pytest.param(["npm install -g some-tool"], id="a package by name"),
    ],
)
def test_a_tool_without_a_version_or_a_digest_is_refused(commands: list[str]) -> None:
    assert unpinned(PINNED_BUILDX) == []
    assert unpinned(commands) != []


def test_tools_are_pinned_fails_on_the_stack_with_an_unpinned_tool(
    app_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rhi.pipeline import stack

    by_tag = [re.sub(r"(tonistiigi/binfmt):[^\s\"]+", r"\1:latest", command) for command in stack.IMAGE_BUILDER]
    assert by_tag != stack.IMAGE_BUILDER
    monkeypatch.setattr(stack, "IMAGE_BUILDER", by_tag)
    assert unpinned(installed(synthesize(tmp_path))) == [f"{MIRROR}/tonistiigi/binfmt:latest"] * 2


def test_the_image_projects_role_reaches_ecr_only_through_the_mirror_s_grant(
    app_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CDK Pipelines gives the image projects' role no ECR permission of its own: the token and
    the pulls of the emulator and BuildKit are what the mirror's grant adds."""
    from rhi.pipeline.mirror import MirrorRepositories

    monkeypatch.setattr(MirrorRepositories, "grant_the_image_builder", lambda *_: None)
    template = synthesize(tmp_path)
    (role,) = {project["ServiceRole"]["Fn::GetAtt"][0] for project in image_projects(template)}
    actions = [
        action
        for policy in template["Resources"].values()
        if policy["Type"] == "AWS::IAM::Policy" and {"Ref": role} in policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    ]
    assert actions
    assert not [action for action in actions if action.startswith("ecr:")]
