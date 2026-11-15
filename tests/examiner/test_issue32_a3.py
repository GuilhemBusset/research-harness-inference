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


def test_inherited_unlisted_acknowledgement_is_detected(cdk, read_app, tmp_path):
    import cdk_nag

    from rhi import config
    from rhi.stage import EndpointStage

    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stage = EndpointStage(app, "Stage", config=config.load({"RHI_CONFIG": "example"}))
    cdk.aws_sns.Topic(stage.stack, "Topic")
    cdk.Validations.of(stage.stack).acknowledge(
        cdk.Acknowledgment(id="AwsSolutions-SNS3", reason="The independent topic holds no messages.")
    )
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app))
    cdk.Aspects.of(stage).add(cdk_nag.WriteNagSuppressionsToCloudFormationAspect())
    templates, creators = read_app(app, Path("src/rhi"))
    expected = [policy.Finding(path, logical_id, "AwsSolutions-SNS3") for path, logical_id in creators]
    assert len(expected) == 1
    assert policy.violations(templates, creators, {}) == expected
