+++
id = "E3-42"
title = "Spike S5, the verdict: the floors from configuration, and one record that holds the whole matrix"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E3-19", "E4-31"]
+++

## Context

Spike S5's runs are issue E3-19. This issue judges them and records them.

Pass, at the class's size over the session's instances: a 95th-percentile time to first token under
5 seconds, first turns apart; the median stream at 15 tokens per second or more and the slowest
tenth at 10 or more; no preemptions. The floors are provisional and held in configuration, with
those values as defaults (sections 7.3 and 17), in the keys issue E4-31 adds,
target_ttft_p95_seconds, target_stream_median_tokens_per_second and
target_stream_p10_tokens_per_second, which the objectives and the inference dashboard read too. If it fails: the levers of section 4.7 in order,
then fewer streams and more instances per session, or the upgrade pool; none of that is drafted. The
record replaces section 4.7's table, and the dry-run class of phase 3 is judged against it.

The record format holds rows that share a name and differ by their labels (issue E1-11), so one
record holds the whole matrix: each row is a quantity, labelled with the pool, the instance type, the
build, the lever, the session shape and the number of streams. The reporter posts numbers whose names
are an enumeration the spike's own job declares, in lower-case letters, digits and underscores
(issues E0-14 and E0-27).

The class size is the owner's decision 5. The upgrade pool's spike S6 reads the same floors through
the same verdict (issue E5-05).

Agents build and test the verdict and the record. Running the spike needs the account and is the
owner's: it is an obligation here, never a pass.

## Deliverables

- [ ] `spikes/s5_capacity.py` judges the runs at the class size against the three floors and the absence of preemptions, reading the floors from the configuration keys target_ttft_p95_seconds, target_stream_median_tokens_per_second and target_stream_p10_tokens_per_second of issue E4-31, whose defaults are those of section 17.
- [ ] It declares the names of the numbers it posts, one per quantity and condition of its record, posts them and the verdict with the code of issue E3-14, and `docs/measurements/s5.md` is the record, with a pending verdict until the spike has run.
- [ ] `tests/unit/spikes/test_s5.py` covers the verdict, the record and where the floors come from.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s5.py tests/unit/test_measurements.py` passes: recorded numbers at the class size with a 95th percentile of 4 seconds, a median stream of 16 tokens per second, a slowest tenth of 11 and no preemption pass; a 95th percentile of 6 seconds, a median of 14, a slowest tenth of 9, or one preemption each fail; and the pending record is valid.
- [ ] `tests/unit/spikes/test_s5.py::test_the_record_holds_the_matrix` writes the rows of recorded runs at two stream counts, two shapes and two builds into one record that the format's own test accepts.
- [ ] `tests/unit/spikes/test_s5.py::test_the_floors_come_from_configuration` gives the verdict a configuration whose target_stream_median_tokens_per_second is 17 and finds a median stream of 16 failing.
- [ ] The spike is run on the coursework pool with the 8-bit and the 4-bit build, and `docs/measurements/s5.md` records what one instance serves, which is an exit criterion of phase 2 (needs gpu:capacity).
- [ ] The levers of section 4.7 are each run and recorded in `docs/measurements/s5.md` against the baseline (needs gpu:capacity).
- [ ] The owner gives the class size, decision 5 of the design, against which the verdict is read (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.7, 7.3, 12, 14, 15, 17
