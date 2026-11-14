+++
id = "E2-70"
title = "The units an instance runs: the engine, the collector and the lifecycle agent"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-38", "E2-58", "E2-89"]
+++

## Context

Each instance runs systemd units (section 4.3): the engine container, a metrics collector, and the
lifecycle agent; GPU pools add the DCGM exporter in phase 2 (issue E3-05). The boot program of issue
E2-38 fetches these unit files with the release, installs them, writes `/run/rhi/release.env` and
starts the collector and the agent. Every unit reads its settings from that file, never from the
repository: the variables are those issue E2-38 lists.

- **The engine** (`rhi-engine`) runs the release's engine image (`RHI_ENGINE_IMAGE`) on port 8000,
  the one listener of the instance that is not on localhost (section 4.1). Its bearer secret comes
  from Secrets Manager at start (`RHI_SECRET_ID`), never from user data (section 4.3), and its value
  is written to no file on the EBS volume. It does not start at boot: the agent starts it after the
  release check (section 5.2; issue E2-34).
- **The collector** (`rhi-collector`) runs an OpenTelemetry collector image pinned by digest, with the
  configuration of issue E2-58, which the release holds at `/opt/rhi/release/collector/config.yaml`
  (issue E2-38), and, in its environment, the pool's name, the workspace's remote-write
  address, the region and the pool's log group: `RHI_POOL`, `RHI_WORKSPACE_URL`, `RHI_REGION` and
  `RHI_LOG_GROUP`, the names issue E2-58's configuration reads.
- **The agent** (`rhi-lifecycle`) runs `python -m rhi_lifecycle`, the agent's program (issue E2-89),
  on Python 3.13, in a container of the official Python image pinned by its multi-platform digest
  (issue E2-33 says why), with the release's agent and its libraries mounted read-only from
  `/opt/rhi/release/agent/`, `/run/rhi` mounted read-write for the record of the current start
  (issue E2-89), the host's network (the instance metadata,
  the engine and its own metrics endpoint on localhost), and, privileged in the host's process
  namespace, the means to run the host's systemctl and docker (issue E2-33's host module). Its
  libraries are pure Python, so one release serves both architectures (issue E2-42).

**Networks and mounts.** The engine's and the collector's containers run on the host's network, as
the agent's does. The collector scrapes the engine at port 8000 and the agent at port 9110 on
localhost (issue E2-58), and the mock engine answers its metrics route to a client on the loopback
address only (issue E2-82): through Docker's bridge, a scrape would arrive from the bridge's address
and be refused. Port 8000 stays the instance's one listener off localhost. The collector also reads
the journal of the instance's units (issue E2-58), so its container mounts the host's journal
read-only: `/var/log/journal`, `/run/log/journal` and `/etc/machine-id`. Its journal receiver runs
journalctl, which the image the unit pins must therefore carry.

Docker's data and the engine's compile cache live on the EBS volume, not on the instance store, which
is wiped when an instance stops (section 4.3): the stopped warm instance keeps its pulled image and
its compile cache only if they are on the volume.

The same files run on the arm64 mock pool and the x86_64 GPU pools (section 4.2): no unit names an
architecture. The images they name by digest are multi-platform. The GPU drop-ins (the NVIDIA runtime,
DCGM) are phase 2's and are installed only on a GPU pool (issues E3-38 and E3-05).

## Deliverables

- [ ] `services/engine/units/rhi-engine.service` runs the engine container of RHI_ENGINE_IMAGE on port 8000 with the pool's secret read from Secrets Manager at start, handed to the container in the environment variable RHI_POOL_SECRET and kept off the EBS volume, is not enabled at boot, and restarts on failure.
- [ ] `services/engine/units/rhi-collector.service` runs the collector image pinned by digest with the configuration /opt/rhi/release/collector/config.yaml and RHI_POOL, RHI_WORKSPACE_URL, RHI_REGION and RHI_LOG_GROUP in its environment, and restarts on failure.
- [ ] `services/engine/units/rhi-lifecycle.service` runs python -m rhi_lifecycle from /opt/rhi/release/agent, mounted read-only, on the Python 3.13 image pinned by its multi-platform digest, with the host's network and /run/rhi mounted read-write, privileged in the host's process namespace, and restarts on failure.
- [ ] The engine's and the collector's containers run on the host's network, and the collector's container mounts the host's journal read-only: /var/log/journal, /run/log/journal and /etc/machine-id.
- [ ] Every unit reads /run/rhi/release.env, no unit names a processor architecture, and Docker's data directory and the engine's compile cache are on the EBS volume.
- [ ] `tests/unit/engine/test_units.py` reads the unit files.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_units.py` passes: the engine listens on port 8000, every other listener is on localhost, the three containers run on the host's network, the collector mounts the journal read-only, the agent's unit runs python -m rhi_lifecycle, every unit reads /run/rhi/release.env, the engine unit is not enabled at boot, and the data and cache directories are on the EBS volume.
- [ ] `tests/unit/engine/test_units.py::test_images_are_pinned` fails when a unit names an image without a digest, and shows the agent's image to be Python 3.13.
- [ ] `tests/unit/engine/test_units.py::test_no_architecture_is_named` fails when a unit file names arm64, aarch64, amd64 or x86_64.

## References

- `docs/design/system-design.md`, sections 4.1, 4.2, 4.3, 5.2
- `docs/design/evidence.md`, E10
