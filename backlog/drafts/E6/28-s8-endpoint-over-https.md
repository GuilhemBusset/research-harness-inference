+++
id = "E6-28"
title = "Spike S8 reaches the gateway over HTTPS from the gateway stack's host name"
labels = ["size:S", "kind:app"]
epic = 665
+++

## Context

The spike jobs are given the gateway's address in `RHI_SPIKE_ENDPOINT`, the gateway stack's
output, which is a bare host name. `common.gateway_address` (`spikes/common.py`) turns it into the
gateway's `https://` address, takes an `https://` address as it is, and raises ValueError for
anything else: the canary's keys are sent over HTTPS only.

`spikes/s8_stickiness.py` does not use it. Its `main` builds its `Run` with the raw value of
`environ[ENDPOINT_VARIABLE]`, and `Run` hands that value to `loadgen.replay` as the address of every
request. In a build on 2026-11-27, given the stack's host name, S8's first request failed because
its address had no scheme (httpx refused it as an unsupported protocol), after a session that did
not stay booked. Its tests in `tests/unit/spikes/test_s8.py` give it `https://gateway.example.com`,
an address it can send to as it is, so they missed it.

The other jobs read it the same way, through `common.gateway_address`:

- S3 (`spikes/s3_harness_task.py`, `run_job`) and its second session (`spikes/s3_second_session.py`,
  `run_job`), first thing after the run's needs, before the configuration is read;
- S5 (`spikes/s5_capacity.py`, `main`), before the configuration is read and before anything is
  sent, as its comment says;
- S7 (`spikes/s7_drain.py`, `main`), with its variable table saying "a host name, reached over HTTPS
  (`common.gateway_address`)", and `tests/unit/spikes/test_s7.py` giving it a host name and checking
  that every request reaches the https address;
- the dry-run class (`spikes/dry_run_class.py`), with the same words in its table as S7.

S8's `main` promises a ValueError with nothing booked, sent or posted. A non-https address must be
refused that way: read before `common.run_spike` is called, as S5 reads it before anything else,
so that no session is booked and no measurement is posted.

Suggested branch: `fix/s8-endpoint-over-https`

## Deliverables

- [ ] `spikes/s8_stickiness.py` reads RHI_SPIKE_ENDPOINT through common.gateway_address in its main, before common.run_spike is called, and builds its Run with the address that returns.
- [ ] The variable table of the docstring of `spikes/s8_stickiness.py` says that RHI_SPIKE_ENDPOINT is the gateway stack's output, a host name, reached over HTTPS through common.gateway_address, in the words of S7's table.
- [ ] `tests/unit/spikes/test_s8.py` gives the job the gateway stack's host name in one case, and an http address in another.

## Acceptance criteria

- [ ] `tests/unit/spikes/test_s8.py` shows that the job given the host name gateway.example.com builds its run with the address https://gateway.example.com, the one its requests are sent to.
- [ ] `tests/unit/spikes/test_s8.py` shows that the job given http://gateway.example.com raises ValueError without calling `common.run_spike` and without booking a session.
- [ ] `uv run --locked pytest tests/unit/spikes/test_s8.py tests/unit/spikes/test_common.py` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] The next run of the job rhi-spike-s8_stickiness sends its requests to the gateway over HTTPS and passes its first request (needs aws:deployed-environment).

## References

- `spikes/common.py`, `gateway_address`
- `spikes/s7_drain.py`, `main` and its variable table
- `spikes/s5_capacity.py`, `main`
- `tests/unit/spikes/test_s7.py`, `test_the_job_reads_of_the_pool_only_what_its_session_needs_and_reaches_the_gateway_over_https`
- Issue #677, spike S8
