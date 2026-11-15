"""The key issuer's function: two routes behind the load balancer (system design, section 6.4).

| Route | Takes | Answers |
| --- | --- | --- |
| `GET /auth/config` | nothing | `{"client_id"}`: the login app's client ID, and nothing else |
| `POST /auth/login` | `{"device_code", "installation"}` | `{"key", "issued_at", "expires_at", "previous_issued_at"}` |

The CLI reads the client ID here because it is not in the repository, starts a device flow with
GitHub, and sends the device code with the identifier of its installation. A login redeems the
code (`rhi_issuer.redeem`) and issues a key under the rules of an issuance (`rhi_issuer.rules`):
one key per installation, two installations, a daily limit. The key is in that one answer and
nowhere else. The three times are whole seconds since the epoch, and `previous_issued_at` is
`null` for a student's first key.

Every other answer is `{"error": {"code", "message"}}`, with a code and a sentence that are
constants of this package: a `rhi_issuer.redeem.Refusal`, or `not_found` for any other path or
method. A body that is not the JSON object above, with both members of the right shape, is
refused before any call to GitHub or to a table.

What the function reads from its environment, which the stack sets:

- `RHI_CLIENT_ID`, the login app's client ID;
- `RHI_COURSE_END`, the course end date, as YYYY-MM-DD;
- `RHI_ROSTER_TABLE` and `RHI_KEYS_TABLE`, the names of the two tables;
- `RHI_ISSUANCES_PER_DAY`, the most keys a student may be issued over one UTC day, 5 when unset.

The function is the target of a load balancer, with multi-value headers off: `handler` takes the
load balancer's event (`httpMethod`, `path`, `body`, `isBase64Encoded`) and returns its answer
(`statusCode`, `headers`, `body`, `isBase64Encoded`). This module logs nothing.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import functools
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import boto3

from rhi_issuer.redeem import GitHub, Refusal, Refused, is_device_code, is_installation, redeem
from rhi_issuer.rules import DEFAULT_ISSUANCES_PER_DAY, issue

CLIENT_ID_VARIABLE = "RHI_CLIENT_ID"
COURSE_END_VARIABLE = "RHI_COURSE_END"
ROSTER_TABLE_VARIABLE = "RHI_ROSTER_TABLE"
KEYS_TABLE_VARIABLE = "RHI_KEYS_TABLE"
ISSUANCES_PER_DAY_VARIABLE = "RHI_ISSUANCES_PER_DAY"

CONFIG_PATH = "/auth/config"
LOGIN_PATH = "/auth/login"

type Answer = dict[str, Any]


def _answer(status: int, body: Mapping[str, object]) -> Answer:
    return {
        "statusCode": status,
        # An answer may hold a key: nothing between the function and the CLI keeps it.
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def _error(status: int, code: str, sentence: str) -> Answer:
    return _answer(status, {"error": {"code": code, "message": sentence}})


def _login_request(event: Mapping[str, Any]) -> tuple[str, str]:
    """The device code and the installation's identifier of a login; `Refused` for any other body."""
    body = event.get("body")
    try:
        if event.get("isBase64Encoded") and isinstance(body, str):
            body = base64.b64decode(body, validate=True).decode("utf-8")
        request = json.loads(body) if isinstance(body, str) else None
    except (binascii.Error, ValueError, RecursionError):
        request = None
    if not isinstance(request, dict):
        raise Refused(Refusal.INVALID_REQUEST)
    device_code, installation = request.get("device_code"), request.get("installation")
    if not is_device_code(device_code) or not is_installation(installation):
        raise Refused(Refusal.INVALID_REQUEST)
    return device_code, installation


@dataclass(frozen=True)
class Issuer:
    """The issuer, with what its environment gives it.

    `roster` and `keys` are the two tables, as table resources of the AWS SDK.
    `issuances_per_day` is the most keys a student may be issued over one UTC day. `github` and
    `clock` are for the tests.
    """

    client_id: str
    course_end: datetime.date
    roster: Any
    keys: Any
    issuances_per_day: int = DEFAULT_ISSUANCES_PER_DAY
    github: GitHub = field(default_factory=GitHub)
    clock: Callable[[], float] = time.time

    def answer(self, event: Mapping[str, Any]) -> Answer:
        """The load balancer's answer to one of its events."""
        route = (event.get("httpMethod"), event.get("path"))
        if route == ("GET", CONFIG_PATH):
            return _answer(200, {"client_id": self.client_id})
        if route == ("POST", LOGIN_PATH):
            try:
                return _answer(200, self._login(event))
            except Refused as refused:
                refusal = refused.refusal
            return _error(refusal.status, refusal.value, refusal.sentence)
        return _error(404, "not_found", "The issuer has no such route.")

    def _login(self, event: Mapping[str, Any]) -> dict[str, object]:
        device_code, installation = _login_request(event)
        entry = redeem(device_code, client_id=self.client_id, roster=self.roster, github=self.github)
        minted = issue(
            entry,
            installation,
            course_end=self.course_end,
            now=int(self.clock()),
            keys=self.keys,
            issuances_per_day=self.issuances_per_day,
        )
        return {
            "key": minted.key,
            "issued_at": minted.issued_at,
            "expires_at": minted.expires_at,
            "previous_issued_at": minted.previous_issued_at,
        }


@functools.cache
def _dynamodb() -> Any:
    """The AWS SDK's DynamoDB resource, made once per execution environment."""
    return boto3.resource("dynamodb")


def from_environment(*, github: GitHub | None = None, clock: Callable[[], float] = time.time) -> Issuer:
    """The issuer its environment describes. A variable that is absent (except the daily limit),
    a course end that is not a date and a daily limit that is not a whole number above zero raise:
    the function then answers nothing."""
    limit = os.environ.get(ISSUANCES_PER_DAY_VARIABLE)
    issuances_per_day = DEFAULT_ISSUANCES_PER_DAY if limit is None else int(limit)
    if issuances_per_day < 1:
        raise ValueError(f"{ISSUANCES_PER_DAY_VARIABLE} is not a whole number above zero")
    dynamodb = _dynamodb()
    return Issuer(
        client_id=os.environ[CLIENT_ID_VARIABLE],
        course_end=datetime.date.fromisoformat(os.environ[COURSE_END_VARIABLE]),
        roster=dynamodb.Table(os.environ[ROSTER_TABLE_VARIABLE]),
        keys=dynamodb.Table(os.environ[KEYS_TABLE_VARIABLE]),
        issuances_per_day=issuances_per_day,
        github=github or GitHub(),
        clock=clock,
    )


def handler(event: Mapping[str, Any], context: object) -> Answer:
    """The function's entry point: one event of the load balancer, one answer."""
    return from_environment().answer(event)
