# Evaluation bundles

Reviewed public evidence of agent-session evaluations, one folder each. An operator writes a
folder here only on purpose, with an explicit export, and it lands through a normal pull request
([design](../design/agent-session-evaluation.md), "Reproducing and comparing runs"):

```sh
scripts/evaluate.sh export .cache/eval/<run> --to docs/evaluations/<name>
```

The export writes a new folder and never replaces one. It reads the saved bundle, holds every
value to the evaluation's public format (`tools/evaluate/contracts.py`), regenerates the derived
files and runs the hygiene scanner over every file before it writes anything
(`tools/evaluate/report.py`). A folder holds:

| File | What it is |
| --- | --- |
| `manifest.json` | The run: its planned workload, the settings requested, what ran it, the settings observed |
| `checkpoints.jsonl` | One line for each attempt of a cell: its record, the seal of its observations, the scheduler's tally, the serving hosts |
| `observations/attempt-<n>.jsonl` | The request, trial and host-sample records of attempt `n` |
| `summary.json`, `report.md` | The scorecard, derived from the files above |
| `summary.sha256` | The digest of `summary.json`, which the experiment issue's comment carries |

Times are milliseconds since the start of the run, and students, hosts, trials and cells are
ordinals local to it. There is no model text, path, address, alias or credential anywhere in a
bundle. Anyone can check a folder offline:

```sh
scripts/evaluate.sh report docs/evaluations/<name>
git diff --exit-code docs/evaluations/<name>
```
