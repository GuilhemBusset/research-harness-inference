+++
id = "E0-38"
title = "Reporter: post every part of a measurement, wait out GitHub's temporary refusals, and complete a posting cut short"
labels = ["size:M", "kind:app"]
epic = 2
+++

## Context

A job that measures (spike S5, spike S8, the weekly GPU canary) cannot post a commit status. It
hands one run to the reporter as one event on the measurement bus (`run_spike` in
`spikes/common.py`, `scripts/pipeline/canary_result.py`), and the reporter's function posts the
statuses `measure.read_event` gives for it, one part after the other (`_measurement` in
`services/reporter/rhi_reporter/handler.py`, #48). The reporter reads no build log, no artifact
and no object: the numbers are in the event. It caps nothing, and it reads nothing back from
GitHub.

Run `4bit-draft-head-1` of S5, on the merge commit of #875 (`c2f71ae`), sent 163 numbers, which
encode as 61 parts. GitHub holds parts 1 to 42, created between 12:46:44 and 12:47:00 UTC on
2026-11-27, and no part after them: the several-agents cell at 8 streams loses its latency
figures, and the cells at 16 streams and at the class's size are missing, the class cell being the
one the verdict reads. The S5 record reads the run as incomplete and writes it pending. The
function's invocation lasted 23 seconds of its one-minute timeout, and ended without an error.
The run of the same morning, `4bit-cache-16bit-1`, posted its 60 parts in 26 seconds.

Why it stops part-way and says nothing an operator watches:

- `StatusClient._call` in `services/reporter/rhi_reporter/github.py` raises `RefusedError` for
  any answer that is not a 2xx, and for no answer. It never posts again, and it reads neither
  `retry-after` nor `x-ratelimit-reset`.
- `_measurement` catches that error, logs one line at WARNING (`REFUSED`) and returns normally.
  Its docstring says so: the rest is not posted. The invocation counts as a success, so Lambda
  does not invoke it again, its error count stays at zero, and no line names the run, the parts
  posted or the parts left. With a credential that stops signing mid-run, it is the only way out of
  the function that stops part-way and returns normally.
- GitHub answers 403 or 429 when a client exceeds a secondary rate limit, with lower limits on
  some endpoints of content creation, and asks the client to wait `retry-after` seconds, or until
  `x-ratelimit-reset`, or at least a minute
  (https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api). A run of S5
  is about 60 creations back to back, the largest burst the App makes; the pipeline's own
  statuses come a few at a time. Both runs went through the same code: what differed was GitHub's
  answer to one post.

Shown locally, without AWS: the job's numbers, rebuilt from the runs it printed, encode as 61
parts whose first 42 are, byte for byte, the 42 statuses GitHub holds. The handler with the real
client, against a stand-in GitHub that accepts 42 statuses and answers the 43rd with 403, 429
or 502, posts 42, writes one WARNING line and returns None.

Why the run cannot simply be sent again: the reporter would post parts 1 to 61 once more. The S5
record (`postings_of`) reads a posting that began while the one before it had not ended as
overlapping and writes no row for the label at all. Parts 43 to 61, posted after the first 42,
make the posting whole. No command posts only the missing parts today.

Posting a run in fewer statuses is not a way out: a description holds at most 140 characters and
the names are the record's. The function's timeout stays at one minute
(`src/rhi/pipeline/reporter.py`, not changed here): about 25 seconds of posting leave time for
short waits, and a longer wait goes to Lambda's own asynchronous retries, which come at least one
and then two minutes later, the minute GitHub asks for. Those retries must complete the posting
they find, not start another, hence the reading of the commit's statuses. The pipeline's own
statuses already fail the invocation on a refusal and are retried; they gain the waits.
`test_a_refused_measurement_is_logged_and_not_retried` in `tests/unit/reporter/test_handler.py`
holds the behaviour this issue replaces, and changes with it.

Jobs exposed: S5 (about 60 parts), S8 (2 to 4) and the canary (1), every job that posts through
the bus. S3's jobs, S7, the dry-run class and the engine contract job send no measurement event:
their record is their log.

Suggested branch: `fix/reporter-partial-measurements`

## Deliverables

- [ ] `services/reporter/rhi_reporter/github.py` lets a status be posted with a deadline on the client's clock and, until that deadline, posts it again when GitHub answers 429, a 403 that carries a `retry-after` header or an `x-ratelimit-remaining` of 0, a 5xx, or nothing: after the seconds `retry-after` gives, else at the time `x-ratelimit-reset` gives, else after 1 second, doubled at each attempt up to 8.
- [ ] `services/reporter/rhi_reporter/github.py` raises `RefusedError` without waiting for any other refusal, for a post given no deadline, and when the next attempt would begin after the deadline, and the error's text still names only the HTTP status and a code of `ERROR_CODES`.
- [ ] `services/reporter/rhi_reporter/github.py` reads the statuses of a commit, every page, each with its id, context, state and description, with the same installation token, and still writes nothing to GitHub but commit statuses.
- [ ] `services/reporter/rhi_reporter/handler.py` gives every post of an invocation the deadline of the Lambda context's remaining time less 10 seconds, and before it posts a measurement reads its commit's statuses: when the label's newest posting there (its newest status of part 1 and each newer status of another part) holds only parts equal in state and description to the event's parts of the same numbers, only the event's other parts are posted, in order, and otherwise every part is posted from part 1.
- [ ] `services/reporter/rhi_reporter/handler.py` logs at ERROR one line of fixed text, `NOT_ALL_POSTED`, with the measurement's id, its run label, the number of its parts the commit holds and the number of its parts, and then raises, whenever a part is left unposted by a refusal, by a failed read of the statuses or by the deadline; the line `REFUSED` and its WARNING go.
- [ ] `spikes/s5_capacity.py` has a command `entries` that reads the runs a job printed (its `Measured` JSON lines, other lines skipped) from `--runs` and a commit's statuses as `gh api --paginate` writes them from standard input, and writes on standard output, as the JSON list of entries that EventBridge's PutEvents takes, the measurement event of `--commit`, `--label` and `--verdict` whose numbers `numbers_of` gives for those runs.
- [ ] `spikes/s5_capacity.py` `entries` writes nothing and exits 1 when a part of the label's newest posting on the commit differs from the rebuilt part of that number, otherwise says on standard error how many parts remain to be posted, and its module docstring gives the commands that complete a posting cut short.

## Acceptance criteria

- [ ] `tests/unit/reporter/test_github.py` fails when a status that GitHub answers first with 429 and `retry-after` 2, then with 201, is not posted twice, 2 seconds apart on the test's clock, or when a wait that would end after the deadline is waited instead of raised.
- [ ] `tests/unit/reporter/test_handler.py` fails when a measurement of 61 parts whose 43rd post GitHub refuses does not log exactly one ERROR record naming S5, its run label, 42 and 61, or returns instead of raising.
- [ ] `tests/unit/reporter/test_handler.py` fails when that event, invoked again on a commit that holds that posting's parts 1 to 42, posts anything but parts 43 to 61 in order, or when, on a commit whose newest posting of the label differs from the event in one part, it posts anything but parts 1 to 61.
- [ ] `tests/unit/spikes/test_s5_entries.py` fails when the entry `entries` writes from a job's printed runs gives, through `measure.read_event`, other statuses than the event the job sent, or when it writes an entry for a commit whose newest posting of the label holds a part that differs.
- [ ] `uv run --locked pytest tests/unit/reporter tests/unit/spikes` passes and `scripts/check.sh fast` exits 0.
- [ ] After the deployment, the owner puts on the measurement bus, with `aws events put-events --entries file://entries.json`, the entries `entries` writes for run `4bit-draft-head-1` of commit `c2f71ae` from that build's printed runs, and the commit then holds its parts 43 to 61 once each and the S5 record reads the run as whole (needs aws:deployed-environment).

## References

- `services/reporter/rhi_reporter/handler.py`
- `services/reporter/rhi_reporter/github.py`
- `services/reporter/rhi_reporter/measure.py`
- `spikes/common.py`
- `spikes/s5_capacity.py`
- `src/rhi/pipeline/reporter.py`
- `docs/design/system-design.md`, sections 8.4 and 12
