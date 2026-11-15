# Measurement records

What the spikes, canaries and exercises of the design measured, one file each. A spike or a canary
posts its numbers as commit statuses, and a housekeeping change records them here
([system design](../design/system-design.md), sections 8.4 and 12). The exit of phase 2 is such
records (section 14), and the record of spike S5 replaces the estimate of section 4.7.

Every record has the format below, so that a test can check it and a tool can write it.
[`tests/unit/test_measurements.py`](../../tests/unit/test_measurements.py) holds every record in
this folder to it:

```sh
uv run --locked pytest tests/unit/test_measurements.py
```

## A record

A record is one Markdown file in this folder for one spike, canary or exercise. Its name is the
id in lower case: `s5.md` for spike S5. This README is the only other Markdown file here.

The file opens with TOML front matter between two `+++` lines. Whatever follows the second `+++`
line is free Markdown for a person: what the numbers mean, what was decided. No test reads it.

The numbers in this example are invented.

```text
+++
id = "S1"
pass_condition = "Warm start under 5 min at the 90th percentile over 20 starts"
verdict = "pending"

[[runs]]
run = "warm"
date = 2026-01-15
commit = "0123456789abcdef0123456789abcdef01234567"
verdict = "pass"

[[runs]]
run = "cold"
date = 2026-01-16
commit = "89abcdef0123456789abcdef0123456789abcdef"
verdict = "pending"
reason = "not measured"

[[rows]]
name = "start_to_serving_p90"
value = 262.5
unit = "s"
labels = { run = "warm", pool = "coursework" }

[[rows]]
name = "stage_duration_median"
value = 48
unit = "s"
labels = { run = "warm", pool = "coursework", stage = "engine-ready" }

[[rows]]
name = "engine_image_on_disk"
value = true
labels = { run = "warm", pool = "coursework" }
+++

The warm run passed. The cold run's job could not start an instance, so it measured nothing.
```

The front matter has these keys and no other. An unknown key, at any level, is an error.

| Key | Required | What it is |
| --- | --- | --- |
| `id` | yes | The id as the design writes it, such as `S5`: a letter, then letters, digits and hyphens |
| `pass_condition` | yes | The pass condition, in the design's words (for a spike, the "Pass" column of section 12) |
| `verdict` | yes | `pass`, `fail`, `waived` or `pending`: see [The verdict](#the-verdict) |
| `waiver` | only when the verdict is `waived` | Where the owner's written waiver is |
| `runs` | no | The runs, as `[[runs]]` tables. A record with no run yet leaves the key out |
| `rows` | no | The rows, as `[[rows]]` tables |

## Runs

One measurement is often run more than once: warm and cold, once per build, once per loader
setting, sometimes on different commits. Each run is one `[[runs]]` table.

| Key | Required | What it is |
| --- | --- | --- |
| `run` | yes | The run label its job gives: lower-case letters, digits and hyphens (`8bit`, `4bit`, `warm`, `cold`, `stream`, `download`). It is the label in the context of the job's commit statuses. No two runs of a record share one |
| `date` | yes | The day it ran, as a TOML date: `2026-01-15`, without quotes |
| `commit` | yes | The commit it ran on: the full id, 40 hexadecimal digits in lower case |
| `verdict` | yes | `pass`, `fail` or `pending`. A run is never `waived` |
| `reason` | only when the verdict is `pending` | Why there is no verdict yet. A job that could not measure leaves a pending run with the reason `not measured` |

A later run of one label replaces that run: its table, and every row that names it. The other
runs and their rows stay as they are.

## Rows

A row is one number or one yes or no, with the conditions it was measured under. Each is one
`[[rows]]` table.

| Key | Required | What it is |
| --- | --- | --- |
| `name` | yes | The quantity, statistic included: lower-case letters, digits and underscores, starting with a letter (`time_to_first_token_p95`, `output_speed_per_stream_median`, `preemptions`, `request_accepted`) |
| `value` | yes | A TOML number (`262.5`, `48`) or `true` or `false`. Never text: `"262.5"` is an error, and so are `nan` and `inf` |
| `unit` | for a number | The unit of a number, as text: `s`, `tokens/s`, `tokens`, `percent`, `count`. A row whose value is `true` or `false` has no `unit` key |
| `labels` | yes | A table that names the conditions |

The labels are what tells apart two rows of one name. A label's name is written like a quantity's
name, and its value is text, also when it reads as a number (`streams = "8"`), because the same
label takes values that are not numbers (`streams = "class"`, for the class's size).

- `run` is required. It is the label of one of the record's runs: the run the row comes from.
- The others say what the design varies, and a record uses the ones its measurement needs: `pool`,
  `instance_type`, `build`, `streams`, `session_shape`, `harness`, `client`, `step`, `stage`.

No two rows of a record have the same name and the same labels. A matrix, such as S5's, is many
rows of one name that differ by their labels:

```toml
[[rows]]
name = "output_speed_per_stream_median"
value = 19.5
unit = "tokens/s"
labels = { run = "8bit", build = "8bit", streams = "8", session_shape = "hands-on" }

[[rows]]
name = "output_speed_per_stream_median"
value = 24.0
unit = "tokens/s"
labels = { run = "4bit", build = "4bit", streams = "8", session_shape = "hands-on" }
```

## The verdict

A run's verdict says whether that run met the pass condition. The record's verdict follows from
its runs by one rule, so that whoever writes a record and whoever reads it cannot disagree:

1. `fail` when a run failed;
2. otherwise `pending` when a run is pending, or when there is no run yet;
3. otherwise `pass`.

The one exception is `waived`. Where the design lets the owner waive a condition in writing (S3),
only the owner writes `waived`, and it stands whatever the runs say. The record then names where
that written waiver is in `waiver`: a path in this repository, or the pull request or issue that
carries it. A record with any other verdict has no `waiver` key.

## Writing a record

A record is public, like everything in this repository (see "Public-repository hygiene" in
[`AGENTS.md`](../../AGENTS.md)). Its values are numbers and yes or no, and its labels name
conditions of the design: no host name, no resource id, no account, no student.

A change to a record goes through a pull request like any other change, and the test above runs
on it.

### From the statuses of a commit

A measurement's job posts each run as commit statuses, under the contexts
`measure/<id>/<run label>/<part>`: `measure/S5/8bit/1`, `measure/S5/8bit/2`, `measure/S1/cold/1`
([`services/reporter/rhi_reporter/measure.py`](../../services/reporter/rhi_reporter/measure.py)).
The id is one of `S1` to `S11`, `gpu-canary` for the weekly canary and `dry-run-class` for the
dry-run class. The state is the run's verdict, success for a pass, failure for a fail and error
when the job could not measure, and the description holds the numbers as `name=number` pairs.

[`tools/measurements/record.py`](../../tools/measurements/record.py) writes the statuses of one
commit into the records of this folder. It reads the list GitHub's API gives, and needs the
standard library only:

```sh
gh api --paginate "repos/{owner}/{repo}/commits/<sha>/statuses" |
    uv run --locked python tools/measurements/record.py --commit <sha> \
        --unit load_time=s --pass-condition "S4=Under 100 s for the coursework model's 31 GB"
```

- Each run on the commit becomes the run of its label, with the commit and the day, in UTC, of
  its newest status, and one row for each number, labelled `run` only. It replaces the run of
  that label and its rows, and leaves the other runs, the pass condition, a waiver and the prose
  as they are. A person adds the other labels of a row afterwards.
- A run posted as error becomes a pending run with the reason `not measured`, and has no row.
- The record's verdict is set by the rule above. A record the owner waived stays waived.
- A status holds no unit and no pass condition. `--unit name=unit` gives the unit of a number;
  without it, the number takes the unit of a row of that name the record already holds.
  `--pass-condition ID=text` gives the pass condition of a record that does not exist yet.
  A number with no unit, and a new record with no pass condition, are refused, and nothing is
  written.
