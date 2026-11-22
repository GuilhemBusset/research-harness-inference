"""The GPU health dashboard: what each GPU of a pool's instances is doing, and whether it is well.

One of the six dashboards of the system design (section 7.3). Its numbers are the NVIDIA DCGM
exporter's, by the names of exporter 4.9 (section 7.2, evidence E19): the exporter of every GPU
instance exports the fields of its counters file, `services/engine/dcgm/counters.csv`, and the
node's collector adds the labels pool, instance and instance_type. Each name below is declared as
coming from that file, which names it.

One row for each pool of the configuration whose engine is not the mock: a mock instance has no
GPU, so its row would hold nothing. A row has seven panels:

| Panel | Field | Unit |
| --- | --- | --- |
| Utilization of memory bandwidth | `DCGM_FI_DEV_MEMORY_UTIL_RATIO` | A ratio |
| Utilization of compute | `DCGM_FI_DEV_GPU_UTIL_RATIO` | A ratio |
| Memory used | `DCGM_FI_DEV_FB_USED_BYTES` | Bytes |
| Temperature | `DCGM_FI_DEV_GPU_TEMP_CELSIUS` | Degrees Celsius |
| Power | `DCGM_FI_DEV_BOARD_POWER_WATTS` | Watts |
| Energy used | The rate of `DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL`, published as `..._total` | Joules a second |
| Xid errors | `DCGM_FI_DEV_XID_ERROR` | The code of the last Xid error |

Memory bandwidth comes first: a stream's speed is bounded by memory traffic, since each step of
the engine reads the weights once and the cached transcript of every stream (section 4.7).

**A gauge is shown for each GPU of each instance.** The exporter labels each GPU's series with its
index (`gpu`). An instance may hold more than one GPU, and a mean or a sum over them would hide the
one that is hot, idle or failing. An Xid error is a code, not a quantity: the series of one GPU
holds the last code it reported, and only that GPU's own series can say which code it was.

**The energy is a rate per series, then summed per instance** (`rules.rate`): the counter restarts
from zero when the driver is reloaded, and a rate over a sum would read that as a fall. Its series
in Prometheus is the field's name followed by `_total`: the node's collector gives every counter it
remote-writes Prometheus' counter suffix, and it takes only a lowercase `_total` for one already
there, so the exporter's `..._JOULES_TOTAL` reaches the workspace as `..._JOULES_TOTAL_total`. A
gauge keeps its name.

Every panel follows the rule of a pool at zero (`rules.engine_timeseries`): it reads "asleep"
while the scaler counts no instance in service, and never shows what a stopped instance last
reported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import dashboard as dashboard_builders

from rhi_common.engine_metrics import MOCK
from rhi_dashboards import rules

if TYPE_CHECKING:
    from grafana_foundation_sdk.builders import timeseries as timeseries_builders

    from rhi.config import Config
    from rhi_common.pool_config import Pool

UID = "gpu"
TITLE = "GPU health"

# The exporter's counters file, which names every field the exporter of a GPU instance exports.
COUNTERS = "services/engine/dcgm/counters.csv"

MEMORY_BANDWIDTH = "DCGM_FI_DEV_MEMORY_UTIL_RATIO"
COMPUTE = "DCGM_FI_DEV_GPU_UTIL_RATIO"
MEMORY_USED = "DCGM_FI_DEV_FB_USED_BYTES"
TEMPERATURE = "DCGM_FI_DEV_GPU_TEMP_CELSIUS"
POWER = "DCGM_FI_DEV_BOARD_POWER_WATTS"
ENERGY = "DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL"
# The energy counter's series, as the collector publishes it: the field's name and the counter's suffix.
ENERGY_SERIES = ENERGY + rules.COUNTER_SAMPLE
XID = "DCGM_FI_DEV_XID_ERROR"

# The label the collector gives every series of an instance (its ID), and the exporter's label for
# a GPU of the instance (its index).
INSTANCE = "instance"
GPU = "gpu"

# Each gauge, in the row's order: its panel's title and its unit, by Grafana's unit identifiers.
GAUGES: dict[str, tuple[str, str]] = {
    MEMORY_BANDWIDTH: ("Utilization of memory bandwidth, per GPU", "percentunit"),
    COMPUTE: ("Utilization of compute, per GPU", "percentunit"),
    MEMORY_USED: ("Memory used, per GPU", "bytes"),
    TEMPERATURE: ("Temperature, per GPU", "celsius"),
    POWER: ("Power, per GPU", "watt"),
}
ENERGY_TITLE = "Energy used, per instance: joules a second"
XID_TITLE = "Xid errors: the last code each GPU reported"

METRICS: tuple[rules.Declared, ...] = tuple(rules.EmittedBy(name, COUNTERS) for name in (*GAUGES, ENERGY, XID))

# A row is 24 columns wide: three panels on each of its first two lines, Xid errors on the third.
THIRD = 8
WHOLE = 24

_PER_GPU = f"{{{{{INSTANCE}}}}} GPU {{{{{GPU}}}}}"
_PER_INSTANCE = f"{{{{{INSTANCE}}}}}"


def _per_gpu(pool: Pool, metric: str) -> str:
    """One series for each GPU of each instance of the pool, whatever other label the exporter gives it."""
    return f"max by ({rules.POOL}, {INSTANCE}, {GPU}) ({rules.selector(metric, {rules.POOL: pool.name})})"


def gauge(pool: Pool, metric: str) -> timeseries_builders.Panel:
    """One of the exporter's gauges, for each GPU of each instance of the pool."""
    title, unit = GAUGES[metric]
    panel = rules.engine_timeseries(pool, title, {_PER_GPU: _per_gpu(pool, metric)}).unit(unit).min(0)
    return panel.max(1) if unit == "percentunit" else panel


def energy(pool: Pool) -> timeseries_builders.Panel:
    """The energy each instance of the pool uses: the counter's rate on each GPU, summed per instance."""
    used = rules.rate(ENERGY_SERIES, {rules.POOL: pool.name}, by=(INSTANCE,))
    return rules.engine_timeseries(pool, ENERGY_TITLE, {_PER_INSTANCE: used}).unit("watt").min(0)


def xid(pool: Pool) -> timeseries_builders.Panel:
    """The last Xid error each GPU of the pool's instances reported: a code, never added to another."""
    panel = rules.engine_timeseries(pool, XID_TITLE, {_PER_GPU: _per_gpu(pool, XID)})
    return panel.unit("none").decimals(0).min(0)


def gpu_pools(config: Config) -> list[Pool]:
    """The pools of the configuration that have a GPU: every one whose engine is not the mock."""
    return [pool for pool in config.pools if pool.engine != MOCK]


def dashboard(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard, with one row for each GPU pool of the configuration, in its order."""
    built = dashboard_builders.Dashboard(TITLE).uid(UID)
    for pool in gpu_pools(config):
        built.with_row(dashboard_builders.Row(pool.name))
        for metric in GAUGES:
            built.with_panel(gauge(pool, metric).span(THIRD))
        built.with_panel(energy(pool).span(THIRD))
        built.with_panel(xid(pool).span(WHOLE))
    return built
