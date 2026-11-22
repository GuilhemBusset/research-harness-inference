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
       --force-new-deployment --query 'service.deployments[0].rolloutState' --output text
   ```

   ECS starts the new tasks, then drains each old one, for up to 30 minutes, the gateway's
   deregistration delay (section 8.5). After rotating the secrets of several pools, one deployment
   of the gateway, after the last of them, is enough.

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
       --factory-key "$HOME/.config/implement-loop/factory-app.pem"
   ```

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
   the function may clear it.

4. **Check that statuses are posted with the new key** (Verify, step 2), and only then delete the
   old key on the App's page, under **Private keys**, by the fingerprint you noted. Deleting it
   first would leave a warm environment unable to post a status until it is retired.

### The factory App's private key

The factory's key never goes to AWS. It is the file that `LOOP_GITHUB_APP_KEY_FILE` names on the
factory's host. The engine never reads it into Python and keeps no copy: `openssl` signs with the
file each time a token is minted, so the next token minted after the file changes uses the new
key, and nothing restarts.

1. **Generate a new key** on the factory App's page, as for the reporter (step 1 above), and note
   the old key's fingerprint.

2. **Replace the file,** keeping its name and its mode:

   ```sh
   install -m 600 "$HOME/Downloads/<new-key>.private-key.pem" "$HOME/.config/implement-loop/factory-app.pem"
   rm "$HOME/Downloads/<new-key>.private-key.pem"
   ```

3. **Check the new key** with the doctor: the line `github identity` mints a token with it.

   ```sh
   scripts/implement.sh doctor
   ```

4. **Delete the old key** on the App's page, by its fingerprint, once the doctor passes.

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

1. **A pool's bearer secret.** The gateway's new deployment has completed, and only it runs:

   ```sh
   aws ecs describe-services --cluster <gateway-cluster> --services <gateway-service> \
       --query 'services[0].deployments[].[status,rolloutState,runningCount]' --output text
   ```

   One line, `PRIMARY COMPLETED` and the number of tasks. Then the endpoint serves through the new
   value. For the mock pool, start a pipeline run with **Release change** on the pipeline's page in
   the CodePipeline console and read the end of its Verify action's CodeBuild log: pytest's summary
   reports tests `passed` and none `failed` or `error`. For a GPU pool, start the weekly canary
   now, which wakes the coursework pool (one GPU start) and runs its smoke tests through the
   gateway; its build ends `SUCCEEDED` in the CodeBuild console:

   ```sh
   aws codebuild start-build --project-name rhi-gpu-canary --query build.id --output text
   ```

2. **The status reporter's key.** A run of the pipeline posts statuses created after the update of
   step 3: start one with **Release change**, wait for it to end, and read the statuses of the
   commit it deployed, the head of `main`. The newest `deploy/<stage>` line carries a time after
   the update. Read it once more after deleting the old key, from the next run.

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/main/statuses \
       --jq '.[] | "\(.created_at) \(.context) \(.state)"'
   ```

3. **The factory's key.** `scripts/implement.sh doctor` shows `github identity` as `PASS`, with the
   App's name, after the old key was deleted.

4. **The canary's key.** The next verification runs its tests: after a **Release change**, its
   `verify/<stage>` status, and the end of its Verify log, say the tests ran and passed, not "No
   test ran: the canary's key is missing.". Or start the canary build of step 1, which uses the
   same key.

5. **The login App.** A student, or your own roster entry, logs in with `rhi login`.

Walked through on the deployed system: not yet.
