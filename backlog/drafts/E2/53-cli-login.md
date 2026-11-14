+++
id = "E2-53"
title = "The rhi CLI: log in with GitHub's device flow and keep the key out of sight"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-50"]
+++

## Context

`rhi login` asks GitHub for a device code for this project's GitHub App and shows the student a
short code to confirm in the browser; it sends the device code to the key issuer and polls; it
stores the key in the operating system's keychain, or in a file only the user can read (section
6.4). Students install the CLI with one `uv tool install` command from this repository (section
8.1).

The CLI is given the endpoint once (`rhi login --endpoint`, from the course's private channel) and
stores it. It reads the login app's client ID from the issuer (`GET /auth/config`) and logs in at
`POST /auth/login` (issue E2-50). Neither the endpoint nor the client ID is in the repository
(section 6.4).

A student has one key per installation of the CLI. An installation is an identifier the CLI creates
at its first login and sends with the device code (section 6.4); the issuer's rules on it are issue
E2-51. Each login shows when the previous key was issued; the issuer's answer carries that time
(issue E2-50). Pointing each harness at the endpoint through a key helper is the next step of the
same command, issue E2-54.

`client/rhi_cli/main.py` is the command dispatcher: each later command (issues E2-54 and E2-55)
adds itself there. The keychain library is keyring, which issue E1-16 pins in its group cli; the
CLI's HTTP calls use the standard library. The CLI's own `client/pyproject.toml` declares keyring at
the version `uv.lock` holds, because a student's `uv tool install` reads no lock file. Keys are generated at run time in the tests: a literal key in
a test file fails the hygiene scan (issue E0-16).

## Deliverables

- [ ] `client/rhi_cli/login.py` adds the command rhi login: it takes the endpoint's address from the option --endpoint once and stores it; reads the login app's client ID from the issuer's `GET /auth/config`; asks GitHub for a device code; prints the short code and the address to confirm it at; sends the device code to the issuer's `POST /auth/login` and polls at the interval GitHub gave.
- [ ] On its first login an installation creates a random identifier, stores it beside the endpoint, and sends it with the device code on every login.
- [ ] Without a stored endpoint and without the option, the command stops and says how to give one; no endpoint and no client ID is in the package.
- [ ] `client/rhi_cli/store.py` keeps the key in the operating system's keychain when there is one, and otherwise in a file that only the user can read.
- [ ] The key is never printed by the login command, and the command prints when the student's previous key was issued, from the issuer's answer.
- [ ] `client/pyproject.toml` makes the CLI installable from this repository with one `uv tool install` command, under the command name rhi, with its libraries at the versions the repository's lock file holds, and `client/rhi_cli/main.py` dispatches its commands.
- [ ] `tests/unit/client/test_login.py` covers the flow against stubs of GitHub and of the issuer, with every key generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/client/test_login.py` passes: a login ends with the key in the store and not in the output, pending and slowed-down answers are polled at the right interval, the client ID used is the one the issuer's stub served, and a second login sends the same installation identifier as the first.
- [ ] `tests/unit/client/test_login.py::test_the_fallback_file_is_private` shows the file is created with access for its owner only, and that a file readable by others is refused when read.
- [ ] `tests/unit/client/test_login.py::test_the_previous_issue_time_is_printed` shows the time the issuer's stub gave in the command's output.

## References

- `docs/design/system-design.md`, sections 6.4, 8.1
- `docs/design/evidence.md`, E17
