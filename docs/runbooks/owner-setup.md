# The owner's one-time steps

What only the owner can do, once, in this order (system design, section 8.6).
[`setup/owner-setup.sh`](../../setup/owner-setup.sh) walks through the nine steps. This page says,
for each one, what it needs, the command, how to check it, and the decision of section 15 of the
design that it rests on. Step 4 has a second half, 4b, which is not a step of section 8.6: it
writes the list of real values that the hygiene scanner checks for on your machine.

**The owner runs the script, from a shell no agent runs in.** No agent runs it, and nothing here
is claimed as done until the owner has seen the check.

Every value on this page is a placeholder: `123456789012` for the account, `us-east-1` for the
region, `example.com` for the domain. The real ones go on the command line, into your file outside
the working tree and into SSM. They are never written into a file of this repository, an issue, a
pull request or a commit message, and neither is anything AWS prints.

## The script

```sh
setup/owner-setup.sh --dry-run                                  # print every step and every command
setup/owner-setup.sh                                            # the walkthrough
setup/owner-setup.sh --step configuration --config <file>       # one step on its own
setup/owner-setup.sh --step values --config <file>              # the scanner's list of real values
setup/owner-setup.sh --step apps --reporter-key <file> --factory-key <file>   # the Apps, read back
```

- It shows each command before it runs it, and runs it only after a `y`. Any other answer skips
  the command.
- `--dry-run` reads no answer and calls neither AWS nor GitHub: `aws`, `gh` and `cdk` are never
  started. It prints `123456789012`, `us-east-1`, `<quota code>`, `<app id>` and
  `<installation id>` where a real run reads the value from your credentials, from you or from
  GitHub. Start with it.
- `--step <name>` runs one step: `quotas`, `domain`, `apps`, `configuration`, `values`, `billing`,
  `bootstrap`, `connection`, `repository` or `roster`. A step can be repeated.
- It writes nothing to the working tree. What it learns (the account, a quota's code, an address,
  an App's ID) goes to the terminal, the configuration goes to SSM, the status reporter's
  credentials go to Secrets Manager, and the configuration's private values go to the scanner's
  list, a file outside the repository (step 4b). The tools it starts write nothing to the working
  tree either: Python keeps no bytecode, and uv's project environment and the CDK's output go to
  a temporary folder that is removed when the script ends. So the script needs no `uv sync` first
  and leaves no `.venv` and no `cdk.out` behind.

It needs, on the machine: the AWS CLI with your credentials and their region set, `gh` logged in
as the repository's admin (for step 8), `uv`, `openssl` (for step 3), and for step 6 the CDK CLI (`npm ci --ignore-scripts`,
[`setup/README.md`](../../setup/README.md)).

**The region and the sign-in.** Two kinds of command reach AWS, and they read the region from
different places:

- the AWS CLI (the `aws` commands, and the CDK CLI of step 6) reads `AWS_REGION`, then
  `AWS_DEFAULT_REGION`, then the `region` of your profile;
- the AWS SDK of the Python steps (the reporter's secret in step 3, the configuration in step 4)
  reads `AWS_DEFAULT_REGION`, then the `region` of your profile, and never `AWS_REGION`.

A region in your profile, or `AWS_DEFAULT_REGION`, reaches both. When you set only `AWS_REGION`,
the script copies it into `AWS_DEFAULT_REGION` before any step runs a command, so every step
works in the same region. When both are set, the script changes neither: keep them equal.

Any profile the AWS CLI accepts works in every step, a profile signed in with `aws login` (your
console credentials) included: the project installs the SDK with its crt extra, which that
sign-in needs. When the SDK cannot make a client, the step says so by the name of the SDK's error
(for example `NoCredentialsError`), never by its text; a missing region it reports as such.

## 1. Request the GPU quotas

**Needs.** The coursework pool's `max_instances`, whether the upgrade pool is wanted, and whether
the optional xl pool is. The quota is per region, so the credentials name the region the endpoint
lives in.

**Command.**

```sh
setup/owner-setup.sh --step quotas --max-instances 2          # add --upgrade-pool, --xl-pool
```

The G-family request is 8 vCPUs for each coursework instance, for one instance more than
`max_instances`: a draining instance and a cold launch can overlap (system design, section 5.4).
The upgrade pool adds 96 vCPUs. So `max_instances = 2` asks for 24 vCPUs, and 120 with the
upgrade pool. A P-family quota, 192 vCPUs for one xl instance, is requested only with `--xl-pool`.
The step looks up each quota's code by its name, then asks before it sends the request:

```sh
aws service-quotas list-service-quotas --service-code ec2 \
  --query "Quotas[?QuotaName=='Running On-Demand G and VT instances'].[QuotaCode,Value]" --output text
aws service-quotas request-service-quota-increase --service-code ec2 \
  --quota-code <quota code> --desired-value 24
```

Phases 0 and 1 need no GPU quota, but nobody documents how long a request takes (section 14):
send it the day the design is approved. The quota is also a ceiling: no bug can run more than it
allows.

**Check.** The request and its status, then the quota once it is granted:

```sh
aws service-quotas list-requested-service-quota-change-history-by-quota --service-code ec2 \
  --quota-code <quota code> --query 'RequestedQuotas[].[Status,DesiredValue]' --output text
aws service-quotas get-service-quota --service-code ec2 --quota-code <quota code> --query Quota.Value
```

**Decision.** 5, class size: it sets `max_instances`, and with it the quota. The defaults of
section 4.2, 2 instances, fit a class of twenty working hands-on. 3, for the upgrade pool. 9, for
the region. The xl pool is built only if the owner asks for it (section 4.2).

## 2. Register the domain

**Needs.** The choice of a domain or a subdomain.

**Command.** None: register the domain, or delegate the subdomain, so that a public hosted zone of
Route 53 in this account answers for it. `setup/owner-setup.sh --step domain` prints this. The
domain and the ID of its hosted zone then go into the configuration file of step 4, as `domain`
and `hosted_zone_id`.

**Check.** The zone exists and is public, and the domain's name servers are the zone's:

```sh
aws route53 list-hosted-zones-by-name --dns-name example.com --max-items 1 \
  --query 'HostedZones[].[Name,Id,Config.PrivateZone]' --output text
dig +short NS example.com
```

**Decision.** 10, the domain.

## 3. Create the three GitHub Apps

**Needs.** A browser: a script cannot set a GitHub App's permissions. Then, to read the Apps back
and store the reporter's credentials:

- the status reporter's and the factory's App's IDs, each on its App's settings page (About);
- the login App's slug, the last part of the address of its settings page,
  `https://github.com/settings/apps/<slug>`;
- the reporter's and the factory's App's private key files, as GitHub downloaded them, kept
  outside every checkout and readable by you alone: the reporter's for instance under
  `$HOME/.config/rhi/`, the factory's where [`green-light.md`](../factory/green-light.md), step 7,
  put it;
- `openssl`, which signs with a key so that the step can read an App back as the App itself;
- your AWS credentials and their region, to store the reporter's secret.

Your `gh` login is not among them. Each App is installable only on your account, which makes it
private, and GitHub answers a request for a private App only to the App's own tokens: your login
gets 404. No command of this step uses it.

**Command.**

```sh
setup/owner-setup.sh --step apps --reporter-key <reporter's key file> --factory-key <factory's key file>
```

The step first prints, for each App, the permissions to set, and the page where an App is
registered: `https://github.com/settings/apps/new` (GitHub, Settings, Developer settings, GitHub
Apps, New GitHub App). For each: Webhook not active, installable only on this account.

| App | Permissions | Installed on | What it leaves you with |
| --- | --- | --- | --- |
| Student login | None. Device flow on | Nobody installs it | Its client ID, which goes into the configuration as `login_client_id` (step 4). It needs no secret |
| Status reporter | Commit statuses, read and write. Nothing else | This repository only | Its private key, which this step stores in the reporter's secret with the IDs GitHub reports |
| The factory's identity | Contents, Pull requests and Issues, read and write; Commit statuses, Checks and Actions, read-only. No Workflows, no Administration | This repository only | Its private key, which stays on the factory's host: the factory has no AWS credentials to read a secret with. [`green-light.md`](../factory/green-light.md), step 7, creates this App |

**Reading each App back.** The step reads the Apps in this order, each after a `y`, through
`src/rhi/owner_apps.py`. An empty answer to an App's ID or slug skips an App that is not
registered yet, and the step can be run again.

1. The status reporter. The step asks for its App ID, and for its key file when `--reporter-key`
   did not name it. It takes this repository's name, `<owner>/<name>`, from the clone's `origin`
   remote, and asks for it when there is none. Then:

   ```sh
   env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_apps reporter \
     --app-id <app id> --key-file <reporter's key file> --repository <owner>/<name>
   ```

   The module signs a JSON Web Token with the App's own key (`openssl` reads the key; its issuer
   is the App's ID) and reads the App with it (`GET /app`), then its installations
   (`GET /app/installations`). For each installation it mints an installation token and lists the
   repositories the installation reaches (`GET /installation/repositories`).
2. The student login App. It has no key, so it is read by its slug with an installation token of
   the status reporter (`GET /apps/<slug>`), and only once the reporter was read back. The module
   then asks GitHub for a device code with the App's client ID
   (`POST https://github.com/login/device/code`): GitHub gives one only when the App's device flow
   is on.

   ```sh
   env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_apps login \
     --slug <slug> --app-id <app id> --key-file <reporter's key file>
   ```

3. The factory's identity, as the reporter, with `factory`, its App ID and the file
   `--factory-key` names (or that the step asks for).

For each App it prints the slug, the ID and the permissions, under the names GitHub's API gives
them, and one line that says whether they are those of section 8.6 of the design. For the reporter
and the factory's App it prints each installation: its ID, its repository selection (`selected`,
or `all`) and the repositories it reaches. For the login App it says that its device flow is on.

| App | The permissions GitHub answers with |
| --- | --- |
| Student login | none |
| Status reporter | `statuses=write` |
| The factory's identity | `actions=read checks=read contents=write issues=write pull_requests=write statuses=read` |

`write` is read and write. GitHub grants `metadata=read` by itself to an App that has a repository
permission, so the step does not count it for the reporter and the factory's App. For the login
App it does: that App has no permission at all. When the line says the permissions are not those
of section 8.6, correct them on the App's settings page (Permissions & events) and run the step
again. An App's ID and its key do not change with its permissions.

**What it refuses.**

- A reporter or a factory's App with an installation on anything other than this repository
  alone: an installation on all repositories, one that reaches another repository too, or a second
  installation, on another account. Change the installation to "Only select repositories", with
  this repository alone (GitHub, Settings, Applications, Installed GitHub Apps, Configure), and run
  the step again.
- A login App whose device flow is off: turn on Enable Device Flow on its settings page.
- A key file inside the working tree, and a link in the tree to a file outside it.
- A key that does not sign for the App's ID: GitHub refuses the token, and the step names the HTTP
  status.

A refused App stops nothing but itself: the step goes on to the next App. Nothing the step prints
holds a key, a token, a device code or the login App's client ID, which is a private value of the
configuration; and it repeats no text GitHub answered.

**The reporter's secret.** Once the reporter is read back and not refused, the step stores its
credentials, after a `y`. The installation ID is the one GitHub reported for this repository: the
step does not ask for it. When the reporter has no installation yet, nothing is stored.

```sh
env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_secret rhi/reporter/github-app \
  --app-id <app id> --installation-id <installation id> --private-key-file <reporter's key file>
```

This writes the Secrets Manager secret `rhi/reporter/github-app`, in the region of your
credentials: it creates the secret when it is absent, and replaces its value when it exists. The
value is one JSON object with the fields `app_id`, `installation_id` (both as GitHub reported
them) and `private_key` (the text of the file). The key is read from the file, which stays where
it is, and is never printed. The reporter's stack refers to this secret by its name and does not
create it: until the secret is stored, the reporter posts nothing. To replace the key later,
generate a new one on the App's settings page and run the step again.

A dry run prints these commands with `<app id>`, `<slug>`, `<installation id>`, `<owner>/<name>`
(when the clone has no `origin` on GitHub) and `<private key file>` (when no option named one) in
place of the values. It checks where the files `--reporter-key` and `--factory-key` name are, and
opens neither.

**Check.**

- The step's line for each App says that its permissions are those of section 8.6, the reporter
  and the factory's App each show one installation, `selected`, on this repository alone, and the
  login App's device flow is on.
- The secret exists, which the step offers to check, and holds its three fields. The second
  command prints the names of the fields and no value:

  ```sh
  aws secretsmanager describe-secret --secret-id rhi/reporter/github-app --query Name --output text
  aws secretsmanager get-secret-value --secret-id rhi/reporter/github-app --query SecretString --output text \
    | jq -r 'keys | join(" ")'      # app_id installation_id private_key
  ```

- For the factory's App, `scripts/implement.sh doctor` shows `github identity` as `PASS`.

**Decision.** None of section 15. The Apps are those of sections 6.4 and 8.4 of the design and of
ADR 0001, amendment 2. That the reporter's credential can write commit statuses on this
repository and nothing else is part of the trust boundary (section 8.4; ADR 0001, amendment 4).

## 4. Write the configuration to SSM

**Needs.** A configuration file that you keep outside the working tree, for instance
`$HOME/.config/rhi/config.toml`, readable by you alone. Start from a copy of
[`config.example.toml`](../../config.example.toml) and replace every placeholder:

| Key | What it holds |
| --- | --- |
| `domain`, `hosted_zone_id` | The domain of step 2 and the ID of its hosted zone |
| `admin_email` | The address of the one admin user, which the alerts go to |
| `instructor_phone` | The phone the class-day notice also goes to |
| `github_owner`, `github_repo` | The repository the pipeline deploys from |
| `login_client_id` | The client ID of the student login App of step 3 |
| `monthly_budget_usd` | The monthly budget, in whole US dollars |
| `time_zone`, `course_end` | The IANA time zone of the schedule, and the day the course ends |
| `session_windows` | An empty list for now: `[]` |
| `pools` | An empty table for now: `pools = {}` |

The loader requires every one of these keys. It refuses a session window until the change that
gives windows their shape, so that list stays empty. A pool is a `[pools.<name>]` table, which
the loader checks with the pool parser; the mock pool of `config.example.toml` is the first one
added. Three keys go with the pools and may be left out: `allowed_instance_types` (by default the
instance types the pools list), `on_demand_price_limit` (5 dollars an hour by default) and
`default_pool` (by default the only pool; with two pools or more it must be given). The windows
and each pool, with its GPU-hour ceilings, are added later, by running this step again.

**Command.**

```sh
setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
```

The step refuses a file inside the working tree, and a link in the tree to a file outside it.
It then runs two commands, the second after a `y`:

```sh
env PYTHONPATH=src:services/common uv run --locked --no-dev python -m rhi.owner_config check <file>
env PYTHONPATH=src:services/common uv run --locked --no-dev python -m rhi.owner_config write <file>
```

The first is the configuration loader (`src/rhi/config.py`), reading the file as the pipeline
will read it: a file it refuses stops the step, with a message that names the key and never its
value. The second is the configuration writer: one String parameter per key under `/rhi/config/`,
over those that exist. It deletes none. A dry run runs the first command too, since it reads the
file and calls nothing.

**To change the configuration later**, edit the file and run the same command again. Other
runbooks and the owner's steps of later changes use this step for that.

**Check.** The names of the parameters, not their values: one per key of the table, except
`pools`, which has a parameter per entry, `/rhi/config/pools/<name>`, holding the entry as a JSON
object: none while the table is empty.

```sh
aws ssm get-parameters-by-path --path /rhi/config/ --recursive --query 'Parameters[].Name' --output text
```

**Decision.** 10, for the domain. 1, dates, for the course end date and, later, the session
windows. 6, money, for the monthly budget and, later, each pool's GPU-hour ceilings. 9, for the
region the parameters are written in. The first deployment of step 6 reads this configuration.

## 4b. Write the list of real values the scanner checks for

No public pattern can name your domain or the admin's address: writing the pattern would publish
the value (system design, section 10). The hygiene scanner therefore also reads a list of real
values, kept outside the repository, and reports each one as `local-value` in every scan on your
machine: the git hooks, `scripts/check.sh`, and the implement loop before each push
([`tools/hygiene/README.md`](../../tools/hygiene/README.md), "Values only the owner knows"). This
step writes that list, so that the check does not depend on your remembering to create it. The
second check of section 10, in the pipeline's synth step, looks for the same values.

**Needs.** The configuration file of step 4. No AWS credentials: the step calls nothing.

**Command.**

```sh
setup/owner-setup.sh --step values --config "$HOME/.config/rhi/config.toml"
```

The whole walkthrough runs it right after step 4, with the same file. The list is the file the
environment variable `HYGIENE_LOCAL_VALUES` names or, when it is unset, the scanner's default
file: `research-harness-inference/hygiene-values` under `$XDG_CONFIG_HOME`, which is
`$HOME/.config` by default. The step prints that path, then asks for further values, one per
line, ended by an empty line: anything else no file of the repository may hold, a student's name
for instance. Then, after a `y`:

```sh
env PYTHONPATH=src:services/common uv run --locked --no-dev python -m rhi.owner_config values <file> --list <list>
```

- It adds the value of each private key of the configuration (`domain`, `hosted_zone_id`,
  `admin_email`, `instructor_phone` and `login_client_id`: `PRIVATE_KEYS` in `src/rhi/config.py`)
  and each value you entered. The further values reach the command on its standard input, not on
  its command line.
- A value is added once, however often the step runs: one the list holds already, in any letter
  case, is not added again. The lines and the comments the list holds are kept. After a change to
  one of the private keys, run the step again: the new value is added, and the old one stays.
- The list is created readable and writable by you alone (mode `600`, its folder `700`), and a
  list that exists is set to that mode.
- It prints how many values the list holds, and never a value.
- It refuses a list whose path is inside the working tree, and a link in the tree to a file
  outside it: nothing is written, in a dry run either.
- It refuses a value the scanner could not read as one line of the list: shorter than four
  characters, or starting with `#`. The refusal names the key, or the number of the entered value.

A dry run prints the path of the list and the command, reads no answer and writes no file.

**Check.** Write one of the values to a scratch file outside the working tree, the real domain
for instance, and scan that file. The scan exits 1 and reports the file under the rule
`local-value`, with the value masked:

```sh
scratch="$(mktemp)"
cat > "$scratch"          # type the domain, then Enter and Ctrl-D: it stays out of the shell's history
python3 tools/hygiene/scan.py --paths "$scratch"      # <scratch file>:1: local-value: ...
rm -- "$scratch"
ls -l "${HYGIENE_LOCAL_VALUES:-${XDG_CONFIG_HOME:-$HOME/.config}/research-harness-inference/hygiene-values}"
```

The last command shows the list as `-rw-------`.

**Decision.** None of section 15. It is the owner-local check of section 10 of the design.

## 5. Turn on billing alerts

**Needs.** The account's root user, or a user allowed to change the billing preferences.

**Command.** None: it is a setting of the Billing console. Under Billing preferences, Alert
preferences, turn on the CloudWatch billing alerts. `setup/owner-setup.sh --step billing` prints
this. Each SNS subscription is confirmed later, from the e-mail it sends to the admin address,
when the stack that creates its topic is deployed.

**Check.** The preference shows as on. Some hours later the account publishes its billing metric,
which AWS keeps in one region whatever the endpoint's own:

```sh
aws cloudwatch list-metrics --namespace AWS/Billing --region us-east-1 \
  --query 'Metrics[].MetricName' --output text      # EstimatedCharges
```

**Decision.** None of its own. The amounts are those of decision 6, in the configuration of step 4.

## 6. Bootstrap the account, then deploy the pipeline stack once

**Needs.** The CDK CLI (`npm ci --ignore-scripts`), and credentials for the region the endpoint
lives in. [`setup/bootstrap/README.md`](../../setup/bootstrap/README.md) describes the two
policies and what they deny.

**Command.**

```sh
setup/owner-setup.sh --step bootstrap
```

It deploys the two policies, reads the account and the region from your credentials, and
bootstraps with the execution policy and the permissions boundary:

```sh
aws cloudformation deploy --stack-name rhi-bootstrap-policies \
  --template-file setup/bootstrap/policies.yaml --capabilities CAPABILITY_NAMED_IAM
aws sts get-caller-identity --query Account --output text
RHI_CONFIG=example node_modules/.bin/cdk bootstrap aws://123456789012/us-east-1 \
  --cloudformation-execution-policies arn:aws:iam::123456789012:policy/rhi-cfn-execution \
  --custom-permissions-boundary rhi-permissions-boundary --output <a temporary folder>
```

After bootstrap, deploy the pipeline stack from the same terminal, in the region where you wrote
the configuration in step 4:

```sh
RHI_CONFIG=ssm node_modules/.bin/cdk deploy RhiPipeline --lookups=false \
  -c pipeline-first-deploy=true --output "$(mktemp -d)"
```

Use `pipeline-first-deploy` for the first deployment only. CloudFormation creates the pipeline
and its pending GitHub connection. Once you authorize the connection in step 7, the first pipeline
execution adds the spike jobs during self-update. By then, CodeBuild can validate their GitHub
source credentials.

**Check.** Both read, and change nothing. The first lists `rhi-cfn-execution` and nothing else,
the second names `rhi-permissions-boundary`. The script offers both.

```sh
aws iam list-attached-role-policies --role-name cdk-hnb659fds-cfn-exec-role-123456789012-us-east-1
aws iam get-role --role-name cdk-hnb659fds-cfn-exec-role-123456789012-us-east-1 --query Role.PermissionsBoundary
```

**Decision.** 9, the region: it is the one of the credentials you bootstrap with.

## 7. Authorize the GitHub connection

**Needs.** The first deployment of step 6: the pipeline stack creates the connection, and a
connection that CloudFormation creates starts pending.

**Command.** None: authorize it once in the console (Developer Tools, Settings, Connections), and
install the AWS connector on this repository only. `setup/owner-setup.sh --step connection` prints
this.

**Check.**

```sh
aws codeconnections list-connections --provider-type-filter GitHub \
  --query 'Connections[].[ConnectionName,ConnectionStatus]' --output text      # AVAILABLE
```

**Decision.** None of section 15. That the connector reaches this repository alone is part of the
trust boundary (system design, section 8.4).

## 8. Apply the ruleset and the repository's settings

**Needs.** `gh` logged in as the repository's admin, and `jq`. These are the steps 2 and 4 of
[`green-light.md`](../factory/green-light.md): if they are done, there is nothing to run.

**Command.**

```sh
setup/owner-setup.sh --step repository
```

It offers the ruleset, secret scanning with push protection, and the no-reply address to commit
with, which you then set yourself with `git config user.email`:

```sh
bash setup/apply-ruleset.sh
gh repo edit --enable-secret-scanning --enable-secret-scanning-push-protection
gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'
```

**Check.** As in `green-light.md`, step 4: the ruleset is `active` and refuses a probe push to
`main`, and both scanners report `enabled`. `git log --format='%an <%ae>' -1` shows the no-reply
address.

**Decision.** None of section 15.

## 9. Enter the roster

**Needs.** The students' numeric GitHub IDs, an alias for each (their metrics carry the alias,
never the ID), your AWS credentials and their region, and the access stack deployed by the
pipeline: the admin CLI finds the tables in it by their logical IDs, and reads the course end from
the configuration of step 4.

**Command.** The admin CLI, `admin/rhi_admin/main.py`. `setup/owner-setup.sh --step roster` prints
the commands and offers the last two. The roster is never written to the repository.

```sh
admin=(env PYTHONPATH=admin:services/common:services/issuer uv run --locked --no-dev python -m rhi_admin.main)
"${admin[@]}" roster add <github-id> <alias>          # 100 million tokens a day and 4 requests; or --budget N --limit N
"${admin[@]}" canary                                   # once, before the pipeline's verification is merged
"${admin[@]}" roster list
```

`roster set <github-id> --budget N --limit N` changes a student's numbers on the entry and on each
of their active keys; the gateway applies them within a minute. `roster remove <github-id>`
removes a student and revokes their keys; their alias is held back from any other entry for 91
days, while what was written under it is kept, so a student put back on the roster takes another
alias. `key issue <github-id>` issues a key without a login,
when GitHub's 50 confirmations an hour run out, and prints it once; `key revoke <github-id>`
revokes every key of a student.

The canary command puts the pipeline's verification on the roster, under the alias `canary`, and
writes its key into the Secrets Manager secret `rhi/canary-key`, creating the secret if it does not
exist. It never prints the key. Running it again replaces the key.

**Check.** `roster list` shows each student, and `canary`, with their budget and limit. The
secret exists:

```sh
aws secretsmanager describe-secret --secret-id rhi/canary-key --query Name --output text
```

**Decision.** None of section 15.
