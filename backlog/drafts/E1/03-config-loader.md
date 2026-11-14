+++
id = "E1-03"
title = "Load configuration from the example or a file, and fail closed"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E1-02"]
+++

## Context

Nothing that identifies the account may be in git (section 10). The app therefore reads its
configuration through one loader with three sources (section 8.1): the committed example, which
every test and every agent uses; a file that git does not track, which the pipeline's synth step
writes from SSM outside its checkout (section 8.2, step 2; issue E1-10); and SSM itself, read with the caller's own credentials, which is how the
owner deploys the pipeline stack once from a laptop (section 8.6, step 6). This issue builds the
loader with the first two; issue E1-17 adds the SSM source, the writer that the synth step and the
owner's walkthrough use, and the AWS SDK they need, so that the app and its tests do not wait for
them. The region and the account never come from configuration: they come from the deploying
credentials (assumption A4).

**Which keys this issue introduces.** The loader fails closed: a source that lacks a required key,
or holds an unknown one, is refused. Once the pipeline is deployed, a later merge that added a
required key the owner has not yet written to SSM would fail the pipeline's synth step, and no agent
can write SSM. So this issue introduces every value only the owner can supply, the ones the owner
writes to SSM once (section 8.6, steps 2 to 4, through the walkthrough of issue E1-13):

| Key | Why | Read by |
| --- | --- | --- |
| The domain, and the ID of its hosted zone | The owner's domain, decision 10; no lookup may find the zone (section 10) | The edge, phase 1 |
| The admin e-mail address | Cognito's one admin user, the owner's SNS topics (sections 7.3, 7.4, 8.4) | The pipeline's own topic, then phase 1 |
| The instructor's phone number | The class-day notice also goes to the instructor's phone (section 7.3) | Phase 3 |
| The GitHub repository, as owner and name | The pipeline's source (section 8.2, step 1) | The pipeline, this epic |
| The login app's client ID | Kept out of the repository; the issuer serves it (section 6.4) | The issuer, phase 1 |
| The monthly budget in US dollars | The backstop that latches every pool (section 11.3), decision 6 | Phase 3 |
| The schedule: its time zone, the course end date and the session windows | Sessions (decision 1), the nightly stop "in the configured time zone" and the expiry of keys and data at course end (sections 6.4, 6.6, 17) | Phases 1 and 3 |
| The pools table | One entry per pool (section 4.2) | Phase 1 on |

**The rule every later issue inherits: a key or a pool field added after this issue has a default
or is optional**, so that a merge never needs a value the owner's deployed configuration lacks. The
keys of the table above are the only required ones, and a test holds that set. An issue that adds a
key names `src/rhi/config.py`, `config.example.toml` and `tests/unit/test_config.py` in its
Deliverables, states the key and its default, and says how the value reaches the service that reads
it (an environment variable, or a parameter). The default is the one the design states, most of them
in section 17 (the student's daily token budget of 100 million tokens, the hold, the rate limits and
the rest); where the design gives none, the issue that adds the key chooses one that keeps the
deployed stage as it was, and says so in its Context. Pool fields follow the same rule in the pool
entry's parser (issues E2-04, E2-68 and E2-69). Where a merge cannot work until the owner writes something to SSM (a
new pool), the issue carries an owner item that names what to write, and its code deploys cleanly
before the owner writes it: a pool that is not in the configuration is simply not built.

**Lists whose entries have no shape yet stay empty.** The session windows and the pools table are
keys of this issue, but their entries get their shape later: issue E4-33 gives a window its shape (a
start, an end, the pools it pre-warms and the instances each needs, sections 4.7 and 5.1), its
checks, an invented schedule in the example and the owner item that writes the owner's dates
(decision 1); issues E2-04, E2-68 and E2-69 give a pool entry its shape and E2-69 adds the mock pool to the example; the
coursework pool's issue (E3-10) adds that pool with the values of sections 4.2 and 17 (16 GPU-hours
a day), the monthly ceiling of 150 arrives with issue E4-02, the stickiness of 3600 s is a pool
setting with that default (issue E3-28), and the upgrade pool arrives with its own issue. Until
then, the loader refuses a session window or a pool entry, so that the owner's configuration in SSM
keeps both lists empty and holds nothing that a later, stricter parser could refuse. The issue that
gives entries their shape replaces that refusal with its parsing. The time zone and the course end
date are read and checked here: an IANA time zone name, and a date.

The loader also names the keys whose values must never appear in the repository, so that the
pipeline's leak check (issue E1-10) and the owner's list of local values (issue E1-15) look for
exactly those. Numbers and dates are not among them: no search could find them without noise. For
the same reason an error names a key or a source, never a value: the pipeline's build logs show it.

## Deliverables

- [ ] `src/rhi/config.py` loads a typed configuration from the source the environment variable RHI_CONFIG names: example (the committed `config.example.toml`) or file (config.local.toml at the repository root, or the TOML file the environment variable RHI_CONFIG_FILE names); an unset or any other value raises.
- [ ] It fails closed: an unknown key, an unreadable source, a missing key that has no default, or a value of the wrong type raises an error that names the key or the source and never a value, and nothing falls back to the example.
- [ ] A key may be left out of a source only if the module's one table of defaults gives it a value: the default the design states for that setting, or, where the design gives none, the one the issue that added the key chose and stated; this issue adds no entry.
- [ ] The configuration holds the keys of the table in Context, all required; the session windows and the pools table must be empty lists; it holds no account ID and no region; and the module names the private keys: the domain, the hosted zone ID, the admin e-mail address, the instructor's phone number and the login app's client ID.
- [ ] `config.example.toml` gives every key the loader knows, with placeholders only: example.com for the domain and the address, 123456789012 wherever an account is shown, and an invented time zone, course end date, budget, phone number, zone ID and client ID; its list of session windows and its pools table are empty.
- [ ] `tests/unit/test_config.py` covers both sources, each failure and the table of defaults.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_config.py` passes: the two sources give the same typed object for the same values, and each failure above raises and names what is wrong without the value.
- [ ] `tests/unit/test_config.py::test_a_default_fills_a_missing_key` shows a key left out of a source taking the value of an entry the test adds to the table of defaults, and a key with no entry raising.
- [ ] `tests/unit/test_config.py::test_the_example_gives_every_key_a_placeholder` fails when a key the loader knows is missing from `config.example.toml`, when the example gives a key that has a default another value than that default, and when it holds a domain or an e-mail address outside example.com or a 12-digit number other than 123456789012.
- [ ] `tests/unit/test_config.py::test_only_the_owners_keys_are_required` fails when a key outside the table in Context has no default and is not optional, and when a session window or a pool entry is accepted.

## References

- `docs/design/system-design.md`, sections 4.2, 6.4, 6.6, 7.3, 8.1, 8.2, 8.6, 10, 11.3, 15, 17
