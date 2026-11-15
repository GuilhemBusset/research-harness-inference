"""The API key format: a prefix, 40 random characters and a checksum (`rhi_common.keys`).

Every key here is generated when the test runs: no key is written in the repository.
"""

from __future__ import annotations

import hashlib
import random
import re
import string
import zlib

from rhi_common import keys

# The 62 letters and digits, written here from the standard library and not from the module.
DIGITS = string.digits + string.ascii_uppercase + string.ascii_lowercase
BATCH = 50


def generated(count: int = BATCH) -> list[str]:
    return [keys.generate_key() for _ in range(count)]


def test_a_key_is_the_prefix_forty_letters_or_digits_and_the_crc32_of_both() -> None:
    for key in generated():
        assert len(key) == 50
        assert key.startswith("rhi_")
        assert set(key[4:]) <= set(DIGITS)
        # The last six characters, read as a base-62 number, are the CRC-32 of the first 44.
        encoded = 0
        for character in key[44:]:
            encoded = encoded * 62 + DIGITS.index(character)
        assert encoded == zlib.crc32(key[:44].encode("ascii"))


def test_keys_come_from_the_operating_system_and_not_from_the_seedable_generator() -> None:
    batch = generated(200)
    assert len(set(batch)) == len(batch)
    # Every letter and digit turns up in 8000 random characters.
    assert set("".join(key[4:44] for key in batch)) == set(DIGITS)
    # The same seed gives the same sequence from `random`: a key that drew on it would repeat.
    random.seed(50)
    first = keys.generate_key()
    random.seed(50)
    assert keys.generate_key() != first


def test_every_generated_key_verifies() -> None:
    for key in generated():
        assert keys.is_valid_key(key)


def test_a_key_with_any_one_character_changed_fails() -> None:
    for key in generated(10):
        for position, original in enumerate(key):
            for replacement in DIGITS + "_-+/= ":
                if replacement != original:
                    changed = key[:position] + replacement + key[position + 1 :]
                    assert not keys.is_valid_key(changed), (position, replacement)


def test_a_candidate_of_the_wrong_shape_fails() -> None:
    key = keys.generate_key()
    for candidate in (
        "",
        key[:-1],
        key + key[-1],
        key[4:],
        "RHI_" + key[4:],
        " " + key,
        key + "\n",
        "Bearer " + key,
    ):
        assert not keys.is_valid_key(candidate), len(candidate)


def test_the_stored_form_is_the_sha256_of_the_key() -> None:
    first, second = generated(2)
    assert keys.key_hash(first) == hashlib.sha256(first.encode()).hexdigest()
    assert keys.key_hash(first) != keys.key_hash(second)
    assert first not in keys.key_hash(first)


def test_the_expression_matches_keys_only() -> None:
    expression = re.compile(keys.KEY_PATTERN)
    for key in generated():
        line = f'    client = Client(base_url=URL, api_key="{key}")  # from the course page'
        assert expression.findall(line) == [key]
        for length in range(len(key)):
            prefix = key[:length]
            assert expression.search(prefix) is None, length
            assert expression.search(line.replace(key, prefix)) is None, length
