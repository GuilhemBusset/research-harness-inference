+++
id = "E2-62"
title = "Replay recorded streams through the gateway, cut everywhere"
labels = ["size:M", "kind:test"]
epic = "E2"
blocked_by = ["E2-31", "E2-77"]
+++

## Context

The gateway parses JSON and SSE for three formats, and that code decides who pays for what. It gets
the heaviest tests in the repository (section 6.2): recorded streams split at arbitrary byte
boundaries, disconnects at every event, and captures of each harness recorded against the mock
engine under an example host and scrubbed (section 10).

This issue builds the harness and the captures the mock engine can produce by itself. Captures of
the real harnesses are recorded against the deployed mock pool in phase 2, by an issue of epic E3.
Phase 2 also adds the request profile, each entry of which is covered by a replayed capture
(section 6.2, step 4): so a capture can say what the gateway must forward for the request it holds,
and the harness checks the body the engine receives against it. The request ID and the cache salt
are set by the gateway for each request and each student (section 6.2, step 4), so they are left out
of that comparison.

A capture holds no key. A key the scrubber's own tests need is generated at run time with the key
module of issue E2-01: a literal key in any file fails the hygiene scan (issue E0-16). The settlement
of every end of a request, which the replay checks, is issue E2-77.

## Deliverables

- [ ] `tests/replay/harness.py` replays a capture through the gateway's body filter, proxy and usage parsers, with the upstream's recorded answer cut at every byte position for short streams and at seeded positions for long ones, and with the client disconnecting after each event.
- [ ] When a capture gives the body the gateway must forward, the harness compares it with the body the upstream receives, leaving out the request ID and the cache salt.
- [ ] `tests/captures/README.md` defines a capture (the request as the client sent it, the body to forward if given, the upstream's answer, the usage expected) and its rules: an example host, no key, no address, no student.
- [ ] `tests/replay/scrub.py` refuses a capture that holds a key in the format of issue E2-01, a host outside example.com, or an address; its tests generate the keys they need at run time, and no file of the repository holds one.
- [ ] `tests/captures/` holds one capture per wire format, streamed and not, produced from the mock engine, and `tests/replay/test_replay.py` replays every capture of the folder.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/replay/test_replay.py` passes: for every capture and every cut, the client receives the recorded bytes, the usage equals the expected one, and the reservation is settled once.
- [ ] `tests/replay/test_replay.py::test_disconnects_are_charged` checks, for a disconnect after each event, that the charge is the tokens observed and never less than the input estimate.
- [ ] `tests/replay/test_replay.py::test_the_forwarded_body_is_checked` replays a capture built in the test whose expected body differs from what the gateway forwards by one field, and the replay fails.
- [ ] `tests/replay/test_replay.py::test_the_scrubber_refuses` gives the scrubber captures with a key, a foreign host and an address, all built in the test, and each is refused.

## References

- `docs/design/system-design.md`, sections 6.2, 10
