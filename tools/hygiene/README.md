# Hygiene scanner

This repository is public. Nothing that identifies the AWS account it deploys to, and no
credential, may be committed. `scan.py` is the deterministic gate for that rule
([ADR 0001](../../docs/adr/0001-software-factory.md), decision 4; system design, "Keeping the
repository public-safe"). It is standard-library Python, needs no install, and runs in under a
second.

## Where it runs

| Where | Command | What it stops |
| --- | --- | --- |
| `.githooks/pre-commit` | `scan.py --staged` | A person or an interactive agent committing a leak |
| `.githooks/pre-push` | `scan.py --rev <sha>` for the tip of each pushed ref | A branch whose tree carries a leak leaving the machine |
| The implement loop's verifier | `scan.py`, in the tree of the exact head | A loop branch being examined or pushed with a leak |
| The implement loop's engine, immediately before each push | `scan.py --rev <head>` and `scan.py --history <main>..<head>`, outside the sandbox, where the owner's list exists (below) | A loop branch leaving the machine with a value only the owner's list names |
| CI, job `hygiene` | `scripts/check.sh hygiene` | A head that is not clean on a clean machine |
| CI, job `history` | `scan.py --history` (the pull request's commits; on `main`, everything) | A value that was committed and later removed, a forbidden file that was later renamed, a value in a commit message or in a commit's author address |

The two hooks run the scanner and the `hygiene.toml` of the work tree, as they are on disk. The
verifier and CI run the committed ones, on a clean checkout: see the limits of the hooks in
[`setup/README.md`](../../setup/README.md).

## Use

```sh
python3 tools/hygiene/scan.py                  # every file git tracks or would add
python3 tools/hygiene/scan.py --staged         # what the next commit adds or changes (staged content)
python3 tools/hygiene/scan.py --paths P...     # these files or directories, as they are on disk
python3 tools/hygiene/scan.py --rev REV        # the tree of one commit
python3 tools/hygiene/scan.py --history [REVS] # every file version, commit message and identity (or of main..HEAD)
python3 tools/hygiene/scan.py --redact         # copy stdin to stdout with every finding masked
scripts/check.sh hygiene                       # the first form, as the gates run it
```

Exit status: `0` clean, `1` findings, `2` usage or environment error (not a git work tree, a
broken `hygiene.toml`). A finding is one line on stdout:

```
path:line: rule: redacted excerpt
```

The excerpt never shows the value, nor anything else on that line that a rule matches, reported
or not. It does show up to 48 characters on each side: a secret of no recognisable shape on the
same line would be among them. Line `0` means the finding is about the file's name. `--history`
adds the blob to the path (`path@<blob id>`) and reports a commit's own text as
`<commit>@<commit id>`, where line 1 is the author and line 2 the committer. A summary goes to
stderr; `--show-allowed` also lists what inline markers let through, and `--quiet` prints nothing
when there is no finding.

The default form needs a git work tree and scans all of it, whatever directory it is run from.
`.gitignore`d files are not read, since they cannot be committed by accident; a forbidden file
that was force-added is. To scan a directory that is not a git work tree, use `--paths`.

## Rules

| Rule | Fires on | Does not fire on |
| --- | --- | --- |
| `account-in-arn` | An ARN whose account field is filled, in any partition | The documented accounts; ARNs with an empty account field or `aws` as the account |
| `account-id` | Exactly twelve digits, also inside a name (`assets-<id>-us-east-1`), a host name or a URL-encoded string; the console's `1234-5678-9012` form, alone or inside a name | The documented accounts; twelve digits inside a run of 16 or more hexadecimal characters (a hash); the fraction of a decimal number; the last group of a UUID; longer digit runs; lockfiles |
| `access-key` | IAM unique IDs (`AKIA`, `ASIA`, `AIDA`, `AROA` and the other prefixes, plus 16 characters); a value of 40 characters or more given by name to a secret key or a session token (`aws_secret_access_key`, `SecretAccessKey`, `aws_session_token`) | The access key and secret key of AWS's documentation; a name with no value after it |
| `token` | GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), Anthropic and OpenAI keys (`sk-ant-`, `sk-proj-`, `sk-` and 32 characters), Hugging Face tokens (`hf_`), Grafana service-account tokens (`glsa_`), a student's API key of this project (`rhi_` and 46 letters or digits, anywhere in a line, when no letter or digit follows), PEM, OpenPGP and PuTTY private keys, a password in a URL (`scheme://<user>:<password>@host`) | Public keys and certificates; values too short to be a token (`rhi_` and 45 characters), and a longer run of letters and digits after `rhi_`; this repository's package names (`rhi_common`, `rhi_gateway` and the others); a template where the password would be (`{token}`, `${PASSWORD}`, `<password>`, `%s`, `****`) |
| `forbidden-file` | A file named `cdk.context.json`, `.env`, `.env.*`, `.envrc`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.ppk`, `id_rsa` and its siblings, `*.tfstate`, or anything under `cdk.out/` or `.aws/` | `.env.example` (its content is still scanned) |
| `endpoint` | Host names of API Gateway (`execute-api`), CloudFront distributions, load balancers (`elb`), Lambda function URLs, also with the region written as a template; Managed Prometheus workspace IDs; `hostedzone/Z…` | Wildcards, and templates of the identifying part (`*.cloudfront.net`, `{id}.execute-api.{region}.amazonaws.com`); service endpoints; the CloudFront host of AWS's documentation |
| `resource-id` | EC2-style IDs in their 17-character form (`i-`, `vpc-`, `subnet-`, `sg-`, `ami-`, `eni-`, `vol-` and others), alone or at the end of a name | The IDs of AWS's documentation |
| `email` | Any address, in any script | `noreply@anthropic.com`, GitHub's own addresses, the reserved example domains (`example.com`, `.example`, `.invalid`, `.test`), `git@github.com`; versions (`ruff@0.17.3`), decorators, file names (`logo@2x.png`) |
| `home-path` | `/home/<name>`, `/Users/<name>`, `/mnt/c/Users/<name>`, `C:\Users\<name>`, `\home\<name>` | Generic users (`user`, `runner`, `ec2-user`, `ubuntu`); paths that merely contain `/home/`; `$HOME`, `~` |
| `local-value` | A value from the owner's own list, in any letter case, in any file (see below) | Anything, where that list does not exist |
| `console-link` | A link to the AWS console: a URL, with its scheme, whose host is `console.aws.amazon.com` or `signin.aws.amazon.com`, or their counterparts in the China and GovCloud partitions (`console.amazonaws.cn`, `signin.amazonaws.cn`, `console.amazonaws-us-gov.com`, `signin.amazonaws-us-gov.com`), or ends with one of them after a dot (a region's console, the sign-in page of an account alias), also with that label written as a template. The whole URL is the finding | AWS's documentation and product pages (`docs.aws.amazon.com`, `aws.amazon.com/console/`); service endpoints; a console host name written without a scheme |
| `build-badge` | The URL, with its scheme, of a CodeBuild project's badge (`codebuild.<region>.amazonaws.com/badges…`), of the badge image of a GitHub Actions workflow (`github.com/<owner>/<repo>/actions/workflows/<file>/badge.svg`, and the older `…/workflows/<name>/badge.svg`), or of a shields.io badge of a build or workflow status, served by `img.shields.io` or `raster.shields.io`. The badge's route decides: the routes shields.io files under its category "Build" for the status of a build, a pipeline, a workflow or a commit's checks (`travis/…`, `circleci/…`, `github/actions/workflow/status/…`, `github/check-runs/…`, `gitlab/pipeline-status/…`, `jenkins/build`, `readthedocs/…` and the like, retired routes included; the table is `_SHIELDS_BUILD_ROUTE`), and a badge whose text the URL writes itself (`badge/…`, `static/…`, `endpoint`) with `build`, `codebuild`, `workflow` or `pipeline` as a word of that text or of its `label` or `message`. The whole URL is the finding | The CodeBuild service endpoint; a link to a workflow's page or file; shields.io badges of anything else (a licence, a version, a coverage), whatever the repository, the branch or the package is called (`github/license/<owner>/build-tools` shows a licence); any of these written without a scheme |

Every rule reads every file, text or not, with two exceptions: `email` is not applied to binary
files (more than one NUL byte per thousand), where an address is a font's or a PDF's own
metadata, and `account-id` is not applied to lockfiles. A file with NUL bytes is also read
without them, which is how UTF-16 and UTF-32 text shows its content, with or without a byte-order
mark and whatever `working-tree-encoding` a `.gitattributes` gives it.

A file's **name** is read like its content. A value in a path is a finding at line `0`, the path
is printed masked wherever it appears, and no marker can allow it: rename the file.

What the patterns cannot see: a real domain name, a public IP address, a bucket or stack name
that gives the owner away, a secret with no recognisable shape (a GitHub client secret is 40
hexadecimal characters), a build badge of a service whose shields.io URL has none of the rule's
words, anything encoded or compressed. The owner's list (below) covers the values the owner
thought of; the rest remains the author's and the examiner's job.

What the rules report although it is harmless: a twelve-digit value that is neither an account
nor one of the shapes above (a `YYYYMMDDHHMM` timestamp, a twelve-digit abbreviated commit ID);
allow it on its line. The account IDs AWS itself owns and CDK writes into templates (the
load-balancer log-delivery accounts, for instance) are reported too: when a snapshot first needs
them, add them to `account_ids` in `hygiene.toml` with their source.

The pattern of a student's API key is the scanner's own copy (`STUDENT_KEY`) of the format that
`services/common/rhi_common/keys.py` defines: the scanner imports nothing from the product, and
`tests/test_student_key.py`, beside this file, keeps the two equal. So a test or a capture that
needs a key generates it at run time with that module, or carries a value that is not a key (one
character short, say).

## Values only the owner knows

No pattern in a public repository can name the real domain, the admin's address or the name of
a student: writing the pattern would publish the value. The owner therefore keeps them in a file
**outside the repository**, one per line (`#` starts a comment line, values are at least four
characters):

```sh
mkdir -p ~/.config/research-harness-inference
$EDITOR ~/.config/research-harness-inference/hygiene-values
chmod 600 ~/.config/research-harness-inference/hygiene-values
```

Wherever that file exists, every scan also reports those values as `local-value`, whatever their
letter case, in every file including lockfiles and binaries, and `redact` masks them. An inline
marker cannot allow one. `HYGIENE_LOCAL_VALUES` names another file; a file named that way must
exist.

Where the file does not exist the rule is off, silently: in CI, on a contributor's machine, and
inside the implement loop's sandbox, whose home directory is empty. Agents never see the values.
The owner's hooks, the owner's `scripts/check.sh`, the green-light scans and the implement loop's
engine, before each push (below), do. A consequence:
`pre-push` on the owner's machine can refuse a branch that passed every other check, if the branch
contains one of these values. That is the leak being stopped, at the last local point.

The implement loop's branches are checked against the list by the engine. The loop pushes its
branches with its own git calls, which run none of the repository's hooks, and its verifier scans
inside the sandbox, where this file does not exist. The engine process itself runs outside the
sandbox, as the owner. Where the file exists, the engine scans immediately before each push of a
branch, an issue's or a revert's: the tree of the head (`--rev`), then the commits the push would
publish (`--history <main>..<head>`). The commands are `LOCAL_VALUE_CHECKS` in
`tools/implementation-loop/implement_loop/config.py`.

- That scan runs the `scan.py` and the `hygiene.toml` of the checkout the engine runs from, never
  the branch's copies, and reads the branch as git objects: nothing a builder wrote is executed
  outside the sandbox.
- A finding is handled as a finding in a branch's history is (ADR 0001, amendment 7): the branch
  is abandoned unpushed, the issue ends `failed` with the reason `history`, and a retry starts on
  a new branch. A revert with a finding is not pushed.
- What this scan finds is the value itself. The engine keeps the path and the rule's name of a
  finding and nothing else: the line number and the excerpt reach no issue, no comment, no agent
  and no file of the run. The agents never see the values, so one can only reach a loop branch
  through text the owner wrote into an issue: that text is what to correct before a retry.
- A scan that cannot run (a list that cannot be read, a `HYGIENE_LOCAL_VALUES` that names no
  file) withholds the push.
- `ship.sh` fetches `main` once more before it pushes, and merges it or rebases onto it if it
  moved: a head the scan did not see. While the engine pushes a scanned head, its git therefore
  has a `pre-push` hook of the engine's own, in the hooks directory the engine owns, which
  refuses any other commit. A head shipping made is verified, certified and scanned before it is
  pushed. Every run of the repository has that same hooks directory, so a push holds a lock on
  it until its hook is removed: two runs push one after the other, each under its own hook.

Where the file does not exist, the engine scans nothing outside the sandbox and pushes as before.

## What to write instead

- An account ID: `123456789012` or `111122223333`. The real one comes from the deploying
  credentials and is never written anywhere in this repository.
- An ARN: `arn:aws:iam::123456789012:role/example`.
- A domain or an address: `example.com`, `someone@example.com`.
- A path: relative to the repository, or `$HOME/...`.
- Real configuration: in SSM, read at deploy time; `config.example.toml` holds placeholders only.

## Allowing one line

A value that is public on purpose and still matches a rule is allowed on its own line, with the
rule and a reason:

```python
BUILD_STAMP = "202611112247"  # hygiene: allow account-id (a YYYYMMDDHHMM build stamp)
```

`hygiene: allow <rule> (<reason>)` covers one finding of that rule on that line; a second finding
needs a second marker. The reason is required: a marker without one allows nothing. A marker is
visible in review and in `--show-allowed`, and an examiner is expected to question it. It cannot
allow a `forbidden-file`, a `local-value` or a value in a file name, and it counts in files only:
`--history` honours no marker in a commit message or an author name.

## `hygiene.toml`

Holds data only: the documented placeholders each rule accepts, the lockfiles the `account-id`
rule skips, and whole-file excludes (empty; meant for recorded test fixtures that cannot carry a
marker, each with its reason). Unknown keys are errors. It does not hold rules, and it is not
where a single value is allowed. The file and the scanner are protected paths and part of the
loop's safety surface: a change to either is a change to the gate and is reviewed as one.

## As a library

```python
import scan   # with tools/hygiene on sys.path

scan.scan_text(text, path="issue-body.md")   # -> list[Finding]: content rules
scan.scan_paths(["docs", "app.py"], root)    # -> list[Finding]: file names and content
scan.redact(text)                            # -> str: every match masked, inline markers ignored
```

`redact` is for anything about to be published by a program: evidence comments, commit-status
descriptions, log excerpts.

## Tests

```sh
uv run --locked pytest tools/hygiene/tests
```

Every rule has positives, the documented placeholders, and look-alikes that must stay quiet. The
values a rule must catch are assembled at run time, so the test file itself scans clean. The last
test scans this repository.
