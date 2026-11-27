+++
id = "E6-29"
title = "Spike S8 holds its load under the per-address rate rule, and says what ended it"
labels = ["size:M", "kind:test"]
epic = 665
+++

## Context

A build of `rhi-spike-s8_stickiness` on the mock pool, on 2026-11-27, booked its two instances,
held its load of 16 streams as 6 canary students, and failed about 100 seconds later with "the load
ended before two instances served: a held stream was refused". The gateway's own log for that
window shows about 3,090 requests on the mock pool, all answered 200, with no cause, then none: the
gateway refused nothing. The public load balancer answered 16 requests itself in the minute the load
ended, one for each held stream, and the web ACL's per-address rule blocked exactly those 16, and
nothing else in the two weeks before; the job's traceback ends in that line, with no other error.

**The cause.** Each `HeldStream` sends one short stream after another with no pause, and the mock
streams its canned turn of 21 tokens at 50 a second, so the 16 streams send about 28 requests a
second, all from the job's one address. The public load balancer's web ACL blocks a source address
over the per-address rate rule: `rate_limits.per_address` of the configuration, 2,000 requests per
300 seconds by default (system design, section 17; `src/rhi/network/waf.py`). The job crosses it in
about 70 seconds; once WAF acts, it answers 403 itself and the gateway never sees the request. Each
stream's next request is answered 403, `loadgen.replay` ends a session at its first request not
answered 200, so all 16 end within a moment, the replay returns with no error, and `two()` finds the
task done and calls that a refusal. Reproduced locally: the real `loadgen.replay`, the real
`HeldStream` sessions and the mock engine, behind a stub that answers 403 from 30 seconds after 300
seconds of the stub's clock first hold more than 2,000 requests, forwarded 2,840 requests answered
200, then every stream ended on one 403, and `Run.two_instances` raised that same line, which names
no status. The limit stays as it is: only this job's back-to-back streams of the mock exceed it.

**What the job says.** `two()` reports every end of the load as "a held stream was refused", and
reads neither the records nor the statuses. When a connection fails, `loadgen.replay` raises and
its task group ends every stream; the `finally` of `two_instances` then awaits the task, and the
replay's error replaces the job's line. A measured student's replay that ends early fails the run
with "a request of a measured student was not answered 200" (`_all_answered`), which names nothing
either. A stream's records carry what ended it: its status, the gateway's cause when it is one of
its own (`rhi_common.causes.Cause`), and, in a replay given `keep_failures`, the fixed kind of a
connection's failure (`loadgen.Failure`); none of them carries text of an answer or of an error.

**The fix.** The held load only has to show both instances serving: the pool has what its session
asks for, and no rule reads its load (section 5.1). So its streams pause between their requests,
enough that the load uses at most half of `per_address` in any 300 seconds, leaving the other half
to the measured students' requests that follow within the same window and to anything else sent
from the job's address. The job already reads the deployed configuration (`config.read_ssm()`) for
the pool's `target_concurrency`; `per_address` comes from the same read. `loadgen.replay` takes its
clock and its `sleep`, so a test can run the load on a clock of its own.

Spike S7 does not share the cause: it sends one slow stream at a time (`slow_stream`), each a single
request whose body trickles, never a replay of short streams, and it already records a refused answer
as its status and an error as one of its fixed kinds.

Suggested branch: `fix/s8-load-under-rate-rule`

## Deliverables

- [ ] `spikes/s8_stickiness.py` gives the job the deployed configuration's per-address rate limit, and its held load spaces each stream's requests so that no 300 seconds hold more than half of that limit of the load's requests.
- [ ] When a held stream ends before the job stops the load, the job stops the load and the run fails with one line that names each way a held stream ended, as its status, the gateway's cause or the kind of its connection's failure, with how many streams ended that way.
- [ ] That line holds no text of an answer or of an error, and the job no longer says a held stream was refused when none was.
- [ ] A measured student's request that is not answered 200 fails the run with a line that names its status, the gateway's cause or the kind of its failure, in place of saying only that it was not answered 200.
- [ ] The description of `spikes/s8_stickiness.py` says, in step 1 and where it lists what comes from the deployed configuration, that the load is spaced under the per-address rate rule and what ends the run when a held stream ends.
- [ ] `tests/unit/spikes/test_s8.py` runs the job's held load through the real replay of the load generator and a stub transport that answers 403 to every request once 300 seconds of the replay's clock hold more than the per-address limit.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s8.py` passes: through that stub, with a per-address limit of 2,000 and a target concurrency of 8, none of the job's 16 held streams has a request answered 403 or ends before the job stops the load, once the test's pool shows both instances serving 600 seconds after the load began on the replay's clock, and no 300 seconds of that clock hold more than 1,000 of the load's requests.
- [ ] `uv run --locked pytest tests/unit/spikes/test_s8.py` passes: a held stream answered 403, one whose stream carries the gateway's cause concurrency_limit, and one whose connection fails each end the run before two instances serve, with a line that names 403, concurrency_limit or read_error, and that holds none of the text the stub put in the answer or the error; a measured student's request answered 403 fails the run with a line that names 403.
- [ ] `uv run --locked ruff check spikes/s8_stickiness.py tests/unit/spikes/test_s8.py` and `uv run --locked ty check spikes/s8_stickiness.py tests/unit/spikes/test_s8.py` report nothing.
- [ ] In the account, a build of the job on the mock pool holds its load until both instances serve, and the web ACL's per-address rule blocks none of its requests (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.1, 5.5, 6.5, 12 and 17
- `spikes/s8_stickiness.py`
- `spikes/loadgen.py`, `replay`, `Record` and `Failure`
- `src/rhi/network/waf.py`, the per-address rule and its 300-second window
- `src/rhi/config.py`, `RateLimits`
- `tests/unit/spikes/test_s8.py`
