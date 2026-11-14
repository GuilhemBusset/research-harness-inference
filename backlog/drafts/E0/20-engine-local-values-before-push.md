+++
id = "E0-20"
title = "Engine: scan with the owner's local values before every push"
labels = ["size:L", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

No public pattern can name the real domain or the admin's address, so the owner keeps those values
in a list outside the repository, and the hygiene scanner reports them under the rule `local-value`
wherever that list exists (`tools/hygiene/README.md`, "Values only the owner knows"). One path is
not covered today, and that README says so: the implement loop pushes its branches with its own git
calls, which run no hooks, and its verifier scans inside the sandbox, where the list does not exist.
A loop branch is checked against every pattern, in its tree and in its history, but not against the
owner's list. The agents never see those values, so one can only reach a loop branch through text
the owner wrote into an issue.

The engine process itself runs outside the sandbox, as the owner. This issue has it scan with the
owner's list immediately before each push, and treat a finding as it treats a finding in a branch's
history (ADR 0001, amendment 7): the branch is abandoned unpushed, the issue ends `failed` with the
reason `history`, and a retry starts a new branch.

Two things follow from where this scan runs. It is outside the sandbox, so it must execute nothing
a builder wrote: it runs the scanner of the checkout the engine runs from, never the copy in the
branch. And what it finds is the secret itself, so nothing of a finding but the rule's name and the
path may be published, given to an agent or kept in the run's records.

This issue changes the engine and its configuration module, and one paragraph of the hygiene
scanner's documentation: protected paths on the safety surface (ADR 0001, decision 10). The builder
may change the files named below because this issue names them. The existing history check is
`HISTORY_CHECKS` in the configuration module and `_history_scan` and `_history_refused` in the
engine.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/engine.py` runs, immediately before each push of a branch and in the engine's own process outside the sandbox, the hygiene scanner over the tree of the head and over the commits the push would publish, with the owner's list of local values, when that list exists.
- [ ] That scan runs the scanner and the allow-list of the checkout the engine runs from, never the branch's copy of them, and executes nothing else from the branch outside the sandbox.
- [ ] `tools/implementation-loop/implement_loop/config.py` holds the command of that scan beside the history checks.
- [ ] A finding of that scan is handled as a finding of the history check is: the branch is abandoned unpushed, the issue ends failed with the reason history, and its next attempt starts on a new branch; a revert with such a finding is not pushed.
- [ ] Of such a finding the engine keeps and passes on the rule's name and the path only: the value, the line it is on and the scanner's excerpt appear in nothing the engine publishes, in no prompt of an agent, and in no event, log or state file of the run.
- [ ] Where no list of local values exists, no scan runs outside the sandbox and the push goes ahead as before; a scan that cannot run withholds the push, as a history check that cannot run does.
- [ ] `tools/hygiene/README.md` no longer says that the loop's pushes are not checked against the owner's list, and says where they are.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_history_checks.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_history_checks.py` showing that a value of a list built in the test, present in the tree of the head or only in an earlier commit of the branch, ends the issue failed with the reason history and nothing pushed, that a retry starts a new branch, and with no earlier test removed or weakened.
- [ ] `tools/implementation-loop/tests/test_history_checks.py` shows that the value, the text of its line and the scanner's excerpt are in no text the fake GitHub received, in no prompt an agent received and in no event or state file of the run, while the rule's name and the file's path are.
- [ ] `tools/implementation-loop/tests/test_history_checks.py` shows that a branch whose own copy of the scanner accepts everything is still refused, and that nothing of that copy was executed outside the sandbox.
- [ ] `tools/implementation-loop/tests/test_history_checks.py` shows that with no list in place no scan runs outside the sandbox and the push goes ahead, and that a list that cannot be read withholds the push.

## References

- `tools/hygiene/README.md`
- `docs/adr/0001-software-factory.md`, decisions 4 and 10, amendment 7
- `docs/design/system-design.md`, section 10
