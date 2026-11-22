"""The keys of the spikes' canary students (system design, sections 6.2, 12 and 17).

A spike reaches a pool only through the gateway, as a student. One canary student is not enough:
a student may run 4 requests at once and the cache salt is per student, so a spike that replays
sessions gives each session a student of its own. So the spikes have one Secrets Manager secret of
their own, `SPIKE_KEYS_SECRET`, apart from the verification's canary secret. The admin CLI fills it
(`rhi_admin.spikes`), a spike reads it (`spikes/common.py`), and its content is decided here and
nowhere else:

    {"students": ["<key>", ...]}

- `students`: the keys of the spikes' students, one student each, at least one.

An earlier version also held, under `spent`, the key of a student with no budget. Until the
deployed secret is written again in the shape above, a value of that earlier shape is read too, and
its `spent` key is ignored.

`read` and `write` take a Secrets Manager client of the AWS SDK; this module imports no SDK. No
error here repeats a key or anything of the secret's value, and a `SpikeKeys` does not show its
keys when it is printed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Self

from rhi_common.keys import is_valid_key

SPIKE_KEYS_SECRET = "rhi/spike-keys"
_DESCRIPTION = "The keys of the canary students the validation spikes run as."


@dataclass(frozen=True)
class SpikeKeys:
    """The secret's content: the keys of the spikes' students."""

    students: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        keys = self.students
        if not keys or not all(isinstance(key, str) and is_valid_key(key) for key in keys):
            raise ValueError("the spikes' keys are not one valid key or more")
        if len(set(keys)) != len(keys):
            raise ValueError("two of the spikes' students have one key")

    def to_json(self) -> str:
        return json.dumps({"students": list(self.students)})

    @classmethod
    def from_json(cls, text: str) -> Self:
        """The content a secret's value holds. ValueError, which repeats nothing of it, for any other value."""
        try:
            held = json.loads(text)
        except ValueError:
            # Not raised from here: the parser's error would travel with it.
            held = None
        # The earlier shape's `spent` key is ignored (module docstring).
        shapes = ({"students"}, {"students", "spent"})
        if not isinstance(held, dict) or set(held) not in shapes or not isinstance(held["students"], list):
            raise ValueError("the spikes' secret does not hold the spikes' keys")
        return cls(students=tuple(held["students"]))


def read(secrets: Any) -> SpikeKeys:
    """The keys the spikes' secret holds."""
    return SpikeKeys.from_json(secrets.get_secret_value(SecretId=SPIKE_KEYS_SECRET).get("SecretString") or "")


def write(secrets: Any, keys: SpikeKeys) -> None:
    """Writes the keys as the secret's value, in place of what it held; creates the secret when there is none."""
    value = keys.to_json()
    try:
        secrets.create_secret(Name=SPIKE_KEYS_SECRET, Description=_DESCRIPTION, SecretString=value)
    except secrets.exceptions.ResourceExistsException:
        secrets.put_secret_value(SecretId=SPIKE_KEYS_SECRET, SecretString=value)
