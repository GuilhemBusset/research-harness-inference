"""Inline images in the remaining standard Responses input-item variants."""

from __future__ import annotations

from typing import Any

import pytest

from test_issue64_responses_probe import IMAGE, forward


@pytest.mark.parametrize(
    "item",
    [
        {
            "type": "computer_call_output",
            "call_id": "call-1",
            "output": {"type": "computer_screenshot", "image_url": IMAGE},
        },
        {
            "type": "code_interpreter_call",
            "id": "ci_probe",
            "container_id": "example-container",
            "code": "draw_plot()",
            "status": "completed",
            "outputs": [{"type": "image", "url": IMAGE}],
        },
    ],
)
def test_inline_images_in_other_standard_input_items(item: dict[str, Any]) -> None:
    key = "outputs" if "outputs" in item else "output"
    assert forward(input=[item])["input"][0][key] == item[key]
