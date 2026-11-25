"""The asset step builds an image for linux/arm64 as well as one for linux/amd64 in the same run
(system design, sections 4.2, 4.3 and 8.2, step 4).

The app's stage holds no image yet, so these tests give the pipeline stack a stage of their own:
one image for each platform and one file. CodeBuild's build hosts are x86_64, so a project that
publishes an image registers an emulator for arm64, from the binfmt project's release on GitHub,
and builds with the Docker daemon's own builder under it. What it installs is pinned by a version
and its checksum, and nothing it runs is an image on Docker Hub, which limits anonymous pulls by
address: the build hosts share theirs.
"""

from __future__ import annotations

import json
import re
import shlex
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

# The two files of one release of the binfmt project, for the x86_64 build host: the tool, and the
# emulators of the QEMU version the release is named after.
EMULATOR_RELEASE = r"https://github\.com/tonistiigi/binfmt/releases/download/deploy/v(?P<qemu>\d+\.\d+\.\d+)-\d+"
EMULATOR_TOOL = re.compile(rf"(?P<release>{EMULATOR_RELEASE})/binfmt_linux-amd64\.tar\.gz")
EMULATORS = re.compile(rf"(?P<release>{EMULATOR_RELEASE})/qemu_v(?P=qemu)_linux-amd64\.tar\.gz")

# The names Docker Hub's registry answers to. An image reference with no host names it too.
DOCKER_HUB = ("docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com")
NAMES_DOCKER_HUB = re.compile(rf"(?<![\w.-])({'|'.join(map(re.escape, DOCKER_HUB))})(?![\w.-])", re.IGNORECASE)
# The Docker commands whose first operand is an image the daemon runs or pulls, or reads from its
# registry, each written alone or under its management command.
TAKES_AN_IMAGE = [
    ["run"],
    ["create"],
    ["pull"],
    ["container", "run"],
    ["container", "create"],
    ["image", "pull"],
    ["manifest", "inspect"],
    ["buildx", "imagetools", "inspect"],
]


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


def images_of(words: list[str]) -> list[str]:
    """The container images a command names: the first operand of each Docker command that takes
    an image (options written as one word), wherever in the line `docker` is called, and each
    `image=` option."""
    found = [part.split("=", 1)[1] for word in words for part in word.split(",") if part.startswith("image=")]
    for index in [index for index, word in enumerate(words) if word.rsplit("/", 1)[-1] == "docker"]:
        operands = [word for word in words[index + 1 :] if not word.startswith("-")]
        for command in TAKES_AN_IMAGE:
            if operands[: len(command)] == command and len(operands) > len(command):
                found.append(operands[len(command)])
    return found


def on_docker_hub(commands: list[str]) -> list[str]:
    """What commands run, pull or name of Docker Hub: each image whose reference names one of
    Docker Hub's hosts or no host, and any other word that names one of Docker Hub's hosts. A
    reference names a host when its first path component, of several, has a `.` or a `:` or is
    `localhost`."""
    found = []
    for command in commands:
        words = shlex.split(command)
        images = images_of(words)
        for image in images:
            first, slash, _ = image.partition("/")
            hosted = slash and ("." in first or ":" in first or first == "localhost")
            if not hosted or first.lower() in DOCKER_HUB:
                found.append(image)
        named = [word for word in words if NAMES_DOCKER_HUB.search(word)]
        found += [word for word in named if not any(image in word for image in images)]
    return found


def selects_a_builder(commands: list[str]) -> list[str]:
    """The commands that build with another builder than the Docker daemon's own, buildx's default:
    one that creates a builder or selects one, by `docker buildx create` or `use`, by `--builder`
    or by `BUILDX_BUILDER`."""
    found = []
    for command in commands:
        words = shlex.split(command)
        calls = [index for index, word in enumerate(words) if word.rsplit("/", 1)[-1] == "docker"]
        buildx = [words[index + 1 : index + 3] for index in calls]
        if (
            any(following in (["buildx", "create"], ["buildx", "use"]) for following in buildx)
            or any(word == "--builder" or word.startswith("--builder=") for word in words)
            or "BUILDX_BUILDER" in command
        ):
            found.append(command)
    return found


def unpinned(commands: list[str]) -> list[str]:
    """The tools that install commands name without a version or a digest: an image without its
    digest, a download without an exact version or whose file no command checks against a
    checksum, a package without an exact version."""
    found = []
    for command in commands:
        words = shlex.split(command)
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


def every_command(project: dict[str, Any]) -> list[str]:
    """The commands of every phase of a project, those of its `finally` blocks among them."""
    phases = build_spec(project)["phases"].values()
    return [command for phase in phases for key in ("commands", "finally") for command in phase.get(key, [])]


def test_each_image_project_registers_the_emulator_from_the_release(template: dict[str, Any]) -> None:
    projects = image_projects(template)
    assert len(projects) == 2
    for project in projects:
        spec = build_spec(project)
        install, build = spec["phases"]["install"]["commands"], spec["phases"]["build"]["commands"]
        words = [shlex.split(command) for command in install]
        # The tool and the emulators: one file each, of the same release, each written where a
        # checksum is checked (`test_tools_are_pinned`) and extracted from there, after the check.
        folders = []
        for file in (EMULATOR_TOOL, EMULATORS):
            ((download, url),) = [(i, url) for i, w in enumerate(words) if (url := file.fullmatch(w[-1]))]
            written = words[download][words[download].index("-o") + 1]
            (check,) = [i for i, command in enumerate(install) if "sha256sum -c" in command and written in command]
            (extract,) = [i for i, w in enumerate(words) if w[0] == "tar" and written in w]
            assert download < check < extract
            folders.append((url["release"], words[extract][words[extract].index("-C") + 1], extract))
        (tool_release, tool_folder, tool_extracted), (qemu_release, qemu_folder, qemu_extracted) = folders
        assert tool_release == qemu_release
        # The registration: the extracted tool, told where the extracted emulators are, for arm64.
        ((register, registration),) = [(i, w) for i, w in enumerate(words) if w[-2:] == ["--install", "arm64"]]
        assert registration[:-2] == [f"QEMU_BINARY_PATH={qemu_folder}", f"{tool_folder}/binfmt"]
        assert register > max(tool_extracted, qemu_extracted)
        # Registered before the build phase, which publishes, and so builds the image.
        assert build and all(PUBLISHES.fullmatch(command) for command in build)
        assert not any(PUBLISHES.fullmatch(command) for command in install)


def test_each_image_project_builds_with_the_default_builder(template: dict[str, Any]) -> None:
    projects = image_projects(template)
    assert len(projects) == 2
    for project in projects:
        variables = build_spec(project)["env"]["variables"]
        # BuildKit, through the pinned plugin, on the builder no command and no variable replaces.
        assert variables["DOCKER_BUILDKIT"] == "1"
        assert "BUILDX_BUILDER" not in variables
        assert any("docker-buildx" in command for command in build_spec(project)["phases"]["install"]["commands"])
        assert selects_a_builder(every_command(project)) == []


def test_no_command_of_an_image_project_runs_pulls_or_names_an_image_on_docker_hub(template: dict[str, Any]) -> None:
    projects = image_projects(template)
    assert len(projects) == 2
    for project in projects:
        commands = every_command(project)
        assert commands
        assert on_docker_hub(commands) == []


# The emulator image and the BuildKit builder as the stack had them, each pinned by digest.
BINFMT = "tonistiigi/binfmt:qemu-v10.2.3@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
BUILDKIT = "moby/buildkit:v0.34.2@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe"
RAN_THE_EMULATOR_IMAGE = f"docker run --privileged --rm {BINFMT} --install arm64"
CREATED_THE_BUILDER = (
    f"docker buildx create --name rhi-assets --driver docker-container --driver-opt image={BUILDKIT}"
    " --driver-opt default-load=true --bootstrap --use"
)
SHA = "@sha256:" + "0" * 64


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(RAN_THE_EMULATOR_IMAGE, [BINFMT], id="the docker run of tonistiigi/binfmt, by digest"),
        pytest.param(CREATED_THE_BUILDER, [BUILDKIT], id="the docker buildx create with image=moby/buildkit, by digest"),
        pytest.param(f"docker pull python{SHA}", [f"python{SHA}"], id="a pull with no host"),
        pytest.param(
            f"docker image pull docker.io/library/python{SHA}",
            [f"docker.io/library/python{SHA}"],
            id="a pull under its management command, from docker.io",
        ),
        pytest.param(
            f"true && docker container run --rm index.docker.io/library/busybox{SHA} true",
            [f"index.docker.io/library/busybox{SHA}"],
            id="a run under its management command, after another command",
        ),
        pytest.param(
            f"docker create --name=emulator tonistiigi/binfmt{SHA}", [f"tonistiigi/binfmt{SHA}"], id="a create"
        ),
        pytest.param(
            f"docker buildx imagetools inspect moby/buildkit{SHA}", [f"moby/buildkit{SHA}"], id="a read of a manifest"
        ),
        pytest.param(
            "curl -fsSL https://registry-1.docker.io/v2/library/python/manifests/3.13",
            ["https://registry-1.docker.io/v2/library/python/manifests/3.13"],
            id="a word that names a host of Docker Hub",
        ),
    ],
)
def test_an_image_on_docker_hub_is_refused(command: str, expected: list[str]) -> None:
    assert on_docker_hub([command]) == expected


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"docker pull public.ecr.aws/docker/library/python{SHA}", id="a pull from another registry"),
        pytest.param(f"docker run --rm localhost:5000/tool{SHA} --install arm64", id="a run from a local registry"),
        pytest.param("docker buildx version", id="a Docker command that takes no image"),
        pytest.param("curl -fsSL -o tool https://example.com/docker.io.tar.gz", id="a file named like the host"),
    ],
)
def test_what_names_no_image_on_docker_hub_is_accepted(command: str) -> None:
    assert on_docker_hub([command]) == []


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(CREATED_THE_BUILDER, id="the docker buildx create with image=moby/buildkit, by digest"),
        pytest.param("docker buildx use rhi-assets", id="a builder selected by name"),
        pytest.param("docker buildx build --builder=rhi-assets .", id="a builder given to one build"),
        pytest.param("export BUILDX_BUILDER=rhi-assets", id="a builder selected by the variable"),
    ],
)
def test_another_builder_than_the_default_is_refused(command: str) -> None:
    assert selects_a_builder(["docker buildx version", "docker build ."]) == []
    assert selects_a_builder([command]) == [command]


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

    release = re.compile(r"(tonistiigi/binfmt/releases)/download/deploy/[^/]+")
    latest = [release.sub(r"\1/latest/download", command) for command in stack.IMAGE_BUILDER]
    assert latest != stack.IMAGE_BUILDER
    monkeypatch.setattr(stack, "IMAGE_BUILDER", latest)
    found = unpinned(installed(synthesize(tmp_path)))
    assert len(found) == 4
    assert all("tonistiigi/binfmt/releases/latest/download/" in url for url in found)
