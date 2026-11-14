+++
id = "E3-43"
title = "Replay: a capture states the request profile it is replayed with and the body the engine must receive"
labels = ["size:S", "kind:test"]
epic = "E3"
blocked_by = ["E2-62", "E3-23"]
+++

## Context

Each entry of a request profile is covered by a replayed capture, and a harness or engine update
that breaks the profile is caught by captures replayed in CI (sections 6.2 and 13). The replay
harness of issue E2-62 checks what the client receives. An entry's coverage needs it to check what
the engine receives too.

A capture therefore states the profile it is replayed with (the names of its entries, in order, and
the reasoning-effort mapping), not a pool of the example configuration: no example pool has a
profile until issue E3-27, and each entry's capture (issues E3-24 to E3-26) must be shown before it.
The harness builds a pool with that profile for the replay and checks the forwarded body against the
one the capture states. A capture that states neither replays as before.

Captures are built under the capture rules of issue E2-62: an example host, and no student key; a
key the replay needs is generated at run time (issue E0-16 makes a literal one fail the hygiene
scan).

## Deliverables

- [ ] `tests/replay/harness.py` replays every capture under `tests/captures/`, its subdirectories included, and, for a capture that states a request profile and the body the engine must receive, replays it for a pool with that profile and checks the forwarded body against the stated one.
- [ ] `tests/captures/README.md` documents the two fields: the profile, as entry names in order with the effort mapping, and the engine-side body.
- [ ] `tests/replay/test_replay.py` gains test_the_forwarded_body_is_checked.

## Acceptance criteria

- [ ] `tests/replay/test_replay.py::test_the_forwarded_body_is_checked` fails a capture, built in the test, whose stated engine-side body differs from what the gateway forwards with the stated profile, and passes the same capture with the body the gateway forwards.
- [ ] `uv run --locked pytest tests/replay/test_replay.py` passes with every capture of the tree, those that state no profile included.

## References

- `docs/design/system-design.md`, sections 6.2, 10, 13
