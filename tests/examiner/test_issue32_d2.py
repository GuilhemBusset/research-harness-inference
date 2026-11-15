"""Independent issue 32 synthesis probe."""
import importlib.util
import json
from pathlib import Path


def wiring(monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    spec = importlib.util.spec_from_file_location("examiner32_app", Path("app.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nearest_module_entries_are_exact(monkeypatch, tmp_path):
    module = wiring(monkeypatch)
    import sys

    import aws_cdk as cdk

    source = tmp_path / "source"
    source.mkdir()
    classes = []
    for name in ("outer", "inner"):
        path = source / f"{name}.py"
        path.write_text("from constructs import Construct\nclass Scope(Construct): pass\n")
        spec = importlib.util.spec_from_file_location(f"examiner32_{name}", path)
        assert spec is not None and spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, loaded)
        spec.loader.exec_module(loaded)
        classes.append(loaded.Scope)
    folder = tmp_path / "listed"
    folder.mkdir()
    expected = {
        "outer": {"AwsSolutions-SNS3": "The outer topic carries no messages."},
        "inner": {
            "AwsSolutions-SNS3": "The inner topic carries no messages.",
            "AwsSolutions-IAM5[Resource::*]": "This independent entry keeps its full identifier.",
        },
    }
    for name, entries in expected.items():
        text = "\n".join(f"{json.dumps(key)} = {json.dumps(reason)}" for key, reason in entries.items())
        (folder / f"{name}.toml").write_text("[nag]\n" + text)
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stack = cdk.Stack(cdk.Stage(app, "Stage"), "Stack")
    outer = classes[0](stack, "Outer")
    cdk.aws_sns.Topic(outer, "OuterTopic")
    cdk.aws_sns.Topic(classes[1](outer, "Inner"), "InnerTopic")
    module.add_nag(app, acknowledged=folder, source=source)
    app.synth()
    resources = [
        resource
        for template in (tmp_path / "assembly").rglob("*.template.json")
        for resource in json.loads(template.read_text()).get("Resources", {}).values()
    ]
    actual = [
        {entry["id"]: entry["reason"] for entry in resource["Metadata"]["cdk_nag"]["rules_to_suppress"]}
        for resource in resources
    ]
    assert len(actual) == 2
    assert expected["outer"] in actual and expected["inner"] in actual
