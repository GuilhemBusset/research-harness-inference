+++
id = "E0-27"
title = "Reporter: accept a measurement from a spike or canary job, and say which commit it goes on"
labels = ["size:M", "kind:infra"]
epic = "E0"
blocked_by = ["E0-12", "E0-14"]
+++

## Context

Spikes that need AWS run as pipeline jobs and post their numeric results through the reporter
(sections 8.4 and 12); so do the weekly GPU canary (section 8.2, step 6; issue E3-13) and the
dry-run class of phase 3 (issue E4-26). Issue E0-14 encodes a measurement as commit statuses under
the prefix measure/; the reporter of issue E0-12 posts statuses for pipeline events only. This
issue is the path between a job and the reporter.

**A job cannot post a status itself, and must not be able to.** The only GitHub credential in the
account is the status reporter App's key, in the reporter's secret (issue E0-12), and only the
reporter's function may read it. A job's role is never given that secret, and a job holds no other
GitHub credential, so it hands its numbers to the reporter instead, as an event.

**The transport.** The pipeline stack gets an EventBridge event bus for measurements only, and a
rule on it that invokes the reporter's function for events of source rhi.measurement. A job sends
one event per measurement with PutEvents. A dedicated bus keeps a job's grant to one resource and
keeps measurement events apart from the pipeline's own events. `src/rhi/pipeline/reporter.py` gives
one function that every job's construct calls (the spike jobs of issue E3-45, the canary of issue
E3-13, the dry run of issue E4-26): it lets the job's role put events on that bus and nothing else,
and gives the job the bus's name in the environment variable RHI_MEASUREMENT_BUS.

**The event**, built by the measure module so that the job and the reporter agree on it: its detail
holds the commit the job ran from, the measurement's id, the run label the job gives (issue E0-14:
lower-case letters, digits and hyphens, such as 8bit or cold), its verdict (pass, fail or error), the
enumeration of names the job declares, and the named numbers. One event is one run. The reporter
checks every part again with the measure module (an id of the fixed set, a run label of the allowed
shape, names of the declared enumeration and of the allowed shape, finite numbers) and refuses the
whole event, posting nothing, if any part fails. Nothing else in the event is read.

**Which commit the statuses go on: the commit the job ran from.** A job runs the code of a commit of
`main`, which CodeBuild gives the build as its resolved source version
(CODEBUILD_RESOLVED_SOURCE_VERSION); the job puts that SHA in the event. The numbers then sit on
the commit whose code produced them, and the housekeeping change that records them (issue E0-14's
record writer) reads them from that commit. The reporter refuses a commit that is not 40
hexadecimal characters; GitHub itself refuses a commit that is not in this repository, which the
reporter logs as a refused call (issue E0-10).

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/pipeline/reporter.py` adds to the pipeline stack an event bus for measurements and a rule on it that invokes the reporter's function for events of source rhi.measurement, `src/rhi/pipeline/stack.py` builds them with the reporter, and `tests/synth/test_reporter.py` checks them.
- [ ] `src/rhi/pipeline/reporter.py` gives one function that lets a job's role put events on that bus and do nothing else, and sets the bus's name in the job's environment variable RHI_MEASUREMENT_BUS; it grants nothing on the reporter's secret.
- [ ] `services/reporter/rhi_reporter/measure.py` builds and reads the measurement event, whose detail holds the commit the job ran from, the measurement's id, the run label, its verdict, the enumeration of names the job declares and the named numbers; `tests/unit/reporter/test_measure_event.py` covers it.
- [ ] `services/reporter/rhi_reporter/handler.py`, for an event of the measurement bus, encodes it with the measure module and posts the statuses on the commit the event names, and posts nothing for an event whose commit is not 40 hexadecimal characters or whose id, run label, verdict, names or numbers the measure module refuses; `tests/unit/reporter/test_handler.py` covers it.
- [ ] `tests/policy/test_pipeline.py` fails when a role other than the reporter function's may read the reporter's secret, and when a role may put events on the measurement bus and also read that secret.
- [ ] Every cdk-nag finding `src/rhi/pipeline/reporter.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_reporter.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_measure_event.py tests/unit/reporter/test_handler.py` passes: an event built by the measure module for S5 with the run label 8bit, whose numbers need two statuses, gives on a stub client statuses under measure/S5/8bit/1 and measure/S5/8bit/2 on the event's commit, which decode back to the same numbers, and a second event for S5 with the run label 4bit gives contexts under measure/S5/4bit/.
- [ ] `tests/unit/reporter/test_handler.py::test_a_bad_measurement_posts_nothing` shows an event with a name outside its declared enumeration, one with an unknown id, one whose run label holds a slash, one whose commit is not a SHA and one with text where a number belongs each leaving the stub client without a call.
- [ ] `uv run --locked pytest tests/synth/test_reporter.py tests/policy/test_pipeline.py` passes: the rule matches the measurement bus only, a role built in the test and given the grant may put events on that bus and read no secret, and the reporter's role is as before.
- [ ] A measurement sent by a job in the account appears as statuses under measure/ on the commit the job ran from (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2, 8.4, 12
- `docs/adr/0001-software-factory.md`, amendment 4
