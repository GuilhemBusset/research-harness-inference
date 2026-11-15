"""Records the numbers a spike or a canary posted on a commit (system design, sections 8.4 and 12).

A measurement's job posts each run as commit statuses under `measure/<id>/<run label>/<part>`
(`services/reporter/rhi_reporter/measure.py`). This tool reads the statuses of one commit, as
GitHub's API lists them, and writes them into the records of `docs/measurements/`, in the format
that folder's README defines:

    gh api --paginate "repos/{owner}/{repo}/commits/<sha>/statuses" |
        uv run --locked python tools/measurements/record.py --commit <sha> \
            --unit load_time=s --pass-condition "S4=<the Pass column of the design's section 12>"

It creates or updates one file per measurement, named after the id in lower case. Each run on the
commit becomes the run of its label, with the commit and the day, in UTC, of its newest status,
and one row for each of its numbers, labelled with the run. A run the job posted as error becomes
a pending run with the reason `not measured` and no row. The run of the same label that the
record held, and every row that names it, are replaced. The other runs and their rows, the pass
condition, a waiver and the prose after the front matter stay as they are. The record's verdict
is the one its runs give, unless the owner wrote `waived`.

A status holds neither the pass condition nor a unit, and the format requires both:

- the unit of a number is the one given for its name (`--unit name=unit`), or else the unit of a
  row of that name the record already holds;
- the pass condition of a record that does not exist yet is the one given for its id
  (`--pass-condition ID=text`). An existing record keeps its own.

A number with no unit, a new record with no pass condition, statuses that do not decode and an
existing file that is not a record are refused before any file is written. It needs the standard
library only, and it prints the files it wrote.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO = Path(__file__).resolve().parents[2]
# The reporter's package is not installed anywhere: its root goes on the path, as for the tests.
sys.path.insert(0, str(REPO / "services" / "reporter"))

from rhi_reporter.measure import MEASURE_CONTEXT, Run, Verdict, context_parts, decode
from rhi_reporter.status import CommitStatus, State

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

FOLDER = REPO / "docs" / "measurements"
FENCE = "+++"
NOT_MEASURED = "not measured"

# The keys of the format, in the order they are written.
RECORD_KEYS = ("id", "pass_condition", "verdict", "waiver")
TABLES = {"runs": ("run", "date", "commit", "verdict", "reason"), "rows": ("name", "value", "unit", "labels")}

_COMMIT = re.compile(r"[0-9a-f]{40}")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_VERDICTS = {Verdict.PASS: "pass", Verdict.FAIL: "fail", Verdict.NOT_MEASURED: "pending"}


def runs_of(statuses: Iterable[Mapping[str, Any]], commit: str) -> list[tuple[Run, date]]:
    """The runs a commit's statuses hold, each with the day, in UTC, of its newest status.

    GitHub lists every status ever posted on the commit: only the newest of each context counts.
    A status whose context is not under `measure/` is another reporter's and is passed over."""
    newest: dict[str, tuple[datetime, CommitStatus]] = {}
    for status in statuses:
        context = status.get("context") if isinstance(status, dict) else None
        if not isinstance(context, str):
            raise ValueError("the statuses are not GitHub's list of a commit's statuses")  # noqa: TRY004 - one refusal
        if not context.startswith(MEASURE_CONTEXT):
            continue
        created, description = status.get("created_at"), status.get("description") or ""
        try:
            posted = datetime.fromisoformat(created if isinstance(created, str) else "")
            state = State(status.get("state"))
        except (TypeError, ValueError):
            posted = state = None
        if posted is None or state is None or posted.tzinfo is None or not isinstance(description, str):
            raise ValueError("a measurement's status lacks its state, its description or its date")
        # GitHub lists the newest first: of two statuses of one instant, the first is kept.
        if context not in newest or posted > newest[context][0]:
            newest[context] = (posted, CommitStatus(commit, state, context, description))

    days: dict[tuple[str, str], datetime] = {}
    for context, (posted, _) in newest.items():
        run = context_parts(context)[:2]
        days[run] = max(days.get(run, posted), posted)
    runs = decode(status for _, status in newest.values())
    return [(run, days[run.measurement, run.label].astimezone(UTC).date()) for run in runs]


def verdict_of(run_verdicts: list[Any]) -> str:
    """The verdict a record's runs give it, by the rule of the format (docs/measurements/README.md)."""
    if "fail" in run_verdicts:
        return "fail"
    if "pending" in run_verdicts or not run_verdicts:
        return "pending"
    return "pass"


def read(text: str) -> tuple[dict[str, Any], str]:
    """The front matter of a record, and what follows it. ValueError when the file is not a
    record this tool can write back whole."""
    lines = text.splitlines(keepends=True)
    bare = [line.rstrip("\r\n") for line in lines]
    try:
        end = bare.index(FENCE, 1)
        record = tomllib.loads("".join(lines[1:end]))
    except ValueError:  # no second fence, or TOML that does not parse
        raise ValueError("an existing file is not a measurement record") from None
    if bare[0] != FENCE or not record.keys() <= {*RECORD_KEYS, *TABLES}:
        raise ValueError("an existing file is not a measurement record")
    for kind, keys in TABLES.items():
        tables = record.setdefault(kind, [])
        if not isinstance(tables, list) or not all(
            isinstance(table, dict) and table.keys() <= set(keys) for table in tables
        ):
            raise ValueError("an existing file is not a measurement record")
    return record, "".join(lines[end + 1 :])


def toml_value(value: object) -> str:
    """A value of a record as TOML. The format has text, numbers, yes or no, dates and, for a
    row's labels, one table."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # JSON's escapes are TOML's, and JSON leaves one character raw that TOML does not allow.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, int | float):
        return repr(value)
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, dict):
        pairs = (
            f"{key if _BARE_KEY.fullmatch(key) else toml_value(key)} = {toml_value(item)}"
            for key, item in value.items()
        )
        return "{ " + ", ".join(pairs) + " }" if value else "{}"
    raise ValueError("an existing record holds a value that the format does not have")


def written(record: Mapping[str, Any]) -> str:
    """The front matter of a record, between its two fences."""
    lines = [FENCE, *(f"{key} = {toml_value(record[key])}" for key in RECORD_KEYS if key in record)]
    for kind, keys in TABLES.items():
        for table in record[kind]:
            lines += ["", f"[[{kind}]]", *(f"{key} = {toml_value(table[key])}" for key in keys if key in table)]
    return "\n".join([*lines, FENCE, ""])


def with_run(record: dict[str, Any], run: Run, day: date, commit: str, units: Mapping[str, str]) -> None:
    """Puts one run into a record, in the place of the run of its label and of that run's rows."""
    entry: dict[str, Any] = {"run": run.label, "date": day, "commit": commit, "verdict": _VERDICTS[run.verdict]}
    rows = []
    if run.verdict is Verdict.NOT_MEASURED:
        entry["reason"] = NOT_MEASURED
    else:
        known = {row.get("name"): row.get("unit") for row in reversed(record["rows"])}
        for name, value in run.numbers.items():
            unit = units.get(name, known.get(name))
            if not isinstance(unit, str) or not unit.strip():
                raise ValueError("a number has no unit: give one for each name the record does not hold yet")
            rows.append({"name": name, "value": value, "unit": unit, "labels": {"run": run.label}})

    def replaced(tables: list[dict[str, Any]], new: list[dict[str, Any]], label_of: Any) -> list[dict[str, Any]]:
        """The tables without those of the run, and the new ones where the first of those was."""
        old = [label_of(table) == run.label for table in tables]
        place = old.index(True) if True in old else len(tables)
        return [
            *tables[:place],
            *new,
            *(table for table, gone in zip(tables[place:], old[place:], strict=True) if not gone),
        ]

    record["runs"] = replaced(record["runs"], [entry], lambda table: table.get("run"))
    record["rows"] = replaced(
        record["rows"], rows, lambda row: row["labels"].get("run") if isinstance(row.get("labels"), dict) else None
    )


def record_statuses(
    statuses: Iterable[Mapping[str, Any]],
    commit: str,
    folder: Path = FOLDER,
    *,
    units: Mapping[str, str] | None = None,
    pass_conditions: Mapping[str, str] | None = None,
) -> list[Path]:
    """Writes the measurements of a commit's statuses into the records of a folder, and returns
    the files written. ValueError, with no file written, for what the module's text lists."""
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError("the commit is not a full commit id in lower case")
    texts: dict[Path, tuple[dict[str, Any], str]] = {}
    for run, day in runs_of(statuses, commit):
        path = folder / f"{run.measurement.lower()}.md"
        if path not in texts:
            if path.exists():
                texts[path] = read(path.read_text(encoding="utf-8"))
            else:
                condition = (pass_conditions or {}).get(run.measurement)
                if not isinstance(condition, str) or not condition.strip():
                    raise ValueError("a new record has no pass condition: give the one of its measurement")
                texts[path] = ({"id": run.measurement, "pass_condition": condition, "runs": [], "rows": []}, "")
            if texts[path][0].get("id") != run.measurement:
                raise ValueError("an existing file is the record of another measurement")
        with_run(texts[path][0], run, day, commit, units or {})

    files = {}
    for path, (record, rest) in texts.items():
        if record.get("verdict") != "waived":  # only the owner writes a waiver, and it stands
            record["verdict"] = verdict_of([run.get("verdict") for run in record["runs"]])
        files[path] = written(record) + rest
    for path, text in files.items():
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(files)


def pairs(values: Sequence[str]) -> dict[str, str]:
    """What `key=value` options give, repeated."""
    return {key: text for key, _, text in (value.partition("=") for value in values)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the measurements a commit's statuses hold.")
    parser.add_argument("--commit", required=True, help="the commit the statuses are of: its full id")
    parser.add_argument(
        "--statuses", default="-", help="GitHub's list of the commit's statuses, as JSON (default: stdin)"
    )
    parser.add_argument(
        "--folder", type=Path, default=FOLDER, help="where the records are (default: docs/measurements)"
    )
    parser.add_argument("--unit", action="append", default=[], metavar="NAME=UNIT", help="the unit of a number")
    parser.add_argument(
        "--pass-condition", action="append", default=[], metavar="ID=TEXT", help="a new record's pass condition"
    )
    args = parser.parse_args(argv)
    try:
        text = sys.stdin.read() if args.statuses == "-" else Path(args.statuses).read_text(encoding="utf-8")
        statuses = json.loads(text)
        if not isinstance(statuses, list):
            raise ValueError("the statuses are not GitHub's list of a commit's statuses")  # noqa: TRY004 - one refusal
        files = record_statuses(
            statuses, args.commit, args.folder, units=pairs(args.unit), pass_conditions=pairs(args.pass_condition)
        )
    except json.JSONDecodeError:
        print("record: the statuses are not JSON", file=sys.stderr)
        return 1
    except ValueError as refusal:
        print(f"record: {refusal}", file=sys.stderr)
        return 1
    for path in files:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
