+++
id = "E1-13"
title = "A walkthrough of the owner's one-time steps that writes nothing account-specific"
labels = ["size:M", "kind:docs"]
epic = "E1"
blocked_by = ["E1-08", "E1-17"]
+++

## Context

Section 8.6 lists what only the owner can do, once, in this order:

1. Request GPU quotas. The G family now: 8 vCPUs for each coursework instance, for one instance more
   than `max_instances` (section 5.4: a draining instance and a cold launch can overlap), and 96 more
   if the upgrade pool is wanted. The P family only for the optional xl pool.
2. Register a domain.
3. Create three GitHub Apps: student login (no permissions, device flow on), status reporter
   (commit statuses on this repository), and the factory's identity (ADR 0001, amendment 2).
4. Write the configuration to SSM: domain, admin e-mail, session schedule, ceilings and budget.
5. Turn on billing alerts in the Billing console.
6. `cdk bootstrap` with the project's execution policy and permissions boundary, then deploy the
   pipeline stack once from a laptop.
7. Authorize the GitHub connection for this repository only.
8. Apply the repository ruleset; enable secret scanning and push protection; set a noreply commit
   address.
9. Enter the roster through the admin CLI.

The design asks for a script that walks through them and writes nothing account-specific to the
working tree. The steps themselves are obligations of the owner, carried where the work that needs
them is: the bootstrap on issue E1-08, the first deployment and the connection on issue E1-10, the
ruleset and the repository settings of step 8 in `docs/factory/green-light.md`, the roster on the
admin CLI's issue, the factory's App on issue E0-03, the confirmation of each SNS subscription on
the issue that creates its topic. This issue carries the rest. The first deployment (issue E1-10)
needs the configuration in SSM, so this walkthrough waits for nothing but the bootstrap policies it
points to and the configuration writer.

**Step 3 here, and in issue E1-18.** A script cannot set a GitHub App's permissions: the owner sets
them in the browser. So this issue's Apps step prints, for each app, the permissions to set and the
GitHub page where an App is registered. Issue E1-18 completes the step: it reads each App back from
GitHub and stores the status reporter's credentials in the reporter's secret.

The decisions of section 15 behind the steps: the domain is decision 10; the session schedule
rests on decision 1 (dates); the ceilings and the monthly budget on decision 6 (money); the
coursework pool's `max_instances`, and with it the quota and the daily ceiling, on decision 5
(class size: the defaults of section 4.2, 2 instances and 16 GPU-hours a day, fit a class of twenty
working hands-on); whether the upgrade pool is wanted on decision 3; the region, which is the one of
the credentials the owner bootstraps with, on decision 9.

Phases 0 and 1 need no GPU quota, but section 14 asks for the quota request the day the owner
approves, because nobody documents how long it takes. The owner need not wait for this script to
send it.

**The configuration step.** It validates the owner's file with the configuration loader of issue
E1-03 before anything is written, and writes it with the writer of issue E1-17, so that the
walkthrough and the pipeline read the same keys the same way. The loader refuses a session window
and a pool entry until the issues that give them their shape (E4-33 for windows, E2-69 for pools),
so what the owner writes now holds the time zone, the course end date, an empty list of session
windows and an empty pools table. The owner adds the windows and each pool later, through the owner
items of those issues. The GPU-hour ceilings belong to each pool's entry; the monthly budget is a key
of the configuration from the start.

## Deliverables

- [ ] `setup/owner-setup.sh` walks through the nine steps of section 8.6 in their order, asks before each command it runs, writes what it learns to SSM or to the terminal and never to the working tree, and with the option --dry-run prints every step and every command and calls neither AWS nor GitHub.
- [ ] Its quota step computes the G-family request from the coursework pool's max_instances, which the owner gives: 8 vCPUs for each instance for one instance more than that number, plus 96 vCPUs if the owner wants the upgrade pool; it asks for a P-family quota only if the owner wants the xl pool.
- [ ] Its GitHub Apps step prints, for each of the three apps, the permissions of section 8.6 to set and the GitHub page where an App is registered, and says that the login app's client ID goes into the configuration and that the factory App's key stays on the factory's host.
- [ ] Its configuration step can be run on its own, with an option that names it, so that runbooks and the owner items of later issues use it to change the configuration; it takes a configuration file the owner keeps outside the working tree, refuses a file inside it and a file the configuration loader refuses, and writes the configuration to SSM with the configuration writer.
- [ ] `docs/runbooks/owner-setup.md` describes each step: what it needs, the command, how to check it, and the decision of section 15 it rests on, with placeholders only.
- [ ] `tests/unit/test_owner_setup.py` runs the dry run in a scratch copy of the repository.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_owner_setup.py` passes: the dry run prints the nine steps in order, the G-family request it prints is 24 vCPUs for a max_instances of 2 and 120 with the upgrade pool, and the scratch copy is byte-identical afterwards.
- [ ] `tests/unit/test_owner_setup.py::test_the_configuration_comes_from_outside_the_tree` shows the dry run refusing a configuration file inside the working tree, and one the configuration loader refuses.
- [ ] The owner requests the G-family quota the walkthrough computes for the class size of decision 5 (needs aws:account).
- [ ] The owner chooses the domain, decision 10 of the design, and registers it (needs human:owner-decision).
- [ ] The configuration is in SSM, written by the walkthrough, with the domain, the admin e-mail address, the time zone, the course end date, the monthly budget of decision 6, an empty list of session windows and an empty pools table (needs aws:account).
- [ ] Billing alerts are turned on in the Billing console (needs aws:console).

## References

- `docs/design/system-design.md`, sections 4.2, 5.4, 8.6, 14, 15
- `docs/adr/0001-software-factory.md`, amendment 2
- `docs/factory/green-light.md`
- `setup/bootstrap/README.md`, delivered by the bounded-bootstrap issue
