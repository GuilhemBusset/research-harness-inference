"""The spikes' canary students: their roster entries, their keys and the spikes' secret (system design, section 12).

A spike reaches a pool through the gateway, as a student, and one student is not enough
(`rhi_common.spike_keys`). `run_spike_keys` is given a number of students. It puts that many on
the roster with the default budget, under the aliases `spike-1`, `spike-2` and so on, and one more
under `spike-spent` whose daily budget is 0, each under a GitHub ID no GitHub account has, unless
the entry is there already. It revokes each student's earlier keys, issues each a new one and
writes them all into the spikes' secret, `rhi/spike-keys`, which it creates when it does not exist. Run
again, it replaces every key, in the keys table and in the secret alike. The keys go to the secret
and nowhere else: nothing here returns, prints or logs one.

A run for fewer students than the run before leaves the other entries on the roster, each with a
key that no secret holds any more.
"""

from __future__ import annotations

import datetime
from typing import Any

from rhi_admin import keys as admin_keys
from rhi_admin.roster import AdminError, add, get
from rhi_common.spike_keys import SpikeKeys, write

SPENT_ALIAS = "spike-spent"
# GitHub numbers its accounts upward from 1 and is far below this: no student can have an ID from
# here on. The spent student has this one, and student n the nth after it.
SPIKE_GITHUB_ID_BASE = 10**18


def spike_alias(number: int) -> str:
    """The alias of the nth student with the default budget, counted from 1."""
    return f"spike-{number}"


def spike_github_id(number: int) -> int:
    """The GitHub ID of the nth student with the default budget, counted from 1; 0 is the spent student."""
    return SPIKE_GITHUB_ID_BASE + number


def run_spike_keys(students: int, *, roster: Any, keys: Any, secrets: Any, course_end: datetime.date, now: int) -> None:
    """Puts `students` students and the spent one on the roster, gives each one new key and writes
    the keys into the spikes' secret. Refused, before anything is written, for fewer than 1 student.

    `secrets` is a Secrets Manager client of the AWS SDK.
    """
    if type(students) is not int or students < 1:
        raise AdminError("the number of students is a whole number, 1 or more")

    def new_key(number: int, alias: str, budget: int | None) -> str:
        github_id = spike_github_id(number)
        if get(github_id, roster=roster) is None:
            add(github_id, alias, roster=roster, course_end=course_end, now=now, daily_token_budget=budget)
        admin_keys.revoke(github_id, keys=keys, course_end=course_end, now=now)
        return admin_keys.issue(github_id, roster=roster, keys=keys, course_end=course_end, now=now)

    issued = SpikeKeys(
        students=tuple(new_key(number, spike_alias(number), None) for number in range(1, students + 1)),
        spent=new_key(0, SPENT_ALIAS, 0),
    )
    write(secrets, issued)
