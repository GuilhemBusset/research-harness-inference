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
pipeline's name, and `<stack>` for the name of a stack of the stage as the CloudFormation console
lists it. The admin CLI is run from the repository's
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
     of its actions; wait. "The deployment waits for the owner's approval in the pipeline.": the
     pipeline this repository builds holds no approval (system design, section 8.4, control 6).
     On a commit merged before the approval was removed, the line is that older pipeline's; on a
     later one, the pipeline in the account is not the one the repository builds. Read the
     execution's actions in the console and compare them with the reviewed list of
     `tests/policy/test_pipeline.py`. "The
     pipeline's execution is being stopped.": someone stopped it; wait for the end;
   - `deploy/<stage>` `success`, "The deployment succeeded.", then `verify/<stage>`: the
     verification's own line says whether its tests ran and passed;
   - `deploy/<stage>` `failure`, "The deployment failed.": an action failed, the deployment of a
     stack or a step before it (source, synthesis, self-update, assets, the copy of the weights).
     "The approval was rejected, or it expired: the deployment did not continue.": the pipeline
     this repository builds holds no approval, so the line does not say what was deployed. Read it
     as the pending line above, and read from the execution's actions which stacks it updated. The
     report follows (step 2);
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
     `CopyWeights`): no stack of the stage was touched by that action, and no stack of this
     execution was updated, since these run before any. There is no rollback to inspect: read the
     action's own details (its CodeBuild log) and fix the cause. A stage's stacks still hold the
     previous deployment.
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
       the resource that could not roll back and why. Then resume the rollback:

       ```sh
       aws cloudformation continue-update-rollback --stack-name <stack>
       ```

       Pass `--resources-to-skip` with the logical ID of a resource only when its rollback cannot
       succeed at all: the skipped resource stays as the failed update left it, and the next
       deployment must bring it right. Read the state again until it is `UPDATE_ROLLBACK_COMPLETE`.

     The rollback of a Pools stack puts the earlier release back in each pool's parameter, and
     the earlier launch template in place. Like the deployment, it replaces no instance (section
     4.3): it changes what the next instance launches with, and nothing that runs.

   **What the instances serve.** A deployment waits while a session is active, so a bad release
   normally found no instance, and the first to launch on it are the instances that the
   verification, or the next session, asks for. An instance that launched while the bad release
   was deployed keeps it until it leaves, whatever was deployed since. On the Grafana dashboard
   "Deployments and releases", the panel "Release each instance serves: image digest and model
   revision" shows the release each running instance of a pool serves. A pool with no instance
   serves no release until its next session, whose instances start with what its parameter names
   then.

4. **Whether a session is booked or active,** on any pool:

   ```sh
   rhi_admin session status
   ```

   The pipeline waits before each stage's deployment while a session is active on any pool
   (section 8.2, step 5), so a repair merged during a class is deployed after the session's end,
   and nothing changes under the class meanwhile. If the repair cannot wait, stop the sessions
   (runbook [a session booked, changed and stopped](sessions.md)) and tell the students to work
   with their own subscriptions. A deployment that passed the wait before a session started still
   runs to its end: its gateway deployment drains tasks during the session, and an instance that
   launches meanwhile may start on either release. So book no session until the repair's
   execution has ended.

## Act

1. **A pool whose launches are suspended.** When `rhi_admin pool status` reads `launch_suspended`
   `true` for a pool, its group launches nothing: the scaler does not raise the pool, and the
   verification's instance on it never comes. A suspended pool costs nothing more, so leave the
   suspension in place while the repair is deployed: with no session active the pool is at zero,
   and the deployment has no instance to replace and needs no launch. Clear it only once every
   deployment action of the repair's execution has succeeded, so that the next launch starts the
   repaired release; cleared sooner, launches resume on the bad one and are abandoned again, each
   one billed. The runbook [a pool whose launches are suspended](suspended-launches.md) has the
   whole of it. The admin command clears the mark, and the scaler resumes the group's launches at
   its next run, which the second command asks for now:

   ```sh
   rhi_admin pool clear-suspension coursework
   rhi_admin pool run
   ```

   with the pool's name in place of `coursework`. The repair's own verification may have waited
   for an instance of the suspended pool in vain: once the suspension is cleared, the test of the
   repair on that pool is the pool's next session, or the weekly canary for the coursework pool.
   Should a launch of the repaired release be abandoned three times, the scaler suspends the pool
   again: the repair did not fix the launch.

2. **Choose: fix forward or revert.**

   - **Fix forward** when the report names the cause and the fix is small and clear: a smoke test
     that failed on a mistake in the change, a resource refused for a property the change set
     wrongly. A fix forward keeps the rest of the merge's work.
   - **Revert** when the cause is not known, when the fix is not small, when the endpoint is worse
     than before for students (a session is near, or launches are suspended), or when a first fix
     forward has failed too. A revert brings back a state that was deployed and verified.
   - **Never by hand.** Changing a stack, a parameter or a group in the console does not fix a bad
     deployment: the next run of the pipeline deploys `main` and the configuration over it again.
     Resuming a rollback (Check, step 3) only lets the pipeline run, and clearing a pool's
     suspension (step 1) only lets the pool launch what the pipeline deployed.

   **What to revert: the code, the configuration, or a machine image.** A deployment deploys three
   inputs, and only the first is in git. Revert the one that changed:

   - **the code**, a merge commit on `main`: step 3;
   - **the configuration in SSM**, which the pipeline's synthesis reads on every execution
     (section 8.2, step 2): a pool's model, model revision, engine flags or instance types, and
     every other setting of the configuration file. A git revert does not touch it: an execution
     started by a git revert synthesizes the same release from the same configuration, so the
     pools keep the bad release, and their next instances launch on it. Put the previous values back in your
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

5. **What a revert does to instances.** Nothing: like every deployment, a revert replaces no
   instance (section 4.3). It puts the earlier release back in each pool's parameter and the
   earlier launch template in place, and the instances of each pool's next session launch on
   them. With no session active, which is when the revert is deployed, no pool has an instance,
   and the revert costs no GPU start of its own; its verification books one when the revert
   changes a GPU pool's engine image, model revision or engine flags (Verify, step 1). An
   instance that launched on the bad release, in a session booked while the bad deployment was
   in place, keeps the bad release until it leaves: stop that session, let its instances go, and
   book it again once the revert is deployed (runbook
   [a session booked, changed and stopped](sessions.md)). A gateway that the bad deployment
   changed is replaced by a new deployment of its service, which drains each old task for up to
   30 minutes (section 8.5).

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

   The smoke tests run on the mock pool every time, on an instance the verification books. They
   run on the coursework pool as well, on an instance the verification books there, and check the
   image digest and model revision that instance reports against the deployed release, only when
   the execution changed a GPU pool's engine image, model revision or engine flags
   (`scripts/pipeline/gpu_verification.py`). When AWS gave no instance within
   `check_wait_minutes` (45 by default), the GPU tests did not run: the `verify/<stage>` status
   then reads "The mock pool's tests passed. The GPU tests did not run, for lack of capacity.",
   which is not a failed release, and the next weekly canary covers it. Whichever GPU pool's
   release changed, the tests read the coursework pool: read another pool's served release
   (`upgrade`, `xl`) on the dashboard "Deployments and releases" at its next session. A deployment
   that changed no GPU pool's release says nothing about any GPU pool's release.

2. **No pool's launches are suspended:**

   ```sh
   rhi_admin pool status
   ```

   A pool that still reads `launch_suspended` `true` was not cleared in Act, step 1, or was
   suspended again by three more failed launches: the release it launches still fails. Go back to
   Check with that pool.

3. **What students reported is gone,** at the next session.

Walked through on the deployed system: not yet.
