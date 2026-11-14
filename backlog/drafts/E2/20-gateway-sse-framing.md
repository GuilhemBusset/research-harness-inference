+++
id = "E2-20"
title = "Gateway: read server-sent events incrementally, at any byte boundary"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E1-02"]
+++

## Context

The gateway streams a response without buffering it and reads token usage as it passes (section 6.2,
step 6). The network cuts a stream wherever it likes: in the middle of an event, of a line, of a
character. The usage parsers of the three formats all sit on one incremental reader of server-sent
events.

## Deliverables

- [ ] `services/gateway/rhi_gateway/sse.py` is fed chunks of bytes and yields each complete event with its name, its data and whether it is a comment; it never changes or delays the bytes it is given.
- [ ] It accepts both line endings, data on several lines, comments, and a chunk boundary anywhere, including inside a multi-byte character.
- [ ] `tests/unit/gateway/test_sse.py` covers the reader.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_sse.py` passes: a recorded stream gives the same events whether it is fed whole, byte by byte, or cut at every possible position.

## References

- `docs/design/system-design.md`, section 6.2
