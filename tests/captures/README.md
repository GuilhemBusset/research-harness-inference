# Captures

A capture is one recorded exchange: a request as a client sent it to the endpoint, and the answer
an engine gave to it. [`tests/replay/test_replay.py`](../replay/test_replay.py) replays every
capture of this folder through the gateway
([`docs/design/system-design.md`](../../docs/design/system-design.md), section 6.2), with the
answer cut at byte positions and with the client leaving after each event
([`tests/replay/harness.py`](../replay/harness.py)). A capture added here is replayed with no
change to the tests.

## What a capture is

One JSON file, `<name>.json`, an object of four parts:

| Part | What it holds |
| --- | --- |
| `request` | The request as the client sent it: `method`, `path` (with its query string, if it had one), `headers` (an object of names and values) and `body` |
| `forward` | Optional. The body the gateway must forward to the engine for this request, as a JSON object |
| `answer` | The upstream's answer: `status`, `headers` and `body`, the bytes the engine sent |
| `usage` | The usage expected: `input_tokens`, the whole input, cached or not, and `output_tokens` |

A `body` is text, UTF-8 on the wire. It is written as one string, or as a list of strings that are
joined end to end: a stream is written one line to a string, so that the file reads and compares
line by line.

How the parts are used:

- The route is the entry of the gateway's table that `method` and `path` name, and its wire
  format is the capture's. The answer is a stream when its `content-type` is
  `text/event-stream`, which is how the gateway itself tells.
- The request's headers do not include the one that carried the key (`authorization` or
  `x-api-key`). A replay presents a key that it generates.
- `forward`, when given, is compared with the body the upstream receives in every replay, as JSON
  values, field by field. The request ID (`request_id`) and the cache salt (`cache_salt`) are left
  out of the comparison and need not be written: the gateway sets them for each request and each
  student. Any other difference fails the replay. This is how a capture covers an entry of a
  pool's request profile (section 6.2, step 4).
- `usage` is what the gateway must settle the request to when the answer ends cleanly. It comes
  from the engine's own count where there is one, not from reading the answer again.
- The client must receive the answer's body byte for byte. So a streamed Chat Completions capture
  is recorded from a request that asks for the usage chunk (`stream_options.include_usage`): the
  gateway asks the engine for that chunk on every stream and hides it from a client that did not
  ask.

## The rules

This repository is public (section 10), and recorded traffic carries what the machine that
recorded it knew. A capture has:

- **an example host**: it is recorded under `example.com` or a name below it, and no other host
  appears in it, in a header or in a URL;
- **no key**: no student's API key, and no other credential;
- **no address**: no IP address, and no e-mail address;
- **no student**: nothing of a real person, such as a name, a GitHub ID, a roster alias, a path of
  their machine, or their code and prompts. The student of a replay is invented.

[`tests/replay/scrub.py`](../replay/scrub.py) refuses a capture that holds a key in the format of
`rhi_common.keys`, a host outside `example.com`, or an IP address. It reads every capture before
it is replayed, so a capture it refuses fails the tests. The hygiene scanner
([`tools/hygiene/README.md`](../../tools/hygiene/README.md)) refuses e-mail addresses and the
credentials of other services here, as in any file. No pattern can find a real domain written
without a scheme, a name, or someone's own text: whoever records a capture reads it before it is
committed.

## The captures of the mock engine

The `mock-*` files are the captures the mock engine can produce by itself: one for each wire
format (Messages, Chat Completions, Responses), streamed and not.
[`tests/replay/record.py`](../replay/record.py) records them in process, with no network: each
request goes through the gateway's body filter to the mock engine, and the usage is what the mock
counted. To record them again, after a change to the mock or to a filter:

```sh
uv run --locked python tests/replay/record.py
```

The mock's clock stands still during a recording, so this rewrites the same files when nothing
has changed.

Captures of the real harnesses are recorded against the deployed mock pool, in phase 2.
