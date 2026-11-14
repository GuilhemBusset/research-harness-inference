+++
id = "E3-05"
title = "GPU metrics: the DCGM exporter 4.9 or later on localhost, scraped by the node's collector, and each node's zone"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E2-58", "E3-38"]
+++

## Context

The second unit of a GPU instance is the NVIDIA DCGM exporter, bound to localhost; the node's
collector scrapes it (section 4.3). The GPU panels read utilization of memory bandwidth and of
compute, memory, temperature, power and energy, and Xid errors (section 7.2). The exporter is pinned
at 4.9 or later: that release renamed the default metrics and changed their units, for instance
`DCGM_FI_DEV_GPU_UTIL_RATIO`, `DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL` and
`DCGM_FI_DEV_BOARD_POWER_WATTS`, and a 4.8 image still emits the old names (evidence E19). The
energy counter is what tokens per joule are computed from (section 7.2).

The unit lives in the folder of units that only a GPU pool's instances install (issue E3-38), so the
boot program needs no change and the mock pool never starts it. Installing a unit does not start it:
the boot program starts only the collector's and the lifecycle agent's units (issue E2-38). So the
same folder holds a drop-in for the collector's unit, `rhi-collector.service.d/dcgm.conf`, with
`Wants=rhi-dcgm.service`: on a GPU instance, starting the collector starts the exporter, and on the
mock pool neither file is installed. The image's digest is in the unit,
and the exporter version that digest is goes in a file of its own beside the fields, the way the
engine image records its version (issue E3-03), so that a test and the advisory check of issue E4-25
can read it. The node's collector configuration is the same file on every pool: on the mock pool its
scrape of the exporter finds nothing listening, which costs one series saying the target is down and
nothing else.

The same configuration gains one label. Spike S8 counts the zones a pool's instances are in, and its
role reads no EC2 API (issue E3-33), so the node's collector adds to every series the label zone,
the instance's availability zone read from the instance metadata, beside pool, instance and
instance_type (issue E2-58). It holds no identifier of the account: a zone's name is a region and a
letter.

## Deliverables

- [ ] `services/engine/units/gpu/rhi-dcgm.service` runs the DCGM exporter from an image pinned by digest, bound to localhost, and restarts on failure, and `services/engine/units/gpu/rhi-collector.service.d/dcgm.conf` makes the collector's unit want it, so that starting the collector starts the exporter.
- [ ] `services/engine/dcgm/VERSION` records the exporter version that digest is, 4.9.0 or later.
- [ ] `services/engine/dcgm/counters.csv` is the exporter's list of fields, naming one for each quantity of section 7.2's GPU row: utilization of memory bandwidth and of compute, memory, temperature, power, energy and Xid errors.
- [ ] `services/engine/collector/config.yaml` scrapes the exporter on localhost, and adds to every series of the node the label zone, the instance's availability zone read from the instance metadata.
- [ ] `tests/unit/engine/test_units.py` and `tests/unit/test_collectors.py` cover the new unit, the collector's drop-in, its version, its fields, the new scrape and the zone label, and `tests/unit/engine/test_boot.py` finds the exporter's unit and the drop-in installed on an instance with an NVIDIA device.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_units.py tests/unit/test_collectors.py` passes: the exporter listens on localhost only, its image has a digest, the version in `services/engine/dcgm/VERSION` is 4.9.0 or later, its fields include DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL and DCGM_FI_DEV_BOARD_POWER_WATTS, the collector has a scrape for it, and the collector's drop-in names rhi-dcgm.service under Wants.
- [ ] `tests/unit/test_collectors.py::test_every_series_says_where_it_comes_from` shows the node's configuration setting the label zone, beside pool, instance and instance_type, on every metrics pipeline.
- [ ] `tests/unit/engine/test_boot.py::test_the_mock_pool_gets_no_gpu_unit` passes with the exporter's unit and the collector's drop-in in the GPU folder: neither is installed on an instance whose PCI bus lists no NVIDIA device, and both are with one.
- [ ] On the coursework pool, GPU utilization, memory, power and energy appear in the workspace for each instance (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.3, 7.2
- `docs/design/evidence.md`, E19
