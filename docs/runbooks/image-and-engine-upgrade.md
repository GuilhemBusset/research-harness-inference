# Machine image and engine upgrade

Two things under a GPU pool are pinned, and each changes only on purpose (system design, section
4.3):

- **The machine image,** AWS's base GPU image with the open-source NVIDIA driver on Ubuntu 22.04.
  The pool's launch template names it by the ID in an SSM parameter of the pool, and only the
  image promotion job writes that parameter, after a canary start on the new image passes. A
  merge never changes the driver under the engine.
- **The engine,** the upstream vLLM image that `services/engine/vllm/Dockerfile` pins by the digest
  of its multi-architecture index, with the upstream version it is in `services/engine/vllm/VERSION`.
  Raising it is a change of those two lines, through a pull request.

**A deployment replaces no instance, and needs none replaced** (section 4.3). A pool has no
instance outside a session, and a deployment waits while a session is active (section 8.2), so a
release normally finds nothing to reach: the instances of the next session launch from the new
launch template, and each reads the pool's release at its start. There is no instance refresh to
start or to wait for. An instance that is in service when a release changes, because a session
was booked while a deployment was under way, keeps what it started with until it leaves. What
checks a release is the pipeline's verification, which books an instance: after a deployment that
changes a GPU pool's engine image, model revision or engine flags, it books one instance of the
coursework pool and asserts that the instance serves the release deployed (Verify, step 2).

**What this runbook rests on.**

- **The promotion job,** a CodeBuild project beside the pipeline, which the owner starts by hand
  and nothing else starts (`scripts/pipeline/README.md`). Its canary start is the one GPU instance
  that runs outside a session: the job launches one instance from the pool's launch template, with
  the candidate image, outside the pool's group, and terminates it whatever came of it.
- **The scheduled advisory check,** `.github/workflows/advisories.yml`: once a day it opens an
  issue when the pinned vLLM version has a high or critical advisory (section 9).
- **The deployment hold:** a step before each stage's deployment that waits while a session is
  active on any pool (section 8.2, step 5). It is a check before the deployment, not a pause of
  one: a deployment that has passed it runs to its end.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`; this runbook needs none. A step that needs the
account is the `aws` command you run with your own credentials, in the region of the endpoint; it
and the commands `gh`, `git` and `curl` are tested only by walking through the runbook.

A name or an ID the account gives is a placeholder below, never the name itself:

| Placeholder | What it stands for | Where it comes from |
| --- | --- | --- |
| `OWNER/REPOSITORY` | This repository on GitHub | |
| `<sha>` | A full commit SHA | |
| `<pipeline>` | The pipeline | The first command below prints it |
| `<execution>` | The ID of one execution of the pipeline | The step that starts or finds it |
| `<promotion-project>` | The CodeBuild project of the image promotion job | `PROJECT_NAME` in `src/rhi/pipeline/image_promotion.py` |
| `<canary-project>` | The CodeBuild project of the weekly GPU canary | `NAME` in `src/rhi/pipeline/canary.py` |
| `<image-parameter>` | The SSM parameter that holds the pool's machine image | `image_parameter` in `src/rhi/pools/image.py`, with the pool's name |
| `<release-parameter>` | The SSM parameter that holds the pool's release | `release_parameter` in `src/rhi/pools/group.py`, with the pool's name |
| `<build>` | The ID of a CodeBuild build | The command that started it, or step 2 of Verify |
| `<heartbeats-table>` | The stage's heartbeats table | Step 3 of Verify prints it |
| `<log-group>`, `<log-stream>` | Where a build's log is | The command below that reads a build |

`coursework` stands for the pool you upgrade. The pipeline's name:

```sh
aws codepipeline list-pipelines --query 'pipelines[].name' --output text
```

A build's state and where its log is, then the end of that log, which is how this runbook reads
what a job or the verification printed:

```sh
aws codebuild batch-get-builds --ids <build> \
    --query 'builds[0].[buildStatus,logs.groupName,logs.streamName]' --output text
aws logs get-log-events --log-group-name <log-group> --log-stream-name <log-stream> \
    --query 'events[].message' --output text
```

The admin CLI is run from the repository's root, with your own AWS credentials; define its name
once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

## When

- **A machine image:** AWS publishes a new release of the base GPU image (a driver fix, a security
  fix, an instance family a pool needs), or a pool's launches fail on the image it has.
- **The engine:** the advisory check opens an issue that names a high or critical advisory of the
  pinned version, or the advisories you read by hand show one (Check, step 4): adopt the fixed
  version at once. Any other new vLLM release waits 7 days after its publication before it is
  adopted (section 17): a compromised release is usually caught within hours (section 9).
- **Between sessions, and not while parked.** A deployment waits while a session is active, so a
  promotion or a merge made during a class is deployed after it. The canary start of a promotion
  asks AWS for one GPU instance beside those of a session, against the same quota. And the
  promotion job refuses a pool that has no group, as a parked stage has none (runbook
  [park and unpark](park-and-unpark.md)).

## Check

1. **No session is active, and none starts before the execution has ended.** One line for each
   pool, with its session, `none`, `booked` or `active`, and its times:

   ```sh
   rhi_admin session status
   ```

   While a session is active on any pool, the deployment waits for its end, and so does the
   verification of the upgrade. A session that is booked and has not started holds nothing: if it
   starts while the deployment is under way, its instances may start on either release, and each
   keeps what it started with. So upgrade when the next session's start is hours away, or move
   that start (runbook [a session booked, changed and stopped](sessions.md)).

2. **No pool's launches are suspended:**

   ```sh
   rhi_admin pool status
   ```

   A pool that reads `launch_suspended` `true` launches nothing, so the verification's instance
   never comes: clear it first (runbook [a pool whose launches are suspended](suspended-launches.md)).

3. **For a machine image: the candidate, and the image it replaces.** Take the candidate's ID, in
   the endpoint's region, and the instance families it lists, from the release notes of AWS's base
   GPU image. The job refuses a pool when the candidate does not list the family of each of the
   group's instance types (G7e for `g7e.2xlarge`). Write down the image the pool has now, which a
   promotion of it puts back should the new one go wrong:

   ```sh
   aws ssm get-parameter --name <image-parameter> --query Parameter.Value --output text
   ```

4. **For the engine: the advisory, and the version that fixes it.** The pinned version:

   ```sh
   cat services/engine/vllm/VERSION
   ```

   The advisories GitHub knows for it, with their severity and the first version that fixes each,
   `<version>` being what the file holds:

   ```sh
   gh api -X GET /advisories -f ecosystem=pip -f affects=vllm@<version> --paginate \
       --jq '.[] | "\(.ghsa_id) \(.severity) \([.vulnerabilities[] | select(.package.name == "vllm") | .first_patched_version] | join(","))"'
   ```

   A `high` or `critical` line is a reason to upgrade now, to the first patched version or a later
   one. Then read the release notes of every vLLM release between the pinned version and the new
   one, for renamed metrics and changed flags: `services/common/rhi_common/engine_metrics.py`
   names vLLM's metrics, and `src/rhi/pools/vllm_command.py` its flags, at the pinned version, and
   the change must bring each of them to the new one.

## Act

### A machine image

1. **Start the promotion job,** with the pool, the candidate's ID and the families its release
   notes list, comma-separated. The command prints the build's ID, `<build>` below:

   ```sh
   aws codebuild start-build --project-name <promotion-project> --query build.id --output text \
       --environment-variables-override '[{"name": "RHI_PROMOTE_POOL", "value": "coursework"}, {"name": "RHI_PROMOTE_IMAGE", "value": "ami-<candidate>"}, {"name": "RHI_PROMOTE_FAMILIES", "value": "G6e,G7e,P5,P5en"}]'
   ```

   The job starts its canary: one GPU instance, from the pool's launch template with the
   candidate in place of the template's image, outside the pool's group and outside any session.
   It is the one GPU instance of this system that no session asked for. It is billed at one
   instance's price for its minutes, and the job terminates it whatever came of it. It changes no
   group's capacity and books nothing.

2. **Read the job's end** in the build's log, with the two commands of the introduction: the build
   has ended once its state reads other than `IN_PROGRESS`. The log has the line
   `promote_image: pool coursework, candidate ...`, one line for each step the job took, and ends
   with one of:

   - `promote_image: promoted`, after `promote_image: pipeline started`: the canary served, the
     parameter holds the candidate, and the job started an execution of the pipeline;
   - `promote_image: not promoted:` and any other cause (`family-not-listed`, `image-unavailable`,
     `canary-not-started`, `canary-failed`, `canary-timed-out`, ...): nothing was written, the
     pool keeps its image, and no execution started. Fix the cause, or choose another candidate,
     and start again. A canary that did not start, or timed out, may only have found no capacity:
     start the job again later;
   - `promote_image: not promoted: pipeline-not-started`: the parameter **was** written, but the
     job started no execution. Start one yourself; the command prints its ID, `<execution>`:

     ```sh
     aws codepipeline start-pipeline-execution --name <pipeline> --query pipelineExecutionId --output text
     ```

     Skip the search by the build's times in step 3: that execution was started after the build,
     by you, and step 3's last paragraph still applies to it.

3. **Find the execution the job started,** when it ended with `promote_image: promoted`, and note
   its ID as `<execution>`. The job prints no
   ID. Its execution started while the job's build ran, between the build's start and end times,
   with the trigger `StartPipelineExecution` and, as the trigger's detail, the job's own role
   rather than your identity. The build's times:

   ```sh
   aws codebuild batch-get-builds --ids <build> --query 'builds[0].[startTime,endTime]' --output text
   ```

   The pipeline's latest executions, with their trigger and its detail (the detail names a role
   with the account: read it in your terminal and never paste it anywhere):

   ```sh
   aws codepipeline list-pipeline-executions --pipeline-name <pipeline> --max-items 10 \
       --query 'pipelineExecutionSummaries[].[pipelineExecutionId,status,startTime,trigger.triggerType,trigger.triggerDetail]' \
       --output text
   ```

   The one execution whose start falls inside the build's times and whose trigger's detail names
   the job's role is `<execution>`. It deploys the commit at the head of `main` again, which
   other executions may have deployed before: what the commit's statuses say can be theirs.
   Verify reads this execution only.

   The pipeline is not set to restart an execution when its self-update (the stage
   `UpdatePipeline`, its action `SelfMutate`) changes it. Read the actions of `<execution>`,
   whichever way it was started (Verify, step 1): when it ended without any action of the stage
   `Rhi`, and no newer execution is in progress, start one with the command of step 2; from then
   on `<execution>` is that new execution, whose ID the command printed.

   The deployment writes the candidate into the pool's launch template. It replaces no instance:
   the instances of the pool's next session launch on the new image.

### The engine

1. **Find the new digest:** that of the multi-architecture index of the upstream release you
   chose, `v<new>` below, as Docker Hub publishes it:

   ```sh
   curl -s https://hub.docker.com/v2/repositories/vllm/vllm-openai/tags/v<new> | jq -r .digest
   ```

2. **Write the change as backlog work,** with the shared `backlog` skill (`/backlog` in Claude
   Code): one draft whose deliverables are the `FROM` line of `services/engine/vllm/Dockerfile`
   with the new tag and digest, `services/engine/vllm/VERSION` with the new version, and the
   metric names and flags of Check, step 4 at the new version; its context names the advisory's
   identifier, the pinned version and the fixed one. Check it:

   ```sh
   scripts/backlog.sh lint
   ```

   The draft is committed and merged like any change (`backlog/README.md`, "Lifecycle"). Then,
   from `main`, preview what publishing would create; it sends nothing:

   ```sh
   scripts/backlog.sh publish
   ```

   Publishing works on every draft under `backlog/drafts/`, not on yours alone: each draft that is
   not on GitHub yet becomes an issue. Read every issue the preview lists. Each one that is not
   your draft must already be on GitHub, which the publisher knows by its marker and never creates
   again; look each up by its title:

   ```sh
   gh issue list --repo OWNER/REPOSITORY --state all --search "<title> in:title"
   ```

   Only when every other draft the preview lists is already an issue, publish:

   ```sh
   scripts/backlog.sh publish --apply
   ```

   When another draft is not on GitHub yet, do not publish: that work is not meant to be sent
   now. Take the branch below instead.

   The implement loop then builds it and opens its pull request, once you launch it on the new
   issue (`/implement <n>`, `docs/factory/operating.md`). For a security fix that cannot wait for the loop, make the change
   on a branch yourself and open the pull request:

   ```sh
   git switch main
   git pull --ff-only
   git switch -c chore/vllm-<new>
   scripts/check.sh all
   git commit -am "Raise the engine to vLLM <new>"
   git push -u origin chore/vllm-<new>
   gh pr create --base main --fill
   ```

   with the two lines edited between the branch and the check.

3. **Merge it,** once the check `required` is green. Note the merge commit's SHA as `<sha>`, then
   the execution that deploys it, `<execution>`:

   ```sh
   aws codepipeline list-pipeline-executions --pipeline-name <pipeline> \
       --query "pipelineExecutionSummaries[?sourceRevisions[0].revisionId=='<sha>'].[pipelineExecutionId,status,startTime]"
   ```

   A new engine image changes the pipeline's asset steps, so the execution's self-update (the
   stage `UpdatePipeline`, its action `SelfMutate`) updates the pipeline, and the pipeline is not
   set to restart an execution when it is updated: the execution can end there without any action
   of the stage `Rhi` (Verify, step 1, lists them), and no other execution starts on its own. Then,
   when no newer execution is in progress, start one; the command prints its ID:

   ```sh
   aws codepipeline start-pipeline-execution --name <pipeline> --query pipelineExecutionId --output text
   ```

   It deploys the head of `main`, which is `<sha>` while nothing was merged after it, and the
   query above lists it too. `<execution>` is the execution of `<sha>` whose actions of the stage
   `Rhi` ran. The pipeline builds the new engine image into ECR, and every pool served by vLLM
   gets a new release. No instance is replaced: the release is what the instances of each pool's
   next session read at their start.

### What a release reaches, and what it costs

A release reaches an instance at its launch, and only then. With no session active, which is when
a deployment runs, no pool has an instance: the deployment has nothing to replace and waits for
nothing, and the upgrade costs no GPU start of its own. Two things do start a GPU instance, each
one instance for its minutes, at the pool's price (3.36 dollars an hour for the example's
`g7e.2xlarge`):

- **the canary start of a promotion** (Act, "A machine image", step 1), outside any session;
- **the verification of an engine upgrade,** which books one instance of the coursework pool,
  waits for it for `check_wait_minutes` (45 by default), runs its tests and stops the session it
  booked (Verify, step 2). When AWS gives no instance in that time, it books nothing more and
  costs nothing: the GPU tests are recorded as not run, for lack of capacity.

A promotion's own execution books no GPU instance: a machine image is no part of a pool's release,
and its test was the canary start. An engine upgrade gives every pool served by vLLM a new
release, the larger pools included, and the verification reads it on the coursework pool only.

## Verify

1. **Every action of the execution succeeded, at its latest attempt.** Read the actions of
   `<execution>`, one line for each attempt, in the order they started, which is the last column:

   ```sh
   aws codepipeline list-action-executions --pipeline-name <pipeline> \
       --filter pipelineExecutionId=<execution> \
       --query 'actionExecutionDetails[].[stageName,actionName,status,startTime]' --output text | sort -k4
   ```

   The list holds every attempt, not every action: an action that failed and was retried, with
   its stage, has one line for each attempt under the same execution. Only the latest attempt of
   an action counts, the last line that names it. An earlier `Failed` line of an action whose last
   line reads `Succeeded` is the attempt the retry replaced, not a failure of the execution.

   The last line of every action reads `Succeeded`, `Verify` included. `Hold` reads `InProgress`
   for as long as a session is active: the deployment waits for its end. An action whose last
   line reads `Failed` is the runbook [a bad deployment](bad-deployment.md); for a machine image,
   its Act, step 2 promotes the image of Check, step 3 back.

2. **For the engine: the verification booked an instance of the coursework pool, and the release
   assertion passed on it.** A Verify that succeeded does not say it: Verify also succeeds when it
   ran no GPU test, and the commit's `verify/<stage>` status can describe another execution of the
   same commit. Read the log of this execution's Verify, at its latest attempt: when Verify was
   retried, each attempt has a build of its own, and an earlier one is not the build that passed.
   The ID of the build of the attempt that started last, `<build>`, between quotes:

   ```sh
   aws codepipeline list-action-executions --pipeline-name <pipeline> \
       --filter pipelineExecutionId=<execution> --output json \
       --query "sort_by(actionExecutionDetails[?actionName=='Verify'], &startTime)[-1].output.executionResult.externalExecutionId"
   ```

   It prints one ID. Then read that build's log, with the two commands of the introduction. A
   release that is verified reads, in this order:

   - `gpu-verification: the release of the pool coursework changed:` and the keys that differ,
     `engine_image` among them, one such line for each pool served by vLLM; then
     `gpu-verification: the smoke tests run on coursework`;
   - `verify: booked the pool coursework for 90 minutes`, twice `check_wait_minutes`, then
     `verify: the pool coursework reads ready`: the instance the verification booked launched on
     the new release and came into service;
   - pytest's summary of the run on the coursework pool, with tests `passed` and none `failed` or
     `error`. Its tests `gpu-release-served` and `release-heartbeat` (`tests/smoke/ids.py`) compare
     the image digest and model revision the pool's instances serve with the deployed release. The
     summaries before it are those of the mock pool;
   - `verify: stopped the session it booked on the pool coursework`.

   Any other end means the pipeline has not verified the release on a GPU:

   - `verify: no instance of the pool coursework was launched in 45 minutes: its tests are not
     run, for lack of capacity`, then `verify: the GPU tests were not run: no-capacity`. No
     capacity is not a failed release: the execution passes on the mock pool's tests, and the
     commit's `verify/<stage>` status reads "The mock pool's tests passed. The GPU tests did not
     run, for lack of capacity." The next weekly canary covers the release, the same way. To have
     it now, start the canary, which books one instance of the coursework pool, and read its
     build's log: it ends with `canary_result: gpu-canary pass sent for` and the day's date when
     its tests ran and passed.

     ```sh
     aws codebuild start-build --project-name <canary-project> --query build.id --output text
     ```

   - `verify: the pool coursework already has a session: it is neither booked nor stopped`:
     someone booked the pool meanwhile, and the tests ran on the instances that were there, which
     may have started before the release. Read step 3 for the pool at its next session;
   - `gpu-verification: the smoke tests run on no GPU pool`: this execution changed nothing of a
     GPU pool's release that only a GPU instance can test. It is the expected line of a promotion's
     execution, since a machine image is no part of a release, and step 4 applies. For the engine
     it means that no pool's release changed in this execution: check that it is the execution
     that deployed the change (Act, "The engine", step 3);
   - `verify: no test was run:` followed by `listener-off` or `canary-missing`: no test ran at all,
     and step 3 applies to every affected pool;
   - `verify: the pool coursework did not read ready in 45 minutes: the tests run`, in place of
     the line that reads ready. This line is not a result. The wait ended before the pool read
     `ready`, and the verification could not say that no instance was launched: one was, or the
     heartbeats table could not tell, as when it was not read, which a line above then says. The
     tests run all the same, and
     the summary that follows decides. With tests `passed` and none `failed` or `error`, the
     instance came into service as the wait ended and served the release: the release is
     verified, as above. With a test `failed` or `error`, it is the next case;
   - a summary of the run on the coursework pool with a test `failed` or `error`: an instance was
     launched and did not serve, or did not serve the release. The verification failed, the
     action Verify reads `Failed` in step 1, and the runbook [a bad deployment](bad-deployment.md)
     applies.

3. **Every affected pool whose release the smoke tests did not read.** The affected pools are the
   promoted pool for a machine image, and every pool served by vLLM for the engine. Of them, the
   smoke tests read only the coursework pool, and only when step 2 found its summary. Each other
   affected pool is checked here, at its next session, when its first instance launches on the new
   release: a pool other than coursework, and the coursework pool itself when step 2 ended
   otherwise. Write down which pools these are.

   What the pool's instances must serve is what its release parameter holds: the digest that ends
   its `engine_image`, after the `@`, and its `model_revision`. The command prints the two, one
   on each line:

   ```sh
   aws ssm get-parameter --name <release-parameter> --query Parameter.Value --output text | jq -r '.engine_image, .model_revision'
   ```

   What they serve is what each instance writes about itself in its row of the heartbeats table,
   which is where the smoke test `release-heartbeat` reads it. The table's name,
   `<heartbeats-table>`:

   ```sh
   aws cloudformation describe-stacks --stack-name Rhi-Pools \
       --query "Stacks[0].Outputs[?OutputKey=='HeartbeatsTable'].OutputValue" --output text
   ```

   and, once the pool's session has an instance in service, the rows of the pool's instances,
   with `coursework` or the pool's name in its place: for each, the stage it reached, the image
   digest and the model revision it serves, and when it wrote the row, in seconds:

   ```sh
   aws dynamodb query --table-name <heartbeats-table> --consistent-read \
       --key-condition-expression '#pool = :pool AND begins_with(#source, :instance)' \
       --expression-attribute-names '{"#pool": "pool", "#source": "source"}' \
       --expression-attribute-values '{":pool": {"S": "coursework"}, ":instance": {"S": "instance#"}}' \
       --query 'Items[].[stage.S,image_digest.S,model_revision.S,written_at.N]' --output text
   ```

   An instance in service writes its row every 5 seconds, with the stage `serving`. Read the rows
   written in the last minute, `date -u +%s` being now in the same seconds: an older row is that
   of an instance that has left. The pool is verified when it has such a row and every such row
   carries the digest and the revision of the parameter. The command prints nothing while the pool
   has no instance, and a row with another stage is an instance still starting: read again once
   `rhi_admin session status` counts it in service.

   In Grafana, the dashboard "Deployments and releases" shows the same over time, in its panel
   "Release each instance serves: image digest and model revision".

   A pool whose first instance on the new release serves anything else is a bad deployment (runbook
   [a bad deployment](bad-deployment.md)). Until each such pool has had a session, its release is
   deployed but not verified: say so wherever you report the upgrade.

4. **For a machine image, the instances of the pool's next session run it.** The promotion
   replaced no instance, and outside a session the pool has none, so the command prints nothing
   until then. Once the first instance of the next session runs, it shows the candidate's ID
   (`coursework` below, or the pool's name in its place):

   ```sh
   aws ec2 describe-instances --filters Name=tag:rhi-pool,Values=coursework \
       Name=instance-state-name,Values=running \
       --query 'Reservations[].Instances[].[ImageId,State.Name]' --output text
   ```

   Until it has, the image is promoted and its canary start served, and no session has run on it:
   say so wherever you report the upgrade.

5. **No pool's launches are suspended:**

   ```sh
   rhi_admin pool status
   ```

6. **For an advisory, close its issue,** with the version now pinned:

   ```sh
   gh issue close <n> --repo OWNER/REPOSITORY --comment "The engine is now vLLM <new>."
   ```

Walked through on the deployed system: not yet.
