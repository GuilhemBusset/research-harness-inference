+++
id = "E3-38"
title = "The engine's arguments reach its container, and the GPU units are installed on GPU instances only"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E2-33", "E2-38", "E2-70", "E2-89"]
+++

## Context

**The engine's flags.** A pool's release carries its engine flags, a list of arguments (issue E2-33),
and the engine's unit runs the container of the release's image (issue E2-70). The mock engine
takes no argument, so nothing yet carries the flags from the release to the container; a vllm pool
needs them (issue E3-04). So the boot program writes the release's flags where the engine's unit
reads them, and the unit gives them to the container as its arguments, in order. A release whose
list is empty, the mock pool's, starts its container exactly as before.

**The GPU units.** The engine's unit runs its container with the NVIDIA runtime on a GPU instance
(section 4.3). Every pool's instances fetch the same unit files, published as one asset with each
release (issues E2-70 and E2-42), and the mock pool's `t4g.small` has no GPU and no NVIDIA runtime: a
drop-in that asked for one there would stop the mock engine from starting, and the deployed stage's
verification would fail. So the GPU units live in a folder of their own, `services/engine/units/gpu/`,
which the boot program installs only on an instance that has an NVIDIA GPU. It tells by the PCI bus,
which lists the GPU whether or not the driver has finished loading: a device whose vendor file under
/sys/bus/pci/devices reads 0x10de, NVIDIA's vendor ID. The instance itself says so, so the release
needs no new key.
The DCGM exporter's unit joins the same folder (issue E3-05), and so does any later unit that only a
GPU instance runs.

**What the drop-in gives the engine's container.** Every GPU of the instance, through the NVIDIA
runtime, and AWS_REGION, from the RHI_REGION the boot program writes, which vLLM's Run:ai streamer
needs to reach the bucket. The engine's container already runs on the host's network on every pool
(issue E2-70), which lets the streamer reach the instance metadata for the instance role's
credentials, so the drop-in does not repeat it. The engine's unit starts the container with options
a drop-in can extend, so the drop-in adds these without restating the unit's command. A deployed
instance runs its units through the lifecycle agent's program (issue E2-89), which starts the
engine.

## Deliverables

- [ ] The boot program under `services/engine/boot/` writes the release's engine flags where `services/engine/units/rhi-engine.service` reads them, and that unit gives them to the engine's container as its arguments, in order; an empty list starts the container as before.
- [ ] `services/engine/units/rhi-engine.service` takes container options from a drop-in, and runs as before without one.
- [ ] `services/engine/units/gpu/rhi-engine.service.d/gpu.conf` gives the engine's container every GPU of the instance through the NVIDIA runtime and AWS_REGION from RHI_REGION, and nothing the engine's unit already gives it.
- [ ] The boot program installs the files of the GPU folder only on an instance whose PCI bus lists a device of NVIDIA's vendor ID 0x10de, and installs none of them otherwise.
- [ ] `tests/unit/engine/test_boot.py` and `tests/unit/engine/test_units.py` cover the flags, the drop-in and the boot program's choice.

## Acceptance criteria

- [ ] `tests/unit/engine/test_boot.py::test_the_mock_pool_gets_no_gpu_unit` runs the boot program in a temporary root whose PCI bus lists no NVIDIA device and finds no file of the GPU folder installed, and with one such device finds the drop-in installed.
- [ ] `tests/unit/engine/test_boot.py::test_the_engine_flags_reach_the_unit` runs the boot program with a release whose flags are three arguments and finds the engine's unit given those three, in order, and with an empty list finds none.
- [ ] `uv run --locked pytest tests/unit/engine/test_units.py tests/unit/engine/test_boot.py` passes, with the units and the boot program still naming no processor architecture.
- [ ] On the deployed mock pool, after this change, the engine still serves and the stage's verification passes (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.2, 4.3
- `docs/design/evidence.md`, E10
