+++
id = "E0-14"
title = "Reporter: post the numbers of spikes and canaries, and record them"
labels = ["size:M", "kind:app"]
epic = "E0"
blocked_by = ["E0-09", "E1-11"]
+++

## Context

Spikes that need AWS run as pipeline jobs and post their numeric results through the reporter, the
same way as a deployment's verdict; a housekeeping change then records them under
`docs/measurements/` (sections 8.4 and 12). The numbers must fit the same rule as every other
status: fixed names and numbers, no free text.

A measurement is one of the validation spikes of section 12, S1 to S11, the weekly GPU canary of
section 8.2, step 6, or the dry-run class of phase 3 (section 14). Each spike's own issue decides
which numbers it posts: S5, for instance, reports several floors at 1, 4, 8 and 16 streams and at
the class's size, on two builds, which no single description of 140 characters holds. So a
measurement may span several statuses, and the names of its numbers are an enumeration its own job
declares and gives the encoder. One measurement is also often run more than once on one commit: S1
warm and cold, S4 per loader setting, S5 per build, S3 by two jobs. A commit status is replaced by
the next one with the same context, so each run carries a run label that its job gives, and the
label is part of the context. This issue builds the encoding and the record writer, not any spike;
how a job hands its numbers to the reporter is issue E0-27.

**Names this issue fixes**, which the jobs and the records share:

- The context prefix is measure/, never deploy/ or verify/: the engine reads a status under
  deploy/<stage>/ or verify/<stage>/ as a fact of that deployment (issues E0-07 and E0-29), so a
  measurement must stay out of both. A measurement's context is measure/<id>/<run label>/<part>:
  the id, the run label, and a part number from 1, present even when one status holds every
  number: measure/S5/8bit/1, measure/S5/8bit/2, measure/S1/cold/1.
- A run label is lower-case letters, digits and hyphens (8bit, 4bit, warm, cold, stream,
  download), given by the job, so that two runs on one commit never replace each other's
  statuses.
- The ids are S1 to S11, gpu-canary for the weekly canary and dry-run-class for the dry-run class,
  so that the record of each, named after its id in lower case (issue E1-11), is
  `docs/measurements/s5.md`, `docs/measurements/gpu-canary.md` or
  `docs/measurements/dry-run-class.md`. The design names neither of the last two; these are chosen
  here.
- A name is lower-case letters, digits and underscores, starting with a letter. A job declares its
  names in code, but the reporter that posts them runs elsewhere (issue E0-27) and cannot import the
  job: it checks the shape again, so that nothing shaped like an address, an ARN or a sentence can
  pass as a name.

A status's state says the run's verdict: success for a pass, failure for a fail, and error when
the job could not measure. The record format of issue E1-11 keeps the verdicts pass, fail, waived
and pending: the record writer holds an error as a run whose verdict is pending with the reason not
measured, and gives the record the verdict E1-11's rule draws from its runs; the owner alone writes
waived. A run's statuses replace that run label's rows and entry in the record and leave the other
runs as they were, so the runs of one measurement on several commits, or by several jobs, sit side
by side, each with its own commit. The record writer writes TOML with the standard library only.

## Deliverables

- [ ] `services/reporter/rhi_reporter/measure.py` encodes one run of a measurement as one or more commit statuses: a context of the form measure/<id>/<run label>/<part>, with a part number from 1 on every status; a state of success for a pass, failure for a fail and error when the job could not measure; and a description of name=number pairs, separated by spaces, in at most 140 characters.
- [ ] The measurement's id is one of S1 to S11, gpu-canary or dry-run-class; the run label is one or more lower-case letters, digits and hyphens; every name is a member of the enumeration the caller gives and is made of lower-case letters, digits and underscores, starting with a letter; every number is finite; anything else is refused before any status is built.
- [ ] It decodes the same form back into the id, the run label, the verdict and the named numbers, joining the parts of one run.
- [ ] `tools/measurements/record.py` turns the statuses of a commit, as GitHub's API returns them, into measurement records in the format issue E1-11 defines, creating or updating one file per measurement in the folder docs/measurements/, with the standard library only: each run becomes the run of that label, with that commit and the date of its statuses, whose rows carry its run label, replacing the run of the same label and keeping the others; a run posted as error becomes a pending run with the reason not measured and no row.
- [ ] `tests/unit/reporter/test_measure.py` and `tools/measurements/tests/test_record.py` cover them, and `pyproject.toml` adds `tools/measurements/tests/` to the test paths.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_measure.py` passes: encoding then decoding returns the same run label and numbers, a run too long for one description is split into parts measure/<id>/<run label>/1, /2 and so on and joined back, two run labels of one id give contexts that differ, a name outside the given enumeration, a name of another shape, a run label with an upper-case letter, a slash or a space, and an unknown id are refused, and no description exceeds 140 characters.
- [ ] `uv run --locked pytest tools/measurements/tests/test_record.py tests/unit/test_measurements.py` passes: a recorded list of statuses becomes records that the format's own test accepts; two runs of S5 on one commit, labelled 8bit and 4bit, become two runs of one record; a later commit's 8bit run replaces the 8bit run and keeps the 4bit one; and a run posted as error becomes a pending run with the reason not measured.

## References

- `docs/design/system-design.md`, sections 8.2, 8.4, 12, 14
