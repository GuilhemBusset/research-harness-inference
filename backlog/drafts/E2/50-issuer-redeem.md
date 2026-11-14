+++
id = "E2-50"
title = "Key issuer: redeem a device code, check the roster, mint a key"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-01", "E2-67"]
+++

## Context

Students log in with the GitHub identity they already have (section 6.4). The CLI obtains a device
code for this project's GitHub App and sends it to the key issuer. The issuer redeems it with
GitHub, which itself guarantees the code belongs to this app; reads the numeric user ID; checks it
against the roster; creates an API key, stores only its SHA-256, returns the key once, and discards
the GitHub token. The roster is keyed by numeric GitHub ID, because handles can be renamed. Keys
expire at course end. Each login shows when the previous key was issued, so that a student notices
a login they did not make.

**The key item carries what the gateway needs.** The roster entry holds the student's alias, daily
token budget and concurrency limit (issue E2-56). The issuer copies all three into the key item of
issue E2-67 when it mints, with the time of issue and the expiry, so that the gateway answers a
request with one read of the keys table and never reads the roster (issue E2-14). The roster and
key items, their attribute names and the keys table's index by student are issue E2-67's.

The login app's client ID is kept out of the repository (section 10): the CLI reads it from the
issuer, at `GET /auth/config`. The client ID and the course end date reach the function in its
environment, as RHI_CLIENT_ID and RHI_COURSE_END, and the two tables' names as RHI_ROSTER_TABLE and
RHI_KEYS_TABLE; issue E2-52 sets them from the configuration and the stage. The issuer's routes all
live under `/auth/`, so that one rule of the load balancer and one of the web ACL cover them. The
design names only `GET /auth/config`; the login is `POST /auth/login`, which takes the device code
and the installation's identifier, an identifier the CLI creates at its first login (section 6.4).
The rules that use it are issue E2-51. The device flow needs no client secret (evidence E17).

The tests run against a stub of GitHub and tables moto creates with the key schemas of issue E2-67.
Keys are generated at run time: a literal key in a test file fails the hygiene scan (issue E0-16).

## Deliverables

- [ ] `services/issuer/rhi_issuer/redeem.py` exchanges a device code with GitHub for a user token under the client ID it is given, reads the numeric user ID, discards the token, which is written to no store and no log, and refuses a user whose ID is not in the roster with a fixed message that reveals nothing about the roster.
- [ ] A device code that GitHub reports as pending, slowed down or expired is answered with a fixed code for each; a device code or an installation identifier of the wrong shape is refused before any call to GitHub.
- [ ] `services/issuer/rhi_issuer/mint.py` creates a key in the format of issue E2-01 and stores the key item of issue E2-67: its SHA-256, the roster entry's alias, daily token budget and concurrency limit, the time of issue and the expiry, the course end date it is given; it returns the key in the answer only, with the time the student's previous key was issued, or that there was none.
- [ ] `services/issuer/rhi_issuer/handler.py` serves two routes behind the load balancer: `GET /auth/config`, which gives the login app's client ID and nothing else, and `POST /auth/login`, which redeems a device code sent with the installation's identifier.
- [ ] `tests/unit/issuer/test_redeem.py` covers the flow against a stub of GitHub and tables that moto creates, with every key generated at run time and none written in the file.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/issuer/test_redeem.py` passes: a roster member gets a key that verifies, only its SHA-256 is in the store, the GitHub token is in no store and no log, and `GET /auth/config` answers the configured client ID.
- [ ] `tests/unit/issuer/test_redeem.py::test_malformed_input_calls_nobody` shows no call to GitHub for a device code of the wrong shape, and `tests/unit/issuer/test_redeem.py::test_outsiders_are_refused` covers a user outside the roster.
- [ ] `tests/unit/issuer/test_redeem.py::test_the_previous_issue_time_is_shown` shows a first login answering that there was no previous key, and a second one answering the time of the first.
- [ ] `tests/unit/issuer/test_redeem.py::test_the_key_carries_the_roster_limits` finds in the stored key item the alias, the budget and the limit of the student's roster entry, and the course end date as its expiry.

## References

- `docs/design/system-design.md`, sections 6.4, 10
- `docs/design/evidence.md`, E17
