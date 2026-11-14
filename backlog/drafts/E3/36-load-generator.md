+++
id = "E3-36"
title = "A load generator that replays agent sessions through the gateway, each as its own student"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E2-62", "E2-63", "E3-14"]
+++

## Context

Spike S5 measures what one instance serves with replayed agent sessions of both shapes, hands-on and
several agents a student (section 12); spike S3 replays a recorded Claude Code session to read how
much of its input the cache served; spike S6 runs the same measurement on the upgrade pool (issue
E5-05). Tools that replay multi-turn agent sessions with growing transcripts exist, while `vllm bench
serve` sends independent requests (evidence E31). This project needs the replay to go through the
gateway, as students do, so that the per-student cache salt, the request profile, the hold and
affinity are all in the path, and so that it measures what a student gets.

What an agent sends (section 4.7; evidence E31): a request re-sends the whole transcript, so input
outweighs output about a hundred to one, and nine-tenths of each request repeats the previous one.
The estimate of section 4.7 assumes transcripts of 50,000 to 100,000 tokens, with a row for 150,000;
turns that add 1,000 to 5,000 input tokens and produce about 500; and 5 to 10 seconds of tools
between turns. Hands-on, one agent runs for each student; in the second session's shape, students
run several agents, one or two streams a student almost all the time. The cache salt is per student,
and a student may run 4 requests at once (section 17), so each replayed session is its own student,
with its own canary key from the spikes' secret (issue E3-14). The tests give it keys generated at
run time by the key module of issue E2-01, which the local stack's roster knows; no test holds a
literal key (issue E0-16).

A session is either recorded, in the capture format of issue E2-62 (Claude Code's sessions are
recorded by issue E3-21), or synthetic, built to given sizes so that the shapes of section 4.7 are
reproducible. What it measures is what the gateway's own histograms measure (section 7.2): the time
to the first token, and to the first token that is not reasoning, and the speed of each request from
its first token to its last.

## Deliverables

- [ ] `spikes/loadgen.py` replays sessions through the gateway, a given number at once, each as its own canary student, sending each session's requests in order with the transcript grown by the previous turns, and pausing between turns as the session says.
- [ ] A session is a recorded one in the capture format of issue E2-62, or a synthetic one built to given sizes: the transcript at its start, the input each turn adds, the output each turn asks for, and the pause between turns.
- [ ] Two presets give the shapes of section 4.7: hands-on, one session a student with transcripts of 50,000 to 100,000 tokens, turns adding 1,000 to 5,000 input tokens and asking for about 500 output tokens, and 5 to 10 seconds between turns; and several agents a student, two such sessions a student at once.
- [ ] It records for each request: the student, whether it was the session's first turn, the seconds to the first token and to the first token that is not reasoning, the output tokens per second from the first token to the last, and the input, cached input and output tokens the answer reported.
- [ ] `tests/unit/spikes/test_loadgen.py` runs it against the local stack of issue E2-63, with the mock engine in process and keys generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_loadgen.py` passes: eight synthetic sessions against the local stack give eight students' records, a session's second request carries the first one's transcript plus the turn between, and only each session's first request is marked as a first turn.
- [ ] `tests/unit/spikes/test_loadgen.py::test_a_recorded_session_replays_in_order` replays a capture of several requests, built in the test, and finds them sent in their recorded order with their recorded pauses on a clock the test controls.

## References

- `docs/design/system-design.md`, sections 4.7, 7.2, 12, 17
- `docs/design/evidence.md`, E31
