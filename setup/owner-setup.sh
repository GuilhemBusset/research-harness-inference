#!/usr/bin/env bash
# The owner's one-time steps (system design, section 8.6), walked through in their order:
#
#   setup/owner-setup.sh --dry-run        print every step and every command, and run none
#   setup/owner-setup.sh                  the walkthrough: asks before each command it runs
#   setup/owner-setup.sh --step configuration --config <file>     one step on its own
#
# The owner runs it, from a shell no agent runs in, with their own AWS credentials and `gh` login.
# docs/runbooks/owner-setup.md describes each step: what it needs, how to check it and the
# decision of the design it rests on.
#
# What the script learns (the account, a quota's code, an address) goes to the terminal or, for
# the configuration, to SSM. Nothing is written to the working tree, by the script or by the tools
# it starts: Python keeps no bytecode, and uv's project environment and the CDK's output go to a
# temporary folder that is removed when the script ends.
#
# With --dry-run no answer is read and neither AWS nor GitHub is called: `aws`, `gh` and `cdk` are
# never started. The one command a dry run does run is the configuration loader, on the file
# --config names: it reads that file and calls nothing.
set -euo pipefail

started_in="$PWD"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

steps=(quotas domain apps configuration billing bootstrap connection repository roster)

usage() {
  cat <<EOF
usage: setup/owner-setup.sh [--dry-run] [--step <name>] [options]

  --dry-run            print every step and every command; call neither AWS nor GitHub
  --step <name>        run one step on its own: ${steps[*]}
  --max-instances <n>  quotas: the coursework pool's max_instances (asked for when not given)
  --upgrade-pool       quotas: the upgrade pool is wanted (96 more G-family vCPUs)
  --xl-pool            quotas: the xl pool is wanted (a P-family quota)
  --config <file>      configuration: the owner's file, kept outside the working tree
EOF
}

die() {
  printf 'owner-setup: %s\n' "$*" >&2
  exit 1
}

dry_run=0
only=""
max_instances=""
upgrade_pool=""
xl_pool=""
config_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --upgrade-pool) upgrade_pool=1 ;;
    --xl-pool) xl_pool=1 ;;
    --step | --max-instances | --config)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      case "$1" in
        --step) only="$2" ;;
        --max-instances) max_instances="$2" ;;
        --config) config_file="$2" ;;
      esac
      shift
      ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
if [ -n "$only" ] && [[ " ${steps[*]} " != *" $only "* ]]; then
  usage >&2
  exit 2
fi

# --- What the tools write as they run stays out of the working tree ---------------------------

export PYTHONDONTWRITEBYTECODE=1
scratch=""
trap '[ -z "$scratch" ] || rm -rf -- "$scratch"' EXIT

outside_the_tree() {   # make the temporary folder, and point uv's project environment into it
  [ -z "$scratch" ] || return 0
  scratch="$(mktemp -d "${TMPDIR:-/tmp}/rhi-owner-setup.XXXXXX")"
  case "$(cd "$scratch" && pwd -P)/" in
    "$repo_root"/*) die "the temporary folder would be inside the working tree: set TMPDIR to a folder outside it" ;;
  esac
  export UV_PROJECT_ENVIRONMENT="$scratch/venv"
}

# --- Showing, asking, running -----------------------------------------------------------------

say() {
  printf '%s\n' "$*"
}

heading() {   # heading <number> <title>
  printf '\n== Step %s of %s: %s ==\n\n' "$1" "${#steps[@]}" "$2"
}

show() {   # print a command as a line a shell would read the same way
  local word line=""
  for word in "$@"; do
    if [[ $word =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then
      line+=" $word"
    elif [[ $word == *\'* && $word != *[\"\$\`\\!]* ]]; then
      line+=" \"$word\""
    else
      line+=" '${word//\'/\'\\\'\'}'"
    fi
  done
  printf '    $%s\n' "$line"
}

confirm() {   # confirm <question>: yes on y or yes; anything else, and the end of the input, is no
  local answer=""
  printf '%s [y/N] ' "$1"
  read -r answer || true
  [[ $answer == [yY] || $answer == [yY][eE][sS] ]]
}

run() {   # run <command...>: show it; in a dry run that is all; otherwise ask, then run it
  show "$@"
  if [ "$dry_run" = 1 ]; then
    say "      (dry run: not run)"
    return 0
  fi
  if ! confirm "    Run it?"; then
    say "      (not run)"
    return 1
  fi
  "$@" || die "that command failed. Nothing after it was run."
}

capture() {   # capture <variable> <command...>: as `run`, and keeps what the command printed.
  # A dry run leaves the variable as it is: the caller put a placeholder there.
  local -n into="$1"
  shift
  show "$@"
  if [ "$dry_run" = 1 ]; then
    say "      (dry run: not run)"
    return 0
  fi
  if ! confirm "    Run it?"; then
    say "      (not run)"
    return 1
  fi
  into="$("$@")" || die "that command failed. Nothing after it was run."
  say "      $into"
}

pause() {   # between the steps of the whole walkthrough, so that no step scrolls past unread
  if [ "$dry_run" = 0 ] && [ -z "$only" ]; then
    printf '\nPress Enter when this step is done, to go on. '
    read -r _ || true
  fi
}

# --- The steps --------------------------------------------------------------------------------

request_quota() {   # request_quota <quota name> <vCPUs>
  local found="" code="<quota code>" now=""
  # Service Quotas names a quota by a code. The name is the one AWS documents for the quota.
  if ! capture found aws service-quotas list-service-quotas --service-code ec2 \
    --query "Quotas[?QuotaName=='$1'].[QuotaCode,Value]" --output text; then
    say "    Without the quota's code no request can be made."
    return 0
  fi
  if [ "$dry_run" = 0 ]; then
    read -r code now <<<"$found" || true
    [ -n "$code" ] || die "this region has no quota of that name."
    say "    The quota is $now vCPUs now. A request for a value that is not above it is refused."
  fi
  run aws service-quotas request-service-quota-increase --service-code ec2 \
    --quota-code "$code" --desired-value "$2" || true
}

step_quotas() {
  heading 1 "Request the GPU quotas"
  cat <<'EOF'
A new account may run no GPU instance: its vCPU quotas for the G and the P family are zero, and
nobody documents how long a request takes, so this is the first step (system design, sections 5.4
and 14). A quota belongs to one region: the requests below go to the region of your credentials.
EOF
  local instances="$max_instances" answer=""
  if [ -z "$instances" ]; then
    instances=2
    if [ "$dry_run" = 1 ]; then
      say "No --max-instances was given: this dry run takes the design's default, 2."
    else
      printf "The coursework pool's max_instances (decision 5: 2 fits a class of twenty working hands-on) [2]: "
      read -r answer || true
      instances="${answer:-2}"
    fi
  fi
  [[ $instances =~ ^[1-9][0-9]{0,3}$ ]] || die "max_instances must be a whole number above zero."
  if [ "$dry_run" = 0 ]; then
    [ -n "$upgrade_pool" ] || ! confirm "Is the upgrade pool wanted (decision 3)?" || upgrade_pool=1
    [ -n "$xl_pool" ] || ! confirm "Is the optional xl pool wanted?" || xl_pool=1
  fi

  # One coursework instance (g7e.2xlarge) has 8 vCPUs, and the quota leaves room for one instance
  # more than max_instances: a draining instance and a cold launch can overlap. One upgrade
  # instance (g7e.24xlarge) has 96.
  local g_family=$((8 * (instances + 1)))
  say ""
  say "The G-family request: 8 vCPUs x ($instances instances + 1) = $g_family vCPUs"
  if [ -n "$upgrade_pool" ]; then
    g_family=$((g_family + 96))
    say "  and 96 vCPUs for the upgrade pool: $g_family vCPUs"
  else
    say "  The upgrade pool is not wanted: nothing is added for it (--upgrade-pool adds 96 vCPUs)."
  fi
  request_quota "Running On-Demand G and VT instances" "$g_family"

  say ""
  if [ -n "$xl_pool" ]; then
    # One xl instance (p5en.48xlarge) has 192 vCPUs. The pool runs scheduled sessions only.
    say "The P-family request, for one instance of the xl pool: 192 vCPUs"
    request_quota "Running On-Demand P instances" 192
  else
    say "No P-family quota is requested: only the xl pool needs one (--xl-pool)."
  fi
  pause
}

step_domain() {
  heading 2 "Register the domain"
  cat <<'EOF'
Choose the domain or the subdomain the endpoint is served under (decision 10) and register it, or
delegate it, so that a public hosted zone of Route 53 in this account answers for it. No command
does this for you.

Two values then go into the configuration file of step 4: the domain (`domain`) and the ID of
its hosted zone (`hosted_zone_id`). Neither is ever written to the repository.
EOF
  pause
}

step_apps() {
  heading 3 "Create the three GitHub Apps"
  cat <<'EOF'
A script cannot set a GitHub App's permissions: you set them in the browser. An App is registered
on this page (GitHub, Settings, Developer settings, GitHub Apps, New GitHub App):

    https://github.com/settings/apps/new

For each App: any name and homepage URL, Webhook not active, installable only on this account.

1. The student login App
   Permissions: none at all, for the repository, the organization or the account.
   Enable Device Flow: on. Nobody installs this App.
   Its client ID goes into the configuration: it is the key `login_client_id` of the file of
   step 4. It is never written to the repository.

2. The status reporter
   Permissions: Commit statuses, read and write. Nothing else.
   Install it on "Only select repositories", with this repository alone.
   Keep its private key outside every checkout. A later step of this walkthrough, which arrives
   with the reporter, reads the App back and stores its credentials in the reporter's secret.

3. The factory's identity (ADR 0001, amendment 2; docs/factory/green-light.md, step 7)
   Permissions: Contents, Pull requests and Issues, read and write; Commit statuses, Checks and
   Actions, read-only. Workflows and Administration: no access.
   Install it on "Only select repositories", with this repository alone.
   Its private key stays on the factory's host, outside every checkout, readable by you alone.
   It never goes to AWS and never into the repository.
EOF
  pause
}

step_configuration() {
  heading 4 "Write the configuration to SSM"
  cat <<'EOF'
The configuration is one file that you keep outside the working tree: a copy of
config.example.toml with your values in place of the placeholders (the domain and its hosted
zone, the admin e-mail address, the monthly budget of decision 6, the time zone, the course end
date of decision 1, ...). For now `session_windows` and `pools` stay empty lists: the loader
refuses an entry until entries have a shape. The GPU-hour ceilings come later, with each pool.

The configuration loader checks the file before anything is written, and the configuration
writer then writes one parameter per key under /rhi/config/, in the region of your credentials.
EOF
  local file="$config_file" checked=1
  local tool=(env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_config)
  if [ -z "$file" ] && [ "$dry_run" = 0 ]; then
    printf '\nThe configuration file, outside the working tree: '
    read -r file || true
    [ -n "$file" ] || die "no configuration file was named."
  fi
  say ""
  if [ -z "$file" ]; then
    say "No --config was given: this dry run checks no file."
    file="<your configuration file>"
    show "${tool[@]}" check "$file"
    say "      (dry run: not run)"
  else
    # Both the path as it was given and the file it leads to: a link in the tree is in the tree.
    local path
    [[ $file == /* ]] || file="$started_in/$file"
    for path in "$(realpath -m -s -- "$file")" "$(realpath -m -- "$file")"; do
      case "$path/" in
        "$repo_root"/*) die "refused: the configuration file is inside the working tree. Keep it outside." ;;
      esac
    done
    [ -f "$file" ] || die "refused: the configuration file is not a file that can be read."
    file="$(realpath -- "$file")"
    outside_the_tree
    if [ "$dry_run" = 1 ]; then
      show "${tool[@]}" check "$file"
      say "      (run in a dry run too: it reads the file and calls nothing)"
      "${tool[@]}" check "$file" || die "the configuration file was refused. Nothing would be written."
    else
      run "${tool[@]}" check "$file" || checked=""
    fi
  fi
  if [ -z "$checked" ]; then
    say "    The file was not checked, so nothing is written."
  elif run "${tool[@]}" write "$file"; then
    say "    To check: the names of the parameters, not their values."
    run aws ssm get-parameters-by-path --path /rhi/config/ --recursive \
      --query 'Parameters[].Name' --output text || true
  fi
  pause
}

step_billing() {
  heading 5 "Turn on billing alerts"
  cat <<'EOF'
In the Billing console, as the account's root user or a user allowed to change billing
preferences: Billing preferences, Alert preferences, and there turn on the CloudWatch billing
alerts. No command sets this. Without it the account publishes no billing metric, and no alarm
on spending can ever fire.

Each SNS subscription is confirmed later, from the e-mail it sends to the admin address, when
the stack that creates its topic is deployed.
EOF
  pause
}

step_bootstrap() {
  heading 6 "Bootstrap the account, then deploy the pipeline stack once"
  cat <<'EOF'
By default `cdk bootstrap` gives CloudFormation administrator access. This bootstrap gives it
the project's execution policy and permissions boundary instead (setup/bootstrap/README.md). The
region is the one of your credentials (decision 9). The CDK CLI must be installed first:
`npm ci --ignore-scripts` (setup/README.md).
EOF
  # The placeholders a dry run prints. A real run reads both from your credentials.
  local account="123456789012" region="us-east-1" output="${TMPDIR:-/tmp}/rhi-owner-setup.XXXXXX/cdk.out"
  local region_known=""
  say ""
  if [ "$dry_run" = 0 ]; then
    if [ ! -x node_modules/.bin/cdk ]; then
      say "The CDK CLI is not installed. Run \`npm ci --ignore-scripts\`, then this step again:"
      say "    setup/owner-setup.sh --step bootstrap"
      pause
      return 0
    fi
    outside_the_tree
    output="$scratch/cdk.out"
    region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
    [ -z "$region" ] || region_known=1
  fi
  say "1. The two policies:"
  run aws cloudformation deploy --stack-name rhi-bootstrap-policies \
    --template-file setup/bootstrap/policies.yaml --capabilities CAPABILITY_NAMED_IAM || true
  say "2. The bootstrap, which names the account and the region:"
  if capture account aws sts get-caller-identity --query Account --output text &&
    { [ -n "$region_known" ] || capture region aws configure get region; }; then
    # The CLI synthesizes the app first, with the example configuration: the bootstrap deploys
    # nothing of the app. What it synthesizes goes to the temporary folder.
    run env RHI_CONFIG=example node_modules/.bin/cdk bootstrap "aws://$account/$region" \
      --cloudformation-execution-policies "arn:aws:iam::$account:policy/rhi-cfn-execution" \
      --custom-permissions-boundary rhi-permissions-boundary --output "$output" || true
    say "3. To check: the first lists rhi-cfn-execution alone, the second names rhi-permissions-boundary."
    local role="cdk-hnb659fds-cfn-exec-role-$account-$region"
    run aws iam list-attached-role-policies --role-name "$role" || true
    run aws iam get-role --role-name "$role" --query Role.PermissionsBoundary || true
  else
    say "    Without the account and the region the bootstrap cannot be named."
  fi
  cat <<'EOF'

The first deployment of the pipeline stack, once, from this machine: the pipeline stack is not
in the repository yet, and its command arrives with it. It needs this bootstrap and the
configuration of step 4.
EOF
  pause
}

step_connection() {
  heading 7 "Authorize the GitHub connection"
  cat <<'EOF'
The pipeline stack creates the connection to GitHub, and it starts pending. After the first
deployment of step 6, authorize it once in the console (Developer Tools, Settings, Connections):
install the AWS connector on "Only select repositories", with this repository alone. No command
does this.
EOF
  pause
}

step_repository() {
  heading 8 "Apply the ruleset and the repository's settings"
  cat <<'EOF'
The ruleset, secret scanning with push protection, and a no-reply address for commits. These are
the steps 2 and 4 of docs/factory/green-light.md, which says how to check that each one binds:
if they are done, run nothing here.
EOF
  say ""
  run bash setup/apply-ruleset.sh || true
  run gh repo edit --enable-secret-scanning --enable-secret-scanning-push-protection || true
  say "    The no-reply address this prints is the one to commit with (\`git config user.email\`):"
  run gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"' || true
  pause
}

step_roster() {
  heading 9 "Enter the roster"
  cat <<'EOF'
The students who may log in, by their numeric GitHub ID, entered through the admin CLI with your
own AWS credentials. The admin CLI is not in the repository yet: its command arrives with it.
The roster is never written to the repository.
EOF
}

if [ "$dry_run" = 1 ]; then
  say "Dry run: every step and every command is printed. Neither AWS nor GitHub is called."
else
  say "Each command is shown before it runs, and runs only after a yes."
fi
say "Nothing is written to the working tree."

for name in "${steps[@]}"; do
  if [ -z "$only" ] || [ "$only" = "$name" ]; then
    "step_$name"
  fi
done
