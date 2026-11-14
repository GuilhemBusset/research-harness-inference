"""Independent two-commit matrix and empty pending record."""

from itertools import product

from test_measurement_runs_probe import HEAD, ROW, RUN, VALIDATOR, document


def test_two_build_matrix_and_no_runs():
    runs = ""
    rows = ""
    for build, commit in (("8bit", "a" * 40), ("4bit", "b" * 40)):
        runs += RUN.replace("download", build).replace("a" * 40, commit)
        for streams, shape in product(("1", "16", "class"), ("hands-on", "several-agents")):
            row = ROW.replace("download", build).replace('build = "8bit"', f'build = "{build}"')
            rows += row.replace(" }", f', streams = "{streams}", session_shape = "{shape}" }}')
    assert VALIDATOR.file_problems(document(runs=runs, rows=rows)) == []
    pending = HEAD.replace('"pass"', '"pending"')
    assert VALIDATOR.file_problems(document(head=pending, runs="", rows="")) == []
