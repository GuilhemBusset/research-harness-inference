"""The gateway's and the pool's metrics, present in the workspace (system design, sections 4.1, 7.1 and 7.2).

The gateway's series are those it counts per student and pool (`rhi_gateway.metrics`), which its
task's collector pushes. The pool's are the quantities of the engine contract's metrics row, under
the names of the per-engine table (`rhi_common.engine_metrics`), with the label `pool` that the
instance's collector adds to every series (`services/engine/collector/config.yaml`).

A series is asked for by the name of one of its samples: a counter's ends in `_total`, and a
histogram is read by its `_count`.
"""

from __future__ import annotations

from typing import Any

import pytest

from rhi_common.engine_metrics import ENGINES, Kind, metric_names
from rhi_gateway import metrics as gateway_metrics

# How long the workspace is given to hold a series: several scrapes and pushes of a collector.
PRESENT_SECONDS = 300.0

_SAMPLE_SUFFIX = {Kind.GAUGE: "", Kind.COUNTER: "_total", Kind.HISTOGRAM: "_count"}


def selector(sample: str, pool: str) -> str:
    return f'{{__name__="{sample}",pool="{pool}"}}'


@pytest.mark.smoke_id("metrics-gateway")
@pytest.mark.usefixtures("serving")
def test_the_gateway_s_metrics_of_the_pool_are_in_the_workspace(stack: Any, workspace: Any) -> None:
    # What the stream of `serving` counted: one request, and the tokens of its settlement.
    families = (gateway_metrics.REQUESTS, gateway_metrics.INPUT_TOKENS, gateway_metrics.OUTPUT_TOKENS)
    selectors = [selector(f"{family}_total", stack.pool) for family in families]
    assert workspace.missing(selectors, seconds=PRESENT_SECONDS) == []


@pytest.mark.smoke_id("metrics-pool")
@pytest.mark.usefixtures("serving")
def test_the_pool_s_metrics_are_in_the_workspace_under_its_engine_s_names(stack: Any, workspace: Any) -> None:
    # The pool's engine is the one whose row is whole in the workspace for the pool. The first row
    # is given the time a series takes to arrive; by then every other row's series have arrived too.
    missing: dict[str, list[str]] = {}
    for engine in ENGINES:
        selectors = [
            selector(name + _SAMPLE_SUFFIX[quantity.kind], stack.pool)
            for quantity, name in metric_names(engine).items()
        ]
        missing[engine] = workspace.missing(selectors, seconds=0.0 if missing else PRESENT_SECONDS)
    assert [] in missing.values(), f"no engine's row of metric names is whole for the pool; missing: {missing}"
