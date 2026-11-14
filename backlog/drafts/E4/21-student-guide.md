+++
id = "E4-21"
title = "The student guide: install, log in, point a harness, wait for a cold pool, and what this model cannot do"
labels = ["size:M", "kind:docs"]
epic = "E4"
blocked_by = ["E2-54", "E2-55", "E3-41", "E4-20"]
+++

## Context

Students install the CLI with one `uv tool install` command from this repository; the student guide
gives it (section 8.1). The guide is also where several risks are handled by words:

- **Login** (section 6.4): confirm only a code your own terminal just printed; each login shows when
  the previous key was issued; a student has at most two installations; the endpoint comes once,
  from the course's private channel, with `rhi login --endpoint`.
- **Harnesses** (sections 5.3, 6.1 and 6.4): `rhi login` sets up Claude Code and Codex to fetch the key
  through a helper, and writes a Codex profile with a 15-minute stream timeout, without which Codex
  fails a long hold at 5 minutes; an SDK script reads the key from `rhi token`. A model is chosen by
  name, and `claude-*` names go to the default pool (section 6.2, step 4).
- **Waiting** (section 5.3): a stream for a cold pool is held up to 7 minutes with keep-alives, a
  non-streaming call up to 4; `rhi wait` is the documented way to start a homework session on a cold
  pool, and `rhi status` shows each pool as cold, warming, ready or unavailable.
- **What the coursework model cannot do here** (section 4.2, second table; section 6.1): a PDF must
  be converted to text first, which the agent can do itself; Claude Code's built-in web search is run
  by its vendor's service and does not work through this endpoint, while the course's science skills
  search public services from the student's machine; several agents at once each take a stream on
  the shared GPU. Harness versions matter: the request profile is tested against named versions
  (section 13), recorded by spike S3 in `docs/measurements/s3.md`.
- **Budget and keys** (sections 6.2, 6.4 and 17): a daily token budget, 100 million tokens by
  default, which counts every input token, cached or not; 4 concurrent requests by default; never
  commit a key; `rhi revoke`, or `rhi revoke --all` when a key is lost; a revoked key stops working
  within 60 seconds.

**What the test checks, and what it does not.** The guide shows commands of several tools: `uv`,
`claude`, `codex` and `rhi`. Only the `rhi` commands belong to this repository, so only they are
checked, against the CLI's own parser. The others are shown as their tools document them.

**Hosts.** The endpoint is private to the course and comes from its channel, so every endpoint
address the guide shows is on example.com. The install command is the one exception: it names this
repository on github.com. The design does not give the repository's GitHub owner, so the guide writes
the address with the placeholders OWNER and REPOSITORY, which the owner fills in when the guide is
handed to students; the guide says so in a line for the instructor.

The guide shows no key, not even an invented one: the hygiene scanner reports anything shaped like a
key (issue E0-16). It names the prefix `rhi_` alone, which the scanner leaves alone.

## Deliverables

- [ ] `docs/student-guide.md` covers, in this order: installing the CLI with one command; logging in, with the warnings of the context; pointing Claude Code, Codex and an SDK script at the endpoint; choosing a model; waiting for a cold pool; what the coursework model cannot do through this endpoint, with the workaround for each, and the harness versions it was tested with; what each of the twelve causes of section 6.2 means and what to do; the daily budget and the concurrency limit; keeping, revoking and replacing a key; where the privacy notice is.
- [ ] Every rhi command it shows is one the CLI's parser accepts, every address in it is on example.com except the install command's address on github.com, and that address carries the placeholders OWNER and REPOSITORY with a line that tells the instructor to fill them in.
- [ ] `tests/unit/test_docs.py` checks the guide's rhi commands against the CLI's own parser, its addresses and its twelve causes.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_docs.py` passes: every rhi command of the guide parses with the CLI, each of the twelve causes of the shared enumeration is explained, and every address other than the install command's is on example.com.
- [ ] The owner fills in the repository's address in the install command when the guide is handed to students (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.2, 5.3, 6.1, 6.2, 6.4, 8.1, 13, 17
- `docs/measurements/s3.md`, delivered by issue E3-41
