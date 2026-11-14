# Security Policy

This repository holds the infrastructure code of an inference endpoint that is deployed to a
real AWS account, and the software factory that builds it. It is public by design: no secret and
nothing that identifies the account belongs in it, and several gates exist to keep it that way.
Reports about either are taken seriously.

## Reporting a vulnerability

Report suspected vulnerabilities **privately**, not in a public issue: use GitHub's **Report a
vulnerability** button under the repository's **Security** tab (private vulnerability reporting).

Include enough detail to reproduce: the affected file, workflow or deployed behaviour, the steps
involved, and the impact.

If you find a **credential, an AWS account identifier or another leaked value** in the
repository, in its history, in an issue or in a pull request, report it the same private way. Do
**not** open a public issue or pull request that quotes it.

## Scope

In scope:

- the infrastructure and service code, and what it deploys: authentication and API keys,
  budgets, network exposure, IAM policies;
- the gates: `.githooks/`, `.github/workflows/`, `scripts/check.sh`, `tools/hygiene/`,
  `setup/pr-only-main.json`;
- the implement loop (`tools/implementation-loop/`) and its sandbox, including any way for an
  agent to reach credentials or to land a change around the gates;
- anything committed that should not have been.

Out of scope: third-party tools and services this repository uses or links to (report those
upstream), and the limits already stated in
[ADR 0001](docs/adr/0001-software-factory.md) under "What the sandbox does not protect".

## Response

The repository is maintained by one person. Expect an acknowledgement within a few business
days.
