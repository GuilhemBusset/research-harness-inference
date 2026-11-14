+++
id = "E1-15"
title = "The owner's setup writes the list of real values the scanner checks for, outside the repository"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E1"
blocked_by = ["E1-13"]
+++

## Context

No public pattern can name the real domain or the admin's address without leaking them. Section 10
of the design answers with two checks that know the literal values: an owner-local hook that holds
them in an untracked file, and the pipeline's synth step (issue E1-10). The hygiene scanner already
has the first half: wherever a list of values exists outside the repository, every scan on that
machine also reports those values, under the rule `local-value` (`tools/hygiene/README.md`, "Values
only the owner knows"). Nothing writes that list today, so the check exists only if the owner
remembers to create it.

The setup walkthrough of issue E1-13 is where the owner gives the real configuration, so it writes
the list. The values are those of the keys the configuration loader names as private (issue
E1-03): the domain, the hosted zone ID, the admin e-mail address, the instructor's phone number and
the login app's client ID, the same ones the pipeline's leak check looks for. Agents never see the
list: it is outside the repository and the sandbox's home directory is empty. The scanner and its
allow-list are not changed.

## Deliverables

- [ ] `setup/owner-setup.sh` gains a step, after the one that writes the configuration to SSM, that writes the value of each private key of that configuration, and any further value the owner enters at its prompt, to the scanner's list of local values: the file the environment variable HYGIENE_LOCAL_VALUES names, or else the scanner's default file under the owner's home directory.
- [ ] The step creates the list with access for its owner only, adds a value only once however often it runs, and prints how many values the list holds, never a value.
- [ ] The step refuses a list whose path is inside the repository's working tree, and with the option --dry-run it prints what it would do and writes no file.
- [ ] `docs/runbooks/owner-setup.md` describes the step and how to check it: a scan of a scratch file that holds one of the values.
- [ ] `tests/unit/test_owner_setup.py` covers the step with the list in a temporary directory.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_owner_setup.py` passes: after the step the list holds each value once and only its owner can read it, the scratch copy of the repository is byte-identical, and `tools/hygiene/scan.py` then reports a scratch file that holds one of the values under the rule local-value.
- [ ] `tests/unit/test_owner_setup.py::test_the_list_is_never_written_in_the_repository` shows the step refusing a list placed inside the working tree.
- [ ] The owner runs the walkthrough through this step with the account's credentials, and a scan on the owner's machine then reports the real domain in a scratch file as local-value (needs aws:account).

## References

- `docs/design/system-design.md`, sections 8.6, 10
- `tools/hygiene/README.md`
