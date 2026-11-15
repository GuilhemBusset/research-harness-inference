import subprocess
import sys
import tomllib
from pathlib import Path


def test_sdk_pin_and_file_operations_without_sdk(tmp_path):
    project = tomllib.loads(Path("pyproject.toml").read_text())
    lock = tomllib.loads(Path("uv.lock").read_text())
    versions = [p["version"] for p in lock["package"] if p["name"] == "boto3"]
    assert len(versions) == 1
    assert "boto3==" + versions[0] in project["project"]["dependencies"]
    program = (
        "import sys; from pathlib import Path; from rhi import config; "
        "c = config.load({'RHI_CONFIG': 'example'}); "
        "config.write_file(c, Path(sys.argv[1])); "
        "assert config.load({'RHI_CONFIG': 'file', 'RHI_CONFIG_FILE': sys.argv[1]}) == c; "
        "assert 'boto3' not in sys.modules and 'botocore' not in sys.modules"
    )
    subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "config.toml")],
        env={"PYTHONPATH": "src"},
        check=True,
    )
