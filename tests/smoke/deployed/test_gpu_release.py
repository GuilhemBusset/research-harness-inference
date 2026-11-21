"""A GPU pool's release, verified after its instance refresh (system design, sections 4.1, 4.3 and
8.2, step 6).

When an execution deployed a GPU pool's release, the verification step runs the smoke tests once
more on the coursework pool, once the refresh has ended (`scripts/pipeline/gpu_verification.py`).
This test is that run's: it wakes the pool it is given with one streamed request, which the
gateway holds while the pool is cold, completes that stream, and then requires every instance of
the pool to report, as the labels of `rhi_lifecycle_release_info`, the image digest and the model
revision of the release the pool's parameter holds: the stopped instance that serves a normal
wake must not keep the old engine. The run on the mock pool leaves it out (`verify.py`).

A replaced instance's series is still returned for 5 minutes after its last sample, so the read is
repeated for longer than that before an older release counts as served.
"""

from __future__ import annotations

from typing import Any

import pytest

from rhi_lifecycle.serving import RELEASE_METRIC

# Longer than the time a series outlives its instance in an instant query.
SERVED_SECONDS = 600.0


@pytest.mark.smoke_id("gpu-release-served")
def test_the_woken_pool_completes_a_stream_and_serves_the_deployed_release(
    stack: Any, workspace: Any, deployed_release: tuple[str, str]
) -> None:
    body = {"model": stack.pool, "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "Hello."}]}
    stream = stack.stream("/v1/messages", body)
    assert stream.status == 200, f"the pool's stream answered {stream.status}"
    assert stream.events and stream.events[-1].name == "message_stop", "the pool did not complete a stream"

    def reported(series: list[dict[str, str]]) -> set[tuple[str | None, str | None]]:
        return {(labels.get("image_digest"), labels.get("model_revision")) for labels in series}

    series = workspace.series_when(
        f'{RELEASE_METRIC}{{pool="{stack.pool}"}}',
        lambda series: reported(series) == {deployed_release},
        seconds=SERVED_SECONDS,
    )
    assert series, "no lifecycle agent of the pool reports a release"
    assert reported(series) == {deployed_release}, (
        "an instance of the pool serves another release than the deployed one"
    )
