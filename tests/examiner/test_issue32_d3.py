"""Independent issue 32 protected-policy probe."""
import importlib.util
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("examiner32_policy", Path("tests/policy/test_nag_acknowledgements.py"))
fixtures = load("examiner32_fixtures", Path("tests/policy/conftest.py"))
cdk = fixtures.cdk
read_app = fixtures.read_app
read_acknowledged = fixtures.read_acknowledged


def test_unprotected_acknowledgement_is_checked_with_its_reason(cdk, read_app, read_acknowledged, tmp_path):
    import cdk_nag

    from rhi import config
    from rhi.stage import EndpointStage

    reason = "This independent topic carries no messages."
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stage = EndpointStage(app, "Stage", config=config.load({"RHI_CONFIG": "example"}))
    topic = cdk.aws_sns.Topic(stage.stack, "Topic")
    cdk.Validations.of(topic).acknowledge(cdk.Acknowledgment(id="AwsSolutions-SNS3", reason=reason))
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app))
    cdk.Aspects.of(stage).add(cdk_nag.WriteNagSuppressionsToCloudFormationAspect())
    templates, creators = read_app(app, Path("src/rhi"))
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
