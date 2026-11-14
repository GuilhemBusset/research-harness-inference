+++
id = "E2-33"
title = "Lifecycle agent: read the pool's release and settings, compare the release with what the instance holds, and act on the host"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16"]
+++

## Context

AWS leaves existing instances alone when a launch template changes, and user data runs only on first
boot (section 4.3; evidence E10). Without care, the stopped instance that serves every normal wake
would keep an old engine while the pipeline reports success. So at every start, including a restart
from the warm pool, the instance reads its pool's current release (engine image digest, model
revision, engine flags) from one parameter, and the lifecycle agent abandons the instance unless
what is on disk equals it.

At every start the boot unit fetches that release's files (the unit files and the lifecycle agent)
from the pipeline's artifact bucket and checks them against the digest recorded in the same
parameter (issue E2-38). That digest is therefore part of a release.

An instance has no other way to read the configuration, so the same parameter also carries what the
programs on the instance need and cannot know otherwise, outside the comparison: a new value changes
how the instance runs, not what it serves.

- The pool's measured start, once spike S1 has measured one, from which the agent sets the deadline
  of a promotion (issue E2-34).
- The instance's settings: the pool's name, its Auto Scaling group, the names of its launch and
  termination hooks, its log group, the workspace's remote-write address, the heartbeat table, and
  the ID of the pool's secret. The boot program writes them to the environment of the units, and the
  collector and the agent read them there (issues E2-38 and E2-70). The region comes from the
  instance metadata.

The parameter's value is one JSON object, which issue E2-42 writes and the boot program reads with
the machine image's own tools, so its keys are fixed here: `engine_image` (the image by repository
and digest), `model_revision`, `engine_flags` (a list), `files` (`bucket`, `key`, `sha256`),
`measured_start_seconds` (absent until measured), and `settings` (`pool`, `group`, `launch_hook`,
`termination_hook`, `log_group`, `workspace_url`, `heartbeat_table`, `secret_id`).

**What the agent runs on.** The repository's code is written for Python 3.12 and later (`ty.toml`),
and its libraries are bundled for Python 3.13, the version of `.python-version` (issue E1-16); the
machine images ship older ones (Ubuntu 22.04's 3.10 on the GPU image, Amazon Linux 2023's 3.9). So
the agent runs in a container of the official Python 3.13 image, pinned by its multi-platform digest,
which serves both processor architectures (issue E2-70). Its libraries are the lifecycle group of
issue E1-16 (the AWS SDK and prometheus-client); it adds none. The agent still acts
on the host's own units and container runtime: the engine is a systemd unit (section 4.3). It does so
through one module, which runs the host's `systemctl` and `docker` in the host's namespaces with
`nsenter`; its container is privileged and shares the host's process namespace for that. Every later
module of the agent acts on the host through it, so that the tests replace one module.

**Pulling from ECR.** The engine image is in the account's ECR (issue E2-42), and the host's docker
holds no credentials for it. So before a pull the host module asks ECR for an authorization token,
with the instance role, which may get one (issue E2-42), and logs the host's docker in to the image's
registry with it. The token reaches docker on its standard input, never in a command's arguments,
where any process of the host could read it.

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/release.py` defines a release (engine image digest, model revision, engine flags, and the SHA-256 digest of the release's files), reads the pool's current release from its one parameter, whose keys are those of Context, reads what the instance holds on disk, and says whether they are equal.
- [ ] It also reads from that parameter, and leaves out of the comparison, the measured start in seconds when the parameter gives one and the instance's settings of Context.
- [ ] A parameter that cannot be read, or holds something that is not a release, counts as not equal.
- [ ] `services/engine/lifecycle/rhi_lifecycle/host.py` pulls an engine image, and starts, restarts and stops the engine's unit rhi-engine, by running the host's own docker and systemctl through nsenter into the host's namespaces, and reports each command's failure to its caller.
- [ ] Before it pulls, `services/engine/lifecycle/rhi_lifecycle/host.py` logs the host's docker in to the image's ECR registry with an authorization token from ECR, given to docker on its standard input and never in a command's arguments.
- [ ] `tests/unit/lifecycle/test_release.py` covers the comparison with stubs for the parameter and the disk, and `tests/unit/lifecycle/test_host.py` covers the host's commands with stand-ins for nsenter and ECR.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_release.py` passes: an instance with the current release continues, one that differs in the image digest, in the model revision, in one flag or in the digest of its files is told to abandon, and a parameter that differs only in the measured start or in one setting is equal.
- [ ] `uv run --locked pytest tests/unit/lifecycle/test_host.py` passes: each of the four actions runs the expected command in the host's namespaces, a pull is preceded by a login to the image's registry whose token is in no command's arguments, and a command that fails is reported as failed.

## References

- `docs/design/system-design.md`, section 4.3
- `docs/design/evidence.md`, E10
