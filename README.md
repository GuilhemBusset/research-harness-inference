# research-harness-inference

A scale-to-zero inference endpoint on AWS for the students of the
[coding-agent cursus](https://github.com/GuilhemBusset/coding-agent-cursus). Students point their
harnesses (Claude Code, Codex CLI, their own SDK scripts) at one HTTPS endpoint; behind it, GPU
machines start when someone needs them and stop when nobody does. The infrastructure is Python
CDK, with uv and ty, deployed only by AWS CodePipeline, in a repository that is safe to make
public.

The repository is built and maintained by a software factory: a backlog of issues goes in,
verified and merged changes come out, and nobody is in the loop during a run.

## Status

**Design, factory and backlog in place. Nothing is deployed.**

- The system design is accepted ([ADR 0002](docs/adr/0002-accept-the-system-design.md)).
- The repository is public. `main` takes changes only through pull requests whose `required`
  check is green, and the backlog is published as GitHub issues: one epic per phase of the
  design's rollout, each with its issues as sub-issues. The owner turns the factory on with
  [`docs/factory/green-light.md`](docs/factory/green-light.md); its last steps (the factory's own
  token, the doctor) come before the implement loop's first run.
- There is no CDK application yet. The first backlog epic adds it.

## Where to read

| Document | What it is |
| --- | --- |
| [`docs/design/`](docs/design/README.md) | The system design: AWS services, request flow, scaling, access, observability, delivery, cost, risks, and the evidence behind each claim |
| [`docs/adr/0001-software-factory.md`](docs/adr/0001-software-factory.md) | The decision on how the factory works, and what it does not protect against |
| [`docs/factory/`](docs/factory/) | Running the factory: the [green-light checklist](docs/factory/green-light.md) that turns it on, and how to [operate the implement loop](docs/factory/operating.md) |
| [`AGENTS.md`](AGENTS.md) | The working conventions, for people and for every coding agent |
| [`setup/README.md`](setup/README.md) | What a machine needs, and the one-time setup scripts |
| [`backlog/`](backlog/README.md) | How new work is drafted and published as GitHub issues, which are the backlog |

## Layout

```
docs/                       design, ADRs, factory runbooks
backlog/                    the issue format, and drafts waiting to be published as GitHub issues
tools/implementation-loop/  the implement loop engine, vendored (see its UPSTREAM.md)
tools/hygiene/              the public-repository scanner: no account ID, ARN or secret gets committed
tools/backlog/              lints and publishes the backlog
scripts/                    check.sh (the one gate script), implement.sh, ship.sh, backlog.sh
setup/                      one-time setup: git hooks, the GitHub ruleset
.githooks/, .github/        the local and server-side gates
```

The CDK application, its constructs, the services and their tests will follow the "Repository
layout" section of the [system design](docs/design/system-design.md).

## Checking a change

```sh
scripts/check.sh fast   # hygiene scan, syntax, lockfile, ruff, ty
scripts/check.sh all    # the above, every test, the backlog lint, and `cdk synth` once there is an app
```

It needs `git`, `bash`, Python 3.12 or newer and [uv](https://docs.astral.sh/uv/); see
[`setup/README.md`](setup/README.md). CI runs the same script and no deployment: pull requests
never hold AWS credentials.

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md). Never put an AWS
account ID, an ARN, a credential, a real domain or a personal e-mail address in a file, an issue
or a pull request: the rules are in [`AGENTS.md`](AGENTS.md#public-repository-hygiene).

## License

[MIT](LICENSE).
