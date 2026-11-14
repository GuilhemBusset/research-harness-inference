"""Every measurement record follows the format of `docs/measurements/README.md`.

A record is one Markdown file per spike, canary or exercise, opened by TOML front matter between
two `+++` lines: an id, the pass condition, a verdict, its runs and its rows. The verdict follows
from the runs by one rule, so that a writer and a reader cannot disagree. The functions here are
that format as code; a change to one is a change to the README.
"""

from __future__ import annotations

import math
import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

MEASUREMENTS = Path(__file__).resolve().parents[2] / "docs" / "measurements"
README = "README.md"

FENCE = "+++"
RECORD_KEYS = {"id", "pass_condition", "verdict", "waiver", "runs", "rows"}
RUN_KEYS = {"run", "date", "commit", "verdict", "reason"}
ROW_KEYS = {"name", "value", "unit", "labels"}
RUN_VERDICTS = ("pass", "fail", "pending")
RECORD_VERDICTS = (*RUN_VERDICTS, "waived")

RECORD_ID = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
RUN_LABEL = re.compile(r"[a-z0-9-]+")
# A quantity's name, and the name of a label.
NAME = re.compile(r"[a-z][a-z0-9_]*")
COMMIT = re.compile(r"[0-9a-f]{40}")


def verdict_of(run_verdicts: list[Any]) -> str:
    """The verdict a record's runs give it: the one rule, for a writer as for a reader."""
    if "fail" in run_verdicts:
        return "fail"
    if "pending" in run_verdicts or not run_verdicts:
        return "pending"
    return "pass"


def front_matter(text: str) -> dict[str, Any]:
    """The TOML between the two `+++` lines that open a record. ValueError when there is none."""
    lines = text.splitlines()
    if not lines or lines[0] != FENCE or FENCE not in lines[1:]:
        raise ValueError("the file does not open with TOML between two +++ lines")
    return tomllib.loads("\n".join(lines[1 : lines.index(FENCE, 1)]))


def is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def unknown_keys(place: str, table: dict[str, Any], known: set[str]) -> list[str]:
    return [f"{place}{key}: an unknown key" for key in table if key not in known]


def tables(record: dict[str, Any], key: str, problems: list[str]) -> list[dict[str, Any]]:
    """The record's runs or rows: a list of tables, empty when the key is absent."""
    value = record.get(key, [])
    if isinstance(value, list) and all(isinstance(entry, dict) for entry in value):
        return value
    problems.append(f"{key}: not a list of tables")
    return []


def record_problems(record: dict[str, Any]) -> list[str]:
    """What keeps the front matter from being a record, each as `<place>: <fault>`; empty when it is one."""
    problems = unknown_keys("", record, RECORD_KEYS)
    if not matches(RECORD_ID, record.get("id")):
        problems.append("id: missing, or not letters, digits and hyphens after a letter")
    if not is_text(record.get("pass_condition")):
        problems.append("pass_condition: missing, or not text")

    run_labels: set[str] = set()
    runs = tables(record, "runs", problems)
    for index, run in enumerate(runs):
        place = f"runs[{index}]"
        problems += unknown_keys(f"{place}.", run, RUN_KEYS)
        label = run.get("run")
        if not isinstance(label, str) or not RUN_LABEL.fullmatch(label):
            problems.append(f"{place}.run: missing, or not lower-case letters, digits and hyphens")
        elif label in run_labels:
            problems.append(f"{place}.run: a second run with this label")
        else:
            run_labels.add(label)
        if type(run.get("date")) is not date:
            problems.append(f"{place}.date: missing, or not a TOML date")
        if not matches(COMMIT, run.get("commit")):
            problems.append(f"{place}.commit: missing, or not a full commit id")
        if run.get("verdict") not in RUN_VERDICTS:
            problems.append(f"{place}.verdict: missing, or not pass, fail or pending")
        elif run["verdict"] == "pending" and not is_text(run.get("reason")):
            problems.append(f"{place}.reason: a pending run gives its reason")
        elif run["verdict"] != "pending" and "reason" in run:
            problems.append(f"{place}.reason: only a pending run gives a reason")

    seen: set[tuple[str, frozenset[tuple[str, str]]]] = set()
    for index, row in enumerate(tables(record, "rows", problems)):
        place = f"rows[{index}]"
        problems += unknown_keys(f"{place}.", row, ROW_KEYS)
        name = row.get("name")
        if not matches(NAME, name):
            problems.append(f"{place}.name: missing, or not lower-case letters, digits and underscores")
        value = row.get("value")
        if isinstance(value, bool):
            if "unit" in row:
                problems.append(f"{place}.unit: true or false has no unit")
        elif isinstance(value, int | float) and math.isfinite(value):
            if not is_text(row.get("unit")):
                problems.append(f"{place}.unit: a number gives its unit")
        else:
            problems.append(f"{place}.value: missing, or not a number or true or false")
        labels = row.get("labels")
        if not isinstance(labels, dict) or not all(matches(NAME, key) and is_text(labels[key]) for key in labels):
            problems.append(f"{place}.labels: missing, or not a table of text")
            continue
        if labels.get("run") not in run_labels:
            problems.append(f"{place}.labels.run: missing, or names no run of the record")
        if isinstance(name, str):
            identity = (name, frozenset(labels.items()))
            if identity in seen:
                problems.append(f"{place}: a second row with this name and these labels")
            seen.add(identity)

    verdict = record.get("verdict")
    if verdict not in RECORD_VERDICTS:
        problems.append("verdict: missing, or not pass, fail, waived or pending")
    elif verdict == "waived":
        if not is_text(record.get("waiver")):
            problems.append("waiver: a waived record says where the owner's written waiver is")
    else:
        if "waiver" in record:
            problems.append("waiver: only a waived record names a waiver")
        if verdict != verdict_of([run.get("verdict") for run in runs]):
            problems.append("verdict: not the one the runs give")
    return problems


def file_problems(text: str, file_name: str | None = None) -> list[str]:
    """What keeps a file from being a record, and from being named after its id; empty when it is one."""
    try:
        record = front_matter(text)
    except ValueError as error:
        return [f"front matter: {error}"]
    problems = record_problems(record)
    record_id = record.get("id")
    if file_name is not None and isinstance(record_id, str) and file_name != f"{record_id.lower()}.md":
        problems.append("id: the file is not named after it in lower case")
    return problems


def folder_problems(folder: Path) -> list[str]:
    """The faults of every record in a folder: each Markdown file but the README, by its file name."""
    return [
        f"{path.name}: {problem}"
        for path in sorted(folder.glob("*.md"))
        if path.name != README
        for problem in file_problems(path.read_text(encoding="utf-8"), path.name)
    ]


def places(text: str) -> list[str]:
    """Where a record's faults are."""
    return [problem.partition(":")[0] for problem in file_problems(text)]


# The parts of a record the tests assemble. No number in them is a measurement.
HEAD = 'id = "S4"\npass_condition = "Under 100 s for the coursework model\'s 31 GB"\nverdict = "pass"\n'
RUN = f'[[runs]]\nrun = "stream"\ndate = 2026-01-15\ncommit = "{"1" * 40}"\nverdict = "pass"\n'
ROW = '[[rows]]\nname = "load_time"\nvalue = 84.5\nunit = "s"\nlabels = { run = "stream", pool = "coursework" }\n'


def record(*parts: str) -> str:
    return "\n".join([FENCE, *parts, FENCE, "", "What the run showed, in prose."])


def changed(part: str, old: str, new: str) -> str:
    assert old in part, old
    return part.replace(old, new)


PENDING = changed(HEAD, '"pass"', '"pending"')
FAILED_RUN = changed(RUN, '"pass"', '"fail"')
PENDING_RUN = changed(RUN, '"pass"', '"pending"\nreason = "not measured"')


def test_every_record_follows_the_format(tmp_path: Path) -> None:
    assert folder_problems(MEASUREMENTS) == []

    # The same check finds the faults of a folder that holds some.
    (tmp_path / README).write_text("Not a record.\n")
    (tmp_path / "s4.md").write_text(record(HEAD, RUN, ROW))
    assert folder_problems(tmp_path) == []
    (tmp_path / "s5.md").write_text(record(changed(HEAD, "S4", "S5"), RUN, changed(ROW, "84.5", '"84.5"')))
    (tmp_path / "s6.md").write_text("Prose with no front matter.\n")
    (tmp_path / "spike-one.md").write_text(record(changed(HEAD, "S4", "S1"), RUN, ROW))
    assert [problem.rsplit(":", 1)[0] for problem in folder_problems(tmp_path)] == [
        "s5.md: rows[0].value",
        "s6.md: front matter",
        "spike-one.md: id",
    ]


def test_the_readme_example_follows_the_format() -> None:
    readme = (MEASUREMENTS / README).read_text(encoding="utf-8")
    examples = re.findall(r"^```\w*\n(\+\+\+\n.*?)^```", readme, flags=re.MULTILINE | re.DOTALL)
    assert examples, "the README shows no record"
    assert [file_problems(example) for example in examples] == [[]] * len(examples)


def test_rejects_a_malformed_record() -> None:
    assert file_problems(record(HEAD, RUN, ROW)) == []

    malformed = {
        "lacks a verdict": (record(changed(HEAD, 'verdict = "pass"\n', ""), RUN, ROW), "verdict"),
        "gives a number as text": (record(HEAD, RUN, changed(ROW, "84.5", '"84.5"')), "rows[0].value"),
        "carries an unknown key": (record(HEAD + 'note = "tuned"\n', RUN, ROW), "note"),
        "is waived without naming the waiver": (record(changed(HEAD, '"pass"', '"waived"'), RUN, ROW), "waiver"),
        "holds two rows with the same name and labels": (record(HEAD, RUN, ROW, changed(ROW, "84.5", "91")), "rows[1]"),
        "holds a row whose run names no run": (
            record(HEAD, RUN, changed(ROW, 'run = "stream"', 'run = "download"')),
            "rows[0].labels.run",
        ),
        "holds a pending run with no reason": (
            record(PENDING, changed(PENDING_RUN, 'reason = "not measured"', "")),
            "runs[0].reason",
        ),
        "passes with a failed run": (record(HEAD, FAILED_RUN, ROW), "verdict"),
        "is pending with every run passed": (record(PENDING, RUN, ROW), "verdict"),
    }
    assert {fault: places(text) for fault, (text, _) in malformed.items()} == {
        fault: [place] for fault, (_, place) in malformed.items()
    }


def test_accepts_a_matrix() -> None:
    commits = {"8bit": "a" * 40, "4bit": "b" * 40}
    runs = [
        f'[[runs]]\nrun = "{build}"\ndate = 2026-01-15\ncommit = "{commits[build]}"\nverdict = "pass"\n'
        for build in commits
    ]
    rows = [
        f'[[rows]]\nname = "{name}"\nvalue = {value}\nunit = "{unit}"\n'
        f'labels = {{ run = "{build}", build = "{build}", streams = "{streams}", session_shape = "{shape}" }}\n'
        for build in commits
        for streams in ("1", "8", "class")
        for shape in ("hands-on", "several-agents")
        for name, value, unit in (("output_speed_per_stream_median", 21.5, "tokens/s"), ("preemptions", 0, "count"))
    ]
    assert len(rows) == 24
    head = changed(HEAD, "S4", "S5")
    assert file_problems(record(head, *runs, *rows)) == []

    # No run yet: the verdict is pending, and nothing else is needed.
    assert file_problems(record(changed(head, '"pass"', '"pending"'))) == []


def test_holds_each_run_to_the_format() -> None:
    two_runs = record(PENDING, RUN, changed(PENDING_RUN, '"stream"', '"download"'), ROW)
    assert file_problems(two_runs) == []

    faults = {
        "an upper-case label": (record(HEAD, changed(RUN, '"stream"', '"Stream"')), "runs[0].run"),
        "a label used twice": (record(HEAD, RUN, RUN), "runs[1].run"),
        "no date": (record(HEAD, changed(RUN, "date = 2026-01-15\n", "")), "runs[0].date"),
        "a date as text": (record(HEAD, changed(RUN, "2026-01-15", '"2026-01-15"')), "runs[0].date"),
        "no commit": (record(HEAD, changed(RUN, f'commit = "{"1" * 40}"\n', "")), "runs[0].commit"),
        "a short commit": (record(HEAD, changed(RUN, "1" * 40, "1" * 7)), "runs[0].commit"),
        "a waived run": (record(HEAD, changed(RUN, '"pass"', '"waived"')), "runs[0].verdict"),
        "an unknown key": (record(HEAD, RUN + 'job = "spike"\n'), "runs[0].job"),
    }
    assert {fault: places(text) for fault, (text, _) in faults.items()} == {
        fault: [place] for fault, (_, place) in faults.items()
    }


def test_holds_each_row_to_the_format() -> None:
    yes = '[[rows]]\nname = "image_on_disk"\nvalue = true\nlabels = { run = "stream" }\n'
    assert file_problems(record(HEAD, RUN, ROW, yes)) == []

    faults = {
        "a number with no unit": (changed(ROW, 'unit = "s"\n', ""), "rows[0].unit"),
        "a unit on true or false": (yes + 'unit = "s"\n', "rows[0].unit"),
        "no value": (changed(ROW, "value = 84.5\n", ""), "rows[0].value"),
        "a value that is no number": (changed(ROW, "84.5", "nan"), "rows[0].value"),
        "no name": (changed(ROW, 'name = "load_time"\n', ""), "rows[0].name"),
        "no run label": (changed(ROW, 'run = "stream", ', ""), "rows[0].labels.run"),
        "a label that is not text": (changed(ROW, '"coursework"', "1"), "rows[0].labels"),
        "an unknown key": (ROW + 'note = "tuned"\n', "rows[0].note"),
    }
    assert {fault: places(record(HEAD, RUN, row)) for fault, (row, _) in faults.items()} == {
        fault: [place] for fault, (_, place) in faults.items()
    }


def test_the_verdict_follows_from_the_runs() -> None:
    assert verdict_of([]) == "pending"
    assert verdict_of(["pass", "pass"]) == "pass"
    assert verdict_of(["pass", "pending"]) == "pending"
    assert verdict_of(["pending", "fail", "pass"]) == "fail"

    assert (
        file_problems(record(changed(HEAD, '"pass"', '"fail"'), FAILED_RUN, changed(PENDING_RUN, "stream", "download")))
        == []
    )

    # The owner's waiver stands whatever the runs say, and only a waived record names one.
    waiver = 'waiver = "docs/measurements/s4.md, under Waiver"\n'
    assert file_problems(record(changed(HEAD, '"pass"', '"waived"') + waiver, FAILED_RUN, ROW)) == []
    assert places(record(HEAD + waiver, RUN, ROW)) == ["waiver"]
