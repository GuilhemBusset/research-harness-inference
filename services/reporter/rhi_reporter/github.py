"""The reporter's GitHub client: it creates commit statuses, as a GitHub App installation.

The App can write commit statuses on one repository and nothing else (system design, section 8.4;
ADR 0001, amendment 4), and creating a status is the only call here that writes to GitHub: there
is none for a commit comment, an issue or a pull request. The one call that reads lists the
statuses of a commit (`statuses`), with the same installation token.

What the client reads, and from where (the construct and the owner's walkthrough share the names):

- the secret's name, from the environment variable `RHI_REPORTER_SECRET`;
- the App's ID, its installation ID and its private key, from the fields `app_id`,
  `installation_id` and `private_key` of that secret's JSON value;
- the repository, as owner/name, from the environment variable `RHI_REPORTER_REPOSITORY`.

The secret is read once, by the first status of an execution environment, and kept. The owner
creates it after the first deployment, so it can be absent: that, a value that is not the three
fields and a key that cannot sign all raise `CredentialError`, which carries nothing AWS said.
A read that failed is not kept: the next status reads again.
`has_credential` reads the secret the same way and only says whether it holds the three fields,
so that the function can post nothing at all while it does not.

A status is posted with an installation token, which GitHub gives for a token the App's key
signed. The installation token stays in memory until less than five minutes of its validity
remain, then a new one is asked for.

**Temporary refusals.** A call given a deadline, a time on the client's clock, is made again while
GitHub's answer is one it asks to be waited out: 429, a 403 that carries `retry-after` or an
`x-ratelimit-remaining` of 0 (a secondary rate limit), a 5xx, or no answer at all. The next attempt
begins after the seconds `retry-after` gives, else at the time `x-ratelimit-reset` gives, else
after `FIRST_WAIT_SECONDS`, doubled at each attempt up to `LONGEST_WAIT_SECONDS`. Any other refusal,
any refusal of a call given no deadline, and one whose next attempt would begin after the deadline
raise `RefusedError` at once, without waiting: a longer wait is left to whoever invokes the caller
again. No attempt begins after the deadline.

The module logs nothing, and no error holds the key, a token or a response's text: `RefusedError`
names the HTTP status and one of GitHub's error codes. Both errors are raised with no cause and no
context, so a traceback shows neither AWS's message nor GitHub's response. The AWS SDK logs what
Secrets Manager answers, the key included, at the debug level: debug logging is off while the
secret is read, whatever level the function runs at.
"""

from __future__ import annotations

import datetime
import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any

import boto3
import jwt
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from collections.abc import Callable
    from email.message import Message
    from http.client import HTTPMessage

SECRET_VARIABLE = "RHI_REPORTER_SECRET"
REPOSITORY_VARIABLE = "RHI_REPORTER_REPOSITORY"
API_URL = "https://api.github.com"

# A token is renewed once less than this much of its validity remains.
RENEW_BEFORE_SECONDS = 300
# The App token only buys the installation token: GitHub allows ten minutes, and a minute of
# clock drift in the past.
APP_TOKEN_SECONDS = 540
TIMEOUT_SECONDS = 10
# More of an answer than this is not read: GitHub's are far shorter.
ANSWER_BYTES = 65536
# A page of a commit's statuses, each with its creator's record, is read whole up to this size,
# and one that is longer is a refusal: a page is never read in part.
PAGE_BYTES = 8 * 1024 * 1024
STATUSES_PER_PAGE = 100
# The wait before the next attempt when GitHub's answer gives no time: doubled at each attempt.
FIRST_WAIT_SECONDS = 1.0
LONGEST_WAIT_SECONDS = 8.0
# The `code` values GitHub documents for a refused request. Anything else is not repeated.
ERROR_CODES = frozenset({"missing", "missing_field", "invalid", "already_exists", "unprocessable", "custom"})

_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/(?!\.{1,2}$)[A-Za-z0-9._-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_NEXT_PAGE = re.compile(r'<([^>]+)>;\s*rel="next"')


class CredentialError(Exception):
    """The App's credential cannot be used: the secret is absent, lacks a field, or its key does
    not sign. The text is fixed."""

    def __init__(self) -> None:
        super().__init__("the reporter's GitHub credential cannot be read from its secret")


class RefusedError(Exception):
    """GitHub did not accept a call. `status` is the HTTP status, None when there was no answer;
    `code` is GitHub's error code, None when the answer has none of `ERROR_CODES`."""

    def __init__(self, status: int | None, code: str | None) -> None:
        self.status = status
        self.code = code
        if status is None:
            text = "GitHub did not answer"
        else:
            text = f"GitHub refused the call: HTTP {status}" + (f", code {code}" if code else "")
        super().__init__(text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a refusal: a token is never sent on to another address."""

    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> None:
        return None


# No proxy from the environment either: the calls go to GitHub's API and nowhere else.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


@dataclass(frozen=True)
class _Credential:
    app_id: str
    installation_id: str
    private_key: str


def _number(value: object) -> str | None:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    return text if isinstance(text, str) and text.isascii() and text.isdigit() else None


def _error_code(answer: object) -> str | None:
    errors = answer.get("errors") if isinstance(answer, dict) else None
    for error in errors if isinstance(errors, list) else []:
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code in ERROR_CODES:
            return code
    return None


def _seconds(value: str | None) -> int | None:
    text = value.strip() if isinstance(value, str) else ""
    return int(text) if text.isascii() and text.isdigit() else None


def _waited_out(status: int | None, headers: Message | None) -> bool:
    """Whether GitHub's answer is one it asks to be waited out: no answer, a 5xx, a 429, or a 403
    of a rate limit."""
    if status is None or status == 429 or 500 <= status < 600:
        return True
    if status != 403 or headers is None:
        return False
    return headers.get("retry-after") is not None or (headers.get("x-ratelimit-remaining") or "").strip() == "0"


def _wait_of(headers: Message | None, now: float, attempt: int) -> float:
    """The seconds before the next attempt, the `attempt`-th made again: what `retry-after` gives,
    else until the time `x-ratelimit-reset` gives when it is still to come, else the doubling wait."""
    after = _seconds(headers.get("retry-after")) if headers is not None else None
    if after is not None:
        return float(after)
    reset = _seconds(headers.get("x-ratelimit-reset")) if headers is not None else None
    if reset is not None and reset > now:
        return reset - now
    return min(FIRST_WAIT_SECONDS * 2 ** (attempt - 1), LONGEST_WAIT_SECONDS)


def _status_of(item: object) -> dict[str, Any] | None:
    """A commit's status as the client gives it: its id, context, state and description, the
    description empty when GitHub gives none. None for an item of another shape."""
    if not isinstance(item, dict):
        return None
    number, context, state, description = (item.get(key) for key in ("id", "context", "state", "description"))
    if type(number) is not int or not isinstance(context, str) or not isinstance(state, str):
        return None
    if description is not None and not isinstance(description, str):
        return None
    return {"id": number, "context": context, "state": state, "description": description or ""}


def _read_credential(secrets: Any) -> _Credential:
    """The App's credential, from the secret `RHI_REPORTER_SECRET` names, read with `secrets` or,
    without one, a client the function's credentials and region make. `CredentialError` when the
    secret cannot be read or lacks one of the three fields."""
    name = os.environ.get(SECRET_VARIABLE)
    # The SDK's debug log repeats the answer, which holds the key or AWS's message.
    quiet = logging.root.manager.disable
    logging.disable(max(quiet, logging.DEBUG))
    try:
        client = boto3.client("secretsmanager") if secrets is None else secrets
        fields = json.loads(client.get_secret_value(SecretId=name)["SecretString"]) if name else None
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
        fields = None
    finally:
        logging.disable(quiet)
    if not isinstance(fields, dict):
        fields = {}
    app_id = _number(fields.get("app_id"))
    installation_id = _number(fields.get("installation_id"))
    private_key = fields.get("private_key")
    if app_id is None or installation_id is None or not (isinstance(private_key, str) and private_key):
        raise CredentialError
    return _Credential(app_id, installation_id, private_key)


def has_credential(secrets: Any = None) -> bool:
    """Whether the secret `RHI_REPORTER_SECRET` names can be read and holds the three fields, read
    as `StatusClient` reads it, with `secrets` or a client of the function's own. Raises nothing
    and logs nothing: the value stays here."""
    try:
        _read_credential(secrets)
    except CredentialError:
        return False
    return True


class StatusClient:
    """Posts commit statuses on the repository `RHI_REPORTER_REPOSITORY` names. One per execution
    environment: it holds the credential and the installation token. Making one reads nothing.

    `secrets` is a Secrets Manager client of the AWS SDK; without one, the function's credentials
    and region make it. `api_url`, `clock` and `sleep`, which waits on that clock, are for the
    tests."""

    def __init__(
        self,
        secrets: Any = None,
        *,
        api_url: str = API_URL,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._secrets = secrets
        self._api_url = api_url
        self._clock = clock
        self._sleep = sleep
        self._credential: _Credential | None = None
        self._token: str | None = None
        self._token_expiry = 0.0

    def post_status(
        self, commit: str, state: str, context: str, description: str, *, deadline: float | None = None
    ) -> None:
        """Creates one status on the commit, a full SHA, waiting out GitHub's temporary refusals
        until `deadline`, a time on the client's clock, when one is given. Raises `CredentialError`
        when the App's credential cannot be used, `RefusedError` when GitHub does not accept a call,
        and `ValueError` for a repository that is not owner/name or a commit that is not a full SHA."""
        repository = _repository_of(commit)
        body = {"state": state, "context": context, "description": description}
        url = f"{self._api_url}/repos/{repository}/statuses/{commit}"
        self._call("POST", url, self._installation_token(deadline), body, deadline=deadline)

    def statuses(self, commit: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        """Every status of the commit, a full SHA, from every page GitHub lists, newest first, each
        with its `id`, `context`, `state` and `description`. Raises as `post_status` does, and
        `RefusedError` for a page that is not a list of statuses or a next page at another address
        than GitHub's API: the token goes nowhere else."""
        repository = _repository_of(commit)
        url: str | None = f"{self._api_url}/repos/{repository}/commits/{commit}/statuses?per_page={STATUSES_PER_PAGE}"
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url is not None:
            seen.add(url)
            token = self._installation_token(deadline)
            status, answer, headers = self._call("GET", url, token, None, deadline=deadline, limit=PAGE_BYTES)
            if not isinstance(answer, list):
                raise RefusedError(status, None)
            found += [one for one in map(_status_of, answer) if one is not None]
            link = _NEXT_PAGE.search((headers.get("link") or "") if headers is not None else "")
            url = link.group(1) if link else None
            if url is not None and (not url.startswith(f"{self._api_url}/") or url in seen):
                raise RefusedError(status, None)
        return found

    def _installation_token(self, deadline: float | None) -> str:
        if self._token is None or self._token_expiry - self._clock() < RENEW_BEFORE_SECONDS:
            credential = self._read_credential()
            url = f"{self._api_url}/app/installations/{credential.installation_id}/access_tokens"
            status, answer, _ = self._call("POST", url, self._app_token(credential), None, deadline=deadline)
            token = answer.get("token") if isinstance(answer, dict) else None
            try:
                expiry = datetime.datetime.fromisoformat(answer["expires_at"]).timestamp()
            except (TypeError, KeyError, ValueError):
                expiry = None
            if not (isinstance(token, str) and token) or expiry is None:
                raise RefusedError(status, None)
            self._token, self._token_expiry = token, expiry
        return self._token

    def _read_credential(self) -> _Credential:
        if self._credential is None:
            self._credential = _read_credential(self._secrets)
        return self._credential

    def _app_token(self, credential: _Credential) -> str:
        now = int(self._clock())
        claims = {"iss": credential.app_id, "iat": now - 60, "exp": now + APP_TOKEN_SECONDS}
        try:
            return jwt.encode(claims, credential.private_key, algorithm="RS256")
        except (jwt.PyJWTError, TypeError, ValueError):
            # Not kept: the owner may store a key that signs.
            self._credential = None
        raise CredentialError

    def _call(
        self,
        method: str,
        url: str,
        bearer: str,
        body: dict[str, str] | None,
        *,
        deadline: float | None,
        limit: int = ANSWER_BYTES,
    ) -> tuple[int, Any, Message | None]:
        """One call to GitHub's API, made again while GitHub asks to wait and `deadline` allows
        (the module's docstring). Returns the status, the JSON and the headers of an accepted call."""
        attempt = 0
        while True:
            if deadline is not None and self._clock() > deadline:
                raise RefusedError(None, None)
            status, answer, headers = self._attempt(method, url, bearer, body, limit)
            if status is not None and 200 <= status < 300:
                return status, answer, headers
            refused = RefusedError(status, _error_code(answer))
            if deadline is None or not _waited_out(status, headers):
                raise refused
            attempt += 1
            now = self._clock()
            wait = _wait_of(headers, now, attempt)
            if now + wait > deadline:
                raise refused
            self._sleep(wait)

    def _attempt(
        self, method: str, url: str, bearer: str, body: dict[str, str] | None, limit: int
    ) -> tuple[int | None, Any, Message | None]:
        """One attempt: the status, None without an answer, the JSON, None when the answer is none
        or is longer than `limit`, and the headers."""
        request = urllib.request.Request(
            url,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "User-Agent": "rhi-reporter",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        # The answer is read outside the handlers below, so that no error raised from here has
        # urllib's error, which holds GitHub's reason text, as its context.
        response: Any = None
        try:
            response = _OPENER.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, http.client.HTTPException):
            pass
        status: int | None = None
        headers: Message | None = None
        text = b""
        if response is not None:
            # The status is known before the text is read: an answer that breaks off keeps it.
            status = response.code if isinstance(response, urllib.error.HTTPError) else response.status
            headers = response.headers
            try:
                with response:
                    text = response.read(limit + 1)
            except (OSError, http.client.HTTPException):
                text = b""
        try:
            answer = json.loads(text) if len(text) <= limit else None
        except ValueError:
            answer = None
        return status, answer, headers


def _repository_of(commit: str) -> str:
    """The repository `RHI_REPORTER_REPOSITORY` names. ValueError for one that is not owner/name,
    or a commit that is not a full SHA."""
    repository = os.environ.get(REPOSITORY_VARIABLE, "")
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError(f"{REPOSITORY_VARIABLE} is not a repository, as owner/name")
    if not _COMMIT.fullmatch(commit):
        raise ValueError("the commit is not a full SHA")
    return repository
