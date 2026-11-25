+++
id = "E6-19"
title = "Client and student guide: wait for a session's instances, start nothing"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-18"]
+++

## Context

Since E6-18 the gateway holds no request and has no wake route. Two things a student uses still
speak of both: the `rhi` CLI and the student guide.

**`rhi wait`** sends a wake today, repeats it every minute, and reads the status in between. It
now starts nothing (system design, section 5.3): it reads the status every 10 seconds, prints each
change, and returns when the pool is ready. It is how a student who arrives before a session's
instances waits for them. It keeps waiting through `warming` and `capacity_unavailable`, which
both mean that the session is on and its instances are not there yet, and it stops with the cause
on `no_session`, on an error of the gateway, and at its timeout, as today. `rhi status` also
prints when a pool's session ends, which the status now gives.

**The Codex profile** that `rhi login` writes carries a stream timeout of 15 minutes, so that Codex
survives a held stream. No stream is held: the profile no longer sets it, and Codex keeps its own
default.

**The causes.** The gateway no longer answers `pool_scheduled_only` or `pool_disabled`. The shared
list loses them, which leaves the nine causes of section 6.2, step 8. Spike S5 counts failed
requests by cause and names those two among its numbers: it loses them too.
`tests/unit/test_docs.py` holds the guide to the causes of the shared list and of the design, and
to the commands the CLI accepts.

**The student guide** says that the instructor switches each session on, that the endpoint holds a
request while a pool starts, and that `rhi wait` starts a pool. It is rewritten to the design:
the instructor books each session ahead of the class; a request to a pool with no session, or
whose instances are not there yet, is answered at once with a cause; `rhi wait` waits and starts
nothing; the three states; the nine causes.

After this issue nothing deployed changes: the CLI is installed by each student, and the guide is a
document.

## Deliverables

- [ ] `client/rhi_cli/wait.py`: the wait sends no wake, reads the status every 10 seconds until the pool is ready, keeps waiting while the pool is warming or unavailable for capacity, and stops with the cause on `no_session`; the status prints the end of a pool's session when the gateway gives one.
- [ ] `client/rhi_cli/harness.py` writes a Codex profile that sets no stream timeout.
- [ ] `services/common/rhi_common/causes.py` has the nine causes of section 6.2, and `spikes/s5_capacity.py` no longer names the two that are gone among its numbers.
- [ ] `docs/student-guide.md` describes sessions the instructor books, the answers at once of a pool that cannot serve, the wait that starts nothing, the three states and the nine causes, and no longer a hold, a wake, a switch, or the states and causes that are gone.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/client` passes: the wait calls only the status, returns 0 on ready after a pool read warming and then unavailable for capacity, and exits 1 with `no_session`; the Codex profile holds no stream timeout.
- [ ] `uv run --locked pytest tests/unit/test_docs.py tests/unit/common tests/unit/spikes tests/unit/gateway` passes: the guide explains each of the nine causes and shows only commands the CLI accepts.
- [ ] `git grep -n -i -e "is held" -e "/wake" -e pool_disabled -e pool_scheduled_only -e "switches each session" -- docs/student-guide.md client/rhi_cli services/common/rhi_common/causes.py` prints nothing.

## References

- `docs/design/system-design.md`, sections 5.3, 5.6 and 6.2
- `client/rhi_cli/wait.py`
- `client/rhi_cli/harness.py`
- `docs/student-guide.md`
- `services/common/rhi_common/causes.py`
