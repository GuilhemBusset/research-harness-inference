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
setup/owner-setup.sh --step apps --reporter-key <file>          # the Apps, read back from GitHub
```

- It shows each command before it runs it, and runs it only after a `y`. Any other answer skips
  the command.
- `--dry-run` reads no answer and calls neither AWS nor GitHub: `aws`, `gh` and `cdk` are never
  started. It prints `123456789012`, `us-east-1`, `<quota code>` and `<app id>` where a real run
  reads the value from your credentials or from GitHub. Start with it.
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
as the repository's admin, `uv`, and for step 6 the CDK CLI (`npm ci --ignore-scripts`,
[`setup/README.md`](../../setup/README.md)).

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

**Needs.** A browser: a script cannot set a GitHub App's permissions. To read the Apps back, `gh`
logged in as the account that owns them: a private App answers an authenticated caller only. To
store the reporter's credentials, your AWS credentials and their region, and two things GitHub
gives you once the reporter is registered and installed:

- its installation ID, the number that ends the address of the installation's page (GitHub,
  Settings, Applications, Installed GitHub Apps, Configure):
  `https://github.com/settings/installations/<installation id>`;
- its private key file, as GitHub downloaded it, kept outside every checkout and readable by you
  alone, for instance under `$HOME/.config/rhi/`.

**Command.**

```sh
setup/owner-setup.sh --step apps --reporter-key <private key file>
```

The step first prints, for each App, the permissions to set, and the page where an App is
registered: `https://github.com/settings/apps/new` (GitHub, Settings, Developer settings, GitHub
Apps, New GitHub App). For each: Webhook not active, installable only on this account.

| App | Permissions | Installed on | What it leaves you with |
| --- | --- | --- | --- |
| Student login | None. Device flow on | Nobody installs it | Its client ID, which goes into the configuration as `login_client_id` (step 4). It needs no secret |
| Status reporter | Commit statuses, read and write. Nothing else | This repository only | Its private key and its installation ID, which this step stores in the reporter's secret |
| The factory's identity | Contents, Pull requests and Issues, read and write; Commit statuses, Checks and Actions, read-only. No Workflows, no Administration | This repository only | Its private key, which stays on the factory's host: the factory has no AWS credentials to read a secret with. [`green-light.md`](../factory/green-light.md), step 7, creates this App |

**Reading each App back.** The step then asks, for each App in turn, for its slug: the last part
of the address of the App's settings page, `https://github.com/settings/apps/<slug>`. An empty
answer skips an App that is not registered yet, and the step can be run again. For a slug it
runs, after a `y`:

```sh
gh api apps/<slug> --jq '"\(.id) \(.permissions | to_entries | map("\(.key)=\(.value)") | sort | join(" "))"'
```

It prints the App's ID and its permissions, under the names GitHub's API gives them, and one line
that says whether they are those of section 8.6 of the design:

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

**The reporter's secret.** Once the reporter is read back, the step asks for its installation ID
and, when `--reporter-key` did not name it, for the private key file. It refuses a key file
inside the working tree, and a link in the tree to a file outside it. Then, after a `y`:

```sh
env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_secret rhi/reporter/github-app \
  --app-id <app id> --installation-id <installation id> --private-key-file <private key file>
```

This writes the Secrets Manager secret `rhi/reporter/github-app`, in the region of your
credentials: it creates the secret when it is absent, and replaces its value when it exists. The
value is one JSON object with the fields `app_id` (the ID read back from GitHub),
`installation_id` and `private_key` (the text of the file). The key is read from the file, which
stays where it is, and is never printed. The reporter's stack refers to this secret by its name
and does not create it: until the secret is stored, the reporter posts nothing. To replace the
key later, generate a new one on the App's settings page and run the step again.

A dry run prints these commands with `<slug>`, `<app id>` and `<installation id>` in place of the
values. It checks where the file `--reporter-key` names is, and does not open it.

**Check.**

- The step's line for each App says that its permissions are those of section 8.6.
- What GitHub's answer does not hold, on the App's own pages: Enable Device Flow is on for the
  login App; the reporter and the factory's App are each installed on "Only select repositories",
  with this repository alone.
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
| `session_windows`, `pools` | Empty lists for now: `[]` |

The loader requires every one of these keys. It refuses a session window and a pool entry until
the changes that give them their shape, so both lists stay empty. The windows and each pool, with
its GPU-hour ceilings, are added later, by running this step again.

**Command.**

```sh
setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
```

The step refuses a file inside the working tree, and a link in the tree to a file outside it.
It then runs two commands, the second after a `y`:

```sh
env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_config check <file>
env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_config write <file>
```

The first is the configuration loader (`src/rhi/config.py`), reading the file as the pipeline
will read it: a file it refuses stops the step, with a message that names the key and never its
value. The second is the configuration writer: one String parameter per key under `/rhi/config/`,
over those that exist. It deletes none. A dry run runs the first command too, since it reads the
file and calls nothing.

**To change the configuration later**, edit the file and run the same command again. Other
runbooks and the owner's steps of later changes use this step for that.

**Check.** The names of the parameters, not their values: one per key of the table, except
`pools`, which has a parameter per entry and so none yet.

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
env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_config values <file> --list <list>
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

The first deployment of the pipeline stack, once, from this machine, follows. The pipeline stack
is not in the repository yet: its command arrives with it. It needs this bootstrap and the
configuration of step 4.

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

**Needs.** The students' numeric GitHub IDs, and the admin CLI, which is not in the repository
yet.

**Command.** It arrives with the admin CLI. `setup/owner-setup.sh --step roster` says so. The
roster is never written to the repository.

**Check.** With the admin CLI, when it exists.

**Decision.** None of section 15.
