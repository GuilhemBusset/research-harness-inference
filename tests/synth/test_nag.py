"""cdk-nag's AWS Solutions pack runs inside synthesis, and a finding fails it (system design, section 8.2, step 2).

A finding is accepted only where the acknowledgement file of the module that created the resource
lists it (section 8.4, control 2). Each test builds its own stacks from a source tree and
acknowledgement files that it writes to a temporary folder, and gives them the plugin as `app.py`
gives it to the app.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]

# What the pack finds on a topic that nothing configures: its publishers need not use TLS.
FINDING = "AwsSolutions-SNS3"
REASON = "The topic of this test carries no message."

# A source tree: a stage, a construct, and a stack of the app itself, as the pipeline's will be.
MODULES = {
    "stage.py": "import aws_cdk as cdk\n\n\nclass Endpoint(cdk.Stage):\n    pass\n",
    "pools/group.py": "from constructs import Construct\n\n\nclass Pool(Construct):\n    pass\n",
    "pipeline/stack.py": "import aws_cdk as cdk\n\n\nclass Pipeline(cdk.Stack):\n    pass\n",
}


@dataclass(frozen=True)
class Tree:
    """The source tree of a test, and the class each of its modules defines."""

    source: Path
    endpoint: Any
    pool: Any
    pipeline: Any


@pytest.fixture
def tree(app_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tree:
    """`MODULES`, written under a temporary folder and imported from there. `app_module` comes
    first: it imports the CDK the way the sandbox needs (`conftest.py`)."""
    source = tmp_path / "src" / "rhi"
    loaded = {}
    for module, text in MODULES.items():
        path = source / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        name = "nag_test_" + module.removesuffix(".py").replace("/", "_")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        loaded[module] = importlib.util.module_from_spec(spec)
        # A class names its module, and the module names its file.
        monkeypatch.setitem(sys.modules, name, loaded[module])
        spec.loader.exec_module(loaded[module])
    return Tree(
        source=source,
        endpoint=loaded["stage.py"].Endpoint,
        pool=loaded["pools/group.py"].Pool,
        pipeline=loaded["pipeline/stack.py"].Pipeline,
    )


def write(folder: Path, files: dict[str, dict[str, str]]) -> Path:
    """Acknowledgement files, each with its nag table, by the module name the file carries."""
    folder.mkdir()
    for module, nag in files.items():
        entries = "".join(f"{json.dumps(finding)} = {json.dumps(reason)}\n" for finding, reason in nag.items())
        (folder / f"{module}.toml").write_text(f"[nag]\n{entries}", encoding="utf-8")
    return folder


def reported(outdir: Path) -> list[tuple[str, str]]:
    """The findings that failed synthesis, each with the path of its resource in the construct
    tree, from the report that synthesis leaves in its output directory. The CDK's own warnings
    are in the report too, and fail nothing."""
    report = json.loads((outdir / "validation-report.json").read_text(encoding="utf-8"))
    return sorted(
        (violation["ruleName"], construct["constructPath"])
        for plugin in report["pluginReports"]
        if plugin["conclusion"] == "failure"
        for violation in plugin["violations"]
        for construct in violation["violatingConstructs"]
    )


def written(construct: Any) -> list[tuple[str, str]]:
    """The acknowledgements in the template, in the metadata of the resource of a topic or of the
    resource itself."""
    import aws_cdk as cdk

    resource = construct if isinstance(construct, cdk.CfnResource) else construct.node.default_child
    stack = cdk.Stack.of(resource)
    assembly = cdk.Stage.of(stack)
    assert assembly is not None
    template = json.loads((Path(assembly.outdir) / stack.template_file).read_text(encoding="utf-8"))
    metadata = template["Resources"][stack.resolve(resource.logical_id)].get("Metadata", {})
    return sorted((rule["id"], rule["reason"]) for rule in metadata.get("cdk_nag", {}).get("rules_to_suppress", []))


@pytest.mark.parametrize(
    ("files", "synthesizes"),
    [
        pytest.param({}, False, id="no-entry"),
        pytest.param({"pools_group": {FINDING: REASON}}, True, id="its-module"),
        # The stage encloses the resource, but the construct inside it is nearer.
        pytest.param({"stage": {FINDING: REASON}}, False, id="another-module"),
    ],
)
def test_a_finding_fails_synthesis_unless_the_file_of_its_module_acknowledges_it(
    files: dict[str, dict[str, str]], synthesizes: bool, app_module: ModuleType, tree: Tree, tmp_path: Path
) -> None:
    import aws_cdk as cdk

    outdir = tmp_path / "cdk.out"
    app = cdk.App(outdir=str(outdir))
    stack = cdk.Stack(tree.endpoint(app, "Stage"), "Stack")
    topic = cdk.aws_sns.Topic(tree.pool(stack, "Pool"), "Topic")
    app_module.add_nag(app, acknowledged=write(tmp_path / "acknowledged", files), source=tree.source)

    if synthesizes:
        app.synth()
        assert reported(outdir) == []
        assert written(topic) == [(FINDING, REASON)]
    else:
        with pytest.raises(RuntimeError):
            app.synth()
        assert reported(outdir) == [(FINDING, "Stage/Stack/Pool/Topic/Resource")]


def test_a_finding_fails_the_synthesis_of_the_app(app_module: ModuleType, tmp_path: Path) -> None:
    import aws_cdk as cdk

    # The app as `conftest.py` builds it, which synthesizes, and one more stack with a finding.
    outdir = tmp_path / "cdk.out"
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(config.load({"RHI_CONFIG": "example"}), outdir=str(outdir), context=context)
    cdk.aws_sns.Topic(cdk.Stack(app, "Seeded"), "Topic")

    with pytest.raises(RuntimeError):
        app.synth()
    assert reported(outdir) == [(FINDING, "Seeded/Topic/Resource")]


def test_a_resource_carries_what_the_file_of_its_module_lists_and_nothing_else(
    app_module: ModuleType, tree: Tree, tmp_path: Path
) -> None:
    import aws_cdk as cdk

    files = {
        "pipeline_stack": {FINDING: "The topic of the pipeline of this test carries no message."},
        "pools_group": {FINDING: REASON},
        "stage": {
            FINDING: "The topic of the stage of this test carries no message.",
            "AwsSolutions-S1": "The stage of this test holds no bucket.",
        },
    }
    # The same topic three times: in a stack of the app, and in a stack of a stage, inside a
    # construct of the source tree and beside it. Then a resource that no module created.
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    topics = {"pipeline_stack": cdk.aws_sns.Topic(tree.pipeline(app, "Pipeline"), "Topic")}
    stack = cdk.Stack(tree.endpoint(app, "Stage"), "Stack")
    topics["pools_group"] = cdk.aws_sns.Topic(tree.pool(stack, "Pool"), "Topic")
    topics["stage"] = cdk.aws_sns.Topic(stack, "Topic")
    of_no_module = cdk.CfnWaitConditionHandle(cdk.Stack(app, "Plain"), "Handle")
    app_module.add_nag(app, acknowledged=write(tmp_path / "acknowledged", files), source=tree.source)
    app.synth()

    assert {module: written(topic) for module, topic in topics.items()} == {
        module: sorted(nag.items()) for module, nag in files.items()
    }
    assert written(of_no_module) == []


def test_an_entry_for_a_pool_lists_the_finding_of_each_configured_pool_and_of_no_other_name(
    app_module: ModuleType, tmp_path: Path
) -> None:
    """An entry whose key holds `<pool>` in place of a pool's name. Grafana's role names each
    pool's log group, which brings a finding of each pool's name: with two pools, neither the
    example's, the stage synthesizes, and the same statement for a name that is no pool's fails it."""
    import aws_cdk as cdk

    from rhi.stage import EndpointStage

    example = config.load({"RHI_CONFIG": "example"})
    (mock,) = example.pools
    pools = tuple(dataclasses.replace(mock, name=name) for name in ("first", "second"))
    configured = dataclasses.replace(example, pools=pools, default_pool="first")
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}

    def synthesize(outdir: Path, also_named: str | None) -> None:
        app = cdk.App(outdir=str(outdir), context=context, analytics_reporting=False)
        role = EndpointStage(app, "Rhi", config=configured).observability.grafana.task_role
        if also_named is not None:
            log_group = cdk.Stack.of(role).format_arn(
                service="logs",
                resource="log-group",
                resource_name=f"/rhi/pools/{also_named}:*",
                arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
            )
            role.add_to_policy(cdk.aws_iam.PolicyStatement(actions=["logs:StartQuery"], resources=[log_group]))
        app_module.add_nag(app)
        app.synth()

    synthesize(tmp_path / "pools", None)
    assert reported(tmp_path / "pools") == []
    with pytest.raises(RuntimeError):
        synthesize(tmp_path / "another", "third")
    # The one finding: those of the two pools are acknowledged here too.
    third = "arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:/rhi/pools/third:*"
    assert reported(tmp_path / "another") == [
        (f"AwsSolutions-IAM5[Resource::{third}]", "Rhi/Observability/Grafana/TaskRole/DefaultPolicy/Resource")
    ]
