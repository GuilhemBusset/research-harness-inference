+++
id = "E2-55"
title = "The rhi CLI: status, wait for a cold pool, and revoke keys"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-27", "E2-53", "E2-78", "E2-83"]
+++

## Context

`GET /status`, with a key, lists the configured pools, by model name, each with a state (`cold`,
`warming`, `ready`, `unavailable`) and, when unavailable, a cause from the list of section 6.2
(issue E2-27). `POST /wake`, with a key, starts a pool under the same rules as a request, without
sending one, and answers at once with the same record for that pool (issue E2-78). An explicit wake
counts as demand for 10 minutes or until the pool is ready, so `rhi wait` repeats it every minute;
it polls the status until the pool is ready, and stops with the cause when the pool is unavailable.
It is the documented way to start a homework session on a cold pool (section 5.3), and the fallback
for any client that misbehaves during a hold.

`rhi wait` gives up after a timeout the student can set with the option --timeout, in minutes. The
design gives no value; the default is 20 minutes, above the launch hook's 15 minutes on the
coursework pool (section 17), so that a start that succeeds is never abandoned by the CLI first.

`rhi revoke` revokes the installation's own key, authenticated by that key, at the issuer's
`POST /auth/revoke`. `rhi revoke --all` revokes every key of the student and requires a fresh device
flow, since it must work when a key is lost (section 6.4), at `POST /auth/revoke-all`. The routes
are the issuer's, issue E2-83; the device flow is the login's, issue E2-53.

Section 8.1 lists the CLI's commands as login, token, wait and revoke; rhi status, beside rhi wait,
prints the same records, and the student guide uses it (issue E4-21). The command dispatcher is
`client/rhi_cli/main.py` (issue E2-53). Keys are generated at run time in the tests: a literal key
in a test file fails the hygiene scan (issue E0-16).

## Deliverables

- [ ] `client/rhi_cli/wait.py` adds the command rhi status, which prints, for each pool `GET /status` lists, its model name, its state and, when it is unavailable, its cause.
- [ ] It adds the command rhi wait, which calls `POST /wake` for the pool named by its model name, repeats that call every minute, polls `GET /status` until the pool is ready, printing the pool's state as it changes, then exits 0.
- [ ] rhi wait stops, prints the cause and exits non-zero when the pool's record reads unavailable, or when the gateway refuses the wake with an error; it exits non-zero at the timeout of its option --timeout, 20 minutes by default.
- [ ] `client/rhi_cli/revoke.py` adds the command rhi revoke, which revokes the stored key at the issuer's `POST /auth/revoke`, authenticated by that key, and removes it from the store.
- [ ] With the option --all, rhi revoke runs a fresh device flow, as the login does, revokes every key of the student with it at `POST /auth/revoke-all`, and removes the stored key; it works with an empty store.
- [ ] `client/rhi_cli/main.py` dispatches the three commands, and `tests/unit/client/test_wait.py` covers them against the gateway's local routes in process and stubs of GitHub and of the issuer, with every key generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/client/test_wait.py` passes: over a wait of three minutes on a clock the test controls, rhi wait sends a wake at once and one more each minute, returns when the pool turns ready, and exits non-zero at its timeout.
- [ ] `tests/unit/client/test_wait.py::test_an_unavailable_pool_stops_the_wait` shows rhi wait on a scheduled-only pool printing pool_scheduled_only and exiting non-zero after one call, and printing budget_reached for a student whose budget is spent.
- [ ] `tests/unit/client/test_wait.py::test_revoke_one_or_all` shows the store empty after a revoke authenticated by the stored key, and, with the option --all and an empty store, a device flow followed by the request that revokes every key.

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 6.4, 8.1, 17
