"""The apps step of the owner's walkthrough (setup/owner-setup.sh): the three GitHub Apps of the
system design, section 8.6, read back from GitHub and judged.

    python -m rhi.owner_apps reporter --app-id <n> --key-file <file> --repository <owner>/<name>
    python -m rhi.owner_apps factory  --app-id <n> --key-file <file> --repository <owner>/<name>
    python -m rhi.owner_apps login --slug <slug> --app-id <reporter's n> --key-file <reporter's file>

Each App is installable only on its owner's account, which makes it private, and GitHub answers
a request for a private App only to the App's own tokens: the owner's `gh` login gets 404. So:

- the status reporter and the factory's App are read with a JSON Web Token signed with the App's
  own private key (`GET /app`, `GET /app/installations`), and an installation token minted from it
  lists the repositories each installation reaches (`GET /installation/repositories`);
- the login App has no key: an installation token of the status reporter reads it by its slug
  (`GET /apps/{slug}`), and a device code request with its client ID shows whether its device
  flow is on.

It prints each App's slug, ID and permissions, and whether these are the permissions of section
8.6; for an App read by its ID, each installation with its ID, its repository selection and the
repositories it reaches. It refuses, with exit status 1, an App that has an installation on
anything other than the one repository named, and a login App whose device flow is off.

Python never reads a key: `openssl` signs with the file. No key, no token, no device code and
not the login App's client ID (a private value of the configuration) is printed, and no message
repeats what GitHub answered: only fixed text, an HTTP status, and the slugs, IDs, permissions
and repository names this step exists to show. Standard library and `openssl` only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Where GitHub is. The tests point both at a stand-in on the loopback interface.
API_VAR, API = "RHI_GITHUB_API_URL", "https://api.github.com"
WEB_VAR, WEB = "RHI_GITHUB_URL", "https://github.com"
API_VERSION = "2022-11-28"
JWT_LIFE_S = 540  # GitHub refuses a JWT that lasts more than ten minutes
CLOCK_DRIFT_S = 60  # dated a minute back, as GitHub recommends
TIMEOUT_S = 30
PAGE = 100

# The permissions of section 8.6, as GitHub's API names them, sorted. `metadata=read`, which
# GitHub grants by itself with any repository permission, is not counted where others are expected.
EXPECTED = {
    "login": "",
    "reporter": "statuses=write",
    "factory": "actions=read checks=read contents=write issues=write pull_requests=write statuses=read",
}

_HINTS = {
    401: " (the App ID, the key or this machine's clock is wrong)",
    403: " (GitHub refused the request)",
    404: " (GitHub knows no such App or installation for this token)",
}


class Refused(Exception):
    """The App is not as section 8.6 has it, or it cannot be read back. Fixed text only."""


@dataclass(frozen=True)
class Installation:
    installation_id: int
    selection: str
    repositories: tuple[str, ...]
    total: int


def _url(env: Mapping[str, str], var: str, default: str) -> str:
    """A base URL: https, or http to this host. A key's signature goes nowhere else."""
    url = env.get(var) or default
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")):
        raise Refused(f"{var} is neither an https URL nor one on this host")
    return url.rstrip("/")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(app_id: int, key_file: str, now: float | None = None) -> str:
    """The App's signed statement of who it is (RS256): its issuer is the App's ID. `openssl`
    reads the key file and signs; this process never holds the key."""
    at = int(time.time() if now is None else now)
    parts = ({"alg": "RS256", "typ": "JWT"}, {"iat": at - CLOCK_DRIFT_S, "exp": at + JWT_LIFE_S, "iss": app_id})
    signing = ".".join(_b64(json.dumps(part, separators=(",", ":")).encode()) for part in parts)
    if not Path(key_file).is_file():
        raise Refused("the private key file is not a file that can be read")
    try:
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_file],
            input=signing.encode(),
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("the `openssl` program could not be run to sign as the App") from None
    if signed.returncode != 0 or not signed.stdout:
        raise Refused("openssl could not sign with the private key file: is it the App's private key?")
    return f"{signing}.{_b64(signed.stdout)}"


def _request(url: str, what: str, *, token: str = "", post: bool = False, form: dict[str, str] | None = None) -> Any:
    """One request; the answer's JSON. An error names the HTTP status, never GitHub's text."""
    headers = {"Accept": "application/json", "User-Agent": "rhi-owner-setup"}
    if token:
        headers |= {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        headers["X-GitHub-Api-Version"] = API_VERSION
    data = None if form is None else urllib.parse.urlencode(form).encode()
    method = "POST" if post or form is not None else "GET"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # `_url` vetted the URL
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise Refused(f"GitHub answered HTTP {error.code} when asked for {what}{_HINTS.get(error.code, '')}") from None
    except (OSError, ValueError):
        raise Refused(f"GitHub could not be reached, or gave no JSON, for {what}") from None


def _object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise Refused(f"GitHub's answer for {what} is not the object expected")
    return data


def _installation_token(api: str, signed: str, installation_id: int) -> str:
    what = "an installation token"
    url = f"{api}/app/installations/{installation_id}/access_tokens"
    answer = _object(_request(url, what, token=signed, post=True), what)
    token = answer.get("token")
    if not isinstance(token, str) or not token:
        raise Refused(f"GitHub's answer for {what} carries no token")
    return token


def _installations(api: str, signed: str) -> list[dict[str, Any]]:
    what = "the App's installations"
    answer = _request(f"{api}/app/installations?per_page={PAGE}", what, token=signed)
    if not isinstance(answer, list) or not all(isinstance(item, dict) for item in answer):
        raise Refused(f"GitHub's answer for {what} is not the list expected")
    return answer


def permissions_line(permissions: object) -> str:
    """Each permission as name=access, in the order of their names."""
    held = permissions if isinstance(permissions, dict) else {}
    return " ".join(sorted(f"{name}={access}" for name, access in held.items()))


def permissions_match(line: str, expected: str) -> bool:
    """Whether the permissions are `expected`. Metadata, read-only, is not a permission anyone
    chose when another one is expected; where none is expected it counts."""
    held = line.split()
    if expected:
        held = [permission for permission in held if permission != "metadata=read"]
    return " ".join(held) == expected


def _describe(out: list[str], answer: dict[str, Any], expected: str) -> None:
    line = permissions_line(answer.get("permissions"))
    out.append(f"The App {answer.get('slug')}, ID {answer.get('id')}")
    out.append(f"  Its permissions: {line or 'none'}")
    if permissions_match(line, expected):
        out.append("  These are the permissions of section 8.6.")
    else:
        out.append(f"  These are NOT the permissions of section 8.6, which gives: {expected or 'none'}.")
        out.append("  Correct them on the App's settings page (Permissions & events), then run this step again.")


def read_app(api: str, app_id: int, key_file: str) -> tuple[dict[str, Any], list[Installation]]:
    """The App as its own JWT reads it, and each installation with the repositories it reaches."""
    signed = jwt(app_id, key_file)
    answer = _object(_request(f"{api}/app", "the App", token=signed), "the App")
    installations = []
    for item in _installations(api, signed):
        installation_id = item.get("id")
        if not isinstance(installation_id, int):
            raise Refused("GitHub's answer for the App's installations holds no installation ID")
        what = f"the repositories of installation {installation_id}"
        token = _installation_token(api, signed, installation_id)
        reached = _object(_request(f"{api}/installation/repositories?per_page={PAGE}", what, token=token), what)
        repositories = reached.get("repositories") or []
        names = tuple(str(repo.get("full_name")) for repo in repositories if isinstance(repo, dict))
        total = reached.get("total_count")
        total = total if isinstance(total, int) else len(names)
        installations.append(Installation(installation_id, str(item.get("repository_selection")), names, total))
    return answer, installations


def installation_problems(installations: Sequence[Installation], repository: str) -> list[str]:
    """Why these installations are not one installation on `repository` alone (none is fine)."""
    problems = []
    if len(installations) > 1:
        problems.append(f"the App has {len(installations)} installations, where section 8.6 has one")
    for one in installations:
        alone = [name.lower() for name in one.repositories] == [repository.lower()] and one.total == 1
        if one.selection != "selected":
            problems.append(f"installation {one.installation_id} is on all repositories, not on selected ones")
        elif not alone:
            problems.append(f"installation {one.installation_id} reaches other repositories than {repository} alone")
    return problems


def check_app(env: Mapping[str, str], role: str, app_id: int, key_file: str, repository: str, out: list[str]) -> None:
    """Reads the reporter or the factory's App back into `out`; raises `Refused` for an
    installation on anything other than `repository` alone."""
    answer, installations = read_app(_url(env, API_VAR, API), app_id, key_file)
    _describe(out, answer, EXPECTED[role])
    if not installations:
        out.append("  It has no installation yet.")
    for one in installations:
        reached = ", ".join(one.repositories) or "none"
        more = "" if one.total <= len(one.repositories) else f" (and {one.total - len(one.repositories)} more)"
        out.append(f"  Installation {one.installation_id}: {one.selection} repositories: {reached}{more}")
    problems = installation_problems(installations, repository)
    if problems:
        raise Refused("; ".join(problems) + ". Install it on \"Only select repositories\", with this repository alone")
    if installations:
        out.append("  It is installed on this repository alone.")


def check_login(env: Mapping[str, str], slug: str, reporter_id: int, reporter_key: str, out: list[str]) -> None:
    """Reads the login App back into `out` with an installation token of the reporter, and
    raises `Refused` when its device flow is off. Its client ID is used, never shown."""
    api = _url(env, API_VAR, API)
    signed = jwt(reporter_id, reporter_key)
    installations = _installations(api, signed)
    installation_id = installations[0].get("id") if installations else None
    if not isinstance(installation_id, int):
        raise Refused("the status reporter has no installation, whose token would read the login App")
    token = _installation_token(api, signed, installation_id)
    answer = _object(_request(f"{api}/apps/{slug}", "the login App", token=token), "the login App")
    _describe(out, answer, EXPECTED["login"])
    client_id = answer.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise Refused("GitHub's answer for the login App holds no client ID")
    what = "a device code"
    try:
        code = _request(f"{_url(env, WEB_VAR, WEB)}/login/device/code", what, form={"client_id": client_id})
    except Refused as error:
        if "HTTP 4" not in str(error):
            raise
        code = None
    if not (isinstance(code, dict) and code.get("device_code")):
        raise Refused(
            "GitHub gave no device code for the login App's client ID: its device flow is off. "
            "Turn on Enable Device Flow on its settings page"
        )
    out.append("  Its device flow is on: GitHub gave a device code for its client ID.")


def _digits(text: str) -> int:
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError("it must be a number")
    return int(text)


def _pattern(pattern: str, what: str) -> Any:
    def check(text: str) -> str:
        if not re.fullmatch(pattern, text):
            raise argparse.ArgumentTypeError(f"it is not {what}")
        return text

    return check


def main(arguments: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Reads one App back and prints it. Returns 0, or 1 when the App is refused or cannot be
    read back."""
    parser = argparse.ArgumentParser(prog="python -m rhi.owner_apps", description=__doc__.partition("\n\n")[0])
    roles = parser.add_subparsers(dest="role", required=True)
    for role in ("reporter", "factory"):
        by_id = roles.add_parser(role, help=f"the {role}'s App, read with its own key")
        by_id.add_argument("--app-id", required=True, type=_digits, help="the App's ID")
        by_id.add_argument("--key-file", required=True, help="the App's private key, kept outside the tree")
        repository = _pattern(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "<owner>/<name>")
        by_id.add_argument("--repository", required=True, type=repository, help="this repository, <owner>/<name>")
    login = roles.add_parser("login", help="the login App, read with a token of the status reporter")
    login.add_argument("--slug", required=True, type=_pattern(r"[A-Za-z0-9-]+", "a slug"), help="the login App's slug")
    login.add_argument("--app-id", required=True, type=_digits, help="the status reporter's App ID")
    login.add_argument("--key-file", required=True, help="the status reporter's private key file")
    options = parser.parse_args(arguments)
    env = os.environ if env is None else env
    out: list[str] = []
    try:
        if options.role == "login":
            check_login(env, options.slug, options.app_id, options.key_file, out)
        else:
            check_app(env, options.role, options.app_id, options.key_file, options.repository, out)
    except Refused as error:
        if out:
            print("\n".join(out))
        print(f"refused: {error}.", file=sys.stderr)
        return 1
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
