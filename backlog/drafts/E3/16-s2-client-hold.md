+++
id = "E3-16"
title = "Spike S2: does each client survive the hold, and how does it show a 402"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E2-54", "E3-38", "E3-45", "E3-46"]
+++

## Context

The hold is sized from the clients (section 5.3): Claude Code tolerates a stream of pings for about
12 minutes; Codex's idle timer ignores SSE comments and defaults to 300 seconds, which is why `rhi
login` writes a profile with a 15-minute stream timeout; the SDKs time a whole call out at 10
minutes. How each client behaves through a full hold is untested (evidence E12). Spike S2: does each
client survive a 7-minute held stream and a 4-minute held call, Codex with its default timer and
with the profile `rhi login` writes (section 12)? Pass: no client aborts with the profile in place.
If it fails: a shorter hold for that client, and `rhi wait` documented. The mock pool is enough: its
start delay makes the hold last. The delay is the mock pool's field start_delay_seconds, 0 by default,
which the pool's release passes to the mock as its flag (issue E3-46, through the engine flags of
issue E3-38); the owner sets it in the configuration in SSM for the run and removes it afterwards.

The two budget causes are answered 402, a status the SDKs do not retry, so that they reach the
student at once. How each harness shows a 402 is part of this spike (section 6.2, step 8); the
canary student whose daily budget is 0 (issue E3-14) gets one on its first request.

**Harness versions.** The student guide notes the harness versions the endpoint was tried with
(section 13), so the spike pins them and its record keeps them. The design names no version. The
pins are, for Claude Code and for Codex, each one's npm package (`@anthropic-ai/claude-code`,
`@openai/codex`) at the newest release published at least 7 days before the issue is built, the
cooling-off section 17 asks before an update is adopted; for the SDK scripts, the SDKs pinned by
issue E3-14.
`spikes/harnesses.py` holds the two npm pins and installs them, so that spike S3 (issues E3-41 and
E3-35) runs the same versions.

Agents build and test the job and the record. Running it needs the account and is the owner's or the
pipeline's: it is an obligation here, never a pass.

## Deliverables

- [ ] `spikes/harnesses.py` pins Claude Code's and Codex's npm packages at exact versions, each the newest release at least 7 days old when this issue is built, installs them into the job, and reports the version of each harness and SDK it runs.
- [ ] `spikes/s2_client_hold.py` runs, against the deployed mock pool whose start delay, its field start_delay_seconds, makes a hold last just under 7 minutes for a stream and 4 minutes for a non-streaming call: Claude Code, Codex with its default timer, Codex with the profile of issue E2-54, an OpenAI SDK script and an Anthropic SDK script.
- [ ] It records, per client and kind of call, the client's version, whether the call completed, and after how many seconds a client that gave up did so.
- [ ] It sends through each client one request with the key of the canary student whose daily budget is 0, which the gateway answers 402 with the cause budget_reached, and records whether the client retried it and whether it showed the user that the budget was spent.
- [ ] It declares the names of the numbers it posts, one per row of its record (per client and kind of call: completed, and the seconds before giving up; per client: the 402 retried, the 402 shown), posts them and the verdict with the code of issue E3-14, and `docs/measurements/s2.md` is the record, its rows labelled with the client, its version and the kind of call, with a pending verdict until the spike has run.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s2_client_hold, which runs `spikes/s2_client_hold.py`.
- [ ] `tests/unit/spikes/test_s2.py` covers the verdict on recorded outcomes and the pins of the harnesses.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s2.py tests/unit/test_measurements.py` passes: outcomes in which only Codex with its default timer aborts pass, outcomes in which a client aborts with the profile fail, and the record has one line per client for the 402.
- [ ] `tests/unit/spikes/test_s2.py::test_the_harnesses_are_pinned` fails when a version in `spikes/harnesses.py` is a range or a tag instead of an exact version.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The spike is run against the deployed mock pool, with its start_delay_seconds set in the configuration in SSM for the run and removed after it, and `docs/measurements/s2.md` records the hold behaviour of each client, which is an exit criterion of phase 2 (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.3, 6.2, 12, 13, 14
- `docs/design/evidence.md`, E12
