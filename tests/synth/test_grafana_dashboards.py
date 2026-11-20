"""The dashboards in Grafana's image (system design, sections 4.7 and 7.4): the image's build
context is assembled at synthesis, from the files of `services/grafana/` and the dashboards built
from the configuration that is synthesized. So the fleet dashboard of the image has one row for
each pool of that configuration, and the image's asset hash, which decides whether Grafana is
deployed again, follows the configuration's pools and nothing of the run.

Nothing here starts a container: the build context is read where synthesis stages it, in the cloud
assembly.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config
from rhi_common import pool_config
from rhi_dashboards import build

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
IMAGE_FOLDER = REPO / "services" / "grafana"
EXAMPLE = config.load({"RHI_CONFIG": "example"})
FLEET = "dashboards/fleet.json"


@dataclass(frozen=True)
class Image:
    """Grafana's image asset in a cloud assembly: its asset hash, and its staged build context."""

    hash: str
    context: Path

    def files(self) -> dict[str, bytes]:
        return files(self.context)

    def fleet_rows(self) -> list[str]:
        return fleet_rows(self.context)


def fleet_rows(context: Path) -> list[str]:
    """The titles of the rows of the fleet dashboard a build context holds, in their order."""
    fleet = json.loads((context / FLEET).read_text(encoding="utf-8"))
    return [panel["title"] for panel in fleet["panels"] if panel["type"] == "row"]


def files(folder: Path) -> dict[str, bytes]:
    """Every file below a folder, by its path under it."""
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def image(assembly: Path) -> Image:
    """The one image asset of the observability stack, which its task definition's container names."""
    (manifest,) = assembly.rglob("RhiObservability*.assets.json")
    ((asset_hash, asset),) = json.loads(manifest.read_text(encoding="utf-8"))["dockerImages"].items()
    template = json.loads(manifest.with_name(manifest.name.replace(".assets.", ".template.")).read_text())
    (task,) = [r for r in template["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    (container,) = task["Properties"]["ContainerDefinitions"]
    assert asset_hash in container["Image"]["Fn::Sub"]
    return Image(asset_hash, manifest.parent / asset["source"]["directory"])


def synthesize(configuration: config.Config, outdir: Path) -> Image:
    """The stage synthesized from a configuration into a directory of its own, bundling nothing.

    The CDK is imported here, once the session's app has started jsii without its package cache
    (the fixture `app_module`): every test that calls this reads the session's assembly first.
    """
    import aws_cdk as cdk

    from rhi.stage import EndpointStage

    context: dict[str, Any] = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"]
    app = cdk.App(outdir=str(outdir), context=context | {"aws:cdk:bundling-stacks": []}, analytics_reporting=False)
    EndpointStage(app, "Rhi", config=configuration)
    app.synth()
    return image(outdir)


def test_the_image_holds_its_folder_s_files_and_the_dashboards_of_the_configuration(cloud_assembly: Path) -> None:
    """The app of the session, synthesized from the example configuration as the pipeline does."""
    built = image(cloud_assembly)
    held = built.files()
    # Every file of the image's folder, as it is: the Dockerfile, the settings, the provisioning.
    in_the_folder = files(IMAGE_FOLDER)
    assert {"Dockerfile", "grafana.ini"} <= in_the_folder.keys()
    assert {path: held.get(path) for path in in_the_folder} == in_the_folder
    # And, in the folder the Dockerfile copies the dashboards from, what the build writes from
    # the configuration: every dashboard of the package, and nothing else.
    dashboards = {f"dashboards/{one.uid}.json": one.text().encode() for one in build.dashboards(EXAMPLE)}
    assert held.keys() - in_the_folder.keys() == dashboards.keys()
    assert {path: held[path] for path in dashboards} == dashboards

    assert FLEET in dashboards
    assert built.fleet_rows() == [pool.name for pool in EXAMPLE.pools] == ["coursework", "mock"]


def test_the_same_configuration_gives_the_same_image(cloud_assembly: Path, tmp_path: Path) -> None:
    """Another synthesis, in another directory and without the pipeline: Grafana is not deployed again."""
    again = synthesize(EXAMPLE, tmp_path)
    assert again.context != image(cloud_assembly).context
    assert again.hash == image(cloud_assembly).hash


def test_one_more_pool_gives_another_image_with_one_more_row(cloud_assembly: Path, tmp_path: Path) -> None:
    mock = next(pool for pool in EXAMPLE.pools if pool.name == "mock")
    more = dataclasses.replace(EXAMPLE, pools=(*EXAMPLE.pools, dataclasses.replace(mock, name="second")))
    with_more = synthesize(more, tmp_path)
    assert with_more.fleet_rows() == ["coursework", "mock", "second"]
    assert with_more.hash != image(cloud_assembly).hash


def test_a_pool_of_each_engine_the_configuration_accepts_has_its_row(app_module: ModuleType) -> None:
    """The build context alone, as the construct assembles it. A dashboard reads an engine's
    metrics by the names of the engine's row (`rhi_common.engine_metrics`), so an engine a pool
    may name and that had no row would fail every synthesis of a configuration that names it."""
    from rhi.observability import grafana  # Imports the CDK: after `app_module`, as in `synthesize`.

    mock = next(pool for pool in EXAMPLE.pools if pool.name == "mock")
    pools = tuple(dataclasses.replace(mock, name=f"on-{engine}", engine=engine) for engine in pool_config.ENGINES)
    context = Path(grafana.image_context(dataclasses.replace(EXAMPLE, pools=pools)))
    assert fleet_rows(context) == [f"on-{engine}" for engine in pool_config.ENGINES]
