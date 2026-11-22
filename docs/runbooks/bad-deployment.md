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

   GitHub lists the newest first and never deletes a status. A status names the commit, not the
   execution of the pipeline that posted it, and every execution of the same commit posts under
   the same contexts. Read the statuses as a summary of the commit: the newest line of each
   context is the latest thing that any execution said there. When the commit was deployed by
   one execution only, and no stage of it was retried, that is the execution's own outcome. Find
   out whether it was, with every execution of the pipeline that names `<sha>` as its source
   revision, the newest first:

   ```sh
   aws codepipeline list-pipeline-executions --pipeline-name <pipeline> \
       --query "pipelineExecutionSummaries[?sourceRevisions[0].revisionId=='<sha>'].[pipelineExecutionId,status,startTime]"
   ```

   The command sets no `--max-items`, so the CLI reads every page of the history, older executions
   included, before it filters; a limit would leave out an older execution of the same commit
   and make the commit look deployed once. CodePipeline keeps the history of its executions for
   12 months only, and the console's history pages through the same list. When the lookup is
   inconclusive (it failed, or you cannot tell whether an execution is missing), do not treat the
   commit as deployed once: take the outcome from the execution's own actions, below, or from the
   owner's e-mail of its failure (step 2).

   When more than one execution names `<sha>` as its source revision (an execution started again
   by hand, a commit that a later execution covers), or a stage was retried (a retry keeps the
   execution's ID, and posts again under the same contexts), the statuses cannot say which
   execution or which attempt posted a line, whatever their times. Then take the outcome from the
   execution you are investigating, `<execution>` below: its actions, each with its status and,
   for a failed one, its error, in the order they ran, the latest attempt first:

   ```sh
   aws codepipeline list-action-executions --pipeline-name <pipeline> \
       --filter pipelineExecutionId=<execution> \
       --query 'actionExecutionDetails[].[stageName,actionName,status,startTime,output.executionResult.errorDetails.code,output.executionResult.errorDetails.message,output.executionResult.externalExecutionSummary]'
   ```

   A failed action's error is in its `errorDetails`, a code and a message, which the provider's
   summary (`externalExecutionSummary`, the last column) may leave empty. The message is AWS's
   text: read it in your terminal and never paste it to GitHub. The CodePipeline console shows the
   same error on the execution's page, under the failed action's details.

   You can also match the owner's e-mail of the failure (step 2), which names the execution, to it.

   What each line says:

   - `deploy/<stage>` `pending`, "The deployment is running.": not finished yet, or between two
     of its actions; wait. "The deployment waits for the owner's approval in the pipeline.": a
     change widened permissions, approve or reject it in the pipeline. "The pipeline's execution
     is being stopped.": someone stopped it; wait for the end;
   - `deploy/<stage>` `success`, "The deployment succeeded.", then `verify/<stage>`: the
     verification's own line says whether its tests ran and passed;
   - `deploy/<stage>` `failure`, "The deployment failed.": an action failed, the deployment of a
     stack or a step before it (source, synthesis, self-update, assets, the permissions check, the
     copy of the weights). "The approval was rejected, or it expired: the deployment did not
     continue.": nothing of the stage was deployed. The report follows (step 2);
   - `verify/<stage>` `failure`, "The verification failed.": the deployment is in place and its
     smoke tests failed. The report follows (step 2);
   - `error`, "The pipeline's execution was stopped before this step finished." or "... was
     canceled before this step finished.": someone stopped the execution, or stopped and abandoned
     it, or it was canceled; nothing failed by itself, and the action that was running may have
     left a stack half way (step 3). "The pipeline reported a state the reporter does not know.":
     read the execution's actions in the console. None of these has a report on GitHub; the
     owner's e-mail of step 2 has the details;
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

   The facts, like the statuses, name no execution, and a failure that has no resource or test to
   name posts no such fact: a fact under a context can be what an earlier execution or attempt on
   the same commit reported. When step 1 found only one execution and no retry, the facts are
   its own. Otherwise trust them only where the selected execution's own records agree: its failed
   action in `list-action-executions`, the CloudFormation events of the stack that action deployed
   since the action started, and for Verify the log of its CodeBuild build.

   The report names parts of this repository and nothing else. AWS's own message, the reason of a
   failure, is in the owner's e-mail from the alerts topic, which the reporter sends for every
   action that failed, was stopped or was abandoned: it names the commit, the execution, the stage
   and the action, the action's error, and each failed resource of the stack the action deploys.
   The CodePipeline console shows the same: open the pipeline, the execution, and read the failed
   action's details (its CloudFormation events for a stack, its CodeBuild log for a build step,
   Verify included).

3. **What the failed deployment left in place.** It depends on the action that failed (step 2's
   `<context>/action`, confirmed in the execution's actions):

   - **Verify** (`verify/<stage>`): the deployment **is** in place, and the endpoint did not pass
     its smoke tests with it.
   - **A step before the deployment** (the source, `Synth`, `SelfMutate`, an asset's publication,
     the permissions check `Check` or its approval, `CopyWeights`): no stack of the stage was
     touched by that action, and no stack of this execution was updated, since these run before
     any. There is no rollback to inspect: read the action's own details (its CodeBuild log, or the
     approval's reviewer) and fix the cause. A stage's stacks still hold the previous deployment.
   - **A stack's `Prepare` action** (`<stack>.Prepare`, which makes a change set): the change set
     was refused, so no update of that stack started; the action's error says why.
   - **A stack's `Deploy` action** (`<stack>.Deploy`, which runs the change set), or an execution
     stopped while such an action ran: an update of that stack started. When the action failed,
     the update failed and CloudFormation rolls it back to its previous template. When the
     execution was stopped, CloudFormation is not told: with **Stop and wait**, the action waits
     for the update to end, and with **Stop and abandon**, the action is dropped while the update
     goes on. Either way the update may still be running, may have completed on the new template,
     or may have failed and rolled back. The failed or stopped status says none of this: it does
     not say which template is in effect, or that a rollback has finished or succeeded. Establish
     it from the stack itself before any repair. Its state:

     ```sh
     aws cloudformation describe-stacks --stack-name <stack> --query 'Stacks[0].StackStatus'
     ```

     and its events since the moment the action started, the newest first (the events of the
     stack itself, whose logical ID is the stack's name, mark where each update began and how it
     ended). `<action-start>` is the action's start time as the execution's actions show it, in
     UTC, written `2026-01-31T14:05:00`. The command sets no `--max-items`: the CLI reads every
     page of the stack's events before it keeps those since `<action-start>`, so an update with
     many resources, or later events, cannot push the start of the update being investigated out
     of the answer:

     ```sh
     aws cloudformation describe-stack-events --stack-name <stack> \
         --query "StackEvents[?Timestamp>='<action-start>'].[Timestamp,LogicalResourceId,ResourceStatus]" \
         --output text
     ```

     An answer with no event of the stack itself means no update of it began after
     `<action-start>`; check the time you gave before reading it so.

     - `UPDATE_IN_PROGRESS` or `UPDATE_COMPLETE_CLEANUP_IN_PROGRESS`: the update is still running
       (an abandoned action, or a stop that has not ended); wait and read it again.
     - `UPDATE_COMPLETE`, with an event of the stack itself `UPDATE_COMPLETE` after the action
       started: the update finished, and the stack is on the new template, the one the stopped
       execution deployed, whatever the pipeline's status says. Treat it as deployed: when it is
       the bad change, revert or fix it like any deployed change. `UPDATE_COMPLETE` with no update
       of the stack after the action started: the change set never ran, and the stack holds the
       previous deployment.
     - `UPDATE_ROLLBACK_IN_PROGRESS` or `UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS`: still
       rolling back; wait and read it again.
     - `UPDATE_ROLLBACK_COMPLETE`: that stack is back on its previous template. A stack that the
       same execution had already updated successfully before the failure stays on the new one.
     - `UPDATE_ROLLBACK_FAILED`: the rollback stopped half way, and no further update of the
       stack can start, the pipeline's included, until it is resumed. Read the stack's events for
       the resource that could not roll back and why. A pool's instance refresh that could not run
       (its group's launches suspended, Act, step 1) is fixed by resuming the launches first. Then
       resume the rollback:

       ```sh
       aws cloudformation continue-update-rollback --stack-name <stack>
       ```

       Pass `--resources-to-skip` with the logical ID of a resource only when its rollback cannot
       succeed at all: the skipped resource stays as the failed update left it, and the next
       deployment must bring it right. Read the state again until it is `UPDATE_ROLLBACK_COMPLETE`.

     A pool's instance refresh is part of its stack's update (section 4.3), so the rollback of a
     Pools stack whose refresh trigger had changed (Act, step 5) puts the earlier release back in
     the pool's parameter and starts a refresh back to it. Do not assume it ran: read the pool's
     refreshes, the newest first, and the release its instances serve. The group of a pool
     carries the tag `rhi-pool` with the pool's name:

     ```sh
     aws autoscaling describe-auto-scaling-groups --filters Name=tag:rhi-pool,Values=coursework \
         --query 'AutoScalingGroups[].AutoScalingGroupName'
     aws autoscaling describe-instance-refreshes --auto-scaling-group-name <group> --max-records 3 \
         --query 'InstanceRefreshes[].[Status,StartTime,EndTime,PercentageComplete]'
     ```

   On the Grafana dashboard "Deployments and releases", the panel "Release each instance serves:
   image digest and model revision" shows the release each running instance of a pool holds. A
   pool with no instance holds no release until it next starts, and starts with what its
   parameter names then.

4. **Whether a session is on.** A repair is deployed outside a session: its refresh replaces
   instances and its gateway deployment drains tasks, in the middle of a class otherwise.

   ```sh
   rhi_admin session status
   ```

   Once the deployment hold is deployed (issue E4-42, see the runbook `session-on-and-off.md`), the
   pipeline waits before each stage's deployment while the switch reads on, so a repair merged
   during a class waits for the session to be switched off; a deployment that passed the hold
   before the session was switched on still runs to its end. Until that hold is deployed, nothing
   holds a deployment: merge the repair only while no session is on, and switch no session on
   until its execution has ended.

## Act

1. **A pool whose launches are suspended.** When `rhi_admin pool status` reads `launch_suspended`
   `true` for a pool, its group's `Launch` process is suspended: no instance refresh can launch a
   replacement, the scaler does not wake it, and the verification cannot start an instance on it.
   The fix or revert of step 2, 3 or 4 would then fail on that pool, or hang until its refresh
   times out. The launches must be running again before the repair's refresh starts, so resume
   them just before you merge the repair, or start its execution, with no session on.

   The admin command clears only the mark; the scaler resumes the group's `Launch` process at its
   next run, every minute. Clear the mark, then ask for one run of the scaler now:

   ```sh
   rhi_admin pool clear-suspension coursework
   rhi_admin pool run
   ```

   with the pool's name in place of `coursework`. Then confirm that the group no longer lists
   `Launch` among its suspended processes (Check, step 3, finds `<group>` from the pool's tag); an
   empty list, or one without `Launch`, is what you want:

   ```sh
   aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <group> \
       --query 'AutoScalingGroups[0].SuspendedProcesses[].ProcessName'
   ```

   Only then merge the repair or start its execution. Until the refresh has replaced them, the
   pool's launches still start the bad release: once the gateway enforces the session switch
   (issue E4-41), nothing wakes the pool with no session on; until then, a student's request can.
   Should three launches fail again before the refresh starts, the scaler suspends the pool again,
   and the refresh then fails (Check, step 3). So, before the execution reaches the Pools stack's
   `Deploy` action (its actions, Check, step 1), read `rhi_admin pool status` once more; if it
   reads `true` again, clear the mark, run the scaler and confirm as above before that action
   starts. Should a launch of the corrected release fail three times, the fix did not fix the
   launch.

2. **Choose: fix forward or revert.**

   - **Fix forward** when the report names the cause and the fix is small and clear: a smoke test
     that failed on a mistake in the change, a resource refused for a property the change set
     wrongly. A fix forward keeps the rest of the merge's work.
   - **Revert** when the cause is not known, when the fix is not small, when the endpoint is worse
     than before for students (a session is near, or launches are suspended), or when a first fix
     forward has failed too. A revert brings back a state that was deployed and verified.
   - **Never by hand.** Changing a stack, a parameter or a group in the console does not fix a bad
     deployment: the next run of the pipeline deploys `main` and the configuration over it again.
     Resuming a rollback (Check, step 3) and resuming a pool's launches (step 1) only let the
     pipeline run.

   **What to revert: the code, the configuration, or a machine image.** A deployment deploys three
   inputs, and only the first is in git. Revert the one that changed:

   - **the code**, a merge commit on `main`: step 3;
   - **the configuration in SSM**, which the pipeline's synthesis reads on every execution
     (section 8.2, step 2): a pool's model, model revision, engine flags or instance types, and
     every other setting of the configuration file. A git revert does not touch it: an execution
     started by a git revert synthesizes the same release from the same configuration, so it
     refreshes no pool back, and leaves the bad instances in service. Put the previous values back in your
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
   that is running. A pool's instance refresh starts when its refresh trigger changes
   (`src/rhi/pools/release.py`), which holds the pool's release (its engine image, model revision,
   engine flags and files), its machine image, and the version of its launch template: any change
   to the launch template, its user data, which carries the boot program, among others, makes a
   new launch template version. The refresh replaces the instances in service first and then
   those in the warm pool (section 4.3). A revert that changes one of these back is such a change,
   and a launch template that is put back is still a new version: its deployment starts a new
   refresh, which replaces again every instance the bad deployment had already refreshed, now
   with the earlier release and launch template. Nothing goes back in place: the instances are
   replaced, the refresh takes about as long as the bad one did, and on a GPU pool each
   replacement is one more GPU start (the mock pool's instances are CPU instances, and cost a CPU
   start). A pool with no instance in service or in the warm pool has nothing to replace. A revert
   that changes no pool's release, machine image or launch template version refreshes no
   instance. A gateway that the bad deployment changed is replaced by a new deployment of its
   service, which drains each old task for up to 30 minutes (section 8.5).

## Verify

1. **The deployment of the revert or the fix is verified.** For a code change, its merge commit;
   for a change to the configuration or an image, the commit at the head of `main` that the
   execution you started deployed. The newest `deploy/<stage>` reads `success`, "The deployment
   succeeded.", and the newest `verify/<stage>` reads `success` with "The verification's tests ran
   and passed.", not one that begins "No test ran":

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/<sha>/statuses --paginate \
       --jq '.[] | "\(.created_at) \(.context) \(.state) \(.description)"'
   ```

   When that commit has more than one execution, or a stage of it was retried (Check, step 1), the
   statuses do not say which one posted them: confirm in the execution you started that every
   action ended `Succeeded`, `Verify` included, with `list-action-executions` as in Check, step 1,
   and that the Verify build's log shows its tests ran.

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
