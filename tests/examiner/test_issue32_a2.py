"""Independent issue 32 protected-policy probe."""
import importlib.util
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("examiner32_policy", Path("tests/policy/test_nag_acknowledgements.py"))
fixtures = load("examiner32_fixtures", Path("tests/policy/conftest.py"))
cdk = fixtures.cdk
read_app = fixtures.read_app
read_acknowledged = fixtures.read_acknowledged


synthesized = fixtures.synthesized
templates = fixtures.templates
created_by = fixtures.created_by
acknowledged = fixtures.acknowledged


def test_real_app_has_no_unlisted_metadata(templates, created_by, acknowledged):
    assert templates
    assert policy.violations(templates, created_by, acknowledged) == []
    for name, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            listed = acknowledged.get(created_by[name, logical_id], {}).get("nag", {})
            for entry in resource.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", []):
                assert entry["id"] in listed
                assert listed[entry["id"]] == entry["reason"]
