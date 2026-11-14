"""Independent checks of the measurement row schema."""

from test_measurement_runs_probe import ROW, VALIDATOR, document


def test_rows_require_typed_values_units_and_run_labels():
    for value in ("90", "0.5", "true", "false"):
        row = ROW.replace("value = 90", f"value = {value}")
        if value in ("true", "false"):
            row = row.replace('unit = "s"\n', "")
        assert VALIDATOR.file_problems(document(rows=row)) == []
    for row in (
        ROW.replace('unit = "s"\n', ""),
        ROW.replace("value = 90", "value = true"),
        ROW.replace("value = 90", 'value = "90"'),
        ROW.replace('run = "download", ', ""),
        ROW.replace('run = "download"', 'run = "absent"'),
    ):
        assert VALIDATOR.file_problems(document(rows=row))
    reordered = ROW.replace('run = "download", build = "8bit"', 'build = "8bit", run = "download"')
    assert VALIDATOR.file_problems(document(rows=ROW + reordered))
