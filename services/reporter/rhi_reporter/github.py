"""The reporter's GitHub client: it creates commit statuses, as a GitHub App installation.

The App can write commit statuses on one repository and nothing else (system design, section 8.4;
ADR 0001, amendment 4), and creating a status is the only call here that writes to GitHub: there
is none for a commit comment, an issue or a pull request.

What the client reads, and from where (the construct and the owner's walkthrough share the names):

- the secret's name, from the environment variable `RHI_REPORTER_SECRET`;
- the App's ID, its installation ID and its private key, from the fields `app_id`,
  `installation_id` and `private_key` of that secret's JSON value;
- the repository, as owner/name, from the environment variable `RHI_REPORTER_REPOSITORY`.

The secret is read once, by the first status of an execution environment, and kept. The owner
creates it after the first deployment, so it can be absent: that, a value that is not the three
fields and a key that cannot sign all raise `CredentialError`, which carries nothing AWS said.
A read that failed is not kept: the next status reads again.

A status is posted with an installation token, which GitHub gives for a token the App's key
signed. The installation token stays in memory until less than five minutes of its validity
remain, then a new one is asked for.

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
# The `code` values GitHub documents for a refused request. Anything else is not repeated.
ERROR_CODES = frozenset({"missing", "missing_field", "invalid", "already_exists", "unprocessable", "custom"})

_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/(?!\.{1,2}$)[A-Za-z0-9._-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}")


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


class StatusClient:
    """Posts commit statuses on the repository `RHI_REPORTER_REPOSITORY` names. One per execution
    environment: it holds the credential and the installation token. Making one reads nothing.

    `secrets` is a Secrets Manager client of the AWS SDK; without one, the function's credentials
    and region make it. `api_url` and `clock` are for the tests."""

    def __init__(
        self,
        secrets: Any = None,
        *,
        api_url: str = API_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secrets = secrets
        self._api_url = api_url
        self._clock = clock
        self._credential: _Credential | None = None
        self._token: str | None = None
        self._token_expiry = 0.0

    def post_status(self, commit: str, state: str, context: str, description: str) -> None:
        """Creates one status on the commit, a full SHA. Raises `CredentialError` when the App's
        credential cannot be used, `RefusedError` when GitHub does not accept a call, and
        `ValueError` for a repository that is not owner/name or a commit that is not a full SHA."""
        repository = os.environ.get(REPOSITORY_VARIABLE, "")
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError(f"{REPOSITORY_VARIABLE} is not a repository, as owner/name")
        if not _COMMIT.fullmatch(commit):
            raise ValueError("the commit is not a full SHA")
        body = {"state": state, "context": context, "description": description}
        self._call(f"/repos/{repository}/statuses/{commit}", self._installation_token(), body)

    def _installation_token(self) -> str:
        if self._token is None or self._token_expiry - self._clock() < RENEW_BEFORE_SECONDS:
            credential = self._read_credential()
            path = f"/app/installations/{credential.installation_id}/access_tokens"
            status, answer = self._call(path, self._app_token(credential), None)
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
            name = os.environ.get(SECRET_VARIABLE)
            # The SDK's debug log repeats the answer, which holds the key or AWS's message.
            quiet = logging.root.manager.disable
            logging.disable(max(quiet, logging.DEBUG))
            try:
                secrets = boto3.client("secretsmanager") if self._secrets is None else self._secrets
                fields = json.loads(secrets.get_secret_value(SecretId=name)["SecretString"]) if name else None
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
            self._credential = _Credential(app_id, installation_id, private_key)
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

    def _call(self, path: str, bearer: str, body: dict[str, str] | None) -> tuple[int, Any]:
        """One POST to GitHub's API. Returns the status and the JSON of an accepted call."""
        request = urllib.request.Request(
            self._api_url + path,
            data=None if body is None else json.dumps(body).encode(),
            method="POST",
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
        text = b""
        if response is not None:
            # The status is known before the text is read: an answer that breaks off keeps it.
            status = response.code if isinstance(response, urllib.error.HTTPError) else response.status
            try:
                with response:
                    text = response.read(ANSWER_BYTES)
            except (OSError, http.client.HTTPException):
                text = b""
        try:
            answer = json.loads(text)
        except ValueError:
            answer = None
        if status is None or not 200 <= status < 300:
            raise RefusedError(status, _error_code(answer))
        return status, answer
