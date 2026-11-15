"""Independent behavioural evidence for issue 50; keys exist only at runtime."""

import binascii
import hashlib
import os
import re
import string
from unittest.mock import patch

from rhi_common import keys

ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase


def reference_key(body: str) -> str:
    unchecked = "rhi_" + body
    value = binascii.crc32(unchecked.encode("ascii"))
    suffix = ""
    for _ in range(6):
        value, digit = divmod(value, 62)
        suffix = ALPHABET[digit] + suffix
    return unchecked + suffix


def test_generation_has_the_required_format_and_crc() -> None:
    for _ in range(32):
        candidate = keys.generate_key()
        assert len(candidate) == 50
        assert candidate[:4] == "rhi_"
        assert all(character in ALPHABET for character in candidate[4:])
        assert candidate == reference_key(candidate[4:44])
        assert keys.is_valid_key(candidate)


def test_generation_reads_operating_system_entropy() -> None:
    with patch("random._urandom", wraps=os.urandom) as entropy:
        candidate = keys.generate_key()
    assert entropy.call_count > 0
    assert keys.is_valid_key(candidate)


def test_independent_keys_verify_and_all_single_ascii_changes_fail() -> None:
    for character in ("0", "A", "z"):
        candidate = reference_key(character * 40)
        assert keys.is_valid_key(candidate)
        for position in range(50):
            for codepoint in range(128):
                replacement = chr(codepoint)
                if replacement != candidate[position]:
                    mutated = candidate[:position] + replacement + candidate[position + 1 :]
                    assert not keys.is_valid_key(mutated), (position, codepoint)
        for malformed in ("", candidate[:-1], candidate + "0", " " + candidate, candidate + "\n"):
            assert not keys.is_valid_key(malformed)
        digest = keys.key_hash(candidate)
        assert digest == hashlib.sha256(candidate.encode("ascii")).hexdigest()
        assert re.fullmatch("[0-9a-f]{64}", digest)


def test_scanner_finds_whole_keys_embedded_in_text_and_no_proper_prefix() -> None:
    pattern = re.compile(keys.KEY_PATTERN)
    for _ in range(32):
        candidate = keys.generate_key()
        assert pattern.findall("before[" + candidate + "]after") == [candidate]
        assert pattern.findall("left" + candidate + "right") == [candidate]
        for length in range(50):
            prefix = candidate[:length]
            assert pattern.search(prefix) is None
            assert pattern.search("before[" + prefix + "]after") is None
