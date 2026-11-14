+++
id = "E3-14"
title = "What the spikes share: timing, the workspace, the canary students' keys, and posting their numbers"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E0-27", "E1-16", "E2-56"]
+++

## Context

Spikes that need AWS run as pipeline jobs and post numeric results through the reporter; the owner
starts the ones that cost real money (sections 8.4 and 12). Spikes read what they measure from the
Prometheus workspace, where every node pushes its metrics (section 7.1), and reach the pools only
through the gateway, with canary students: the scaler stays the only writer of a pool's size
(section 5.1). This issue is the code every spike shares; the jobs that run them are issue E3-45.

**Posting.** A job cannot post a commit status itself: it hands its numbers to the reporter as the
measurement event of issue E0-27, built by `services/reporter/rhi_reporter/measure.py`, sent with
PutEvents to the bus named in the environment variable RHI_MEASUREMENT_BUS, for the commit
CodeBuild gives in CODEBUILD_RESOLVED_SOURCE_VERSION. Each spike declares the enumeration of its
numbers' names; a name is lower-case letters, digits and underscores (issue E0-27).

**The run label.** One event is one run, and the event carries the run label the job gives,
lower-case letters, digits and hyphens, which keeps two runs of one measurement apart in their
statuses and in the record (issues E0-14, E0-27 and E1-11): spike S1 warm and cold, S4 per loader
setting, S5 per build, S3 by two jobs. The design gives no rule for choosing it, so this issue sets
one: the label is the environment variable RHI_RUN_LABEL, which the owner sets when starting the
job (a CodeBuild start may override a project's variables), and without it the job's name with its
underscores replaced by hyphens (s3-harness-task, s3-second-session), so that two jobs never share a
label by default. A label of another shape stops the job before it measures anything. A spike that
raises before it has its numbers still sends one event, with the verdict error and no number, which
the record holds as a pending run with the reason not measured (issue E1-11).

**Records.** Each spike's issue commits its record in the format of issue E1-11 with its id, its
pass condition, the verdict pending, no run and no row: rows hold measured values, and each names
its run. The rows a spike's issue describes are the ones its job's numbers become; the record writer
of issue E0-14 adds each run, with its commit, date, verdict and rows, in a housekeeping change.

**The canary students.** One canary key is not enough. A student may run 4 requests at once
(section 17) and the cache salt is per student (section 6.2), so spike S5 replays each session as
its own student, up to the class's size; spike S2 needs a student whose daily budget is spent. So
the spikes read their keys from one Secrets Manager secret of their own, whose fixed name and content
are defined once in `services/common/rhi_common/spike_keys.py`: a list of keys of students with the
default budget, and one key of a student whose daily budget is 0, which the gateway answers 402 with
the cause budget_reached from its first request. The admin CLI creates that secret and fills it, as
it does the verification's canary secret (issue E2-56): the owner runs one command, the command
creates the roster entries under fixed aliases, issues their keys, writes them into the secret
(creating it if it does not exist) and prints none of them. The pipeline stack never creates it.

**Tests and keys.** No test or record holds a literal student key: a key a test needs is generated
at run time by the key module of issue E2-01 (issue E0-16 makes a literal one fail the hygiene scan).

**Tools.** `spikes/` joins the scope of ruff, ty and pytest's import path. Its tests live under
`tests/unit/spikes/`, so pytest collects nothing under `spikes/`. The SDK scripts of spikes S2, S3 and
S6 use the OpenAI and Anthropic Python SDKs, which no service needs, so this issue pins them in a
dependency group of the root project.

## Deliverables

- [ ] `spikes/common.py` gives the spikes timing, the median and any percentile of samples, reading a series from the Prometheus workspace with a signed request, and the pass condition as a function of the numbers.
- [ ] `spikes/common.py` reads the canary students' keys from the spikes' secret, and sends a spike's numbers and verdict as the measurement event of issue E0-27, with the names the spike declares and the run label of the Context, to the bus RHI_MEASUREMENT_BUS names, for the commit CODEBUILD_RESOLVED_SOURCE_VERSION names; a spike that raises before it has its numbers sends the verdict error and no number.
- [ ] `services/common/rhi_common/spike_keys.py` defines the spikes' secret's fixed name and its content, the keys of students with the default budget and the key of one student whose daily budget is 0, and reads and writes that content.
- [ ] `admin/rhi_admin/spikes.py` adds a command that, for a number of students it is given, creates canary roster entries under fixed aliases with one more whose daily budget is 0, issues their keys and writes them into the spikes' secret, creating the secret if it does not exist, without printing a key; running it again replaces the keys.
- [ ] `pyproject.toml`, `ruff.toml` and `ty.toml` put `spikes/` in the scope of ruff and ty and on pytest's import path, and `pyproject.toml` pins the OpenAI and Anthropic Python SDKs exactly in a dependency group the tests install, which `uv.lock` records.
- [ ] `tests/unit/spikes/test_common.py` covers the shared code, and `tests/unit/admin/test_spike_keys.py` the command, against stubbed clients and with keys generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_common.py` passes: the 90th and 95th percentiles of known samples are right, a recorded answer of the workspace reads back as its series, and a set of numbers sent to a stubbed bus decodes, with the measure module, into the same numbers, verdict, commit and run label.
- [ ] `tests/unit/spikes/test_common.py::test_the_run_label` finds the label RHI_RUN_LABEL gives when it is set, s3-harness-task for the job s3_harness_task when it is not, a label with an underscore or a capital refused before anything is measured, and a spike that raises sending one event with the verdict error and no number.
- [ ] `uv run --locked pytest tests/unit/admin/test_spike_keys.py` passes: the command run for 3 students leaves one secret with 3 keys of the default budget and one of budget 0, run again replaces all four, and no key appears in its output.
- [ ] `scripts/check.sh fast` exits 0 with `spikes/` in the scope of ruff and ty.
- [ ] The owner runs the spikes' command of the admin CLI for the class size before the first paid spike (needs aws:account).

## References

- `docs/design/system-design.md`, sections 5.1, 6.2, 7.1, 8.4, 12, 17
