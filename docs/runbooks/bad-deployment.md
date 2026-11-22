# A bad deployment

Every merge to `main` is deployed by the pipeline, and there is one environment (system design,
sections 8.2 and 8.5). A deployment is bad when the pipeline fails on it (the deployment or its
verification) or when it passes and the endpoint is still wrong. The pipeline's result comes back
as commit statuses on the merge commit, with a structured report of a failure beside them (section
8.4). There is no rollback button: a bad deployment is fixed forward or reverted, by a pull request,
through the same pipeline (section 8.5).

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. Anything else (the AWS console, GitHub, `aws`,
`gh`, `git`) is written out in full and is tested only by walking through the runbook.

`OWNER/REPOSITORY` below stands for this repository on GitHub, `<sha>` for the full SHA of the
merge commit, and `<stage>` for the name of the stage the pipeline deploys. The admin CLI is run
from the repository's root, with your own AWS credentials; define its name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

## When

- **A status on a merge commit reads failure or error:** `deploy/<stage>` (the deployment, or a
  step before it: source, synthesis, self-update, assets) or `verify/<stage>` (the smoke tests
  against the deployed endpoint).
- **The statuses read success and the endpoint is still wrong:** students report errors that
  started with the deployment, a dashboard changed for the worse after it, or the scaler has
  suspended a pool's launches (`launch_suspended` reads `true` in `rhi_admin pool status`: a launch
  that fails every time, after a change to a pool's release).
- **A `verify/<stage>` status reads success with a description that begins "No test ran":** the
  deployment is not verified (the canary's key is missing, or the endpoint has no HTTPS listener).
  It is not a bad deployment by itself: say why no test ran and fix that (runbook
  [secret and token rotation](secret-rotation.md) for the canary's key).

## Check

1. **The statuses of the merge commit.** Each status has a fixed context and a description chosen
   from a fixed list; none carries text from AWS (section 8.4):

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/<sha>/statuses --paginate \
       --jq '.[] | "\(.created_at) \(.context) \(.state) \(.description)"'
   ```

   GitHub lists the newest first. Read, for each context, its newest line:

   - `deploy/<stage>` `pending`, "The deployment is running.": not finished yet; wait. "The
     deployment waits for the owner's approval in the pipeline.": a change widened permissions,
     approve or reject it in the pipeline;
   - `deploy/<stage>` `success`, "The deployment succeeded.", then `verify/<stage>`: the
     verification's own line says whether its tests ran and passed;
   - `deploy/<stage>` or `verify/<stage>` `failure`: the deployment failed, or its verification
     did. The report follows (step 2);
   - "Superseded: this commit is covered by the execution of commit" and a SHA: a self-update of
     the pipeline cancelled this commit's run. The named commit's execution deploys this one too:
     read that commit's statuses instead.

2. **The failure report.** A failure comes with more statuses on the same commit, one per fact, in
   the failure state. Their contexts start with the failed status's own context:

   - `<context>/action`: the stage and the action that failed;
   - `<context>/resource/<n>`: the logical ID of a resource that failed, and CloudFormation's
     status code for it (`CREATE_FAILED`, `UPDATE_FAILED`, ...);
   - `<context>/test/<n>`: the ID of a smoke test that failed (`tests/smoke/ids.py` lists them);
   - `<context>/lifecycle/<n>`: the last lifecycle stage an instance of a tested pool reached,
     the step its start stopped at (`services/engine/lifecycle/rhi_lifecycle/stages.py`);
   - `<context>/dropped`: how many facts were left out, because they were not on an allow-list.

   The report names parts of this repository and nothing else. AWS's own message, the reason of a
   failure, is in the owner's e-mail from the alerts topic, which the reporter sends for every
   failed action, and in the CodePipeline console: open the pipeline, the failed execution, and
   the failed action's details (its CloudFormation events, or its CodeBuild log for Verify).

3. **What the failed deployment left in place.** A failed `deploy/<stage>`: CloudFormation rolls
   the failed stack back to its previous template, so the endpoint runs what it ran before, except
   for a stack that the same run had already updated successfully before the failure. A pool's
   instance refresh is part of its stack's update (section 4.3): when the refresh itself failed,
   or a later resource of the stack did, the rollback puts the earlier release back, and that
   starts a refresh back to it. A failed `verify/<stage>`: the deployment **is** in place, and
   the endpoint did not pass its smoke tests with it.

4. **Whether a session is on.** The pipeline holds every deployment while a session is on (section
   5.6), so a fix or a revert merged during a class waits for the session to be switched off:

   ```sh
   rhi_admin session status
   ```

## Act

1. **Choose: fix forward or revert.**

   - **Fix forward** when the report names the cause and the fix is small and clear: a smoke test
     that failed on a mistake in the change, a resource refused for a property the change set
     wrongly. A fix forward keeps the rest of the merge's work.
   - **Revert** when the cause is not known, when the fix is not small, when the endpoint is worse
     than before for students (a session is near, or launches are suspended), or when a first fix
     forward has failed too. A revert brings back a state that was deployed and verified.
   - **Never by hand.** Changing a stack, a parameter or a group in the console does not fix a bad
     deployment: the next run of the pipeline deploys `main` over it again.

2. **To revert,** revert the merge commit on a branch and open a pull request against `main`; the
   ruleset lands it like any other change:

   ```sh
   git switch main
   git pull --ff-only
   git switch -c fix/revert-bad-deployment
   git revert --no-edit <sha>
   git push -u origin fix/revert-bad-deployment
   gh pr create --base main --fill
   ```

   Pull requests land on `main` squashed, as one commit with one parent; should `<sha>` have two
   parents, add `-m 1` to `git revert`. When later commits depend on the bad one, revert them too,
   newest first, in the same branch.

3. **To fix forward,** make the fix on a branch named for it, and open a pull request the same way,
   or file it as an issue for the implement loop.

4. **What a revert does to instances already refreshed.** A deployment does not change an instance
   that is running; a change to a pool's release, to the engine image or to the machine image
   starts an instance refresh, which replaces the instances in service first and then those in the
   warm pool (section 4.3). A revert that changes a pool's release back is such a change: its
   deployment starts a new refresh, which replaces again every instance the bad deployment had
   already refreshed, now with the earlier release. Nothing goes back in place: each replaced
   instance costs one more GPU start, the refresh takes as long as the bad one did, and the
   verification checks the served release against the deployed one again. A revert that touches
   no pool's release, engine image or machine image refreshes no instance. A gateway that the bad
   deployment changed is replaced by a new deployment of its service, which drains each old task
   for up to 30 minutes (section 8.5).

## Verify

1. **The revert's or the fix's merge commit is deployed and verified.** Its newest
   `deploy/<stage>` reads `success`, "The deployment succeeded.", and its newest `verify/<stage>`
   reads `success` with "The verification's tests ran and passed.", not one that begins "No test
   ran":

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/main/status \
       --jq '.sha, (.statuses[] | "\(.context) \(.state) \(.description)")'
   ```

   Check that the SHA printed first is the merge commit of your pull request. When the change
   touched a pool, the verification also ran on the coursework pool and checked the served image
   digest and model revision against the deployed release.

2. **No pool's launches are suspended,** and each pool reads its configured mode:

   ```sh
   rhi_admin pool status
   ```

   The scaler does not lift the mark `launch_suspended` by itself, even after a good deployment:
   clear it once the deployment that fixed the launch is verified (the command refuses a pool
   whose launches are not suspended).

   ```sh
   rhi_admin pool clear-suspension coursework
   ```

3. **What students reported is gone,** at the next session.

Walked through on the deployed system: not yet.
