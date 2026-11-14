# Tests that start a container

A test belongs here when the only way to make its check is to start a container: the real image
of a component is run and the test talks to it. The first is the Grafana sign-in test of the
system design ([`docs/design/system-design.md`](../../docs/design/system-design.md), section
7.4). A test that can make its point without a container belongs in one of the other folders
under `tests/` (see "Where tests live" in [`AGENTS.md`](../../AGENTS.md)).

## Who runs them

The local gate does not run this folder. The host of the coding agents has no container runtime
(ADR 0001, amendment 5), so:

- `scripts/check.sh fast` and `scripts/check.sh all` do not run it;
- `scripts/check.sh tests`, and any `pytest` run that names no path, does not collect it:
  `pyproject.toml` keeps the folder out of the default test run.

CI's job `container-tests` runs it, with one step of the gate script, and that job is part of
the `required` check:

```sh
scripts/check.sh container-tests
```

The step runs the folder with the locked pytest. It fails when no container runtime answers, and
under CI it fails when one of its tests is skipped, so a green result always means that the tests
ran. While the folder holds no test, the step says so and passes. On a machine that has Docker,
the same command runs the tests locally.

## In an issue

An acceptance item that rests on a test in this folder cannot be shown on the agents' host. It
ends with the capability it needs:

```text
- [ ] `tests/container/test_grafana_sign_in.py` passes (needs host:container-runtime)
```

The agents still write the test and everything that can be checked without a container. The run
of the test is CI's.

## Writing one

- Name the file `test_*.py`, as everywhere else.
- Do not skip when the runtime or an image is missing: a skip fails the CI job. Let the test
  fail.
- The rules of the other tests hold here too: no AWS credentials, and nothing that identifies
  the account.
