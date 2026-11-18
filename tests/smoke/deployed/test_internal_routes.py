"""The forbidden routes through the internal load balancer (system design, sections 4.1 and 5.3).

The engine answers routes such as `/invocations` and `/pause` without its secret, so the internal
load balancer is the control: its listener forwards the four inference routes and answers a fixed
404 to everything else, the engine's health route included, which only the target groups' health
checks call (`rhi.pools.routing`). The requests name the pool as the gateway does, in the header
`X-Rhi-Pool`, so that nothing but the path keeps them from the pool's engines.
"""

from __future__ import annotations

from typing import Any

import pytest

# The header the internal load balancer chooses a pool by (`rhi_gateway.proxy`).
POOL_HEADER = "X-Rhi-Pool"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("POST", "/invocations", marks=pytest.mark.smoke_id("internal-invocations")),
        pytest.param("POST", "/pause", marks=pytest.mark.smoke_id("internal-pause")),
        pytest.param("POST", "/tokenize", marks=pytest.mark.smoke_id("internal-tokenize")),
        pytest.param("GET", "/metrics", marks=pytest.mark.smoke_id("internal-metrics")),
        pytest.param("GET", "/health", marks=pytest.mark.smoke_id("internal-health")),
    ],
)
def test_the_internal_load_balancer_answers_a_forbidden_route_with_its_404(
    stack: Any, internal_endpoint: str, method: str, path: str
) -> None:
    answer = stack.http.request(method, internal_endpoint + path, headers={POOL_HEADER: stack.pool})
    assert answer.status_code == 404
