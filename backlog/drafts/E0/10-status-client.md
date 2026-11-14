+++
id = "E0-10"
title = "Reporter: post commit statuses as a GitHub App installation"
labels = ["size:M", "kind:app"]
epic = "E0"
blocked_by = ["E1-02", "E1-03", "E1-16"]
+++

## Context

The reporter's credential is a GitHub App installation that can write commit statuses on this
repository and nothing else (section 8.4; ADR 0001, amendment 4). Its private key is a secret in
Secrets Manager that the owner stores once (section 8.6, step 3); it is never on the factory's host.
The repository's owner and name come from the configuration key of issue E1-03, never from the
source tree.

**What the client reads, and from where.** These names are shared with the construct (issue E0-12)
and the owner's walkthrough (issue E1-18), so they are fixed here:

| What | Where the function finds it |
| --- | --- |
| The secret's name | The environment variable RHI_REPORTER_SECRET, set by the construct |
| The App's ID, its installation ID, its private key | The fields app_id, installation_id and private_key of that secret's JSON value |
| The repository, as owner/name | The environment variable RHI_REPORTER_REPOSITORY, set by the construct from the configuration |

The secret may not exist yet when the stack is first deployed: the owner creates it (issue E0-12).
The client then raises one error of its own, which the handler turns into "post nothing"; the
error carries nothing of AWS's message.

The signature needs a JWT library with an RSA backend, which has native code. Issue E1-16 pins it,
with the AWS SDK for Python, in the root project's reporter dependency group, and builds a
function's asset from `uv.lock` without Docker.

## Deliverables

- [ ] `services/reporter/rhi_reporter/github.py` signs an App token with the private key, exchanges it for an installation token, and posts a status on a commit of the repository that RHI_REPORTER_REPOSITORY names.
- [ ] It reads the App's ID, its installation ID and its private key, on a cold start only, from the fields app_id, installation_id and private_key of the secret that RHI_REPORTER_SECRET names; a secret that is absent or lacks one of the three fields raises one error of the module's own, whose text holds nothing of AWS's message.
- [ ] The installation token is kept in memory until less than five minutes of its validity remain, then renewed, and is written to no log.
- [ ] A refused call is reported by its HTTP status and GitHub's error code only, never by the response text.
- [ ] Creating a commit status is the client's only call that writes to GitHub: it has none for a commit comment, an issue or a pull request.
- [ ] The client uses the signing library and the AWS SDK that issue E1-16 pins in the reporter group, and adds no dependency of its own; `tests/unit/reporter/test_github.py` covers the client against a stub of GitHub's API and a stub of Secrets Manager.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_github.py` passes: a status is posted with a token minted from a key generated in the test, the token is reused until five minutes before its expiry and renewed after, and neither the key nor a token is in the captured logs.
- [ ] `tests/unit/reporter/test_github.py::test_a_missing_credential_is_one_error` shows an absent secret and a secret without private_key each raising the module's error, whose text holds nothing of the stub's message.

## References

- `docs/design/system-design.md`, sections 8.4, 8.6
- `docs/adr/0001-software-factory.md`, amendment 4
