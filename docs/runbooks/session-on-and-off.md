# A session switched on and off

The endpoint serves students only while a session is on, and the instructor decides when it is:
there is no schedule in the configuration (system design, section 5.6). One record, the session
switch, says on or off; only the admin CLI writes it, and a missing record reads as off. This
runbook is the class day: what to check before, switching the session on and waking each pool,
and switching it off after.

Like every check against the endpoint, it assumes the public HTTPS listener is on, which it is not
by default.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. Anything else (the AWS console, GitHub, `aws`,
`gh`, `git`) is written out in full and is tested only by walking through the runbook.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer uv run --locked --no-dev python -m rhi_admin.main'
```

`rhi` is the students' CLI, logged in with your own roster entry (`rhi login`). The pools are named
by their model name, the name `rhi status` prints; `coursework` below stands for each pool the class
uses.

## When

- Before a class: from the day before, for the checks, and up to an hour before it starts, to
  switch the session on and wake each pool.
- After the class, once the last student has finished: switch the session off.

## Check

Do these the day before the class, while no session is on: a deployment waits as long as a
session is on (sections 5.6 and 8.2).

1. **The GPU quota covers one instance more than each pool's `max_instances`** (section 5.4): a
   draining instance and a cold launch can overlap. A coursework instance takes 8 vCPUs, so
   `max_instances = 2` needs 24 vCPUs of the G-family quota. Read the quota in the region of the
   endpoint:

   ```sh
   aws service-quotas list-service-quotas --service-code ec2 --query "Quotas[?QuotaName=='Running On-Demand G and VT instances'].Value" --output text
   ```

   If it is short, request more with the quota step of the owner-setup script; an increase can take
   days, so do this well ahead of the class:

   ```sh
   setup/owner-setup.sh --step quotas --max-instances 2
   ```

2. **A pool's maximum, if the class needs more instances.** Edit `max_instances` of the pool in
   your configuration file, kept outside the working tree, then validate and write it to SSM:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   The quota of step 1 must cover the new maximum plus one. The new value takes effect only once a
   pipeline run has deployed it: start one with **Release change** on the pipeline's page in the
   CodePipeline console.

3. **Every configuration change has landed.** A deployment that is still running, or queued, when
   the session is switched on waits until it is switched off, and runs then. On GitHub, the latest
   commit of `main` must carry a `deploy/<stage>` status that reads success, and the pipeline's
   page in the CodePipeline console must show no execution in progress. The statuses can be read
   with `gh`:

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/main/status --jq '.statuses[] | "\(.context) \(.state)"'
   ```

4. **No session is on yet,** and nothing was left switched on from the last class:

   ```sh
   rhi_admin session status
   ```

## Act

1. **Switch the session on,** up to an hour before the class. From then on the gateway serves every
   student on the roster, and the scaler takes each pool from zero to its `max_instances` by load
   (section 5.1).

   ```sh
   rhi_admin session on
   ```

2. **Wake each pool** the class uses. `rhi wait` asks for the pool, repeats the request every
   minute, and prints each change of state until the pool is ready. From the warm pool a start
   takes an estimated 2 to 6 minutes; a cold launch, when the warm instance cannot start, about
   3 to 13 (section 5.2).

   ```sh
   rhi wait coursework
   ```

   It exits with the cause when the pool is unavailable: `capacity_unavailable` is the runbook
   [capacity failure](capacity-failure.md), `pool_disabled` the runbook
   [a disabled pool](disabled-pool.md), and `no_session` means the switch does not read on.

3. **After the class, switch the session off.** Students' requests and wakes are then refused at
   once with `no_session`; requests already running finish, and each pool falls to zero
   `idle_minutes` after its last request, by its idle rule (section 5.6). Deployments that waited
   for the session run now.

   ```sh
   rhi_admin session off
   ```

## Verify

1. After switching it on, the switch reads on, with your identity and the time:

   ```sh
   rhi_admin session status
   ```

2. `rhi status` shows each pool as `cold`, `warming`, `ready` or `unavailable`, with the cause when
   it is unavailable. Before the class starts, every pool the class uses reads `ready`:

   ```sh
   rhi status
   ```

3. After switching it off, `rhi_admin session status` reads off, and `rhi status` lists each pool
   as `unavailable` with the cause `no_session`.

4. Within `idle_minutes` of the last request, plus a few minutes for the scaler to act, each pool
   has no instance in service: in Grafana, the panel Instances of the fleet and scaling dashboard
   shows no instance in service and a desired capacity of zero for each pool.

Walked through on the deployed system: not yet.
