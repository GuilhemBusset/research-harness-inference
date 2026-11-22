"""The students' privacy notice carries the sections the design names (section 6.6)."""

from __future__ import annotations

import re
from pathlib import Path

NOTICE = Path(__file__).resolve().parents[2] / "docs" / "privacy-notice.md"

HEADINGS = {
    "who is responsible",
    "what is recorded",
    "what is not recorded",
    "where it is kept",
    "for how long",
    "who can see it",
    "whether it counts toward grading",
    "how to ask for a copy or for deletion",
}


def test_privacy_notice_has_its_headings() -> None:
    text = NOTICE.read_text(encoding="utf-8")
    found = {m.strip().lower() for m in re.findall(r"^##\s+(.+)$", text, re.MULTILINE)}
    assert HEADINGS <= found, sorted(HEADINGS - found)


def test_privacy_notice_states_ninety_days() -> None:
    assert "90 days" in NOTICE.read_text(encoding="utf-8")
