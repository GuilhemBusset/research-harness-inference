# A bad deployment

Every merge to `main` is deployed by the pipeline, and there is one environment (system design,
sections 8.2 and 8.5). A deployment is bad when the pipeline fails on it (the deployment or its
verification) or when it passes and the endpoint is still wrong. The pipeline's result comes back
as commit statuses on the merge commit, with a structured report of a failure beside them (section
8.4). There is no rollback button: a bad deployment is fixed forward or reverted, by a pull request
or a change to the configuration, through the same pipeline (section 8.5).

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. Anything else (the AWS console, GitHub, `aws`,
`gh`, `git`) is written out in full and is tested only by walking through the runbook.

`OWNER/REPOSITORY` below stands for this repository on GitHub, `<sha>` for the full SHA of the
merge commit, `<stage>` for the name of the stage the pipeline deploys, `<pipeline>` for the
pipeline's name, `<stack>` for the name of a stack of the stage as the CloudFormation console
lists it, and `<group>` for a pool's Auto Scaling group. The admin CLI is run from the repository's
root, with your own AWS credentials; define its name once in your shell:

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

   GitHub lists the newest first and never deletes a status. One commit can be deployed by more
   than one execution of the pipeline (a retry, an execution started again by hand, the execution
   that covers a superseded one), and every execution posts under the same contexts. So the
   newest line of a context is the latest thing any execution said there, not necessarily what
   the execution you are looking at said. Find the executions that deployed the commit, the
   newest first, with their start times (or open the pipeline in the CodePipeline console, whose
   history lists the same):

   ```sh
   aws codepipeline list-pipeline-executions --pipeline-name <pipeline> --max-items 10 \
       --query 'pipelineExecutionSummaries[].[pipelineExecutionId,status,startTime,sourceRevisions[0].revisionId]'
   ```

   Take the newest execution whose source revision is `<sha>`, or the one you are investigating,
   and read only the status lines created after its start time: those are its own. A line
   created before it is stale, from an earlier attempt on the same commit. Then, for each context,
   that execution's newest line:

   - `deploy/<stage>` `pending`, "The deployment is running.": not finished yet, or between two
     of its actions; wait. "The deployment waits for the owner's approval in the pipeline.": a
     change widened permissions, approve or reject it in the pipeline;
   - `deploy/<stage>` `success`, "The deployment succeeded.", then `verify/<stage>`: the
     verification's own line says whether its tests ran and passed;
   - `deploy/<stage>` or `verify/<stage>` `failure`: the deployment failed, or its verification
     did. The report follows (step 2);
   - `error`: the reporter met a state it does not know. It posts no report for it: go to the
     console (step 2);
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

   A fact status belongs to the failure when it was posted with it: its `created_at` is after the
   start of the failed execution (step 1) and within a minute or so of the failed
   `deploy/<stage>` or `verify/<stage>` line. A fact created before that start is stale: it is
   what an earlier attempt on the same commit reported, and it says nothing about this one. An
   execution that failed without a resource or a test to name posts no such fact, so a stale fact
   of an earlier attempt can be the only one listed under its context.

   The report names parts of this repository and nothing else. AWS's own message, the reason of a
   failure, is in the owner's e-mail from the alerts topic, which the reporter sends for every
   failed action, and in the CodePipeline console. When the facts are missing, stale, or do not
   say enough, go there: open the pipeline, the failed execution of step 1, and read the failed
   action's details (its CloudFormation events, or its CodeBuild log for Verify).

3. **What the failed deployment left in place.** A failed `verify/<stage>`: the deployment **is**
   in place, and the endpoint did not pass its smoke tests with it. A failed `deploy/<stage>`:
   CloudFormation rolls the failed stack back to its previous template, but a failed status does
   not say that the rollback has finished, or that it succeeded. Read the state of the stack the
   failed action deployed:

   ```sh
   aws cloudformation describe-stacks --stack-name <stack> --query 'Stacks[0].StackStatus'
   ```

   - `UPDATE_ROLLBACK_IN_PROGRESS` or `UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS`: still rolling
     back; wait and read it again.
   - `UPDATE_ROLLBACK_COMPLETE`: that stack is back on its previous template. A stack that the
     same execution had already updated successfully before the failure stays on the new one.
   - `UPDATE_ROLLBACK_FAILED`: the rollback stopped half way, and no further update of the stack
     can start, the pipeline's included, until it is resumed. Read the stack's events for the
     resource that could not roll back and why. A pool's instance refresh that could not run (its
     group's launches suspended, Act, step 1) is fixed by clearing the suspension first. Then
     resume the rollback:

     ```sh
     aws cloudformation continue-update-rollback --stack-name <stack>
     ```

     Pass `--resources-to-skip` with the logical ID of a resource only when its rollback cannot
     succeed at all: the skipped resource stays as the failed update left it, and the next
     deployment must bring it right. Read the state again until it is `UPDATE_ROLLBACK_COMPLETE`.

   A pool's instance refresh is part of its stack's update (section 4.3), so the rollback of a
   Pools stack whose release had changed puts the earlier release back in the pool's parameter
   and starts a refresh back to it. Do not assume it ran: read the pool's refreshes, the newest
   first, and the release its instances serve. The group of a pool carries the tag `rhi-pool`
   with the pool's name:

   ```sh
   aws autoscaling describe-auto-scaling-groups --filters Name=tag:rhi-pool,Values=coursework \
       --query 'AutoScalingGroups[].AutoScalingGroupName'
   aws autoscaling describe-instance-refreshes --auto-scaling-group-name <group> --max-records 3 \
       --query 'InstanceRefreshes[].[Status,StartTime,EndTime,PercentageComplete]'
   ```

   On the Grafana dashboard "Deployments and releases", the panel "Release each instance serves:
   image digest and model revision" shows the release each running instance of the pool holds. A
   pool with no instance holds no release until it next starts, and starts with what its
   parameter names then.

4. **Whether a session is on.** The pipeline holds every deployment while a session is on (section
   5.6), so a fix or a revert merged during a class waits for the session to be switched off:

   ```sh
   rhi_admin session status
   ```

## Act

1. **A pool whose launches are suspended.** When `rhi_admin pool status` reads `launch_suspended`
   `true` for a pool, its group's `Launch` process is suspended: no instance refresh can launch a
   replacement, the scaler does not wake it, and the verification cannot start an instance on it.
   The fix or revert of step 3 or 4 would then fail on that pool, or hang until its refresh times
   out. Clear the suspension once the fix or revert is merged, with no session on, and before
   its deployment reaches the pool's stack (while its `deploy/<stage>` is `pending`):

   ```sh
   rhi_admin pool clear-suspension coursework
   ```

   with the pool's name in place of `coursework`. With no session on, the gateway admits no
   request, so nothing wakes the pool and the bad release is not launched again in the meantime;
   the refresh and the verification then launch on the corrected release. Should a launch fail
   three times again, the scaler suspends the pool again: the fix did not fix the launch.

2. **Choose: fix forward or revert.**

   - **Fix forward** when the report names the cause and the fix is small and clear: a smoke test
     that failed on a mistake in the change, a resource refused for a property the change set
     wrongly. A fix forward keeps the rest of the merge's work.
   - **Revert** when the cause is not known, when the fix is not small, when the endpoint is worse
     than before for students (a session is near, or launches are suspended), or when a first fix
     forward has failed too. A revert brings back a state that was deployed and verified.
   - **Never by hand.** Changing a stack, a parameter or a group in the console does not fix a bad
     deployment: the next run of the pipeline deploys `main` and the configuration over it again.
     Resuming a rollback (Check, step 3) and clearing a suspension (step 1) only let the pipeline
     run.

   **What to revert: the code, the configuration, or a machine image.** A deployment deploys three
   inputs, and only the first is in git. Revert the one that changed:

   - **the code**, a merge commit on `main`: step 3;
   - **the configuration in SSM**, which the pipeline's synthesis reads on every execution
     (section 8.2, step 2): a pool's model, model revision, engine flags or instance types, and
     every other setting of the configuration file. A git revert does not touch it: an execution
     started by the revert synthesizes the same release from the same configuration, starts no
     refresh, and leaves the bad instances in service. Put the previous values back in your
     configuration file and run the configuration step, which writes it to SSM; then start an
     execution of the pipeline in its console (**Release change**) so that it deploys:

     ```sh
     setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
     ```

   - **a GPU pool's machine image**, the parameter `/rhi/images/<pool>` that image promotion
     writes and the launch template resolves at deployment. A git revert leaves it too. Put the
     previous image back by promoting it again, with the same job (`scripts/pipeline/README.md`),
     which starts its canary on it and, only if it serves, writes the image parameter and starts
     the pipeline:

     ```sh
     aws codebuild start-build --project-name rhi-promote-image \
         --environment-variables-override '[{"name": "RHI_PROMOTE_POOL", "value": "coursework"}, {"name": "RHI_PROMOTE_IMAGE", "value": "ami-<previous>"}, {"name": "RHI_PROMOTE_FAMILIES", "value": "G6e,G7e,P5,P5e,P5en,P6"}]'
     ```

     with the pool, the image that served before, and the families its release notes list.

3. **To revert the code,** revert the merge commit on a branch and open a pull request against
   `main`; the ruleset lands it like any other change:

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

4. **To fix forward,** make the fix on a branch named for it, and open a pull request the same way,
   or file it as an issue for the implement loop. A fix of the configuration or of a machine image
   goes the way of step 2.

5. **What a revert does to instances already refreshed.** A deployment does not change an instance
   that is running; a change to a pool's release (its engine image, model revision, engine flags
   or files) or to its machine image starts an instance refresh, which replaces the instances in
   service first and then those in the warm pool (section 4.3). A revert that changes a pool's
   release or image back is such a change: its deployment starts a new refresh, which replaces
   again every instance the bad deployment had already refreshed, now with the earlier release.
   Nothing goes back in place: the instances are replaced, the refresh takes about as long as the
   bad one did, and on a GPU pool each replacement is one more GPU start (the mock pool's
   instances are CPU instances, and cost a CPU start). A pool with no instance in service or in
   the warm pool has nothing to replace. A revert that touches no pool's release or image
   refreshes no instance. A gateway that the bad deployment changed is replaced by a new
   deployment of its service, which drains each old task for up to 30 minutes (section 8.5).

## Verify

1. **The deployment of the revert or the fix is verified.** For a code change, its merge commit;
   for a change to the configuration or an image, the commit at the head of `main` that the
   execution you started deployed. Among the lines created after that execution's start (Check,
   step 1), `deploy/<stage>` reads `success`, "The deployment succeeded.", and its `verify/<stage>` reads `success`
   with "The verification's tests ran and passed.", not one that begins "No test ran":

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/<sha>/statuses --paginate \
       --jq '.[] | "\(.created_at) \(.context) \(.state) \(.description)"'
   ```

   The smoke tests run on the mock pool every time. They run on the coursework
   pool as well, and check there the image digest and model revision its instances report
   against the deployed release, only when an instance refresh of a GPU pool started during that
   execution and ended `Successful` (`scripts/pipeline/gpu_verification.py`). A revert that
   refreshed only another GPU pool (`upgrade`, `xl`) is tested on coursework, not on that pool:
   read that pool's served release on the dashboard "Deployments and releases" at its next start.
   A deployment that refreshed no GPU pool says nothing about any GPU pool's release.

2. **No pool's launches are suspended,** and each pool reads its configured mode:

   ```sh
   rhi_admin pool status
   ```

   A pool that still reads `launch_suspended` `true` was not cleared in Act, step 1, or was
   suspended again by three more failed launches: the release it launches still fails. Go back to
   Check with that pool.

3. **What students reported is gone,** at the next session.

Walked through on the deployed system: not yet.
