+++
id = "E1-11"
title = "A format for measurement records, and a test that holds every record to it"
labels = ["size:M", "kind:docs"]
epic = "E1"
blocked_by = ["E1-02"]
+++

## Context

Spikes and canaries produce numbers, and the design records them under `docs/measurements/` by a
housekeeping change (sections 8.4 and 12); the exit of phase 2 is such records (section 14). A fixed
format lets a test check them and lets later tools write them.

The format has to fit what the spikes of section 12 now measure, which is more than one number
each:

- S5 measures every quantity of section 4.7's table (output speed per stream, time to first token,
  cached share, cache capacity, preemptions and the rest), with replayed sessions of two shapes
  (hands-on, and several agents a student), at 1, 4, 8 and 16 streams and at the class's size, for
  the 8-bit and the 4-bit build. Its record replaces the estimate of section 4.7.
- S1 measures each stage of a start over 20 starts, warm and cold; S4 a load time; S6 the upgrade
  pool by the measures of section 4.7, and the xl pool only if the owner wants it.
- S2, S3, S8 and S9 are mostly yes or no, per client, per harness, per build or per step: S3 asks
  each harness, on each build, whether each step of a task works, and how much of a replayed
  session's input is cached.
- S7 mixes durations and yes or no; S11 counts diagnostics and waits for a deployment.

So a record holds rows, each a named number or a yes or no, with labels that say under which
condition it was measured. A verdict may be waived only by the owner in writing (S3), and the
record says where that waiver is.

**Runs.** One measurement is often run more than once: S1 warm and cold, S4 per loader setting, S5
per build, S3 by two jobs, sometimes on different commits. Each run has a run label its job gives,
lower-case letters, digits and hyphens (8bit, 4bit, warm, cold, stream, download), which is also
part of its statuses' context (issue E0-14). So a record lists its runs, each with its label, the
date and commit it ran on and its own verdict, and every row names the run it comes from by the
label run; a later run of one label replaces that run and leaves the others. A job that could not
measure leaves a run whose verdict is pending with the reason not measured. The verdicts stay pass,
fail, waived and pending, and the record's verdict follows from its runs by one rule, so that a
writer and a reader cannot disagree: fail when a run failed; otherwise pending when a run is pending
or there is no run yet; otherwise pass. Only the owner writes waived, whatever the runs say.

## Deliverables

- [ ] `docs/measurements/README.md` defines a record: one Markdown file per spike, canary or exercise, named after its id in lower case (s5 for spike S5), opened by TOML front matter between two `+++` lines that gives its id, the pass condition as the design states it, a verdict (pass, fail, waived or pending), its runs and its rows.
- [ ] Each run has a run label of lower-case letters, digits and hyphens, unique in the record, the date and the commit it ran on, and a verdict of pass, fail or pending; a pending run gives its reason, such as not measured.
- [ ] Each row has a quantity name, a value that is a number or true or false, a unit (none for true or false), and labels naming the conditions it was measured under, such as pool, instance type, build, streams, session shape, harness or client, among which the label run names one of the record's runs.
- [ ] The record's verdict is fail when a run failed, otherwise pending when a run is pending or there is no run, otherwise pass, except that a waived verdict, which names where the owner's written waiver is, stands whatever the runs say.
- [ ] `tests/unit/test_measurements.py` validates every record in the docs/measurements folder against that format.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_measurements.py` passes on the tree as it stands.
- [ ] `tests/unit/test_measurements.py::test_rejects_a_malformed_record` fails records, built in the test, that lack a verdict, give a number as text, carry an unknown key, are waived without naming the waiver, hold two rows with the same name and the same labels, hold a row whose run label names no run, hold a pending run with no reason, or give a verdict other than the one their runs give.
- [ ] `tests/unit/test_measurements.py::test_accepts_a_matrix` accepts a record, built in the test, whose rows share names and differ by their labels in streams, build and session shape, from two runs labelled 8bit and 4bit on two commits, and a record with no run and a pending verdict.

## References

- `docs/design/system-design.md`, sections 4.7, 8.4, 12, 14
