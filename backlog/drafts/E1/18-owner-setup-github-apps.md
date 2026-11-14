+++
id = "E1-18"
title = "The walkthrough reads the GitHub Apps back and stores the status reporter's credentials"
labels = ["size:S", "kind:docs"]
epic = "E1"
blocked_by = ["E1-13"]
+++

## Context

Step 3 of section 8.6: the owner creates three GitHub Apps, student login (no permissions, device
flow on), status reporter (commit statuses on this repository) and the factory's identity (ADR 0001,
amendment 2). A script cannot set an App's permissions; the owner does it in the browser, guided by
the walkthrough's Apps step (issue E1-13). This issue completes that step.

**Reading each App back.** After the owner has registered an App, the step asks for its public name
(its slug), reads the App's ID and permissions back with the GitHub CLI, which the owner is logged in
to (`setup/README.md`), and says whether the permissions are the ones section 8.6 gives. A private
App can be read this way only by an authenticated caller.

**The status reporter's credentials.** The reporter's construct (issue E0-12) refers to a Secrets
Manager secret it does not create, named rhi/reporter/github-app, whose JSON value holds the fields
app_id, installation_id and private_key that the reporter's client reads (issue E0-10). The step
creates that secret, or updates its value, from the App's ID it read back, the installation ID the
owner gives and the private key file the owner downloaded, which stays outside the working tree.
The key is never printed. Until the owner has stored it, the reporter posts nothing (issue E0-12).

The factory App's private key stays on the factory's host, outside the repository and outside the
sandbox, where issue E0-03 reads it: the factory has no AWS credentials to read Secrets Manager
with. The login app needs no secret for the device flow; its client ID goes into the configuration.

## Deliverables

- [ ] `setup/owner-setup.sh` reads each App the owner has registered back with the GitHub CLI, by the slug the owner gives, prints its ID and its permissions, and says whether they are those of section 8.6.
- [ ] For the status reporter, it asks for the installation ID on this repository and the path of the private key file, refuses a key file inside the working tree, and writes the fields app_id, installation_id and private_key to the Secrets Manager secret rhi/reporter/github-app, creating it if it is absent, without printing the key.
- [ ] With the option --dry-run, the step prints the commands it would run, the secret's name included, and calls neither AWS nor GitHub.
- [ ] `docs/runbooks/owner-setup.md` describes the step and how to check it, with placeholders only, and `tests/unit/test_owner_setup.py` covers its dry run.

## Acceptance criteria

- [ ] `tests/unit/test_owner_setup.py::test_the_reporter_key_comes_from_outside_the_tree` shows the dry run refusing a key file inside the working tree, printing the secret's name and never the content of a key file outside it, and leaving the scratch copy of the repository byte-identical.
- [ ] The student login app exists with no permission and the device flow on, the status reporter app exists with commit statuses only and is installed on this repository only, and its three fields are in the secret rhi/reporter/github-app, stored by the walkthrough (needs aws:account).

## References

- `docs/design/system-design.md`, sections 6.4, 8.4, 8.6
- `docs/adr/0001-software-factory.md`, amendments 2 and 4
- `setup/README.md`
