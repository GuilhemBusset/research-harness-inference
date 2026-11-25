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

1. **No session is active.** Rotating a pool's bearer secret restarts the pool's engines and
   replaces the gateway's tasks, which cuts requests under way. Every pool reads `none`, or
   `booked` with a start that leaves time for the rotation:

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
and the gateway sends it. Writing a new value replaces no instance and needs none replaced: each
reader takes the new value when it next starts.

1. **Write a new value.** A new version of the secret, 48 letters and digits, generated by Secrets
   Manager, as the stack generated the first one. The simplest way is the console: Secrets
   Manager, the secret `/rhi/pools/coursework/bearer`, **Retrieve secret value**, **Edit**, a new
   value of 48 letters and digits, **Save**.

   With `aws`, the value must not be an argument of a command: any process on the machine, and
   any audit of commands, can read a process's arguments. So it goes from the command that
   generates it to the one that stores it on a pipe, and the second one reads it from its standard
   input (`file:///dev/stdin`). It passes through no file and is printed nowhere; `tr` drops the
   line end that `--output text` adds, which would otherwise become part of the secret:

   ```sh
   aws secretsmanager get-random-password --password-length 48 --exclude-punctuation \
       --no-include-space --query RandomPassword --output text \
       | tr -d '\n' \
       | aws secretsmanager put-secret-value --secret-id /rhi/pools/coursework/bearer \
           --secret-string file:///dev/stdin --query VersionId --output text
   ```

   It prints the new version's ID, and nothing of the value.

2. **What restarts on the instances.** Nothing to do by hand. Each instance in service has its
   lifecycle agent send its engine a one-token completion every minute, with the secret as it is
   now in Secrets Manager (section 4.3). The engine still holds the value it read at its start, so
   it refuses that completion, and the agent restarts the engine's unit, which reads the new value
   as it starts. Within a minute or two of step 1, every running engine of the pool restarts,
   reloads its model (minutes on a GPU pool), and answers with the new value. With no session
   active, a pool has no instance, and nothing restarts here at all: the instances of its next
   session read the new value at their start.

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
   repository, readable by you alone. Note the fingerprints GitHub shows, the old key's and the
   new one's.

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

   The step goes on when it cannot store the key: when the reporter is not read back, has no
   installation, or the write is refused, it says so and stores nothing, and the secret still
   holds the old key. Its output then lacks the line that reads the secret's name back. Whatever
   the output, check that the secret's current version, `AWSCURRENT`, holds the new key: the
   fingerprint of the key it holds must be the new key's, as GitHub showed it. The key goes from
   Secrets Manager to `openssl` on a pipe, and only its fingerprint is printed:

   ```sh
   aws secretsmanager get-secret-value --secret-id rhi/reporter/github-app --version-stage AWSCURRENT \
       --query SecretString --output text \
       | python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin)["private_key"])' \
       | openssl pkey -pubout -outform DER | openssl sha256 -binary | openssl base64
   ```

   GitHub shows a fingerprint as `SHA256:` followed by this value. Go on only when the value is
   the new key's. The old key's fingerprint, or an error, means the new key is not stored: do not
   reload the reporter and do not delete the old key; read what the Apps step said, fix it, and
   run step 2 again.

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
   first would leave a warm environment unable to post a status until it is retired. A fresh
   status shows the new key at work only because step 2 confirmed that the secret holds the new
   key and no other.

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

3. **Replace the file at the configured path,** keeping its name, with the mode `600`. The new
   key is first copied beside the old file, into a file that `mktemp` creates for this attempt
   with the mode `600` and a name no other file has, so no file already there is touched. Only a
   complete copy is moved over the old file, which within one directory replaces it in one step.
   Should the copy fail or be cut short (a directory that is not writable, a full disk, a
   file-size limit), the old file is untouched and still signs, the partial copy is removed, the
   download is kept, and the command says so. The download is removed only once the configured
   path holds the new key, and the command reports the two apart: which key the configured path
   holds, then whether the download is gone:

   ```sh
   staged=""
   if staged=$(mktemp "$LOOP_GITHUB_APP_KEY_FILE.new.XXXXXX") \
       && cat "$HOME/Downloads/<new-key>.private-key.pem" > "$staged" \
       && cmp -s "$HOME/Downloads/<new-key>.private-key.pem" "$staged" \
       && mv -f "$staged" "$LOOP_GITHUB_APP_KEY_FILE"; then
     echo "the new key is installed"
     rm "$HOME/Downloads/<new-key>.private-key.pem" \
       || echo "the download could not be removed: delete it by hand"
   else
     if [ -n "$staged" ]; then rm -f "$staged"; fi
     echo "not installed: the old key is in place and the download is kept"
   fi
   ```

   Go on to step 4 only after "the new key is installed", whatever the line after it says: a
   download that could not be removed is a copy of the new key to delete by hand, and does not
   change which key signs. Otherwise the engine and the doctor still sign with the old key, which
   a passing doctor would not tell apart: fix the cause (the file's owner or its directory's
   permissions) and run this step again.

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
   you rotated, send one request through the gateway to that pool, with the canary's key, while
   the pool's session is active and `rhi status` lists it `ready`: the canary's key is served as
   any other, on a pool that has a session and an instance in service (section 5.6). The
   request names the pool by its `model`; the key goes from Secrets Manager to curl on a pipe, and
   is printed nowhere. `<endpoint>` is the endpoint's address, as `rhi login` uses it:

   ```sh
   aws secretsmanager get-secret-value --secret-id rhi/canary-key --query SecretString --output text \
       | sed 's/^/Authorization: Bearer /' \
       | curl -sS --fail-with-body --max-time 600 -H @- -H 'content-type: application/json' \
           https://<endpoint>/v1/messages \
           -d '{"model": "coursework", "max_tokens": 16, "messages": [{"role": "user", "content": "Say OK."}]}'
   ```

   with the rotated pool's name in place of `coursework`. An answer with a message's content
   confirms the rotation for that pool: the gateway sent the new value and the restarted engine
   took it.

   When the request can reach the pool. A request starts no instance, and the gateway keeps none
   waiting: it answers at once, from what the pool's session has (section 5.3).

   - **The pool has no active session:** the answer is `no_session`, and nothing of the pool runs
     with either value, so there is nothing to test yet. Send the request at the pool's next
     session, once `rhi status` lists it `ready` (runbook
     [a session booked, changed and stopped](sessions.md)). Booking a session only for this test
     is one GPU start on a GPU pool, and may wait for capacity.
   - **The session is active and no instance is in service yet:** the answer is `warming` or
     `capacity_unavailable`. Wait until `rhi status` lists the pool `ready`, and send the request
     again.
   - **The session is active and an instance is in service:** the request is served.

   An error is diagnosed by its cause, not taken as a failed rotation by default. The cause is the
   answer's `error.type`: the gateway answers a Messages request with an error body
   `{"type": "error", "error": {"type": ..., "message": ...}}`, whose `error.type` is one of the
   causes below:

   - `engine_error`, the pool `ready`: the engine answered the gateway with an error, which a
     bearer mismatch gives, and so do other faults of an engine. Within minutes of the rotation,
     or before step 1 reads `PRIMARY COMPLETED`, an engine may still be restarting or an old
     gateway task still serving: wait for both and send it again. Should it persist, establish
     whether there is a mismatch before acting on one. A reader holds the new value only when it
     started after the new version was written, since each reads the secret at its start. When
     the current version was written:

     ```sh
     aws secretsmanager list-secret-version-ids --secret-id /rhi/pools/coursework/bearer \
         --query "Versions[?contains(VersionStages, 'AWSCURRENT')].CreatedDate" --output text
     ```

     When each running gateway task started:

     ```sh
     aws ecs describe-tasks --cluster <gateway-cluster> \
         --tasks $(aws ecs list-tasks --cluster <gateway-cluster> --service-name <gateway-service> --query taskArns --output text) \
         --query 'tasks[].[startedAt,lastStatus]' --output text
     ```

     When each running instance of the pool last started (`<group>` is the pool's group, the one
     tagged `rhi-pool` with the pool's name), and whether its engine was started again since. Every
     start of the engine's unit, `rhi-engine.service`, reads the secret, whatever started it: the
     lifecycle agent's restart, which writes the line "the engine did not answer a completion: its
     unit was restarted"; systemd's own restart, 5 seconds after the engine exits with a failure,
     which writes no line of the agent's; or any other start of the unit. So read, in the pool's log group `/rhi/pools/<pool>`, the instance's stream for the lines of the
     unit `rhi-engine.service` (the engine's own output, which starts again from its first lines,
     the model's loading, at each start), as well as the agent's line:

     ```sh
     aws ec2 describe-instances \
         --filters Name=tag:aws:autoscaling:groupName,Values=<group> Name=instance-state-name,Values=running \
         --query 'Reservations[].Instances[].[InstanceId,LaunchTime]' --output text
     ```

     Read the result this way:

     - a gateway task that started before the version was written still sends the old value:
       the mismatch is the gateway's, so start its deployment again (Act, step 3);
     - an instance that last started before the version, whose engine shows no start after it
       (neither the agent's restart line nor the engine's first lines again), still runs an engine
       with the old value: its lifecycle agent did not restart it. Its log may say
       why ("the pool's secret cannot be read: the engine is not checked this minute" means the
       agent cannot read the secret; "its unit could not be restarted" means the restart failed).
       Fix that cause; the instance's next start, or the agent's next successful check, gives the
       engine the new value;
     - every gateway task and every engine started after the version: both hold the new value, so
       the error is not a bearer mismatch. It is another fault of the engine; the lifecycle
       agent's minute check uses Chat Completions while this request uses Messages, so its silence
       does not clear the engine. Read the engine's own lines in `/rhi/pools/<pool>` around the
       time of the request. If the pool must stop serving meanwhile, stop its session (runbook
       [a session booked, changed and stopped](sessions.md)).
   - `invalid_key`: the canary's key, not the pool's secret: the canary has no valid key (its
     part, below).
   - `concurrency_limit`, `no_session`, `warming`, `capacity_unavailable`, `time_limit`: nothing
     to do with the bearer secret; the rotation is not tested yet. Send the request again once the
     cause has passed (for capacity, runbook
     [a session short of capacity](capacity-failure.md)).

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
   - Or the weekly canary, started now; it books one instance of the coursework pool, one GPU start:

     ```sh
     aws codebuild start-build --project-name rhi-gpu-canary --query build.id --output text
     ```

     In the CodeBuild console, the canary's log of that build ends with the line
     `canary_result: gpu-canary pass sent for` and the day's date, after pytest's summary of tests
     `passed` and none `failed`. A line `no test was run`, or a verdict `not_measured` or `fail`,
     is not a pass, whatever the build's status.

6. **The login App.** A student, or your own roster entry, logs in with `rhi login`.

Walked through on the deployed system: not yet.
