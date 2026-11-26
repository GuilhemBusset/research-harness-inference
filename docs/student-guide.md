# Student guide: the course's inference endpoint

During class sessions, the course runs an open-weight model on its own GPU machines. You reach it
from Claude Code, Codex, OpenCode, Pi or a script of your own, through one small command, `rhi`. This guide
covers installing `rhi`, logging in, pointing your tools at the endpoint, and what to do when a
request fails.

> **For the instructor:** before you hand this guide to students, replace OWNER and REPOSITORY in
> the install command below with the GitHub owner and the name of this repository.

## 1. Install the CLI

You need uv, the Python tool manager (install it as its own documentation says). Then run this
one command:

```sh
uv tool install "git+https://github.com/OWNER/REPOSITORY#subdirectory=client"
```

It installs the `rhi` command on your `PATH`. Do this on each machine you work on (at most two, see
below).

## 2. Log in

The endpoint's address comes once, from the course's private channel. Give it to your first login:

```sh
rhi login --endpoint https://inference.example.com
```

`rhi` stores the address, so later logins are just `rhi login`. Keep the address in the course's
channel: do not post it anywhere public.

The login uses your GitHub account. `rhi` prints a short code and a GitHub address. You open that
address in your browser and enter the code. When GitHub confirms it, the endpoint checks that you
are on the course roster and gives your machine a key. `rhi` stores the key in your operating
system's keychain, or in a file only you can read, and never prints it.

Three warnings:

- **Confirm only a code that your own terminal has just printed.** Someone who sends you a code
  (by message, by e-mail, as a "classmate") and gets you to confirm it receives a key in your
  name. Never confirm a code you did not ask for.
- **Each login tells you when your previous key was issued.** If that time is not one of your own
  logins, someone else got a key in your name: run `rhi revoke --all` at once (section 10) and
  tell the instructor.
- **You can have at most two installations**, a laptop and a desktop, say. Each holds one key. A
  new login on an installation replaces that installation's key, and the old key stops working
  within 60 seconds. The number of keys you can be issued in a day is also limited, so do not log
  in again and again to fix a problem: read section 8 first.

Your key expires at the end of the course.

## 3. Point your harness at the endpoint

### Claude Code

`rhi login` sets up Claude Code for you. In Claude Code's user settings (`~/.claude/settings.json`)
it sets the endpoint's address (`ANTHROPIC_BASE_URL`) and a key helper (`apiKeyHelper`) that runs
`rhi token` whenever Claude Code needs the key. The key itself is not written into the file.

After the login, start Claude Code as usual:

```sh
claude
```

Anthropic does not support Claude Code with models other than its own. The course checks that it
completes a task on the coursework model (section 7), but expect rough edges.

### Codex

`rhi login` writes a Codex profile named `rhi` (`~/.codex/rhi.config.toml`): the endpoint, the
default model, and `rhi token` as the command that gives Codex the key. Use Codex with that
profile:

```sh
codex --profile rhi
```

Always pass `--profile rhi`: without it, Codex does not know the endpoint. The profile sets no
timeout, so Codex keeps its own. Your own `config.toml` is never changed by `rhi login`.

### An SDK script

A script of your own reads the key from `rhi token` each time it starts, and never keeps it in the
code or in a file. With the OpenAI Python SDK:

```python
import subprocess

from openai import OpenAI

key = subprocess.run(["rhi", "token"], capture_output=True, text=True, check=True).stdout.strip()
client = OpenAI(base_url="https://inference.example.com/v1", api_key=key)
reply = client.chat.completions.create(
    model="coursework",
    messages=[{"role": "user", "content": "Say hello."}],
)
print(reply.choices[0].message.content)
```

Use the endpoint's address from the course's channel, followed by `/v1`.

### OpenCode

`rhi login` does not write OpenCode's settings: you add the endpoint as a provider yourself. Put
this entry in OpenCode's configuration file, `~/.config/opencode/opencode.json` (if the file
already exists, add the `rhi` provider to its `providers`):

```json
{
  "providers": {
    "rhi": {
      "name": "Course endpoint",
      "env": ["RHI_API_KEY"],
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "https://inference.example.com/v1"
      },
      "models": {
        "coursework": {
          "name": "Coursework model"
        }
      }
    }
  }
}
```

Replace `https://inference.example.com` with the endpoint's address from the course's channel, and
keep the `/v1`. The model's name, `coursework`, is the default pool's name, as `rhi status` lists it
(section 4).

The entry holds no key: it names the environment variable OpenCode reads the key from,
`RHI_API_KEY`. OpenCode reads that variable in the server process that sends the requests, and
OpenCode's shared background server, if one is already running, did not start from your shell and
does not see it. So set the variable from `rhi token` in the shell that starts OpenCode, then start
OpenCode in that same shell in standalone mode, which runs a private server from this shell, and
choose `rhi/coursework` with `/models`:

```sh
export RHI_API_KEY="$(rhi token)"
opencode --standalone
```

The key stays in that shell's memory and is never written into a file. Do not put the `export` line
in a shell startup file, and do not give the key to the shared background server with
`opencode service set env`: it would keep the key beyond your shell. After a new login, the old key
stops working: set the variable again from `rhi token` and restart OpenCode.

Spike S3 (section 7) has not yet run OpenCode, so the course has no tested version of it yet.

### Pi

`rhi login` does not write Pi's settings either. Pi reads providers from its `models.json`, in its
agent folder, `~/.pi/agent/models.json`. Put this entry in it (if the file already exists, add the
`rhi` provider to its `providers`):

```json
{
  "providers": {
    "rhi": {
      "baseUrl": "https://inference.example.com/v1",
      "api": "openai-completions",
      "apiKey": "!rhi token",
      "models": [
        { "id": "coursework" }
      ]
    }
  }
}
```

Replace `https://inference.example.com` with the endpoint's address from the course's channel, and
keep the `/v1`. `openai-completions` is Pi's name for OpenAI Chat Completions, which the endpoint
serves. The model's name, `coursework`, is the default pool's name, as `rhi status` lists it
(section 4).

The entry holds no key. The leading `!` of `apiKey` tells Pi to run a command for the key: Pi runs
`rhi token` each time it sends a request, so the key is in no file, and after a new login Pi uses
the new key with nothing to change. Start Pi as usual and choose `coursework` with `/model`.

Spike S3 (section 7) has not yet run Pi, so the course has no tested version of it yet.

## 4. Choose a model

Each model the endpoint serves is a **pool** of GPU machines, and you choose a pool by its model
name. `rhi status` lists the pools your key may use, by name:

```sh
rhi status
```

Any model name that starts with `claude-` goes to the default pool, the coursework model. This is
what Claude Code sends unless you tell it otherwise, so it needs no setting. The Codex profile
names the default pool's model. The OpenCode and Pi entries name it too (section 3). In an SDK
script, give the model's name as `model`.

## 5. The endpoint serves during class sessions only

The model serves students only during class sessions. A session is a booking of one pool, with a
start, an end and a number of GPU machines, and the instructor books each one ahead of the class.
The machines are asked for when the session starts, so that they are there when you arrive. The
endpoint has no timetable of the sessions to come: while a pool's session is on, `rhi status`
shows when it ends.

Sessions are booked pool by pool, so one pool can have a session on while another has none. A
request to a pool that has no session on gets its answer at once: an error with the cause
`no_session`. `rhi status` shows that pool as unavailable with that cause, whatever the other
pools show. When the coursework pool is the one with no session on, `rhi wait coursework` stops
with the same cause. Nothing you send starts a session or a machine. The rest of `rhi` works at
any time: log in before your first session, check `rhi status`, and revoke a key whenever you need
to. A request already running when its pool's session ends is allowed to finish.

For homework, between sessions, and whenever the endpoint is down or not ready, use your own
subscriptions to hosted models. The endpoint does not stand in for them.

## 6. Waiting for a session's machines

A pool answers once one of its session's machines is in service. A machine takes an estimated 3 to
13 minutes to start, and when no GPU machine is free in the cloud, the pool waits for one, which
can take half an hour or more. That is why the instructor books a session early. When you arrive
before the machines all the same, the pool is in one of two situations:

- a machine is starting: the pool's state is `warming`;
- the pool is waiting for a GPU machine to become available: its state is `unavailable`, with the
  cause `capacity_unavailable`. The pool keeps asking until it has its machines.

The endpoint keeps no request waiting. A request to a pool that cannot serve yet gets its answer at
once: an error with the cause `warming` or `capacity_unavailable`. Claude Code, Codex and the SDKs
retry such an error a few times by themselves, which covers the last moments of a start and
nothing longer. A token count, which Claude Code sends often to measure its context, gets the same
answer, and Claude Code carries on without it. No request, and no command of `rhi`, starts a
machine or makes one come faster.

So before you start your harness, wait until the pool is ready:

```sh
rhi wait coursework
```

The command reads the pool's state every 10 seconds, prints it each time it changes, and returns
when the pool is ready. It starts nothing: it only waits. It keeps waiting while the pool is
`warming` or waits for a machine, stops with the cause `no_session` when the pool has no session
on, and gives up after 20 minutes. Pass `--timeout` with a number of minutes to change that:

```sh
rhi wait coursework --timeout 45
```

`rhi status` shows each pool in one of three states, and, while its session is on, when the
session ends:

| State | Meaning |
| --- | --- |
| `ready` | The session is on and a machine is in service: the pool answers now |
| `warming` | The session is on and a machine is starting |
| `unavailable` | The pool cannot answer, and `rhi status` prints the cause (section 8): `capacity_unavailable` when the session is on and the pool waits for a GPU machine, `no_session` when the pool has no session on |

## 7. What the coursework model cannot do here

The endpoint runs an open-weight model behind its own gateway, not your harness vendor's service.
A few things your harness does elsewhere do not work here:

| What | Why | What to do instead |
| --- | --- | --- |
| Reading a PDF | Claude Code sends a PDF in a form the model cannot read. The endpoint replaces it with a line saying it cannot read the document | Convert the PDF to text first. You can ask the agent to do it: for example, "convert paper.pdf to text with a command-line tool and read the text file" |
| Claude Code's built-in web search and web fetch | They are run by Anthropic's own service, not by Claude Code on your machine, so they do not work through this endpoint | The course's science skills search public services from your machine, and they work here. You can also search in your browser and paste what you found |
| Running several agents at once | Each agent, and each subagent, takes a stream on the same shared GPU as the rest of the class, and counts toward your concurrent requests (section 9) | Run agents one after another, or limit how many subagents run at once. This leaves room on the GPU for your classmates |

**Harness versions.** The endpoint changes the requests each harness sends so that the model can
serve them, and a harness update can change those requests. These are the versions the course
tests the endpoint with: spike S3 installs exactly these, gives each harness a task of the course's
kind on the coursework model, and runs the task's check on what the harness left:

| Harness | Version |
| --- | --- |
| Claude Code | 2.1.323 |
| Codex | 0.169.0 |
| OpenAI Python SDK, for the SDK script | 3.57.0 |
| OpenCode | Not yet run by spike S3 |
| Pi | Not yet run by spike S3 |

The spike stops if an installed harness reports another version, and each run prints the versions
it used in its log. What those runs show decides which harnesses each model supports, and the
instructor tells you which those are. With a newer version, something may fail that worked before. If it
does, tell the instructor which version you run (`claude --version`, `codex --version`).

## 8. When a request fails: the causes

Every error the endpoint produces names one cause from this list, with a fixed sentence. When you
ask for help, give the cause's name. Never paste your key.

| Cause | What it means | What to do |
| --- | --- | --- |
| `invalid_key` | The key is missing, malformed, unknown, revoked or expired, or the request carried two different keys | Run `rhi login`. If you set an API key of your own for this harness (in an environment variable, say), remove it, so that only the key from `rhi token` is sent |
| `not_found` | The request went to an address the endpoint does not serve | Check the endpoint's address: Claude Code takes it as given, Codex, OpenCode, Pi and SDK scripts take it followed by `/v1`. Run `rhi login` again to rewrite the settings of Claude Code and Codex; for OpenCode and Pi, check the base URL of their entry (section 3) |
| `request_refused` | The endpoint refused the body of the request: not JSON, too large, or an image given by URL instead of inline | Send images inline, and split very large inputs. If a harness does this on its own, tell the instructor which harness and version |
| `concurrency_limit` | You already have as many requests running as you are allowed (section 9) | Wait for one to end: harnesses and SDKs retry by themselves. Run fewer agents at once |
| `no_session` | No session is on for this pool (section 5) | Use your own subscription to a hosted model. The pool answers again during the next session the instructor books |
| `warming` | The pool's session is on and a machine is starting, but none is in service yet (section 6) | Run `rhi wait coursework`, with the model's name, then try again |
| `capacity_unavailable` | The pool's session is on, and the pool waits for a GPU machine to become available (section 6) | Run `rhi wait coursework`, with the model's name: the pool keeps asking for its machines. If it lasts, use your own subscription for this session |
| `engine_error` | The model failed, or ended its answer early | Try again. If it keeps happening, start a fresh session of your harness and tell the instructor |
| `time_limit` | The request reached the 30-minute limit | Split the work into smaller requests |
| `context_length_exceeded` | The conversation has grown longer than the model's context | Nothing, in Claude Code: it compacts the session by itself and goes on. In another harness, compact the session (its own command for it), or start a new one |

## 9. The concurrency limit

You can have 4 requests running at once, unless the instructor tells you otherwise. Every agent and
subagent you run counts, on all your installations together. A request over the limit is refused
with `concurrency_limit`, and harnesses and SDKs retry it after a few seconds.

## 10. Keeping, revoking and replacing your key

Keys start with `rhi_`, so that scanners recognise one that leaks.

- **Never commit a key**, never paste it into a settings file, an issue, a chat or a script. The
  harnesses get it from `rhi token` when they need it; leave it there.
- **To stop using an installation** (before you sell or return a machine, say), revoke its key:

  ```sh
  rhi revoke
  ```

- **If a key is lost or leaked**, or a login shows a key you did not ask for, revoke every key of
  yours. This needs no key: it asks you to confirm a fresh code with GitHub, as a login does.

  ```sh
  rhi revoke --all
  ```

  On a machine where `rhi` has never logged in, give the endpoint's address as well:
  `rhi revoke --all --endpoint https://inference.example.com`.

- **To replace a key**, log in again with `rhi login`. The new key replaces that installation's old
  one.

A revoked key stops working within 60 seconds. After `rhi revoke --all`, run `rhi login` on each
installation you still use.

## 11. Privacy

What the endpoint records about you, where, for how long, and how to ask for a copy or for deletion
is in the [privacy notice](privacy-notice.md). The instructor gives you the signed copy. Your
prompts and the model's answers are not stored.
