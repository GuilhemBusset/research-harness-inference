#!/usr/bin/env bash
# The owner's one-time steps (system design, section 8.6), walked through in their order:
#
#   setup/owner-setup.sh --dry-run        print every step and every command, and run none
#   setup/owner-setup.sh                  the walkthrough: asks before each command it runs
#   setup/owner-setup.sh --step configuration --config <file>     one step on its own
#   setup/owner-setup.sh --step values --config <file>            the scanner's list of real values
#   setup/owner-setup.sh --step apps --reporter-key <file> --factory-key <file>   the Apps, read back
#
# The owner runs it, from a shell no agent runs in, with their own AWS credentials and `gh` login.
# docs/runbooks/owner-setup.md describes each step: what it needs, how to check it and the
# decision of the design it rests on.
#
# What the script learns (the account, a quota's code, an address, an App's ID) goes to the
# terminal, the configuration goes to SSM, and the status reporter's credentials go to Secrets
# Manager: the reporter's private key is read from its file and never printed. The Apps are read
# back with their own keys, which only `openssl` reads, to sign; no key or token is printed. The private values
# of the configuration go to the hygiene scanner's list of local values, a file outside the
# repository that only you can read, and are never printed. Nothing is written
# to the working tree, by the script or by the tools it starts: Python keeps no bytecode, and uv's
# project environment and the CDK's output go to a temporary folder that is removed when the
# script ends.
#
# With --dry-run no answer is read and neither AWS nor GitHub is called: `aws`, `gh` and `cdk` are
# never started. The one command a dry run does run is the configuration loader, on the file
# --config names: it reads that file and calls nothing.
set -euo pipefail

started_in="$PWD"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

# The nine steps of section 8.6, in their order. `values` is the second half of the fourth: it
# has a name of its own, to run alone, and the number 4b.
steps=(quotas domain apps configuration values billing bootstrap connection repository roster)
numbered=9

usage() {
  cat <<EOF
usage: setup/owner-setup.sh [--dry-run] [--step <name>] [options]

  --dry-run            print every step and every command; call neither AWS nor GitHub
  --step <name>        run one step on its own: ${steps[*]}
  --max-instances <n>  quotas: the coursework pool's max_instances (asked for when not given)
  --upgrade-pool       quotas: the upgrade pool is wanted (96 more G-family vCPUs)
  --xl-pool            quotas: the xl pool is wanted (a P-family quota)
  --config <file>      configuration, values: the owner's file, kept outside the working tree
  --reporter-key <file>  apps: the status reporter's private key file, kept outside the working tree
  --factory-key <file>   apps: the factory's App's private key file, kept outside the working tree
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
reporter_key=""
factory_key=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --upgrade-pool) upgrade_pool=1 ;;
    --xl-pool) xl_pool=1 ;;
    --step | --max-instances | --config | --reporter-key | --factory-key)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      case "$1" in
        --step) only="$2" ;;
        --max-instances) max_instances="$2" ;;
        --config) config_file="$2" ;;
        --reporter-key) reporter_key="$2" ;;
        --factory-key) factory_key="$2" ;;
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

# --- The region the AWS CLI and the AWS SDK read ---------------------------------------------

# For every command a step runs. The AWS CLI reads AWS_REGION, then AWS_DEFAULT_REGION; the AWS
# SDK of the Python steps (at the version uv.lock holds) reads AWS_DEFAULT_REGION and never
# AWS_REGION. When only AWS_REGION is set, both read it.
if [ -n "${AWS_REGION:-}" ] && [ -z "${AWS_DEFAULT_REGION:-}" ]; then
  export AWS_DEFAULT_REGION="$AWS_REGION"
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
  printf '\n== Step %s of %s: %s ==\n\n' "$1" "$numbered" "$2"
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

outside_path() {   # outside_path <variable> <what the file is>: refuses a path in the working tree
  # The variable holds the path as it was given and, afterwards, as an absolute path. Both the
  # path as it was given and the file it leads to are checked: a link in the tree is in the tree.
  # The file need not exist, and is not opened.
  local -n the_path="$1"
  local path
  [[ $the_path == /* ]] || the_path="$started_in/$the_path"
  for path in "$(realpath -m -s -- "$the_path")" "$(realpath -m -- "$the_path")"; do
    case "$path/" in
      "$repo_root"/*) die "refused: the $2 is inside the working tree. Keep it outside." ;;
    esac
  done
}

outside_file() {   # outside_file <variable> <what the file is>: as `outside_path`, for a file
  # that exists. Afterwards the variable holds the file's real path.
  local -n the_file="$1"
  outside_path "$1" "$2"
  [ -f "$the_file" ] || die "refused: the $2 is not a file that can be read."
  the_file="$(realpath -- "$the_file")"
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

# The module that reads an App back from GitHub, with the App's own tokens. A private App answers
# no one else: not your `gh` login, which no command of the apps step uses.
apps_tool=(env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_apps)
this_repository=""    # <owner>/<name>, once `repository` has found it
app_installation=""   # what `read_app` read: the App's installation on this repository

repository() {   # sets this_repository: from the `origin` remote of this clone, or asked
  # git reads its own configuration, and calls nobody.
  [ -z "$this_repository" ] || return 0
  local top="" url=""
  top="$(git -C "$repo_root" rev-parse --show-toplevel 2>/dev/null)" || top=""
  if [ "$top" = "$repo_root" ]; then
    url="$(git -C "$repo_root" remote get-url origin 2>/dev/null)" || url=""
  fi
  if [[ $url =~ ^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?$ ]]; then
    this_repository="${BASH_REMATCH[2]%.git}"
    say "    This repository, as the origin remote names it: $this_repository"
  elif [ "$dry_run" = 1 ]; then
    this_repository="<owner>/<name>"
  else
    printf '    This repository on GitHub, as <owner>/<name> (no origin remote names it): '
    read -r this_repository || true
    [[ $this_repository =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "a repository is named <owner>/<name>."
  fi
}

app_key() {   # app_key <variable: the file an option named, or nothing> <the option>
  # Asks for the file when no option named it, and refuses one in the working tree. The file is
  # not opened here: `openssl` reads it, to sign, and nothing prints it.
  local -n key_into="$1"
  if [ -z "$key_into" ] && [ "$dry_run" = 1 ]; then
    say "    No $2 was given: this dry run checks no file."
    key_into="<private key file>"
    return 0
  fi
  if [ -z "$key_into" ]; then
    printf '    Its private key file, outside the working tree: '
    read -r key_into || true
    [ -n "$key_into" ] || die "no private key file was named."
  fi
  outside_file "$1" "private key file"
}

read_back() {   # read_back <variable> <command...>: as `capture`, but a refusal stops this App only.
  # Returns 1 when the command was not run, or when it refused the App or could not read it.
  local -n read_into="$1"
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
  local status=0 line=""
  read_into="$("$@")" || status=$?
  while IFS= read -r line; do
    [ -z "$line" ] || say "      $line"
  done <<<"$read_into"
  if [ "$status" != 0 ]; then
    say "      Not read back, or refused: nothing more is done with this App."
    return 1
  fi
}

read_app() {   # read_app <reporter|factory> <variable for its ID> <variable for its key file> <option>
  # Reads the App back by its ID, with a token its own key signs. Afterwards app_installation
  # holds the ID of its installation on this repository, as GitHub reported it, or nothing.
  # Returns 1 when the App was not read back, or was refused. A dry run prints the command, with
  # placeholders.
  local -n id_into="$2"
  local -n key_of="$3"
  local found="" line=""
  app_installation=""
  id_into="<app id>"
  if [ "$dry_run" = 0 ]; then
    printf "    Its App ID, on the App's settings page (nothing, when the App is not registered yet): "
    read -r id_into || true
    if [ -z "$id_into" ]; then
      say "      (not read back)"
      return 1
    fi
    [[ $id_into =~ ^[0-9]+$ ]] || die "an App ID is a number."
  fi
  app_key "$3" "$4"
  repository
  [ "$dry_run" = 1 ] || outside_the_tree
  read_back found "${apps_tool[@]}" "$1" --app-id "$id_into" --key-file "$key_of" \
    --repository "$this_repository" || return 1
  while IFS= read -r line; do
    [[ ! $line =~ ^\ *Installation\ ([0-9]+): ]] || app_installation="${BASH_REMATCH[1]}"
  done <<<"$found"
}

read_login() {   # read_login <the reporter's App ID, or nothing when it was not read back>
  # The login App has no key: a token of the status reporter reads it, by its slug.
  local slug="<slug>" found=""
  if [ "$dry_run" = 0 ]; then
    if [ -z "$1" ]; then
      say "    Its reader is a token of the status reporter, which was not read back: not read back."
      return 0
    fi
    printf '    Its slug (nothing, when the App is not registered yet): '
    read -r slug || true
    if [ -z "$slug" ]; then
      say "      (not read back)"
      return 0
    fi
    [[ $slug =~ ^[A-Za-z0-9-]+$ ]] || die "a slug holds letters, digits and hyphens only."
  fi
  read_back found "${apps_tool[@]}" login --slug "$slug" --app-id "${1:-<app id>}" \
    --key-file "$reporter_key" || true
}

store_reporter() {   # store_reporter <the reporter's App ID> <its installation ID, as GitHub reported it>
  # The reporter's construct refers to this secret by its name and does not create it.
  local secret="rhi/reporter/github-app"
  local app_id="$1" installation="$2"
  local tool=(env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_secret)
  if [ "$dry_run" = 1 ]; then
    installation="<installation id>"
  elif [ -z "$installation" ]; then
    say "    The reporter has no installation on this repository yet, so its credentials are not stored."
    return 0
  fi
  say "    The secret $secret, created if it is absent, with the App's ID and its installation's ID"
  say "    as GitHub reported them. The key is read from its file, not printed."
  if run "${tool[@]}" "$secret" --app-id "$app_id" --installation-id "$installation" \
    --private-key-file "$reporter_key"; then
    say "    To check: the secret's name, not its value."
    run aws secretsmanager describe-secret --secret-id "$secret" --query Name --output text || true
  fi
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
   Keep its private key outside every checkout. This step stores it in the reporter's secret.

3. The factory's identity (ADR 0001, amendment 2; docs/factory/green-light.md, step 7)
   Permissions: Contents, Pull requests and Issues, read and write; Commit statuses, Checks and
   Actions, read-only. Workflows and Administration: no access.
   Install it on "Only select repositories", with this repository alone.
   Its private key stays on the factory's host, outside every checkout, readable by you alone.
   It never goes to AWS and never into the repository.

Once the Apps are registered, this step reads each one back from GitHub. An App installable only
on this account is private: GitHub answers for it to the App's own tokens only, and your `gh`
login gets nothing, so no command here uses it.

- The status reporter and the factory's App are read by their App ID (the App's settings page,
  About), with a token signed by their own private key: --reporter-key and --factory-key name the
  key files, or the step asks for them. `openssl` signs with a key; nothing prints it.
- The login App is read by its slug, the last part of the address of its settings page
  (https://github.com/settings/apps/<slug>), with a token of the status reporter. Its device flow
  is checked by asking GitHub for a device code with its client ID, which is never printed.

For each App the step prints its slug, its ID and its permissions as GitHub's API names them
(`statuses` is Commit statuses, `write` is read and write), and says whether they are those of
section 8.6. GitHub grants Metadata, read-only, by itself to an App that has a repository
permission. For the reporter and the factory's App it prints each installation with the
repositories it reaches. It refuses an App installed on anything other than this repository
alone, and a login App whose device flow is off.

For the status reporter the step then stores what the reporter posts a status with, in a Secrets
Manager secret in the region of your credentials: the App's ID and the ID of its installation on
this repository, both as GitHub reported them, and the private key. The key is read from its
file, which stays where it is, and is never printed. Until it is stored, the reporter posts
nothing.
EOF
  local reporter_id="" factory_id="" reporter_read=""
  say ""
  say "Reading back the status reporter, with its own key."
  if read_app reporter reporter_id reporter_key --reporter-key; then
    reporter_read="$reporter_id"
    store_reporter "$reporter_id" "$app_installation"
  else
    say "    The reporter was not read back, or was refused, so its credentials are not stored."
  fi
  say ""
  say "Reading back the student login App, with a token of the status reporter."
  read_login "$reporter_read"
  say ""
  say "Reading back the factory's identity, with its own key."
  read_app factory factory_id factory_key --factory-key || true
  pause
}

step_configuration() {
  heading 4 "Write the configuration to SSM"
  cat <<'EOF'
The configuration is one file that you keep outside the working tree: a copy of
config.example.toml with your values in place of the placeholders (the domain and its hosted
zone, the admin e-mail address, the monthly budget of decision 6, the time zone, the course end
date of decision 1, ...). For now `session_windows` stays an empty list: the loader refuses a
window until windows have a shape. `pools` stays an empty table, `pools = {}`, until you add the
mock pool, the `[pools.mock]` table of the example; each later pool brings its GPU-hour ceiling.

The configuration loader checks the file before anything is written, and the configuration
writer then writes one parameter per key under /rhi/config/, in the region of your credentials.
EOF
  local file="$config_file" checked=1
  local tool=(env PYTHONPATH=src:services/common uv run --locked --no-dev python -m rhi.owner_config)
  if [ -z "$file" ] && [ "$dry_run" = 0 ]; then
    printf '\nThe configuration file, outside the working tree: '
    read -r file || true
    [ -n "$file" ] || die "no configuration file was named."
    config_file="$file"   # the next step reads the same file
  fi
  say ""
  if [ -z "$file" ]; then
    say "No --config was given: this dry run checks no file."
    file="<your configuration file>"
    show "${tool[@]}" check "$file"
    say "      (dry run: not run)"
  else
    outside_file file "configuration file"
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

step_values() {
  heading 4b "Write the list of real values the hygiene scanner checks for"
  cat <<'EOF'
No public pattern can name your domain or your address: writing the pattern would publish the
value (system design, section 10). The hygiene scanner therefore also reads a list of real values
that is kept outside the repository, and reports each of them as `local-value` in every scan on
this machine: your hooks, your scripts/check.sh, the implement loop before each push
(tools/hygiene/README.md, "Values only the owner knows").

This step writes that list: the value of each private key of the configuration file of step 4
(the domain, the hosted zone ID, the admin e-mail address, the instructor's phone number and the
login app's client ID), and any further value you enter here, such as a student's name. A value
is added once, however often the step runs, and none is printed.
EOF
  # Where the scanner looks: tools/hygiene/scan.py, `load_local_values`.
  local home="${HOME:-}"
  [ -n "$home" ] || home=~
  local list="${HYGIENE_LOCAL_VALUES:-${XDG_CONFIG_HOME:-$home/.config}/research-harness-inference/hygiene-values}"
  local file="$config_file" further="" value=""
  local tool=(env PYTHONPATH=src:services/common uv run --locked --no-dev python -m rhi.owner_config)
  outside_path list "list of local values"
  say ""
  if [ -n "${HYGIENE_LOCAL_VALUES:-}" ]; then
    say "The list is the file HYGIENE_LOCAL_VALUES names. It is created if it is absent, for you alone:"
  else
    say "The list is the scanner's default file. It is created if it is absent, for you alone:"
  fi
  say "    $list"
  if [ -z "$file" ] && [ "$dry_run" = 0 ]; then
    printf '\nThe configuration file, outside the working tree: '
    read -r file || true
    [ -n "$file" ] || die "no configuration file was named."
  fi
  if [ -z "$file" ]; then
    say "No --config was given: this dry run checks no file."
    file="<your configuration file>"
  else
    outside_file file "configuration file"
  fi
  if [ "$dry_run" = 0 ]; then
    say "Further values to check for, one per line. An empty line ends them:"
    while read -r value && [ -n "$value" ]; do
      further+="$value"$'\n'
    done
  fi
  say ""
  # The further values reach the module on its standard input: no command line holds one.
  show "${tool[@]}" values "$file" --list "$list"
  if [ "$dry_run" = 1 ]; then
    say "      (dry run: not run, and no file is written)"
  elif ! confirm "    Run it?"; then
    say "      (not run)"
  else
    outside_the_tree
    printf '%s' "$further" | "${tool[@]}" values "$file" --list "$list" ||
      die "that command failed. Nothing after it was run."
    say "    To check: write one of the values to a scratch file outside the working tree, and scan it."
    say "    The scan must report it as local-value:"
    show python3 tools/hygiene/scan.py --paths "<scratch file>"
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

After bootstrap, follow the first-deployment command in docs/runbooks/owner-setup.md, step 6.
It creates the pipeline and its pending GitHub connection. The pipeline adds the spike jobs on
its first self-update after you authorize that connection.
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
own AWS credentials, once the pipeline has deployed the access stack. Each student gets an alias,
which their metrics carry instead of the ID, and 4 concurrent requests unless you give another
number (--limit). Add each student with the first command; the
roster is never written to the repository.

The canary command puts the pipeline's verification on the roster and writes its key into the
secret rhi/canary-key, creating it: run it once before the verification is merged. Running it
again replaces the key, which it never prints.
EOF
  local tool=(env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main)
  say ""
  show "${tool[@]}" roster add "<github-id>" "<alias>"
  run "${tool[@]}" canary || true
  run "${tool[@]}" roster list || true
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
