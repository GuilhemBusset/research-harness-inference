"""A GPU pool's release, verified after its instance refresh (system design, sections 4.1, 4.3 and
8.2, step 6).

When an execution deployed a GPU pool's release, the verification step runs the smoke tests once
more on the coursework pool, once the refresh has ended (`scripts/pipeline/gpu_verification.py`).
This test is that run's: it wakes the pool it is given with one streamed request, which the
gateway holds while the pool is cold, completes that stream, and then requires every instance of
the pool to report, as the labels of `rhi_lifecycle_release_info`, the image digest and the model
revision of the release the pool's parameter holds: the stopped instance that serves a normal
wake must not keep the old engine. The run on the mock pool leaves it out (`verify.py`).

An instant query still returns a series for 5 minutes after its last sample, so a read made soon
after the wake can hold only what a stopped instance reported before it stopped, or an instance
the refresh replaced. A read is taken as the release served only once `FRESH_SECONDS` have passed
since the stream ended, when every series it returns has been sampled after the wake, and the read
is repeated until `SERVED_SECONDS` have passed before an older release counts as served.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from rhi_lifecycle.serving import RELEASE_METRIC

# The time a series outlives its last sample in an instant query, and two scrapes of 15 seconds.
FRESH_SECONDS = 330.0
# The longest the read is repeated: longer than `FRESH_SECONDS`.
SERVED_SECONDS = 600.0


@pytest.mark.smoke_id("gpu-release-served")
def test_the_woken_pool_completes_a_stream_and_serves_the_deployed_release(
    stack: Any, workspace: Any, deployed_release: tuple[str, str]
) -> None:
    body = {"model": stack.pool, "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "Hello."}]}
    stream = stack.stream("/v1/messages", body)
    assert stream.status == 200, f"the pool's stream answered {stream.status}"
    assert stream.events and stream.events[-1].name == "message_stop", "the pool did not complete a stream"
    fresh = time.monotonic() + FRESH_SECONDS

    def reported(series: list[dict[str, str]]) -> set[tuple[str | None, str | None]]:
        return {(labels.get("image_digest"), labels.get("model_revision")) for labels in series}

    series = workspace.series_when(
        f'{RELEASE_METRIC}{{pool="{stack.pool}"}}',
        lambda series: time.monotonic() >= fresh and reported(series) == {deployed_release},
        seconds=SERVED_SECONDS,
    )
    assert time.monotonic() >= fresh, "the release was read before the reports from before the wake had gone"
    assert series, "no lifecycle agent of the pool reports a release"
    assert reported(series) == {deployed_release}, (
        "an instance of the pool serves another release than the deployed one"
    )
