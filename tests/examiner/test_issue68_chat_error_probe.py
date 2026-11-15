"""An engine error must not become a clean end just because usage was observed."""

from __future__ import annotations

import pytest

from rhi_gateway.usage.chat import ChatStreamUsage, Usage


@pytest.mark.parametrize("asked", [False, True])
def test_error_after_observed_usage_is_not_clean(asked: bool) -> None:
    reader = ChatStreamUsage(client_asked_for_usage=asked)
    reader.feed(
        b'data: {"choices":[{"delta":{"content":"partial"}}],'
        b'"usage":{"prompt_tokens":12,"completion_tokens":7}}\n\n'
    )
    assert reader.observed == Usage(12, 7, False)
    reader.feed(b'data: {"error":{"type":"server_error","message":"overloaded"}}\n\n')
    reader.feed(b"data: [DONE]\n\n")
    reader.finish()
    assert reader.observed == Usage(12, 7, False)
