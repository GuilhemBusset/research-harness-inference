"""The release the pool's instances serve is the one deployed (system design, sections 4.1 and 4.3).

A deployment does not reach an existing instance by itself: an instance holds the release it was
prepared with until it is replaced. Each instance's lifecycle agent reports the image digest and
the model revision it serves as the labels of `rhi_lifecycle_release_info`
(`rhi_lifecycle.serving`), and the pool's release parameter holds the release the pipeline
deployed (`rhi_lifecycle.release`). The test requires every such series of the pool to carry the
parameter's release, and at least one to exist.

A replaced instance's series is still returned for 5 minutes after its last sample, so the read is
repeated for longer than that before an older release counts as served.
"""

from __future__ import annotations

from typing import Any

import pytest

from rhi_lifecycle.serving import RELEASE_METRIC

# Longer than the time a series outlives its instance in an instant query.
SERVED_SECONDS = 600.0


@pytest.mark.smoke_id("release-served")
@pytest.mark.usefixtures("serving")
def test_the_pool_s_lifecycle_agents_report_the_deployed_release(
    stack: Any, workspace: Any, deployed_release: tuple[str, str]
) -> None:
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
