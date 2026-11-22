# Machine image and engine upgrade

Two things under a GPU pool are pinned, and each changes only on purpose (system design, section
4.3):

- **The machine image,** AWS's base GPU image with the open-source NVIDIA driver on Ubuntu 22.04.
  The pool's launch template names it by the ID in the SSM parameter `/rhi/images/<pool>`, and only
  the image promotion job writes that parameter, after a canary start on the new image passes. A
  merge never changes the driver under the engine.
- **The engine,** the upstream vLLM image that `services/engine/vllm/Dockerfile` pins by the digest
  of its multi-architecture index, with the upstream version it is in `services/engine/vllm/VERSION`.
  Raising it is a change of those two lines, through a pull request.

A deployment does not reach an existing instance by itself. A new machine image, or a new engine
image in a pool's release, starts an instance refresh of the pool's group, which replaces the
instances in service first and then those in the warm pool, and the pipeline's verification then
asserts that the coursework pool serves the deployed release. Each such deployment costs one GPU
start per warm instance (Act, "What the refresh costs").

**What this runbook rests on.**

- **The promotion job** is in place: the CodeBuild project `rhi-promote-image`, which the owner
  starts by hand and nothing else starts (`scripts/pipeline/README.md`).
- **The scheduled advisory check** lands with issue E4-25: a GitHub Actions workflow that opens an
  issue when the pinned vLLM version has a high or critical advisory (section 9). Until it is in
  `.github/workflows/`, nobody is told: read the advisories by hand (Check, step 4), weekly while a
  course runs.
- **The deployment hold** lands with issue E4-42: a check before each stage's deployment that waits
  while the session switch reads on (section 8.2). Until it is deployed, nothing holds a
  deployment: start a promotion or merge an engine change only while no session is on, and switch
  none on until its execution has ended.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`; this runbook needs none. Anything else (the
AWS console, GitHub, `aws`, `gh`, `git`, `curl`) is written out in full and is tested only by
walking through the runbook.

`OWNER/REPOSITORY` below stands for this repository on GitHub, `<pipeline>` for the pipeline's
name, `<execution>` for the ID of one of its executions, `<sha>` for a full commit SHA, `<group>`
for a pool's Auto Scaling group and `coursework` for the pool you upgrade. The admin CLI is run
from the repository's root, with your own AWS credentials; define its name once in your shell:

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
- **Not during a session, and not while parked.** A refresh replaces the instances a class would
  use, and the promotion job refuses a pool that has no group, as a parked stage has none (runbook
  [park and unpark](park-and-unpark.md)).

## Check

1. **No session is on,** and none will be until the execution has ended:

   ```sh
   rhi_admin session status
   ```

   It must read `off`. Until the deployment hold of issue E4-42 is deployed, nothing else keeps the
   refresh out of a class.

2. **No pool's launches are suspended,** and the pool reads its configured mode:

   ```sh
   rhi_admin pool status
   ```

   A pool that reads `launch_suspended` `true` cannot launch the refresh's replacements: clear it
   first (runbook [a bad deployment](bad-deployment.md), Act, step 1).

3. **For a machine image: the candidate, and the image it replaces.** Take the candidate's ID, in
   the endpoint's region, and the instance families it lists, from the release notes of AWS's base
   GPU image. The job refuses a pool when the candidate does not list the family of each of the
   group's instance types (G7e for `g7e.2xlarge`). Write down the image the pool has now, which a
   promotion of it puts back should the new one go wrong:

   ```sh
   aws ssm get-parameter --name /rhi/images/coursework --query Parameter.Value --output text
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
   notes list, comma-separated:

   ```sh
   aws codebuild start-build --project-name rhi-promote-image \
       --environment-variables-override '[{"name": "RHI_PROMOTE_POOL", "value": "coursework"}, {"name": "RHI_PROMOTE_IMAGE", "value": "ami-<candidate>"}, {"name": "RHI_PROMOTE_FAMILIES", "value": "G6e,G7e,P5,P5en"}]'
   ```

   In the console, the same is CodeBuild, the project `rhi-promote-image`, **Start build with
   overrides**, and the three environment variables above.

2. **Read the job's end** in its build log (the build's page in the CodeBuild console). It prints
   `promote_image: pool coursework, candidate ...`, one line for each step it took, and ends with
   one of:

   - `promote_image: promoted`, after `promote_image: pipeline started`: the canary served, the
     parameter holds the candidate, and the job started an execution of the pipeline;
   - `promote_image: not promoted:` and a cause (`family-not-listed`, `image-unavailable`,
     `canary-failed`, `canary-timed-out`, ...): nothing was written, the pool keeps its image, and
     no execution started. Fix the cause, or choose another candidate, and start again. With
     `pipeline-not-started` the parameter **was** written: start an execution with **Release
     change** on the pipeline's page in the CodePipeline console.

   The canary start is one GPU start, at one instance's price for its minutes; the job terminates
   the canary whatever came of it.

3. **Find the execution the job started,** and note its ID as `<execution>`. The job prints no ID:
   it is the first execution that started after the job's `pipeline started`, with the trigger
   `StartPipelineExecution`:

   ```sh
   aws codepipeline list-pipeline-executions --pipeline-name <pipeline> --max-items 5 \
       --query 'pipelineExecutionSummaries[].[pipelineExecutionId,status,startTime,trigger.triggerType]' \
       --output text
   ```

   Should its self-update action change the pipeline, CodePipeline runs it again in a newer
   execution, and that one is `<execution>`. That execution deploys the commit at the head of `main` again, which other executions may have
   deployed before: what the commit's statuses say can be theirs. Verify reads this execution only.

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
   identifier, the pinned version and the fixed one. Check it and publish it:

   ```sh
   scripts/backlog.sh lint
   scripts/backlog.sh publish --apply
   ```

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

3. **Merge it with no session on** (Check, step 1), once the check `required` is green. Note the
   merge commit's SHA as `<sha>`, then the execution that deploys it, `<execution>`:

   ```sh
   aws codepipeline list-pipeline-executions --pipeline-name <pipeline> \
       --query "pipelineExecutionSummaries[?sourceRevisions[0].revisionId=='<sha>'].[pipelineExecutionId,status,startTime]"
   ```

   When the pipeline updates itself on the way (its self-update action changes the pipeline, and
   CodePipeline runs the change again), the query lists more than one execution of the commit:
   `<execution>` is the newest, the one that ran past the self-update to the stage's deployment.
   The pipeline builds the new engine image into ECR, and every pool served by vLLM gets a new
   release, which starts a refresh of each one that has instances.

### What the refresh costs

The refresh replaces each instance of the pool, in service and in the warm pool. A replaced warm
instance is one GPU start: the new instance launches, pulls the engine's image, checks its release,
completes its launch hook and stops. With the example's coursework pool, `warm_instances = 2` on
`g7e.2xlarge`, that is two starts at 3.36 dollars an hour each for the minutes they run, and one
more for each instance in service, which is why the refresh runs with no session on and nothing in
service. The verification then wakes the coursework pool once to run its smoke tests on it, which
costs about what a run of the weekly GPU canary costs, about 1.50 dollars with the idle tail
(section 11.2). A promotion adds its canary start (Act, step 2). A pool with no instance in service
or warm has nothing to replace, and an engine change refreshes every pool served by vLLM, the
larger pools included, while a machine image refreshes only the promoted pool.

## Verify

1. **Every action of the execution succeeded.** Read the actions of `<execution>`, the latest
   attempt first:

   ```sh
   aws codepipeline list-action-executions --pipeline-name <pipeline> \
       --filter pipelineExecutionId=<execution> \
       --query 'actionExecutionDetails[].[stageName,actionName,status,startTime]' --output text
   ```

   Every action reads `Succeeded`, `Verify` included. A failed one is the runbook
   [a bad deployment](bad-deployment.md); for a machine image, its Act, step 2 promotes the image of
   Check, step 3 back.

2. **The release assertion ran on the coursework pool, and passed.** A Verify that succeeded does
   not say it: Verify also succeeds when it ran no test, and the commit's `verify/<stage>` status
   can describe another execution of the same commit. In the detail page of `<execution>`, open the
   details of the action Verify, which link to its CodeBuild log, and read it:

   - `gpu-verification: the instance refresh of the pool coursework ended Successful` (one such
     line for each GPU pool that was refreshed), then `gpu-verification: the smoke tests run on
     coursework`: the decision found the refreshes this execution started, and waited for them;
   - then two of pytest's summaries that each report tests `passed` and none `failed` or `error`:
     the run on the mock pool, and the run on the coursework pool, whose tests
     `gpu-release-served` and `release-heartbeat` (`tests/smoke/ids.py`) compare the image digest
     and model revision the pool's instances serve with the deployed release. Both passed: the
     coursework pool serves the new release.

   Any other end means the release is not verified:

   - `gpu-verification: the smoke tests run on no GPU pool`: no refresh of a GPU pool began during
     this execution (the pool had no instance, or nothing of its release changed);
   - `verify: no test was run:` followed by `listener-off` or `canary-missing`: no test ran at all;
   - a summary with a test `failed` or `error`, or a refresh that ended otherwise than
     `Successful`: the verification failed, and the runbook [a bad deployment](bad-deployment.md)
     applies.

3. **Another pool that was refreshed,** an engine change's larger pools (`upgrade`, `xl`): the
   verification tests the coursework pool whichever GPU pool's refresh it found, never that pool.
   At that pool's next start, in Grafana, on the dashboard "Deployments and releases", the panel
   "Release each instance serves: image digest and model revision" shows its instances' image
   digest and model revision; they equal what its release parameter holds:

   ```sh
   aws ssm get-parameter --name /rhi/pools/upgrade/release --query Parameter.Value --output text
   ```

4. **For a machine image, the instances run it.** Every instance of the pool, the stopped warm ones
   included, shows the candidate's ID:

   ```sh
   aws ec2 describe-instances --filters Name=tag:rhi-pool,Values=coursework \
       Name=instance-state-name,Values=running,stopped \
       --query 'Reservations[].Instances[].[ImageId,State.Name]' --output text
   ```

5. **No pool's launches are suspended:**

   ```sh
   rhi_admin pool status
   ```

6. **For an advisory, close its issue,** with the version now pinned:

   ```sh
   gh issue close <n> --repo OWNER/REPOSITORY --comment "The engine is now vLLM <new>."
   ```

Walked through on the deployed system: not yet.
