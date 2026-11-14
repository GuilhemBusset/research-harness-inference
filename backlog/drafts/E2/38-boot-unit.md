+++
id = "E2-38"
title = "A boot unit that fetches the pool's release at every start and checks its digest"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-33"]
+++

## Context

User data runs only on an instance's first boot (section 4.3; evidence E10), so it holds no settings
and installs one thing: a small boot unit. At every start, including a restart from the warm pool,
that unit reads the pool's current release (engine image digest, model revision, engine flags) from
one parameter, fetches that release's unit files and lifecycle agent from the pipeline's artifact
bucket, checks them against the digest recorded in the same parameter, and starts them. The release,
with that digest and the instance's settings, is defined by issue E2-33 (the keys of the parameter's
JSON value) and written by issue E2-42; the units themselves are issue E2-70.

What the boot program needs and where it comes from:

- **Which parameter.** The launch template tags every instance with `rhi-pool`, the pool's name, and
  `rhi-release-parameter`, the name of the pool's release parameter, and lets the instance read its
  tags through the instance metadata (issue E2-71). The boot program reads that tag with IMDSv2, which
  the launch template requires; it needs no AWS call for it.
- **The region**, from the instance metadata.
- **Everything else** from the parameter: the files' bucket, key and SHA-256 digest, the engine
  image, and the settings (the pool, the group, the two hook names, the log group, the workspace's
  remote-write address, the heartbeat table, the secret's ID).

The boot program runs before any container and before the agent's Python (issue E2-33 says why that
Python is in a container). So it is a bash program that needs only what both machine images provide:
bash, coreutils, tar, curl, the AWS CLI, and the image's own python3 with its standard library,
which reads the parameter's JSON. The GPU image is AWS's base GPU image on Ubuntu 22.04 (section
4.3); the mock pool's is the arm64 image of issue E2-71, chosen to provide the same tools.

It writes the settings to `/run/rhi/release.env`, one `NAME=value` line each, which every unit reads
(issue E2-70): `RHI_POOL`, `RHI_REGION`, `RHI_RELEASE_PARAMETER`, `RHI_ENGINE_IMAGE`, `RHI_GROUP`,
`RHI_LAUNCH_HOOK`, `RHI_TERMINATION_HOOK`, `RHI_LOG_GROUP`, `RHI_WORKSPACE_URL`, `RHI_HEARTBEAT_TABLE`
and `RHI_SECRET_ID`, and `RHI_MEASURED_START` when the parameter gives one.

**The release's files are one tar archive** (issue E2-42), whose SHA-256 digest is that of its
bytes, so the program checks the archive as it was fetched, before it unpacks anything. At its root
the archive holds three things, which this issue fixes so that the boot program, the units and the
release agree: `units/`, the unit files of issue E2-70 (with any subfolder a later issue adds);
`collector/config.yaml`, the node's collector configuration of issue E2-58; and `agent/`, the
lifecycle agent with its libraries. It unpacks the archive under `/opt/rhi/release/` and installs
the unit files at the top of `units/` under `/run/systemd/system/`, which is emptied at every stop,
so that a start never runs the units of an earlier release. It then starts two units by name,
`rhi-collector` and `rhi-lifecycle`, which issue E2-70 writes; the agent starts the engine (issue
E2-34).

The same files run on two processor architectures: the mock pool's `t4g.small` is an arm64 type and
the GPU pools' types are x86_64 (section 4.2). The user data and the boot program name no
architecture.

## Deliverables

- [ ] `services/engine/boot/user-data.sh` installs `services/engine/boot/rhi-boot.service` and the boot program `services/engine/boot/rhi-boot.sh` and nothing else, and holds no version, digest, flag, setting or secret.
- [ ] The boot unit runs the boot program at every start, including a restart from the warm pool; the program reads the parameter named by the instance's tag rhi-release-parameter through IMDSv2, fetches the archive from the bucket and key the parameter names, and checks the SHA-256 digest of its bytes against the parameter's.
- [ ] When the digest matches, it unpacks the archive under /opt/rhi/release, writes /run/rhi/release.env with the variables of Context, installs the unit files at the top of the archive's folder units under /run/systemd/system, and starts the units rhi-collector and rhi-lifecycle.
- [ ] When the tag or the parameter cannot be read, the fetch fails or the digest differs, it starts none of the fetched units and exits with a failure.
- [ ] Neither the user data nor the boot program names a processor architecture, and the boot program uses only bash, coreutils, tar, curl, the AWS CLI and python3 with its standard library.
- [ ] `tests/unit/engine/test_boot.py` runs the boot program in a temporary root against stand-ins for the instance metadata, the AWS CLI and systemctl.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_boot.py` passes: with an archive whose digest the parameter names, the units rhi-collector and rhi-lifecycle are started and release.env holds the settings of the parameter; with one byte of the archive changed, or a parameter that cannot be read, no unit is started.
- [ ] `tests/unit/engine/test_boot.py::test_user_data_holds_no_settings` fails when `services/engine/boot/user-data.sh` holds a digest, a version, a model name or a secret.
- [ ] `tests/unit/engine/test_boot.py::test_no_architecture_is_named` fails when the user data or the boot program names arm64, aarch64, amd64 or x86_64.
- [ ] `bash -n services/engine/boot/rhi-boot.sh` exits 0.

## References

- `docs/design/system-design.md`, sections 4.2, 4.3
- `docs/design/evidence.md`, E10
