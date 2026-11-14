+++
id = "E0"
kind = "epic"
title = "Factory: what ADR 0001 leaves open, and the deployment verdict"
labels = ["kind:factory"]
+++

The software factory is built by `docs/adr/0001-software-factory.md`, and the scaffolding already
has most of it: the vendored engine with its sandbox, standing and history checks and dollar
ceilings, the hygiene scanner, the hooks, CI and the ruleset definition. This epic holds what the
ADR's Consequences and amendments leave to the backlog, and what the scaffolding does not do yet.

**The factory's own GitHub identity** (amendment 2): the engine mints the App's installation token
(E0-03). With it, GitHub refuses a factory push that changes a workflow file, so the engine hands
such a change to the owner instead of failing on the push, and brings it up to date again if
`main` moves into a conflict while the owner has not acted (E0-23).

**What the engine publishes, and what it pushes:** redaction of every text it sends GitHub (E0-04),
and a scan with the owner's local values before every push (E0-20).

**The deployment verdict** (`docs/design/system-design.md`, section 8.4; ADR decision 5 and
amendment 4). The pipeline side is the status reporter: commit statuses from fixed vocabularies,
an approval that waits for the owner included (E0-09), posted as a GitHub App installation (E0-10),
a failure report made of allow-listed parts (E0-11), the construct in the pipeline stack (E0-12), a
verdict for superseded executions (E0-13), the numbers of spikes and canaries (E0-14) and the event
by which a job hands them to the reporter (E0-27), the failed smoke tests named in the report
(E0-21), the last lifecycle stage an instance reached (E0-26), and what a status may not carry sent
to the owner by SNS (E0-22). The engine side is the `deploying` phase, which releases the landing
claim while a deployment waits for the owner's approval (E0-07), the failure summary it gives a
fix-forward (E0-29), and the items a verified deployment discharges (E0-08). The engine waits for
no context until the owner lists them in `tools/implementation-loop/loop.toml`, through a pull
request of their own, once statuses appear on merge commits: an obligation of E0-08.

**The gates the design asks for:** the pipeline and the bootstrap as protected paths (E0-02), the
scanner's rule for a student key (E0-16) and for console links and build badges (E0-24), the tests
that start a container (amendment 5: the gate step, E0-28; the CI job, which the owner lands,
E0-17; the engine's reading of its result, E0-19), and the invariant on the ruleset's source of
required checks (E0-18).

**Measurements of the factory itself:** the examiner on seeded defects (E0-05) and the cost of
each issue (E0-06).

**Only with a yes to decision 8 of the design** (owner approval for the trust boundary, amendment
3): owner review on the protected paths (E0-15), and the engine waiting for that review instead of
retrying the merge (E0-25). With a no, both drafts are deleted before publication.

## How to run this epic

Most of this epic does not need the rest of the backlog, but a few issues wait for much of it, and
launching the whole epic (`/implement E0`) would pull those prerequisites in as external issues:
through E0-08, E0-21 and E0-26 (which wait for the verification step of E2-64) and E0-05 (which
waits for E2's security groups and tables), about 65 issues of E1 and E2. So:

- **Run alone, one at a time, before E1** (`/implement <n>` for each): the engine and gate issues
  E0-02, E0-03, E0-04, E0-06, E0-07, E0-29, E0-19, E0-20, E0-23, E0-28, E0-18 and E0-24. They touch
  the engine and its configuration, so they serialize anyway; E0-02 should land before E1-08 and
  E1-09 create the paths it protects. E0-17 follows E0-23 and E0-28, and is landed by the owner.
- **Run after E1-10** (the pipeline deploys): the reporter chain E0-09, E0-10, E0-11, E0-12,
  E0-13, E0-14, E0-22 and E0-27. E0-09, E0-10, E0-11 and E0-14 can start as soon as their E1
  blockers land.
- **Run after E2**: E0-21, then E0-26 and E0-08, which wait for it; and E0-05. E0-16 waits only
  for the key format of E2-01.
- **Only with a yes to decision 8**: E0-25, then E0-15.

Not drafted, because the ADR only asks to revisit it: a scheduler that picks up ready issues without
a launch.
