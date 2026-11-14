# Isolation: what an agent can reach

The implement loop runs code nobody has reviewed yet: two vendors' agents, and the commands they
write. [ADR 0001](../adr/0001-software-factory.md), decision 3, puts every such process in a
sandbox that holds no AWS material and no GitHub login. This page says what that sandbox is, what
is in its allow-list, how to check it on a host, and what it does **not** protect. The code is
`tools/implementation-loop/implement_loop/sandbox.py`; the allow-list is the `[sandbox]` table of
`tools/implementation-loop/loop.toml`. Both are on the safety surface.

## Inside and outside

Outside, as the owner, with the owner's files and the `gh` login: the engine and the supervisor.
They run their own code, `git`, `gh` and `scripts/ship.sh`, and nothing an agent wrote.

Inside, each in a sandbox of its own, started by the engine and gone when the call ends:

| What | Where it is started |
| --- | --- |
| Every agent call: `claude -p`, `codex exec`, and the `codex mcp list` that precedes each Codex call | `live.py` |
| The doctor's smoke call and the supervisor's "is this vendor back" call | `environment.py` |
| Every check command: the issue's commands, the examiner's probes and reproductions, the standing checks, the history checks | `verify.py` |

There is no setting, flag or variable that turns the sandbox off. When it cannot be built
(bubblewrap missing, a malformed allow-list, a path that must be bound and is not there), nothing
is started: the issue waits (`environment`), and `run` and `supervise` refuse to start at all,
because the doctor fails. Only the engine's tests replace it, with a pass-through their helper
module installs; the engine never does.

## What a process inside sees

The sandbox is [bubblewrap](https://github.com/containers/bubblewrap): unprivileged Linux
namespaces, no daemon, no image, no root.

| | Inside |
| --- | --- |
| The root filesystem | Read-only. |
| `/home`, `/root`, `/run`, `/mnt`, `/media`, `/var/tmp`, the home directory `$HOME` names and the one the account really has | Empty directories. Whatever was there does not exist: `~/.aws`, the `gh` login under `~/.config/gh`, `~/.ssh`, `~/.gitconfig`, the session history of both vendors, every other project, the session's sockets, and on WSL the Windows drives and the socket that starts Windows programs. |
| `/tmp` | Empty and private to the call. |
| The tools | Read-only: the directories listed under `toolchain`. |
| The work | The call's worktree and the private git directory of that call, read-write for a working role and for a check command, read-only for a read-only role. The repository's object store, read-only. Never the main checkout, never another issue's worktree. |
| Package caches | The run's own, under its `.loop` directory, read-write (`UV_CACHE_DIR`, `npm_config_cache`). Never the owner's: what an agent leaves in a cache would later run outside. |
| A vendor's login | For that vendor's own calls only: the one file its login is kept in, read-write, and its settings file, read-only. The other vendor's does not exist, and a check command has neither. |
| Processes | Only its own (a PID namespace). It dies with the engine's child process, timeouts and stops included. |
| The network | The host's. Not isolated: see below. |
| The environment | Scrubbed: see below. |

The home directory is an allow-list, not a mask: it starts empty, and only what `loop.toml`
names and what the engine binds for that call is put back. A directory nobody thought of is
absent, not exposed.

Which directories the engine binds for a call comes from the engine's own knowledge of its
worktrees, never from a file an agent can write: a worktree's `.git` file is believed only when it
names a directory directly under the run's `agent-git/`, and what an agent writes into that
directory (`alternates`, `commondir`) is not read.

## The allow-list

`tools/implementation-loop/loop.toml`:

```toml
[sandbox]
toolchain = ["~/.local/bin", "~/.local/share/claude", "~/.codex/packages",
             "~/.local/share/uv/python", "~/.local/share/mise/installs"]
hide = ["/home", "/root", "/run", "/mnt", "/media", "/var/tmp"]

[sandbox.login]
claude = ["~/.claude/.credentials.json"]
codex = ["~/.codex/auth.json"]

[sandbox.settings]
claude = []
codex = ["~/.codex/config.toml"]
```

A path that does not exist on a host is skipped. An unknown key, or a value that is not a list of
paths, refuses to build the sandbox.

To add a path (a tool installed somewhere else, a vendor that keeps its login in another file):

1. Name the narrowest path that works: a directory of binaries, or one file. Never a directory
   that also holds a login, a token, a history or anything the owner's next session would run
   (`~/.claude`, `~/.codex`, `~/.config`, `~`). A test of the engine refuses those.
2. Write the reason on its line.
3. Run `scripts/implement.sh doctor`. The `sandbox` line must stay `PASS`.

The table is a protected path and part of the safety surface: a builder changes it only when an
issue names it, and a fault-repair run never does.

## The environment

One scrub serves agents and checks (`sandbox.clean_env`). It removes:

- every `AWS_*`, `AMAZON_*`, `CDK_DEFAULT_*`, `GOOGLE_*`, `GCLOUD_*` and `AZURE_*` variable;
- the GitHub tokens and the SSH agent's socket;
- the handles of the launching session (`VSCODE_*`, the D-Bus and display sockets, the WSL
  interop socket, the launching Claude Code session's own socket and port);
- pointers to credentials kept elsewhere (`KUBECONFIG`, `DOCKER_HOST`, `DOCKER_CONFIG`, `NETRC`,
  `BOTO_CONFIG`, `PGPASSFILE`);
- every variable named like a secret (a token, a password, a key, a credential, a session key, a
  cookie, a nonce), whatever the letter case;
- every variable whose value has the shape of a credential, whatever its name: an AWS access key
  ID, a GitHub, Anthropic or OpenAI token, a private key, a URL that carries a password;
- each vendor's API-key variables, except in that vendor's own calls.

It then sets `AWS_EC2_METADATA_DISABLED=true`, `AWS_CONFIG_FILE=/dev/null` and
`AWS_SHARED_CREDENTIALS_FILE=/dev/null`, points `GH_CONFIG_DIR` at an empty directory, and
switches off git's prompts and credential helpers.

This is a list of what is removed, not of what is kept: the tools read too many settings from
their environment for the reverse. A secret in a variable whose name says nothing and whose value
has no recognisable shape passes. Launch the loop from a shell that holds none.

## The engine's own git

The engine commits, merges and pushes outside the sandbox, in worktrees an agent wrote to. For
the length of a run its git therefore takes no hook from the repository (`core.hooksPath` is
pinned to an empty directory the engine owns), starts no file-system monitor and uses no `ext::`
transport, whatever a worktree's or an agent's configuration says. Without that, the builder's
own copy of `.githooks/pre-commit` would run as the owner at the engine's next commit.

So the hooks of `.githooks/` do not look at what the loop commits and pushes. The engine's own
gates take their place, each inside the sandbox:

- the standing checks (the hygiene scan of the tree) before every commit of something an agent
  wrote, a builder's round or an examiner's probe, and on every head that is verified;
- the history checks (the hygiene scan of every commit the branch adds to `main`, with each
  commit's message, author and committer) on every head that is verified, and once more
  immediately before every push.

One thing the hooks do on the owner's machine has no equivalent in a run: the scanner's
`local-value` rule, which reads the values the owner keeps outside the repository
([`tools/hygiene/README.md`](../../tools/hygiene/README.md)). Inside the sandbox that list does
not exist, by design, and a run starts no scanner outside it. A branch the loop pushes was never
compared with it.

## Checking it on a host

```sh
scripts/implement.sh doctor            # the `sandbox`, `commit identity` and `git config` lines
scripts/implement.sh doctor --canary   # everything the canary tried and saw
scripts/implement.sh doctor --smoke    # also one small real call to each vendor, inside the sandbox
```

The canary starts a probe inside the real sandbox three times (as a check command, as a Claude
call, as a Codex call) with stand-in credentials planted in its environment, and fails the
doctor if the probe:

- can list or read `~/.aws`, the `gh` login directory, `~/.ssh`, or a credentials file the
  launching environment names, at its own place or through any other mount of the same files
  (on WSL the root filesystem is mounted a second time under `/mnt`);
- finds a planted variable, or any `AWS_*` variable other than the three that switch the SDKs'
  credential sources off;
- cannot write to its worktree, or leaves a marker in the engine's directory, the repository,
  the home directory or `/usr`;
- brings a hidden directory back: by unmounting it, from the sandbox or from a user namespace
  of its own where it is root, or by binding the filesystem a second time;
- as one vendor, finds the other vendor's login, or does not find its own;
- on WSL, reaches a Windows drive or the interop socket.

On the host this was written on (WSL2, bubblewrap 0.11.1, Claude Code 2.1.322, Codex CLI
0.167.0), on 2026-11-12:

```text
PASS  sandbox   bubblewrap 0.11.1; hidden: ~/.aws, the gh login, ~/.ssh; no credential variable;
                only the worktree is writable; unmount and second bind refused
```

and, from `--canary`, for the check command (the two vendor profiles differ only in the login
they find):

```text
"visible": {}                 nothing of the credential paths could be read
"env": []                     none of the 19 planted variables arrived
"direct_umount": {"/home": "EPERM", "~": "EPERM", "/mnt": "EPERM", "/run": "EPERM", ...}
"nested": {"attempts": {"umount ~": "EINVAL", "umount /home": "EINVAL", ...,
                        "bind / without its submounts": "EINVAL",
                        "remount /usr read-write": "EINVAL", "remount ~ read-write": "EPERM"},
           "visible": {}}
"windows": []
"tools": {"bash": true, "git": true, "node": true, "python3": true, "uv": true}
```

A look by hand, with the engine's own launcher and scrub (it prints what a check command sees):

```sh
python3 - <<'EOF'
import os, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, "tools/implementation-loop")
from implement_loop import sandbox
work = Path(tempfile.mkdtemp())
look = 'ls ~/.aws ~/.config/gh ~/.ssh; ls -A ~; env | grep "^AWS_"; touch here && echo "worktree: writable"; touch /usr/x'
argv = sandbox.Bubblewrap().command(["bash", "-c", look], work, mounts=sandbox.Mounts(writable=(work,)))
subprocess.run(argv, cwd=work, env=sandbox.clean_env(os.environ, work / "no-gh"), check=False)
EOF
```

Both vendors were also made to use their tools inside it, each with one small call. Claude's
`Grep` worked on the checkout, its `Read` found no `~/.ssh/known_hosts`, and it declined to
open `~/.aws/config`, citing this repository's rule. Codex, in its read-only mode, ran
`ls ~/.aws` and `ls ~/.config/gh ~/.ssh` (no such file or directory, three times),
`env | grep -c "^AWS_"` (3: the three settings above) and `touch probe.txt` (read-only file
system).

## The vendor's login

A vendor's own calls must be able to read that vendor's login, and to save a refreshed token.
Only the login file is bound, and a bound file has one limit: it can be rewritten in place, but
not replaced by another file (a rename onto it fails with `EBUSY`).

- Codex rewrites `~/.codex/auth.json` in place. A refresh inside the sandbox reaches the real
  file.
- Claude Code replaces `~/.claude/.credentials.json` with a new file when it refreshes outside a
  sandbox. On a bound file it falls back to writing in place: observed for its `~/.claude.json`
  with a stand-in file and no login, **not** observed for the login file across a real refresh.
  A call that started before the owner's own session refreshed the login keeps reading the old
  file until it ends.

If a long run ever ends with Claude calls failing to authenticate, this is the first place to
look. The arrangement that has no file to refresh is a long-lived token: `claude setup-token`
once, and `CLAUDE_CODE_OAUTH_TOKEN` in the shell that launches the loop. The scrub passes that
variable to Claude's own calls and to nothing else.

## What is not protected

Stated plainly, from ADR 0001's consequences and from what building it showed.

- **Network egress is open.** An agent can send out whatever it can read: the repository, the
  issue text, and the login of the vendor it runs as. The network namespace is the host's, so
  everything listening on `localhost` (a local database, an editor's helper ports, a model
  server) and every abstract Unix socket is reachable from inside. On a cloud machine that
  includes the instance metadata service: the variable that disables it is a default an agent
  can unset. Do not run the loop on a machine whose instance role can do anything.
- **It is a namespace sandbox on a shared kernel, not a virtual machine.** A kernel bug, or a
  bubblewrap bug, is an escape.
- **The engine and the supervisor run outside it**, as the owner, with the `gh` login. Until the
  factory has an identity of its own, that login reaches every repository of the owner.
- **The vendor's own login is readable and writable by that vendor's calls.** It has to be. An
  agent can copy it out, or log its CLI into another account.
- **The rest of the root filesystem is readable.** `/etc`, `/usr`, `/opt`, `/var` and `/srv` are
  there, read-only. A credential kept outside the home directory and outside the hidden
  directories is visible; the canary knows only the locations listed above, and the files the
  launching environment names.
- **The repository's git configuration is readable.** Agents see `.git/config`, or a copy of it.
  The doctor's `git config` line fails when it carries a credential (a token in a remote URL, an
  extra header).
- **Two calls see the main checkout.** The doctor's smoke call and the supervisor's reachability
  call run there, read-only, with a fixed prompt: untracked and ignored files (a `.env`, a
  `cdk.out/`), and the run state under `.git/implementation-loop/`, are readable by them. Keep
  no credential in the checkout. Every other call works in a worktree of tracked files.
- **The environment scrub goes by names and shapes** (above).
- **The owner's own list of values is not consulted** for what the loop pushes (above).
- **A hygiene marker is not a gate.** `hygiene: allow <rule> (<reason>)` on a line lets one value
  past the scan, also when a builder writes it. Only the examiner questions it; its prompt says
  to.
- **What is merged runs outside.** In CI (without secrets), in the pipeline, and on the owner's
  machine. `.github/`, `.githooks/`, the gate scripts and the agents' own configuration
  (`.claude/`, `.agents/`, `.codex/`: a hook or a setting there runs in the owner's next
  interactive session) are protected paths for that reason. Protection means an examiner looked,
  or the issue asked; it is not a sandbox.
- **The sandbox says nothing about AWS.** A merged change is deployed by a pipeline with broad
  rights in the account. The limits there are the policy tests, the protected pipeline path and
  what the system design puts on the AWS side.
