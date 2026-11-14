"""Independent checks that the record test examines files in its folder."""

import pytest

from test_measurement_runs_probe import VALIDATOR, document


def test_folder_and_record_test_detect_bad_files(tmp_path, monkeypatch):
    records = tmp_path / "records"
    records.mkdir()
    (records / "README.md").write_text("Human documentation.\n")
    (records / "s4.md").write_text(document())
    monkeypatch.setattr(VALIDATOR, "MEASUREMENTS", records)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    VALIDATOR.test_every_record_follows_the_format(scratch)
    (records / "s5.md").write_text("No front matter.\n")
    assert any("s5.md" in problem for problem in VALIDATOR.folder_problems(records))
    with pytest.raises(AssertionError):
        VALIDATOR.test_every_record_follows_the_format(scratch)
    (records / "s5.md").unlink()
    (records / "wrong.md").write_text(document())
    assert any("wrong.md" in problem for problem in VALIDATOR.folder_problems(records))
