"""A GPU pool's release, verified after its instance refresh (system design, sections 4.1, 4.3 and
8.2, step 6).

When an execution deployed a GPU pool's release, the verification step runs the smoke tests once
more on the coursework pool, once the refresh has ended (`scripts/pipeline/gpu_verification.py`).
This test is that run's: it sends the pool it is given one streamed request, completes that
stream, and then compares what the pool's lifecycle agents reported, as the labels of
`rhi_lifecycle_release_info`, with the image digest and the model revision of the release the
pool's parameter holds: the stopped instance that serves a normal wake must not keep the old
engine. The run on the mock pool leaves it out (`verify.py`).

**Which reports count.** Every report made since the run began (`run_started`), which is after
the refresh ended. In a run of the whole folder, the stream that wakes the pool is
`../test_smoke.py`'s first, which the gateway holds while the pool is cold, and this test's stream
comes later, to a pool in service already: the reports made during the wake count as much as
those made after this test's stream. A run of this test alone wakes the pool with its own stream.

**What was reported, and when.** An instant query of the metric returns each series' latest sample
for 5 minutes, unless a stale marker ends it sooner, and it does not say when that sample was
taken. So it cannot tell a report from before the run, of an instance that has stopped or that
the refresh replaced, from one made during the run and then marked stale. The test therefore
reads `count_over_time` of the metric over the time since the run began: the series that have a
sample taken in that window, a stale marker being no sample, whenever it reaches the workspace.

The test fails as soon as any series sampled since the run began carries another release. It
passes once a series sampled since its stream ended carries the deployed release, and one more
read, `DELIVERY_SECONDS` later, still finds no other release: by then every sample taken until the
first read has reached the workspace, a push the collector retried included.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from rhi_lifecycle.serving import RELEASE_METRIC

# Before the first read: a scrape every 15 seconds, then a push.
SETTLE_SECONDS = 60.0
# Between a sample being taken and its arrival in the workspace, at the longest: a scrape every 15
# seconds, then a push that the collector's remote-write exporter retries for up to 5 minutes (its
# default `retry_on_failure`, which `services/engine/collector/config.yaml` keeps).
DELIVERY_SECONDS = 360.0
# The longest the deployed release is waited for, from the first read.
SERVED_SECONDS = 600.0
# Between two reads of the workspace.
READ_SECONDS = 10.0


@pytest.mark.smoke_id("gpu-release-served")
def test_the_woken_pool_completes_a_stream_and_serves_the_deployed_release(
    stack: Any, workspace: Any, deployed_release: tuple[str, str], run_started: float
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

    def assert_only_the_deployed_release() -> None:
        others = sorted(str(one) for one in reported(run_started) - {deployed_release})
        assert not others, f"an instance of the pool reported another release during the run: {others}"

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
    time.sleep(DELIVERY_SECONDS)
    assert_only_the_deployed_release()
