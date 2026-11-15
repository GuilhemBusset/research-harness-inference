"""Independent issue 32 protected-policy probe."""
import importlib.util
import sys
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_stage(name, monkeypatch, tmp_path):
    """A source tree of one module, `stage.py`, whose stage holds nothing: the app's own stage now
    brings the network, and a probe needs a resource that only the stage module created."""
    source = tmp_path / "src" / "rhi"
    source.mkdir(parents=True)
    path = source / "stage.py"
    path.write_text("import aws_cdk as cdk\n\n\nclass EndpointStage(cdk.Stage):\n    pass\n")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return source, module.EndpointStage


policy = load("examiner32_policy", Path("tests/policy/test_nag_acknowledgements.py"))
fixtures = load("examiner32_fixtures", Path("tests/policy/conftest.py"))
cdk = fixtures.cdk
read_app = fixtures.read_app
read_acknowledged = fixtures.read_acknowledged


def test_unprotected_acknowledgement_is_checked_with_its_reason(
    cdk, read_app, read_acknowledged, monkeypatch, tmp_path
):
    import cdk_nag

    source, EndpointStage = probe_stage("examiner32_d3_stage", monkeypatch, tmp_path)

    reason = "This independent topic carries no messages."
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stage = EndpointStage(app, "Stage")
    topic = cdk.aws_sns.Topic(cdk.Stack(stage, "Stack"), "Topic")
    cdk.Validations.of(topic).acknowledge(cdk.Acknowledgment(id="AwsSolutions-SNS3", reason=reason))
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app))
    cdk.Aspects.of(stage).add(cdk_nag.WriteNagSuppressionsToCloudFormationAspect())
    templates, creators = read_app(app, source)
    assert len(creators) == 1
    folder = tmp_path / "listed"
    folder.mkdir()
    for module_name, listed_reason, accepted in (
        ("nag", reason, False),
        ("stage", "This is a different independent reason.", False),
        ("stage", reason, True),
    ):
        for path in folder.glob("*.toml"):
            path.unlink()
        (folder / f"{module_name}.toml").write_text(f'[nag]\n"AwsSolutions-SNS3" = "{listed_reason}"\n')
        found = policy.violations(templates, creators, read_acknowledged(folder))
        assert (found == []) is accepted
