+++
id = "E4-22"
title = "Runbooks: class-day preflight, capacity failure, a tripped ceiling or kill switch and re-arming"
labels = ["size:M", "kind:docs"]
epic = "E4"
blocked_by = ["E1-13", "E4-03", "E4-06", "E4-07", "E4-27"]
+++

## Context

Three of the nine runbooks of section 9, the ones an instructor needs on a class day. Each runbook
is tested once, by a walk-through on the deployed system; like every check against the endpoint, it
assumes the owner has turned on the public HTTPS listener, which is off by default (issue E2-47).

- **Preflight.** The day before: the session's window and its number of instances are in the
  configuration (issue E4-33), and the GPU quota covers one instance more than that number (section
  5.4). The pool is pre-warmed from 60 minutes before (section 5.4); thirty minutes before, the go or
  no-go notice arrives (issue E4-06). Deployments are held from one hour before to one hour after
  (issue E4-07), so a configuration change has to land before that.
- **Capacity failure.** The group spans three zones and may list fallback types; a warm instance
  that fails to start is replaced by a cold launch; the gateway holds until the deadline, then
  answers `capacity_unavailable` (section 5.4). The runbook has one fallback command: switch the
  default model to a pool that is up (section 5.4; issue E4-27). Whether a managed model may stand
  in when no pool is up is the owner's decision 12, which the design does not build.
- **Ceiling or kill switch tripped.** A pool at a GPU-hour ceiling, latched by the budget at 100%,
  or disabled by the admin answers `pool_ceiling_reached` or `pool_disabled` (sections 6.2 and 11.3).
  Re-arming: the admin CLI clears a latch or re-enables a pool (issue E4-03); a ceiling is
  configuration, so raising it is a deployment, which the hold around a session delays.

**What "every command it shows" means, for these runbooks and the six of issues E4-23 and E4-24.**
A step is one of three kinds:

- A command of this repository: the rhi CLI, the admin CLI, or a script of the repository. It is
  written in a code block and checked: an rhi command parses with the rhi CLI's parser, an admin
  command with the parser of the admin CLI's command line (`admin/rhi_admin/main.py`, which also
  declares its program name), and a script exists at the path the block names.
- A change to the configuration in SSM (a session window, a ceiling, the flag parked): there is no
  command for it beyond the owner's own. It is written as an edit of the owner's configuration file,
  kept outside the working tree, followed by the configuration step of the owner-setup script
  `setup/owner-setup.sh` (issue E1-13), which validates and writes it; the script is checked to
  exist.
- A step in the AWS console, on GitHub, or with another tool (aws, git, gh): written out in full, and
  not checked, since no parser of this repository can check it. The walk-through on the deployed
  system is what tests it.

The test tells the kinds apart by a code block's first word: rhi, the admin CLI's program name, or a
path of the repository; any other first word is a step of the third kind.

## Deliverables

- [ ] `docs/runbooks/class-day-preflight.md`: what to check the day before and on the day, what the go or no-go notice says, and what to do on a no-go.
- [ ] `docs/runbooks/capacity-failure.md`: how a capacity failure shows during a session, the one command that switches the default model to a pool that is up, and how to switch back.
- [ ] `docs/runbooks/ceiling-tripped.md`: how a GPU-hour ceiling, the budget latch and a disabled pool each show, how to decide whether to re-arm, and the commands and configuration changes that re-arm each.
- [ ] Each runbook has the sections when, check, act and verify, and writes each step as one of the three kinds of the context.
- [ ] `tests/unit/test_runbooks.py` checks, for every runbook it lists, the four sections, that every rhi command parses with the rhi CLI's parser, that every admin command parses with the admin CLI's parser, and that every path of the repository a code block starts with exists.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_runbooks.py` passes for the three runbooks: each has the four sections, each rhi and admin command it shows parses, and each script it names exists.
- [ ] `tests/unit/test_runbooks.py::test_detects_an_unknown_command` fails a runbook, built in the test, that shows an rhi subcommand the CLI does not have, and one that names a script that does not exist.
- [ ] Each of the three runbooks is walked through once on the deployed system, and its date is recorded at its foot (needs aws:deployed-environment).
- [ ] The owner says whether a managed model may stand in for a session when no GPU capacity is available, decision 12 of the design (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 5.4, 6.2, 8.2, 9, 11.3, 15
