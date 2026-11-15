"""Redeeming a device code: from GitHub's device flow to a roster entry (system design, section 6.4).

The CLI starts a device flow for the login app and sends the device code here. `redeem` exchanges
it with GitHub for a user token, under the app's client ID: GitHub refuses a code that another app
started, so nothing else has to check whose code it is. The flow needs no client secret. The token
is used once, to read the numeric ID of the user who confirmed the code, and is then dropped: it is
returned to nobody, written to no table and never logged, and this module logs nothing at all. The
roster is keyed by that ID, because a handle can be renamed.

Every way a login gives no key is one `Refusal`: a fixed code, an HTTP status and one sentence,
written here and nowhere else. Nothing GitHub or AWS says is repeated. The three codes of a device
code that is not redeemed yet are those of the device flow itself (RFC 8628, section 3.5), which
is what the CLI's polling already understands. A user outside the roster and a roster item that
cannot be read are refused alike, with a sentence that says nothing of who is on the roster.

The shapes of the device code and of the installation's identifier are checked before anything
else: `redeem` calls GitHub only for a device code of the right shape.

`Refused` is raised with no cause and no context, so a traceback shows nothing GitHub answered.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, TYPE_CHECKING, Any, Self, TypeGuard

from rhi_common.records import RecordError
from rhi_common.students import RosterEntry
from rhi_common.tables import ROSTER

if TYPE_CHECKING:
    from http.client import HTTPMessage

# The grant of the device flow (RFC 8628), which GitHub's token address takes.
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT_SECONDS = 10
# More of an answer than this is not read: GitHub's are far shorter.
ANSWER_BYTES = 65536

# GitHub documents a device code as 40 characters; the ones it gives are letters and digits.
_DEVICE_CODE = re.compile(r"[0-9A-Za-z]{40}", re.ASCII)
# The identifier the CLI makes at its first login: 16 to 64 letters, digits, `-` and `_`, which
# holds a UUID in either of its writings and a random URL-safe token.
_INSTALLATION = re.compile(r"[0-9A-Za-z_-]{16,64}", re.ASCII)


class Refusal(StrEnum):
    """Why a login gave no key; the value is the code the answer carries."""

    status: int
    sentence: str

    def __new__(cls, code: str, status: int, sentence: str) -> Self:
        refusal = str.__new__(cls, code)
        refusal._value_ = code
        refusal.status = status
        refusal.sentence = sentence
        return refusal

    INVALID_REQUEST = ("invalid_request", 400, "The login request is not well formed.")
    AUTHORIZATION_PENDING = (
        "authorization_pending",
        400,
        "The code has not been confirmed on GitHub yet: ask again after the interval.",
    )
    SLOW_DOWN = ("slow_down", 400, "The code was asked for too often: wait longer between two requests.")
    EXPIRED_TOKEN = ("expired_token", 400, "The code has expired: start the login again.")
    LOGIN_REFUSED = ("login_refused", 400, "GitHub did not accept the code: start the login again.")
    GITHUB_UNAVAILABLE = ("github_unavailable", 502, "GitHub could not be reached: try again later.")
    NOT_ENROLLED = ("not_enrolled", 403, "This GitHub account cannot log in to this endpoint.")
    # The rules of an issuance, applied by `rhi_issuer.rules`.
    TOO_MANY_INSTALLATIONS = (
        "too_many_installations",
        403,
        "This account already has keys on two installations: run rhi revoke --all, then log in again.",
    )
    TOO_MANY_ISSUANCES = (
        "too_many_issuances",
        429,
        "This account has been issued as many keys as it may today: log in again after midnight UTC.",
    )


# What GitHub answers for a device code it does not redeem yet, or any more. Any other error of
# GitHub's is `LOGIN_REFUSED`.
_NOT_REDEEMED = {
    "authorization_pending": Refusal.AUTHORIZATION_PENDING,
    "slow_down": Refusal.SLOW_DOWN,
    "expired_token": Refusal.EXPIRED_TOKEN,
}


class Refused(Exception):
    """A login that gives no key. It carries the refusal and nothing else."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal


@dataclass(frozen=True)
class GitHub:
    """The two addresses of GitHub a login calls. The tests give those of a stub."""

    token_url: str = "https://github.com/login/oauth/access_token"
    user_url: str = "https://api.github.com/user"


def is_device_code(value: object) -> TypeGuard[str]:
    """Whether the value has the shape of a device code of GitHub's."""
    return isinstance(value, str) and _DEVICE_CODE.fullmatch(value) is not None


def is_installation(value: object) -> TypeGuard[str]:
    """Whether the value has the shape of an installation's identifier."""
    return isinstance(value, str) and _INSTALLATION.fullmatch(value) is not None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure: a device code or a token is never sent on to another address."""

    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> None:
        return None


# No proxy from the environment either: the calls go to GitHub and nowhere else.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _call(url: str, *, form: dict[str, str] | None = None, bearer: str | None = None) -> tuple[int | None, Any]:
    """One call to GitHub: a POST of the form when there is one, a GET otherwise.

    It returns the HTTP status and the JSON of the answer, each `None` when there is none, and
    raises nothing: no error of urllib's, which holds GitHub's text, is the context of another.
    """
    headers = {"Accept": "application/json", "User-Agent": "rhi-issuer"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None if form is None else urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(url, data=data, method="GET" if data is None else "POST", headers=headers)
    response: Any = None
    try:
        response = _OPENER.open(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, http.client.HTTPException):
        pass
    if response is None:
        return None, None
    status = response.code if isinstance(response, urllib.error.HTTPError) else response.status
    try:
        with response:
            return status, json.loads(response.read(ANSWER_BYTES))
    except (OSError, http.client.HTTPException, ValueError):
        return status, None


def _user_id(device_code: str, client_id: str, github: GitHub) -> int | Refusal:
    """The numeric ID of the user who confirmed the device code, or why GitHub gives none.

    The user token lives in this function only: it goes to GitHub's user address and nowhere else.
    """
    form = {"client_id": client_id, "device_code": device_code, "grant_type": GRANT_TYPE}
    status, answer = _call(github.token_url, form=form)
    if not isinstance(answer, dict):
        return Refusal.GITHUB_UNAVAILABLE
    # GitHub answers 200 to a code it does not redeem, and says why in `error`.
    if "error" in answer:
        error = answer["error"]
        return _NOT_REDEEMED.get(error, Refusal.LOGIN_REFUSED) if isinstance(error, str) else Refusal.LOGIN_REFUSED
    token = answer.get("access_token")
    if status != http.client.OK or not (isinstance(token, str) and token):
        return Refusal.GITHUB_UNAVAILABLE
    status, user = _call(github.user_url, bearer=token)
    user_id = user.get("id") if isinstance(user, dict) else None
    if status != http.client.OK or isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return Refusal.GITHUB_UNAVAILABLE
    return user_id


def redeem(device_code: str, *, client_id: str, roster: Any, github: GitHub | None = None) -> RosterEntry:
    """The roster entry of the student who confirmed the device code; `Refused` for anyone else.

    `client_id` is the login app's. `roster` is the roster table, as the table resource of the AWS
    SDK (`boto3.resource("dynamodb").Table`). A device code of the wrong shape is refused before
    any call to GitHub, and the roster is read only for a user GitHub has named.
    """
    if not is_device_code(device_code):
        raise Refused(Refusal.INVALID_REQUEST)
    user_id = _user_id(device_code, client_id, github or GitHub())
    if isinstance(user_id, Refusal):
        raise Refused(user_id)
    # A consistent read: a student added a moment ago can log in, one removed a moment ago cannot.
    item = roster.get_item(Key={ROSTER.partition_key.name: user_id}, ConsistentRead=True).get("Item")
    entry = None
    if item is not None:
        try:
            entry = RosterEntry.from_item(item)
        except RecordError:
            # An item that is not a roster entry gives nobody a key.
            entry = None
    if entry is None:
        raise Refused(Refusal.NOT_ENROLLED)
    return entry
