+++
id = "E3-21"
title = "Record what each harness really sends, against the mock pool, for the replay tests and the request profile"
labels = ["size:S", "kind:test"]
epic = "E3"
blocked_by = ["E1-16", "E2-54", "E2-62", "E2-63", "E2-90", "E3-27", "E3-43"]
+++

## Context

The gateway's replay tests use captures of each harness recorded against the mock engine under an
example host and scrubbed (sections 6.2 and 10). The mock engine's own captures exist since issue
E2-62. The real harnesses' requests (Claude Code's, Codex's, an SDK script's) can only be recorded by
running those harnesses, which is done against the deployed mock pool.

The request profile needs these captures most (section 6.2, step 4). Each of its entries is covered
by a replayed capture, and a harness or engine update that breaks the profile is caught by captures
replayed in CI (section 13). So Claude Code's captures must hold what the profile rewrites: its
default reasoning effort, `high`; system messages placed mid-conversation; and a document block,
which is how it sends a PDF. They also hold an image inside a tool result, which the model must read
(section 4.2). The entries are issues E3-24 to E3-26, and a capture can state the body the engine
must receive for a pool (issue E3-43).

A real session also carries what the hygiene rules forbid in the repository: the recording machine's
home directory and user name, which Claude Code puts in its system prompt and its tool results
(AGENTS.md, public-repository hygiene), and the student key the harness sends. The recorder replaces
them before anything is written. A literal student key in a test or a capture fails the hygiene scan
once issue E0-16 lands, so the recorder's tests generate the keys they send at run time with the key
module of issue E2-01, and no capture holds one. The load generator of spikes S3 and S5 replays
recorded sessions in the same format (issue E3-36).

A capture of a harness's request through the request profile states the profile it is replayed with
(issue E3-43), so the captures recorded here replay through the coursework pool's profile (issue
E3-27) without a pool of the example configuration. The recorder itself needs neither; the owner's
item does, and it reaches the deployed mock pool through the public endpoint (issue E2-90), which
is why this issue comes after all three.

## Deliverables

- [ ] `tests/replay/record.py` is a local recorder a person puts between a harness and the endpoint: it saves each request of a session, in order, and its answer, in the capture format, replaces the host by the example host, removes the student key wherever it appears, and passes the result through the scrubber of issue E2-62.
- [ ] Before it writes a capture, it replaces the recording machine's home directory and user name, wherever they appear in a request or an answer, by fixed neutral values.
- [ ] `tests/replay/test_record.py` covers the recorder against the local stack of the smoke tests, with keys generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/replay/test_record.py` passes: a request sent through the recorder to the local stack is saved as a capture that the scrubber accepts and that the replay harness replays.
- [ ] `tests/replay/test_record.py::test_the_home_directory_is_replaced` records a request whose system prompt and tool result name a home directory and a user name, and finds neither in the capture, nor the key the request was sent with.
- [ ] Captures of Claude Code, with its default effort, a system message placed mid-conversation, a PDF read and an image in a tool result, of Codex and of an SDK script, recorded against the deployed mock pool, are added under `tests/captures/`, each stating the coursework pool's request profile, and replay in `tests/replay/test_replay.py` (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.2, 6.2, 10, 13
