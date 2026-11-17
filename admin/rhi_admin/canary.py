"""The canary: the roster entry and key the pipeline's verification runs with (system design, section 8.2).

`run_canary` puts the canary on the roster under a fixed alias and a GitHub ID no GitHub account
has, unless it is there already, revokes the canary's earlier keys, issues a new one and writes it
into the Secrets Manager secret `rhi/canary-key`, which the verification step reads. The secret is
created when it does not exist, so the owner can run the command before the verification is
merged; run again, the command replaces the key in the secret and in the keys table alike. The
key goes to the secret and nowhere else: nothing here returns, prints or logs it.
"""

from __future__ import annotations

import datetime
from typing import Any

from botocore.exceptions import ClientError

from rhi_admin import keys as admin_keys
from rhi_admin.roster import add, get

CANARY_ALIAS = "canary"
# GitHub numbers its accounts from 1: no student can have this ID.
CANARY_GITHUB_ID = 0
CANARY_SECRET = "rhi/canary-key"
_DESCRIPTION = "The key of the canary roster entry, which the pipeline's verification uses."


def run_canary(*, roster: Any, keys: Any, secrets: Any, course_end: datetime.date, now: int) -> None:
    """Puts the canary on the roster, gives it one new key and writes that key into `CANARY_SECRET`.

    `secrets` is a Secrets Manager client of the AWS SDK.
    """
    if get(CANARY_GITHUB_ID, roster=roster) is None:
        add(CANARY_GITHUB_ID, CANARY_ALIAS, roster=roster, course_end=course_end, now=now)
    admin_keys.revoke(CANARY_GITHUB_ID, keys=keys, now=now)
    key = admin_keys.issue(CANARY_GITHUB_ID, roster=roster, keys=keys, course_end=course_end, now=now)
    try:
        secrets.create_secret(Name=CANARY_SECRET, Description=_DESCRIPTION, SecretString=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceExistsException":
            raise
        secrets.put_secret_value(SecretId=CANARY_SECRET, SecretString=key)
