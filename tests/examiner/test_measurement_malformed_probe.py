"""Independent malformed documents for the eight acceptance cases."""

from test_measurement_runs_probe import HEAD, ROW, RUN, VALIDATOR, document


def test_eight_malformed_records_are_rejected():
    assert VALIDATOR.file_problems(document()) == []
    cases = (
        (document(head=HEAD.replace('verdict = "pass"\n', "")), "verdict"),
        (document(rows=ROW.replace("value = 90", 'value = "90"')), "value"),
        (document(head=HEAD + 'unexpected = true\n'), "unexpected"),
        (document(head=HEAD.replace('"pass"', '"waived"')), "waiver"),
        (document(rows=ROW + ROW), "second row"),
        (document(rows=ROW.replace("download", "unknown")), "names no run"),
        (document(head=HEAD.replace('"pass"', '"pending"'), runs=RUN.replace('"pass"', '"pending"')), "reason"),
        (document(runs=RUN.replace('"pass"', '"fail"')), "not the one the runs give"),
    )
    for text, expected in cases:
        faults = VALIDATOR.file_problems(text)
        assert any(expected in fault for fault in faults), (expected, faults)
