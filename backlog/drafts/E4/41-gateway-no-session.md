+++
id = "E4-41"
title = "Gateway: while no session is on, answer a student's request and wake with no_session"
labels = ["size:M", "kind:app"]
epic = 6
+++

## Context

The gateway decides whether a student is served: while the session switch is off, it answers a
student's inference request and wake at once with the cause `no_session`, so no student's demand
reaches the scaler, and each pool falls to zero by its idle rule (section 5.6). The scaler is not
changed: it stays a function of demand (section 5.1).

**The cause.** `no_session`, status 503, no `Retry-After` (the gateway does not know when the next
session starts), and a fixed sentence that holds no number and no name of the deployment, such as
"No session is on: this endpoint serves only during class sessions." It joins the causes of
`services/common/rhi_common/causes.py` (section 6.2, step 8), and renders in each wire format as the
others do. The two causes answered 402 stay until a later change removes the budget (section 14).

**Reading the switch.** The switch's record and its item are built: `SessionSwitch` and
`session_is_on` in `services/common/rhi_common/records.py`, `SESSION_SWITCH_KEY` in
`services/common/rhi_common/tables.py`, which the scaler and the admin CLI already read. The gateway
already reads each pool's records from the pool-state table every 5 seconds
(`services/gateway/rhi_gateway/readiness.py`). It reads the switch with them. A read that fails
keeps the last value read; before the first read succeeds the switch reads off.

**What it refuses, and what it does not.** While the switch is off, for a student's key:

- the four forwarded routes end at once with `no_session`, before anything is reserved, held or
  woken; `POST /wake` ends the same way and asks the scaler for nothing;
- `GET /status` lists each pool as `unavailable` with the cause `no_session`, so `rhi status` and
  `rhi wait` say so;
- `GET /v1/models` and `HEAD /api/hello` answer as before.

**The owner's own entries are served whatever the switch:** the canary of the pipeline's
verification, GitHub ID 0 (`admin/rhi_admin/canary.py`), and the spikes' students, GitHub IDs from
10^18 on (`admin/rhi_admin/spikes.py`). No GitHub account has those IDs. The verification, the
weekly canary and the spikes run while no session is on. The two numbers move to
`services/common/rhi_common/students.py`, which the gateway and the admin CLI both import.

The keys and their GitHub IDs reach the gateway on the key's record (`rhi_gateway.keystore`).

## Deliverables

- [ ] `services/common/rhi_common/causes.py` gains the cause no_session, status 503, with no Retry-After and one fixed sentence, and its docstring counts the causes it holds.
- [ ] `services/common/rhi_common/students.py` names the GitHub ID of the canary and the first GitHub ID of the spikes' students, says whether a GitHub ID is one of the owner's own entries, and `admin/rhi_admin/canary.py` and `admin/rhi_admin/spikes.py` import the two numbers from it.
- [ ] `services/gateway/rhi_gateway/readiness.py` reads the session switch with the pools' records every 5 seconds, keeps the last value read when a read fails, and reads off until its first read succeeds.
- [ ] While the switch is off, `services/gateway/rhi_gateway/app.py` and `services/gateway/rhi_gateway/local.py` end a student's request on a forwarded route, and a student's wake, at once with no_session, reserving, holding and waking nothing, and list every pool as unavailable with that cause in a student's status; a key of the owner's own entries is served as if the switch were on.
- [ ] `tests/unit/gateway/test_session_switch.py` covers the switch on the forwarded routes, the wake, the status and the owner's entries, and `tests/unit/gateway/test_errors.py` renders no_session in each wire format.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_session_switch.py` passes: with the switch off a student's Messages, Chat Completions, Responses and token-count requests each get no_session with status 503 and no Retry-After, nothing is reserved and no wake is asked; a student's wake gets no_session and asks nothing; a student's status lists every pool unavailable with no_session; the canary's and a spike student's requests are forwarded; with the switch on a student's request is forwarded.
- [ ] `tests/unit/gateway/test_session_switch.py::test_the_switch_reads_off_until_read` finds a gateway whose first read of the switch failed refusing a student with no_session, and one whose later read failed keeping the value it last read.
- [ ] `uv run --locked pytest tests/unit/gateway/test_errors.py tests/unit/gateway/test_readiness.py tests/unit/gateway/test_local.py tests/unit/admin tests/unit/common` passes.
- [ ] In the account, with the session switched off, a student's key gets no_session and the pipeline's verification with the canary's key passes; with it switched on, the student's request is served (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 5.6 and 6.2
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 1
- `services/gateway/rhi_gateway/readiness.py`
- `services/gateway/rhi_gateway/local.py`
