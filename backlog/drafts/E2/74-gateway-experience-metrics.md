+++
id = "E2-74"
title = "Gateway: what students experience, per pool: output speed, tokens per turn, requests in flight and held"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-28", "E2-79"]
+++

## Context

Section 7.2 measures each quantity of section 4.7 from two sources where it can. The engine sees its
own work; the gateway sees what the student got, which adds the wait in front of the engine, the
hold and the network. The gateway's side, per pool and wire format and never labelled by student:

- a histogram of tokens per second for each request, from its first token to its last;
- the transcript size and the output per turn, the two assumptions under the estimate, from the
  tokens settled;
- requests in flight and held, per pool.

The time to the first token and to the first answer is issue E2-84. The per-student series and the
metrics endpoint are issue E2-79; the students with a request in the last five minutes come from its
per-student counter. The speed of a request only means something on a stream, where tokens are sent
as they come.

The objectives of section 7.3 are read from these histograms against floors held in configuration:
the median stream at 15 tokens per second or more and the slowest tenth at 10 or more (section 17).
A floor that falls between two buckets cannot be read exactly, so each floor is a bucket boundary.

## Deliverables

- [ ] `services/gateway/rhi_gateway/experience.py` observes the speed of each streamed request with at least two output tokens, per pool and wire format: its output tokens after the first, over the seconds from its first token sent to its last.
- [ ] It observes, for every request that settles, its input tokens and its output tokens, per pool and wire format.
- [ ] It gives, per pool, the requests in flight and the requests held, equal to the counts the heartbeat of issue E2-28 reports.
- [ ] The speed histogram's bucket boundaries include 10 and 15 tokens per second, none of the module's series carries a student label, and they are served with the gateway's metrics of issue E2-79.
- [ ] `services/gateway/rhi_gateway/proxy.py` and `services/gateway/rhi_gateway/app.py` report each streamed request's first and last token times and each settled request's tokens, and `tests/unit/gateway/test_experience.py` covers the module.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_experience.py` passes, and `tests/unit/gateway/test_experience.py::test_speed_per_request` observes 20 tokens per second for a stream of 401 output tokens whose first token is sent at 2 seconds and its last at 22, on a clock the test controls.
- [ ] `tests/unit/gateway/test_experience.py::test_tokens_per_turn` observes 90,000 input and 500 output tokens for a request settled with that usage, in each wire format.
- [ ] `tests/unit/gateway/test_experience.py::test_in_flight_and_held_match_the_heartbeat` finds the module's two counts equal to the heartbeat's for two requests in flight and one held.
- [ ] `tests/unit/gateway/test_experience.py::test_no_student_label` finds no alias, key or GitHub ID in any series of the module after requests from two students.

## References

- `docs/design/system-design.md`, sections 4.7, 7.2, 7.3, 17
