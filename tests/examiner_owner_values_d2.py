"""List permissions, repeated additions and redacted count through the real entry point."""

import io
import stat

from rhi import config, owner_config


def test_existing_list_is_preserved_restricted_and_not_duplicated(tmp_path, monkeypatch, capsys):
    loaded = config.load({"RHI_CONFIG": "example"})
    values = tmp_path / "values"
    values.write_text("# retained comment\nPreviously entered placeholder\n")
    extra = "Additional placeholder"
    for entered in (extra, extra.upper()):
        values.chmod(0o644)
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{entered}\n{loaded.domain}\n"))
        assert owner_config.main(["values", str(config.EXAMPLE_FILE), "--list", str(values)]) == 0
        lines = [line for line in values.read_text().splitlines() if not line.startswith("#")]
        expected = {str(getattr(loaded, key)) for key in config.PRIVATE_KEYS}
        expected |= {"Previously entered placeholder", extra}
        assert set(lines) == expected and len(lines) == len(expected)
        assert stat.S_IMODE(values.stat().st_mode) == 0o600
        output = capsys.readouterr()
        assert f"holds {len(expected)} values" in output.out
        assert all(value not in output.out + output.err for value in expected | {entered})
    assert values.read_text().startswith("# retained comment\nPreviously entered placeholder\n")
