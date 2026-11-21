# Captures

A capture is one recorded exchange: a request as a client sent it to the endpoint, and the answer
an engine gave to it. [`tests/replay/test_replay.py`](../replay/test_replay.py) replays every
capture of this folder through the gateway
([`docs/design/system-design.md`](../../docs/design/system-design.md), section 6.2), with the
answer cut at byte positions and with the client leaving after each event
([`tests/replay/harness.py`](../replay/harness.py)). A capture added here, or in a subdirectory
of this folder at any depth, is replayed with no change to the tests. It is named by its path under
this folder without `.json`: `coursework/effort.json` is the capture `coursework/effort`.

## What a capture is

One JSON file, `<name>.json`, an object of four parts, and of a fifth when it states a request
profile:

| Part | What it holds |
| --- | --- |
| `request` | The request as the client sent it: `method`, `path` (with its query string, if it had one), `headers` (an object of names and values) and `body` |
| `profile` | Optional. The request profile the capture is replayed with: `request_profile`, the names of its entries in the order they apply, and `reasoning_effort`, optional, the mapping from each value of reasoning effort a request may send to the value the engine receives |
| `forward` | Optional, and required with `profile`. The engine-side body: what the gateway must forward to the engine for this request, as a JSON object |
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
  student. Any other difference fails the replay.
- `profile`, when given, is written as a pool's configuration writes the two fields
  (`rhi_common.pool_config`), for example
  `{"request_profile": ["first_entry", "second_entry"], "reasoning_effort": {"high": "xhigh"}}`.
  Every pool of the replay then has that profile, and the gateway's body filters run its entries,
  in that order, as the gateway's entry point has them run (`rhi_gateway.profile`). `forward` is
  then the body the engine must receive with the profile applied. This is how a capture covers an
  entry of a pool's request profile (section 6.2, step 4): it states the profile itself, not a
  pool of the example configuration, so it can be shown before any pool names the entry. A
  profile that names an entry the gateway does not have fails the replay.
- A capture that states neither `profile` nor `forward` is replayed with no profile, and what the
  gateway forwards is not compared; one that states only `forward` is compared with no profile.
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

## The captures of the real harnesses

The only way to record what Claude Code, Codex or an SDK script really sends is to run it, so the
owner records these captures against the deployed mock pool through the public endpoint.
[`tests/replay/record.py`](../replay/record.py) serves a recorder on a loopback port. The harness
is pointed at the recorder in place of the endpoint, and the recorder forwards each request to the
endpoint and streams the answer back unchanged. Each exchange is saved in the order its request
arrived, numbered from `001`:

- `<n>.json` is a capture the replay tests replay: an engine's answer, status 200, on a forwarded
  route of the gateway. Its usage is what the answer reports, read the way its route writes it: a
  usage object, or the top-level `input_tokens` of a token count.
- `<n>.gateway` holds every other exchange in the same format: a route the gateway answers itself
  (`GET /v1/models`, ...), a path outside its table, or a refusal. Only the gateway took part in
  these, so there is nothing to replay. The replay tests read `*.json` only, and leave these out.

Before it writes a capture, the recorder replaces each of these wherever it appears in a request
or an answer: the endpoint's host and its own address become `gateway.example.com`, the student
key becomes `the-student-key`, the recording machine's home directory becomes `/home/user`, and its
user name becomes `user`, inside longer words too. It drops the headers that carry the key and the
cookie. While a cold pool held a stream, the gateway wrote keep-alives that the engine never sent,
and the recorder leaves them out of the answer. The scrubber then reads the capture. If it refuses
the capture, the recorder does not write it and prints the rule and the place.

What the recorder cannot see, such as a name that a stream writes in pieces or a student's own
text, is for the person who records to find: read every capture before it is committed.

### How the owner records them

Each harness keeps the model name a student would use: Claude Code its own default `claude-*`
model, which the gateway gives to the default pool, and Codex and the SDK script `coursework`.
That keeps Claude Code's default reasoning effort, `high`, which it chooses for its own models.
`--pool mock` makes the recorder write `mock` into the `model` field of what it sends upstream, so
every request reaches the deployed mock pool and wakes no GPU. The capture still keeps the model
the harness sent. `--profile coursework` makes each capture state the coursework pool's request
profile and the body the engine must receive with that profile applied, so the capture replays
through that profile. The key is the owner's own canary key, in `RHI_KEY`. Start one recorder for
each harness, in a terminal of its own, and stop it with Ctrl-C:

```sh
uv run --locked python tests/replay/record.py session --upstream https://<endpoint> \
  --out tests/captures/harness/claude-code --pool mock --profile coursework --port 8765
```

- **Claude Code**: run it in an empty folder made for the recording, with no model and no effort
  chosen:
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_API_KEY="$RHI_KEY" claude`. In one session,
  get it to place a system message mid-conversation (load a skill, or let its task list change),
  to read a PDF (a document block), and to read a PNG (an image in a tool result).
- **Codex**: give it a provider for the recorder, then record into
  `tests/captures/harness/codex`: a `[model_providers.recorder]` table with
  `base_url = "http://127.0.0.1:8765/v1"`, `env_key = "RHI_KEY"` and `wire_api = "responses"`,
  then `codex -c model_provider=recorder -m coursework`.
- **An SDK script**: point the Anthropic or OpenAI SDK at `http://127.0.0.1:8765` (with `/v1` for
  OpenAI's), with `model="coursework"` and the key from `RHI_KEY`, and record into
  `tests/captures/harness/sdk`. A streamed Chat Completions request asks for the usage chunk
  (`stream_options.include_usage`), as stated above.

Then read each capture, check that Claude Code's carry `"effort": "high"`, a `system` message after
the first turn, a `document` block and an `image` inside a `tool_result`, and run
`uv run --locked pytest tests/replay/test_replay.py`. The captures are added once that passes.
[`tests/replay/test_record.py`](../replay/test_record.py) runs the same recorder against the local
stack of the smoke tests.
