+++
id = "E3-45"
title = "Run each spike as a job the owner starts, from an explicit list"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E0-27", "E2-64", "E3-14"]
+++

## Context

Spikes that need AWS run as pipeline jobs; the owner starts the ones that cost real money, and a
spike job is never started by a merge (sections 8.4 and 12). The shared code is issue E3-14.

**An explicit list.** `src/rhi/pipeline/spikes.py` holds the list of spike jobs: each entry names
the job and the script under `spikes/` it runs. A job exists only for an entry, so a module under
`spikes/` that is not a job (`spikes/common.py`, the load generator of issue E3-36) never becomes
one. This issue builds the jobs from the list and leaves the list empty; each spike's issue adds its
own entry, and only that line of the protected module changes. The rules every spike job keeps are a
policy test over every entry, so adding an entry changes no reviewed list.

**What a job is.** A CodeBuild project of the pipeline stack, started by the owner on a commit of
`main` (its head by default), which reads this repository through the pipeline's connection and has
no webhook, no schedule and no rule that starts it. It installs the locked environment with uv and
runs its script with the repository's package roots on the import path. Its role may: read the
configuration's parameters; read the pool-state records; read the spikes' secret of issue E3-14 by
its fixed name; query the Prometheus workspace; send the measurement event, through the grant of
issue E0-27; write its own logs. Nothing else: no Auto Scaling, EC2 or IAM action, and no other
secret.

**One role, one log group and one key for every job.** cdk-nag acknowledges each finding by its own
id, which carries the logical ID of the resource it is on, and has no prefix matching (issue E1-07).
CodeBuild's construct adds, for each project, statements on that project's own log group and report
groups to the role it is given, so a role per job, or a shared role that each project extends, would
bring a new finding with every entry, in the protected file
`tests/policy/acknowledged/pipeline_spikes.toml`, which a spike's issue does not name. So
`src/rhi/pipeline/spikes.py` creates one role, one log group and one key, once, whatever the list
holds; it writes the role's statements itself (the list above, the pipeline's connection, writing to
that log group and using that key), hands the role to every job in a form that accepts no new
statement (CDK's `without_policy_updates`), and points every job's logs at the shared group, with no
report group. What the AWS Solutions pack finds on a project itself is fixed, never acknowledged,
since an acknowledgement would carry that project's ID: every job is encrypted with that key (the
pack's rule on a project without a customer-managed key), runs CodeBuild's standard image, and is
not privileged. A new entry then adds a project and nothing else: no statement and no finding. Each
spike's issue therefore names only `src/rhi/pipeline/spikes.py`.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/pipeline/spikes.py` holds an explicit list of spike jobs, empty in this issue, each entry naming a job and the script under `spikes/` it runs, and builds one job per entry as the Context describes.
- [ ] Every spike job runs on one role, created once whatever the list holds, which may do what the Context lists and nothing else and receives the measurement grant of issue E0-27, logs to one log group shared by every job, and is encrypted with one key created once; a new entry adds no statement to that role and no cdk-nag finding.
- [ ] `src/rhi/pipeline/stack.py` builds the spike jobs of that list.
- [ ] `tests/policy/test_pipeline.py` checks every spike job of the app's list, and of a list of two entries built in the test: no webhook, no schedule and no rule starts it, and its role holds no action outside the Context's list, none of Auto Scaling, EC2 or IAM, and no secret but the spikes'.
- [ ] Every cdk-nag finding `src/rhi/pipeline/spikes.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_spikes.toml`.
- [ ] `tests/synth/test_spike_jobs.py` checks the jobs a list built in the test gives: one per entry, each running its script, every one on the one role and the one log group, and the same cdk-nag finding ids for two entries as for one.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_spike_jobs.py` passes: a list of two entries gives two jobs, each running its own script, and an empty list gives none.
- [ ] `tests/synth/test_spike_jobs.py::test_a_new_entry_brings_no_new_finding` passes: an app whose list holds two entries gives exactly the cdk-nag finding ids of the same app with one entry, and both jobs name the same role and the same log group.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py` passes: no spike job has a trigger, and no spike role may set a group's capacity, start or stop an instance, or read a secret other than the spikes'.
- [ ] `scripts/check.sh synth` exits 0 with the empty list.

## References

- `docs/design/system-design.md`, sections 5.1, 7.1, 8.4, 12
