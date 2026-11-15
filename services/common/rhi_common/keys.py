"""The format of a student's API key (system design, sections 6.2 and 6.4).

A key is 50 characters: the prefix `rhi_`, 40 random characters among the 62 ASCII letters and
digits, and 6 characters that encode the CRC-32 of the 44 characters before them. The prefix and
the fixed length give it a shape a secret scanner can find in a homework branch; the checksum lets
the gateway refuse junk before any lookup. The issuer mints keys, the gateway verifies them and the
CLI stores them, so the format is decided here and nowhere else.

The checksum catches typing and copying mistakes; it is not a secret and proves nothing about who
made the key. Only the SHA-256 of a key is ever stored: `key_hash` is what the issuer writes and
what the gateway looks up.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import zlib

PREFIX = "rhi_"
# The 62 letters and digits, in ASCII order: the random part is drawn from them, and they are the
# digits of the checksum, a base-62 number.
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
RANDOM_LENGTH = 40
# Six base-62 digits hold every CRC-32 value: 62**6 is larger than 2**32.
CHECKSUM_LENGTH = 6
KEY_LENGTH = len(PREFIX) + RANDOM_LENGTH + CHECKSUM_LENGTH

# The one expression that matches every key and no shorter string, for secret scanners. It has no
# anchor, boundary or look-around, so it finds a key anywhere in a text and can be copied as it is
# into another regular expression engine. It describes the shape only: the checksum is checked by
# `is_valid_key`.
KEY_PATTERN = r"rhi_[0-9A-Za-z]{46}"

_KEY = re.compile(KEY_PATTERN, re.ASCII)


def _checksum(unchecked: str) -> str:
    """The CRC-32 of a key's prefix and random part, as six base-62 digits, the highest first."""
    value = zlib.crc32(unchecked.encode("ascii"))
    digits = []
    for _ in range(CHECKSUM_LENGTH):
        value, digit = divmod(value, len(ALPHABET))
        digits.append(ALPHABET[digit])
    return "".join(reversed(digits))


def generate_key() -> str:
    """A new key, its random part drawn from the operating system's random source."""
    unchecked = PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(RANDOM_LENGTH))
    return unchecked + _checksum(unchecked)


def is_valid_key(candidate: str) -> bool:
    """Whether the candidate has the shape of a key and carries its own checksum.

    It needs nothing but the string: no table, no network. A true answer says the string is
    well formed, not that the key was ever issued.
    """
    if _KEY.fullmatch(candidate) is None:
        return False
    return candidate[-CHECKSUM_LENGTH:] == _checksum(candidate[:-CHECKSUM_LENGTH])


def key_hash(key: str) -> str:
    """The SHA-256 of a key, in lowercase hexadecimal: the only form of a key that is stored."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
