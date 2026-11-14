+++
id = "E0-24"
title = "Hygiene scanner: refuse links to the AWS console and build badges"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

Section 10 of the design: "no console links or build badges anywhere public". `AGENTS.md` lists both
among what is never written. A console link carries the region and the resource it opens, often the
account; a build badge points at a project in the account. Only the reporter is held to the rule
(issue E0-09: a status carries no link). Nothing scans the repository for either, so a link pasted
into a runbook or a README would reach a commit, the loop's verifier and CI unchallenged.

The scanner's rules are code in `tools/hygiene/scan.py`, each documented in the table "Rules" of
`tools/hygiene/README.md`. The tree has no console link and no badge today. The values the tests
need are assembled at run time, so that the test file itself scans clean.

A console link is a URL, with its scheme, whose host is console.aws.amazon.com or
signin.aws.amazon.com, or ends with one of them after a dot (a region's console, an account alias's
sign-in page), or is one of the console and sign-in hosts of the China and GovCloud partitions. A
build badge is a URL of the badge of a CodeBuild project, of the badge image of a GitHub Actions
workflow, or of a shields.io badge of a build or workflow status. AWS's documentation pages, service
endpoints, and a host name written without a scheme (as in this paragraph) are neither.

This issue changes `tools/hygiene/`, a protected path on the safety surface (ADR 0001, decision 10);
the builder may change the files named below because this issue names them. The allow-list
`hygiene.toml` is not changed.

## Deliverables

- [ ] `tools/hygiene/scan.py` gains a rule console-link, which reports a link to the AWS console as the context of this issue defines it, and a rule build-badge, which reports a build badge as it defines it.
- [ ] An inline marker can allow either rule on its line, like the other content rules, and the redaction mode masks both.
- [ ] `tools/hygiene/README.md` lists both rules in its table "Rules", with what they fire on and what they do not.
- [ ] `tools/hygiene/tests/test_console_and_badges.py` covers both rules, with values assembled at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tools/hygiene/tests/test_console_and_badges.py` passes: a regional console link, a sign-in page of an account alias, a CodeBuild badge, a GitHub Actions badge and a shields.io build badge are each reported, and masked by the redaction.
- [ ] `tools/hygiene/tests/test_console_and_badges.py::test_look_alikes_stay_quiet` shows no finding for a page of AWS's documentation, a service endpoint, a console host name written without a scheme, and a shields.io badge that shows a licence.
- [ ] `scripts/check.sh hygiene` exits 0 on the tree as it stands.

## References

- `docs/design/system-design.md`, section 10
- `AGENTS.md`, "Public-repository hygiene"
- `tools/hygiene/README.md`
- `docs/adr/0001-software-factory.md`, decisions 4 and 10
