"""A cached-value caller must not erase a wake completed by an in-flight readiness cycle."""

from __future__ import annotations

from typing import Any

from rhi_gateway.readiness import Readiness
from rhi_gateway.waker import Waker


def test_expiry_lookup_preserves_completion_observed_before_expiry() -> None:
    now = 1000.0
    waker: Waker | None = None

    def clock() -> float:
        return now

    class Health:
        def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
            return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}]}

    class Table:
        def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
            nonlocal now
            # Health already answered at 1599. A table read delays publication past expiry;
            # meanwhile a demand-report caller asks for the cached pending count.
            now = 1601.0
            assert waker is not None
            waker.pending("mock")
            return {}

    readiness = Readiness({"mock": "group-mock"}, elbv2=Health(), table=Table(), clock=clock)
    waker = Waker(readiness, lambda_client=object(), function_name="scaler", clock=clock)
    waker.record("mock")
    now = 1599.0
    readiness.refresh()
    assert waker.pending("mock") == 0
    assert waker.ended_ready_at("mock") == 1599.0, "readiness preceded expiry, so this wake ended ready"
