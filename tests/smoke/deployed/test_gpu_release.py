"""A GPU pool's release, verified after its instance refresh (system design, sections 4.1, 4.3 and
8.2, step 6).

When an execution deployed a GPU pool's release, the verification step runs the smoke tests once
more on the coursework pool, once the refresh has ended (`scripts/pipeline/gpu_verification.py`).
This test is that run's: it wakes the pool it is given with one streamed request, which the
gateway holds while the pool is cold, completes that stream, and then compares what the pool's
lifecycle agents reported, as the labels of `rhi_lifecycle_release_info`, with the image digest and
the model revision of the release the pool's parameter holds: the stopped instance that serves a
normal wake must not keep the old engine. The run on the mock pool leaves it out (`verify.py`).

**What was reported, and when.** An instant query of the metric returns each series' latest sample
for 5 minutes, unless a stale marker ends it sooner, and it does not say when that sample was
taken. So it cannot tell a report from before the request, of an instance that has stopped or that
the refresh replaced, from one made after the request and then marked stale. The test therefore
reads `count_over_time` of the metric over the time since the request: the series that have a
sample taken in that window, a stale marker being no sample, whenever it reaches the workspace.

The test fails as soon as any series sampled since the request carries another release. It passes
once a series sampled since the stream ended carries the deployed release, and one more read,
`SETTLE_SECONDS` later, still finds no other release: by then every sample taken until the first
read has reached the workspace.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from rhi_lifecycle.serving import RELEASE_METRIC

# Between a sample being taken and its arrival in the workspace: a scrape every 15 seconds, then a
# push.
SETTLE_SECONDS = 60.0
# The longest the deployed release is waited for, from the first read.
SERVED_SECONDS = 600.0
# Between two reads of the workspace.
READ_SECONDS = 10.0


@pytest.mark.smoke_id("gpu-release-served")
def test_the_woken_pool_completes_a_stream_and_serves_the_deployed_release(
    stack: Any, workspace: Any, deployed_release: tuple[str, str]
) -> None:
    selector = f'{RELEASE_METRIC}{{pool="{stack.pool}"}}'

    def reported(since: float) -> set[tuple[str | None, str | None]]:
        """The releases carried by the series of the pool with a sample taken after `since`, now."""
        now = time.time()
        window = int((now - since) * 1000)
        if window <= 0:
            return set()
        series = workspace.series(f"count_over_time({selector}[{window}ms])", at=now)
        return {(labels.get("image_digest"), labels.get("model_revision")) for labels in series}

    def assert_only_the_deployed_release() -> set[tuple[str | None, str | None]]:
        releases = reported(sent)
        others = sorted(str(one) for one in releases - {deployed_release})
        assert not others, f"an instance of the pool reported another release after the request: {others}"
        return releases

    sent = time.time()
    body = {"model": stack.pool, "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "Hello."}]}
    stream = stack.stream("/v1/messages", body)
    assert stream.status == 200, f"the pool's stream answered {stream.status}"
    assert stream.events and stream.events[-1].name == "message_stop", "the pool did not complete a stream"
    done = time.time()

    time.sleep(SETTLE_SECONDS)
    deadline = time.monotonic() + SERVED_SECONDS
    while True:
        assert_only_the_deployed_release()
        if deployed_release in reported(done):
            break
        assert time.monotonic() < deadline, "no lifecycle agent of the pool reported the deployed release"
        time.sleep(READ_SECONDS)
    time.sleep(SETTLE_SECONDS)
    assert_only_the_deployed_release()
