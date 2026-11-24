+++
id = "E3-52"
title = "Spike S5: count the failed requests by cause, post them, and fail a job whose class-size runs failed more than a floor of them"
labels = ["size:M", "kind:test"]
epic = 5
+++

## Context

Spike S5's first full run on the coursework pool (run `8bit-baseline-1`) read pass while about
four requests in five failed: the lifecycle agent restarted a busy engine again and again (issue
E3-51), and each restart failed every request in flight. The verdict (`CapacitySpike.verdict` and
`passes` in `spikes/s5_capacity.py`) reads only the time to first token, the stream speeds and the
preemptions, and those come from the requests that got an answer. It never reads the requests that
did not, so the survivors of a failing run can pass it.

The job also undercounts and hides the failures. `experience_of` counts as failed only a record
not answered 200, so a stream answered 200 and then ended by the gateway's error event, as an
engine restart ends one, counts as answered. And it posts one count, `failed_requests`, with
neither the cause nor the status of any failure, though each record of the load generator carries
both (`loadgen.Record`, its status and the gateway's cause).

Each of the gateway's causes has one status (`rhi_common.causes.Cause`), so counts by cause, with
the requests answered 200 and then ended by an error event counted apart, are counts by status and
cause. The names follow those of the other quantities, so each is posted for each shape and
condition (`failed_engine_error_several_agents_class`):

- failed_share: the failed requests in percent of the requests sent, the number the verdict reads;
- failed_ followed by a cause's name (failed_engine_error, failed_capacity_unavailable), one for
  each of the eleven causes: the failed requests that named that cause, in their answer or in an
  error event of their stream;
- failed_in_stream: the failed requests answered 200 whose stream then carried the gateway's error
  event, which are also counted under their cause;
- failed_without_cause: the failed requests that named no cause of the gateway's, such as an
  answer of the edge.

The counts by cause and failed_without_cause add up to failed_requests. A count of these last
three kinds that is zero is not posted, to keep postings short; failed_requests and failed_share
are posted for every run that sent a request.

The floor is read from the deployed configuration as the other three are, in a new key,
target_failed_requests_percent, a percentage above 0 and at most 100, taken as
`cached_share_flag_percent` is. The design gives no floor for S5's failed requests. The default
here, 0.5, is the share of failures that section 7.3's objective for the gateway's answers allows
(99.5% answered); that objective counts only the gateway's own faults, while S5 counts every
failure of a load nothing else shares, so the owner confirms or replaces the value, and the
design's sections 12 and 17 then name it. A failed share above the floor is a miss like the
others: it fails the job whatever else is missing. The upgrade pool's spike S6 reads the same
verdict (issue E5-05).

Suggested branch: `fix/s5-failed-requests`

## Deliverables

- [ ] `spikes/s5_capacity.py` counts as failed every request whose record is not answered 200 or names one of the gateway's causes, a stream answered 200 and ended by the gateway's error event among them, and posts that count as failed_requests.
- [ ] For each run it posts failed_share and, each only when above zero, the count of failed requests for each cause of the gateway's vocabulary, failed_in_stream and failed_without_cause, under the names the Context gives.
- [ ] Its verdict fails the job when the class-size run of either shape has a failed_share above target_failed_requests_percent, whatever else is missing, and posts it as not measured when such a run has no failed_share and misses no floor.
- [ ] `src/rhi/config.py` holds target_failed_requests_percent, a percentage above 0 and at most 100 whose default is 0.5, which `config.example.toml` gives beside the three floors and `tests/unit/test_config.py` covers.
- [ ] `docs/measurements/s5.md` names the new quantities in its table of what the job posts, and the failed-share floor in its pass_condition and in its section on the verdict.
- [ ] `tests/unit/spikes/test_s5.py` and `tests/unit/spikes/test_s5_runs.py` cover the verdict and the counts, with stand-in records, gateway and workspace.

## Acceptance criteria

- [ ] `tests/unit/spikes/test_s5.py` holds that a job whose class-size runs meet the other floors fails when the class-size failed_share of either shape is 1 and passes when both are 0.5, and that a configuration whose target_failed_requests_percent is 2 passes a failed_share of 1.5 that the default fails.
- [ ] `tests/unit/spikes/test_s5_runs.py` holds that a run of 100 requests whose stand-in replay answers one 502 with engine_error, one 200 whose stream ends with the engine_error event and one 503 with capacity_unavailable posts failed_requests 3, failed_share 3, failed_engine_error 2, failed_in_stream 1, failed_capacity_unavailable 1, and no other count of failures.
- [ ] `uv run --locked pytest tests/unit/spikes tests/unit/test_config.py tests/unit/test_measurements.py` passes.
- [ ] The owner keeps 0.5% as the default of target_failed_requests_percent or names another, and the design's S5 row of section 12 and its section 17 then name that floor (needs human:owner-decision).

## References

- `spikes/s5_capacity.py`, `experience_of`, `figures_of`, `Floors` and `CapacitySpike`
- `spikes/loadgen.py`, `Record`
- `services/common/rhi_common/causes.py`
- `docs/measurements/s5.md`
- `docs/design/system-design.md`, sections 7.3, 12 and 17
