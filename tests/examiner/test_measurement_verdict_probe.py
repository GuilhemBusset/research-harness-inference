"""Independent checks of the verdict rule through complete record documents."""

from itertools import product

from test_measurement_runs_probe import HEAD, RUN, VALIDATOR, document


def test_verdict_precedence_and_owner_waiver():
    for count in range(3):
        for verdicts in product(("pass", "fail", "pending"), repeat=count):
            expected = "fail" if "fail" in verdicts else "pending" if not verdicts or "pending" in verdicts else "pass"
            runs = ""
            for index, verdict in enumerate(verdicts):
                run = RUN.replace("download", f"run-{index}").replace('verdict = "pass"', f'verdict = "{verdict}"')
                runs += run + ('reason = "not measured"\n' if verdict == "pending" else "")
            for declared in ("pass", "fail", "pending"):
                faults = VALIDATOR.file_problems(document(HEAD.replace('"pass"', f'"{declared}"'), runs, ""))
                assert bool(faults) == (declared != expected), (verdicts, declared, faults)
            waived = HEAD.replace('"pass"', '"waived"')
            assert VALIDATOR.file_problems(document(waived, runs, ""))
            assert VALIDATOR.file_problems(document(waived + 'waiver = "docs/waiver.md"\n', runs, "")) == []
