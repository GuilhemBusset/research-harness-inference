# Secret and token rotation

The endpoint and its factory hold few secrets of their own, and no rotation is automatic. Each is
rotated by hand, when it may have been exposed, when someone who could read it no longer should,
or at the start of a term. This runbook covers, for each one, how to rotate it and what restarts:

| Secret | Where it is | Who reads it, and when |
| --- | --- | --- |
| A pool's bearer secret (section 4.3) | Secrets Manager, `/rhi/pools/<pool>/bearer` | The pool's engine, at its start; the lifecycle agent, at each minute's check; each gateway task, once, at its start |
| The status reporter App's private key (section 8.6) | Secrets Manager, `rhi/reporter/github-app` | The reporter function, once per execution environment, at its first status |
| The factory App's private key (ADR 0001, amendment 2) | A file on the factory's host, `LOOP_GITHUB_APP_KEY_FILE` | `openssl`, each time the engine mints a token |
| The student login App | Nothing: it has no deployed credential | The issuer uses its client ID alone |
| The canary's key (section 8.2) | Secrets Manager, `rhi/canary-key` | The verification step and the weekly GPU canary, at each run |

Section 8.6 says the three Apps' secrets go to Secrets Manager. Of the three, only the status
reporter's is there: the login App's device flow needs no secret, and the factory's key never goes
to AWS.

Like every check against the endpoint, the checks of this runbook that go through the gateway (the
verification's smoke tests, the GPU canary) assume the public HTTPS listener is on, which it is
not by default.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. Anything else (the AWS console, GitHub, `aws`,
`gh`, `git`) is written out in full, with placeholders in angle brackets for what only your
account shows, and is tested only by walking through the runbook.

The admin CLI and `aws` are run from the repository's root, with your own AWS credentials, in the
region the endpoint is deployed to. The code blocks call the admin CLI `rhi_admin`; define that
name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

`coursework` below stands for a pool, by its name in the configuration; repeat a pool's steps for
each pool, the mock pool included. `OWNER/REPOSITORY` stands for this repository on GitHub.

## When

- **A secret may have been exposed:** it appeared in a log, a terminal recording, a screenshot, a
  commit or a chat; a laptop that held a key file was lost; GitHub warns that an App's key was
  found in a public place.
- **Someone who could read it no longer should:** a person who held the owner's AWS access or the
  factory's host leaves the course.
- **At the start of a term,** to begin it with secrets nobody from the last one has seen.
- **The canary's key** also when the verification reports "No test ran: the canary's key is
  missing." (runbook [a bad deployment](bad-deployment.md)): then it is created rather than
  rotated, with the same command.

A student's key is not here: it is the runbook [a leaked key](leaked-key.md).

## Check

1. **No session is on.** Rotating a pool's bearer secret restarts the pool's engines and replaces
   the gateway's tasks, which cuts requests under way:

   ```sh
   rhi_admin session status
   ```

2. **No pipeline execution and no canary build is running.** A verification that is running when
   the canary's key changes loses its key, and one that runs while the gateway's tasks are being
   replaced may fail. On the pipeline's page in the CodePipeline console, no execution is in
   progress; in the CodeBuild console, the project `rhi-gpu-canary` has no build in progress.

3. **No implement loop run is active.** The factory's key is read by every token the engine
   mints; replace it between runs only.

4. **What each consumer is,** by the marks of this repository, not by a name AWS gave it:

   - the gateway's service: in the ECS console, the cluster of the gateway's stack, and in it the
     service whose task definition has the container `Gateway`. Note the cluster's name and the
     service's name; they are `<gateway-cluster>` and `<gateway-service>` below;
   - the reporter function: in the Lambda console, the function whose environment variable
     `RHI_REPORTER_SECRET` reads `rhi/reporter/github-app`. Its name is `<reporter-function>`
     below.

## Act

### A pool's bearer secret

The engines of a pool require the pool's bearer secret on their inference routes (section 4.1),
and the gateway sends it. Writing a new value starts no instance refresh and needs none: each
reader takes the new value when it next starts.

1. **Write a new value.** A new version of the secret, 48 letters and digits, generated by Secrets
   Manager, as the stack generated the first one. The value passes through no file:

   ```sh
   aws secretsmanager put-secret-value --secret-id /rhi/pools/coursework/bearer \
       --secret-string "$(aws secretsmanager get-random-password --password-length 48 \
           --exclude-punctuation --no-include-space --query RandomPassword --output text)"
   ```

   In the console, the same is: Secrets Manager, the secret `/rhi/pools/coursework/bearer`,
   **Retrieve secret value**, **Edit**, a new value of 48 letters and digits, **Save**.

2. **What restarts on the instances.** Nothing to do by hand. Each instance in service has its
   lifecycle agent send its engine a one-token completion every minute, with the secret as it is
   now in Secrets Manager (section 4.3). The engine still holds the value it read at its start, so
   it refuses that completion, and the agent restarts the engine's unit, which reads the new value
   as it starts. Within a minute or two of step 1, every running engine of the pool restarts,
   reloads its model (minutes on a GPU pool), and answers with the new value. An instance stopped
   in the warm pool reads the new value when it is next started. With no session on, a pool
   usually has no instance in service, and nothing restarts here at all.

3. **Replace the gateway's tasks.** Each gateway task read every pool's secret once, at its start,
   and keeps it: until it is replaced, it sends the old value, and the restarted engines refuse
   it. A new deployment of the service starts tasks that read the new value:

   ```sh
   aws ecs update-service --cluster <gateway-cluster> --service <gateway-service> \
       --force-new-deployment --query "service.deployments[?status=='PRIMARY'].id" --output text
   ```

   The command prints the deployment ID of the new deployment: note it, it is `<deployment-id>`
   in Verify. ECS starts the new tasks, then drains each old one, for up to 30 minutes, the
   gateway's deregistration delay (section 8.5). The service has ECS's deployment circuit breaker
   with rollback: when the new tasks fail to start, ECS rolls back to the previous deployment,
   which then runs and completes again with the old tasks and the old value. So it is the
   deployment with this ID that must complete, not whichever deployment is primary at the end.
   After rotating the secrets of several pools, one deployment of the gateway, after the last of
   them, is enough.

### The status reporter App's private key

A GitHub App may hold more than one private key at a time, and each signs until it is deleted.
So the new key is added first, every reader is moved to it, and the old key is deleted last.

1. **Generate a new key.** On GitHub: **Settings**, **Developer settings**, **GitHub Apps**, the
   status reporter, **Edit**, then under **Private keys**, **Generate a private key**. GitHub
   downloads it once. Move it out of the downloads folder, outside every checkout of this
   repository, readable by you alone. Note the fingerprint GitHub shows for the old key.

2. **Store it in the reporter's secret.** The Apps step of the owner's walkthrough reads each App
   back with its own key, which proves the new key signs, and replaces the value of
   `rhi/reporter/github-app` with the App's ID, its installation's ID and the new key. It reads the
   factory's App back too, with the key file you name:

   ```sh
   setup/owner-setup.sh --step apps --reporter-key "$HOME/.config/rhi/reporter-app.pem" \
       --factory-key "$LOOP_GITHUB_APP_KEY_FILE"
   ```

   `$HOME/.config/rhi/reporter-app.pem` is an example path: give the path where you put the new
   key. `--factory-key` takes the factory's current key file, the one `LOOP_GITHUB_APP_KEY_FILE`
   names (the factory App's part, below, says how to read it).

3. **Move the reporter to the new key.** The reporter reads the secret once in each execution
   environment, at the first status it posts there, and keeps the key for that environment's
   life; an environment already warm keeps signing with the old key. An update of the function's
   configuration makes Lambda send every later invocation to a new environment, which reads the
   secret again. Change the description, which nothing reads:

   ```sh
   aws lambda update-function-configuration --function-name <reporter-function> \
       --description "Credential reloaded" --query LastUpdateStatus --output text
   ```

   The template sets no description, so the change does no harm; a later deployment that updates
   the function may clear it. The command returns while the update is still `InProgress`, and
   until the update has completed Lambda goes on invoking the function with its previous
   configuration, in environments that may hold the old key. Wait for it, then read its result:

   ```sh
   aws lambda wait function-updated --function-name <reporter-function>
   aws lambda get-function-configuration --function-name <reporter-function> \
       --query '[LastUpdateStatus,LastUpdateStatusReasonCode]' --output text
   ```

   Go on only when it reads `Successful`. `Failed` means the rotation failed: warm environments
   keep the old key, so do not delete it; read the reason in the Lambda console, fix it, and run
   the update again.

4. **Check that statuses are posted with the new key** (Verify, step 3), and only then delete the
   old key on the App's page, under **Private keys**, by the fingerprint you noted. Deleting it
   first would leave a warm environment unable to post a status until it is retired.

### The factory App's private key

The factory's key never goes to AWS. It is the file that `LOOP_GITHUB_APP_KEY_FILE` names on the
factory's host. The engine never reads it into Python and keeps no copy: `openssl` signs with the
file each time a token is minted, so the next token minted after the file changes uses the new
key, and nothing restarts.

1. **Generate a new key** on the factory App's page, as for the reporter (step 1 above), and note
   the old key's fingerprint.

2. **Find the file the engine reads.** The engine signs with the file `LOOP_GITHUB_APP_KEY_FILE`
   names in the environment the loop is launched from: the walkthrough of
   `docs/factory/green-light.md` sets it in the shell, or in a file outside the repository that
   the shell reads, with an example path that yours may not match. Read it in a shell set up as
   the one you launch the loop from, and check that it names an existing file:

   ```sh
   printenv LOOP_GITHUB_APP_KEY_FILE
   test -f "$LOOP_GITHUB_APP_KEY_FILE" && echo "the configured key file exists"
   ```

   An empty answer, or no file there, means the variable is not set where the loop runs: stop,
   and find where it is set before going on. Replacing another file would leave the engine on the
   old key, and the doctor too.

3. **Replace the file at the configured path,** keeping its name and its mode:

   ```sh
   install -m 600 "$HOME/Downloads/<new-key>.private-key.pem" "$LOOP_GITHUB_APP_KEY_FILE"
   rm "$HOME/Downloads/<new-key>.private-key.pem"
   ```

4. **Check the new key** with the doctor, from the same shell: the line `github identity` mints a
   token with the file the variable names.

   ```sh
   scripts/implement.sh doctor
   ```

5. **Delete the old key** on the App's page, by its fingerprint, once the doctor passes.

### The student login App

The login App has no deployed credential to rotate. Its device flow needs no client secret and no
private key: the issuer redeems a device code with the App's client ID alone (section 6.4), the
configuration's `login_client_id`, which is not a secret and which GitHub does not rotate.

1. On the App's page, under **Client secrets** and **Private keys**, delete any that exist:
   nothing reads them, and a key that exists can be stolen.
2. Only if the App itself had to be deleted and registered again does its client ID change: put the
   new one in `login_client_id` in your configuration file, write it to SSM, and start a pipeline
   run with **Release change** on the pipeline's page in the CodePipeline console. The deployment
   updates the issuer. Students keep their keys: a key does not depend on the App.

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

### The canary's key

The canary is a roster entry the pipeline's verification and the weekly GPU canary use (section
8.2). One command revokes the canary's earlier keys, issues a new one and writes it to the secret
`rhi/canary-key`; the key is printed nowhere:

```sh
rhi_admin canary
```

The old key stops working within 60 seconds, the gateway's lookup cache (section 6.4). Nothing
restarts: the verification step and the canary job read the secret at the start of each run, so
the next run uses the new key.

## Verify

1. **A pool's bearer secret: the gateway's deployment.** The deployment the rotation started, by
   the deployment ID that Act noted, has completed:

   ```sh
   aws ecs describe-services --cluster <gateway-cluster> --services <gateway-service> \
       --query "services[0].deployments[?id=='<deployment-id>'].[status,rolloutState,rolloutStateReason]" \
       --output text
   ```

   It reads `PRIMARY COMPLETED`. Anything else is not a reload: `IN_PROGRESS` is still rolling
   out, so wait and read it again; `FAILED`, or a deployment that is no longer listed while
   another one is primary, means the circuit breaker rolled back to the previous deployment,
   whose tasks still send the old value. The rotation is then not done: read the service's
   events in the ECS console and the log group `/rhi/gateway`, fix the cause, and start the
   deployment again (Act).

2. **A pool's bearer secret: the endpoint serves the rotated pool.** For each pool whose secret
   you rotated, send one request through the gateway to that pool, with the canary's key: the
   canary is the owner's roster entry, served whatever the session switch (section 8.2), and it
   may wake a pool that students may not, a `scheduled-only` one. The request names the pool by
   its `model`; the key goes from Secrets Manager to curl on a pipe, and is printed nowhere.
   `<endpoint>` is the endpoint's address, as `rhi login` uses it:

   ```sh
   aws secretsmanager get-secret-value --secret-id rhi/canary-key --query SecretString --output text \
       | sed 's/^/Authorization: Bearer /' \
       | curl -sS --fail-with-body --max-time 600 -H @- -H 'content-type: application/json' \
           https://<endpoint>/v1/messages \
           -d '{"model": "coursework", "max_tokens": 16, "messages": [{"role": "user", "content": "Say OK."}]}'
   ```

   with the rotated pool's name in place of `coursework`. An answer with a message's content is a
   pass: the gateway sent the new value and the restarted engine took it. On a GPU pool with no
   instance, the request wakes it, which is one GPU start; the gateway holds a call for up to 4
   minutes (section 5.3), so an error whose cause is `warming` only means the pool is still
   starting: send the request again until it answers. A pool the admin disabled answers
   `pool_disabled` and is not tested: enable it first, or test it when it is next enabled
   (runbook [a disabled pool](disabled-pool.md)). An error with any other cause after the pool is
   `ready` is a failed rotation: the gateway and the engines do not hold the same value. The pool
   goes back to sleep by itself when it is idle.

   The weekly canary tests the coursework pool and no other, so it can stand in for this request
   only for coursework (step 5 says how to read its result).

3. **The status reporter's key.** A run of the pipeline posts statuses created after the update of
   the reporter's configuration (its Act, step 3) completed: start one with **Release change**,
   wait for it to end, and read the statuses of the commit it deployed, the head of `main`. The
   newest `deploy/<stage>` line carries a time after the update. Read it once more after deleting
   the old key, from the next run.

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/main/statuses \
       --jq '.[] | "\(.created_at) \(.context) \(.state)"'
   ```

4. **The factory's key.** `scripts/implement.sh doctor` shows `github identity` as `PASS`, with the
   App's name, after the old key was deleted.

5. **The canary's key.** A build that ends without error does not show that the key works: the
   verification and the canary both end in success when no test ran, with the canary's secret
   missing or the HTTPS listener off. Read what the tests did.

   - The verification: after a **Release change**, its `verify/<stage>` status reads "The
     verification's tests ran and passed.", not one that begins "No test ran", and the end of
     its Verify action's CodeBuild log reports tests `passed` and none `failed` or `error`.
   - Or the weekly canary, started now; it wakes the coursework pool, one GPU start:

     ```sh
     aws codebuild start-build --project-name rhi-gpu-canary --query build.id --output text
     ```

     In the CodeBuild console, the canary's log of that build ends with the line
     `canary_result: gpu-canary pass sent for` and the day's date, after pytest's summary of tests
     `passed` and none `failed`. A line `no test was run`, or a verdict `not_measured` or `fail`,
     is not a pass, whatever the build's status.

6. **The login App.** A student, or your own roster entry, logs in with `rhi login`.

Walked through on the deployed system: not yet.
