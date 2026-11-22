#!/usr/bin/env python3
"""Check the pinned engine version against GitHub's advisory database (system design, section 9).

Reads the vLLM version the engine image is built from (services/engine/vllm/VERSION), reads the
advisories of GitHub's global security advisory database for the pip package vllm through the REST
API, and exits 1, naming each advisory, when the pinned version is inside the affected range of
one the database rates high or critical. With --open-issues it also opens one plain issue per such
advisory in the repository, unless an open issue already names its identifier.

The issue gives the advisory's public identifier, the pinned version and the fixed version, in
this script's own words: nothing else is copied from the advisory. Turning it into backlog work is
the owner's step.

Standard library only, so the scheduled workflow (.github/workflows/advisories.yml) runs it with
the runner's python3 and installs nothing. The token is the run's own (GH_TOKEN or GITHUB_TOKEN);
the API address and the repository default to the variables a GitHub Actions run sets.

Exit status: 0 when no serious advisory affects the pinned version, 1 when one does, 2 when the
check could not be made.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO / "services" / "engine" / "vllm" / "VERSION"
PACKAGE = "vllm"
SERIOUS = frozenset({"high", "critical"})
DEFAULT_API = "https://api.github.com"

_VERSION = re.compile(
    r"^v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>a|alpha|b|beta|rc|c|pre|preview)[-_.]?(?P<pre_n>\d*))?"
    r"(?:[-_.]?(?:post|rev|r)[-_.]?(?P<post>\d*))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev>\d*))?"
    r"(?:\+[a-z0-9.]+)?$",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}
_CONSTRAINT = re.compile(r"^\s*(>=|<=|==|=|>|<)?\s*(\S+)\s*$")
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_LAST = float("inf")


class CheckError(Exception):
    """The check could not be made."""


def version_key(text: str) -> tuple[Any, ...]:
    """A sort key for a Python package version (PEP 440's release, pre, post and dev parts)."""
    match = _VERSION.match(text.strip())
    if not match:
        raise CheckError(f"not a version: {text!r}")
    release = [int(part) for part in match["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    if match["pre"]:
        pre: tuple[float, int] = (_PRE_RANK[match["pre"].lower()], int(match["pre_n"] or 0))
    elif match["dev"] is not None and match["post"] is None:
        pre = (-1, 0)  # 1.0.dev1 sorts before 1.0a1
    else:
        pre = (_LAST, 0)
    post = int(match["post"] or 0) if match["post"] is not None else -1
    dev = int(match["dev"] or 0) if match["dev"] is not None else _LAST
    return (tuple(release), pre, post, dev)


def in_range(version: str, affected: str) -> bool:
    """Whether a version is inside an advisory's range, such as `>= 0.5.0, < 0.5.5`."""
    key = version_key(version)
    for constraint in affected.split(","):
        match = _CONSTRAINT.match(constraint)
        if not match:
            raise CheckError(f"not a version range: {affected!r}")
        operator, bound = match[1] or "=", version_key(match[2])
        holds = {
            ">=": key >= bound,
            ">": key > bound,
            "<=": key <= bound,
            "<": key < bound,
            "=": key == bound,
            "==": key == bound,
        }[operator]
        if not holds:
            return False
    return True


@dataclass(frozen=True)
class Finding:
    identifier: str
    fixed: str | None


def serious(advisories: list[dict[str, Any]], version: str) -> list[Finding]:
    """The high and critical advisories whose range for pip vllm holds the version."""
    found: dict[str, Finding] = {}
    for advisory in advisories:
        if advisory.get("severity") not in SERIOUS or advisory.get("withdrawn_at"):
            continue
        for vulnerability in advisory.get("vulnerabilities") or []:
            package = vulnerability.get("package") or {}
            name = re.sub(r"[-_.]+", "-", str(package.get("name", ""))).lower()
            if package.get("ecosystem") != "pip" or name != PACKAGE:
                continue
            affected = vulnerability.get("vulnerable_version_range")
            if not affected or not in_range(version, affected):
                continue
            fixed = vulnerability.get("first_patched_version")
            if isinstance(fixed, dict):
                fixed = fixed.get("identifier")
            found.setdefault(advisory["ghsa_id"], Finding(advisory["ghsa_id"], fixed or None))
    return list(found.values())


class GitHub:
    def __init__(self, api: str, token: str | None) -> None:
        self.api = api.rstrip("/")
        self.token = token

    def _request(self, url: str, body: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                link = _LINK_NEXT.search(response.headers.get("Link") or "")
                return json.load(response), (link[1] if link else None)
        except urllib.error.HTTPError as error:
            raise CheckError(f"GitHub answered {error.code} to {request.method} {urllib.parse.urlsplit(url).path}") from None
        except urllib.error.URLError:
            raise CheckError(f"GitHub could not be reached at {urllib.parse.urlsplit(url).path}") from None

    def pages(self, path: str, **query: str) -> list[Any]:
        url: str | None = f"{self.api}{path}?{urllib.parse.urlencode({**query, 'per_page': '100'})}"
        items: list[Any] = []
        while url:
            page, url = self._request(url)
            items.extend(page)
        return items

    def advisories(self) -> list[dict[str, Any]]:
        return self.pages("/advisories", ecosystem="pip", affects=PACKAGE)

    def open_issue_texts(self, repository: str) -> list[str]:
        issues = self.pages(f"/repos/{repository}/issues", state="open")
        return [f"{issue.get('title') or ''}\n{issue.get('body') or ''}" for issue in issues if "pull_request" not in issue]

    def open_issue(self, repository: str, title: str, body: str) -> None:
        self._request(f"{self.api}/repos/{repository}/issues", {"title": title, "body": body})


def issue_text(finding: Finding, version: str) -> tuple[str, str]:
    """The issue's title and body: the identifier, the pinned version, the fixed version."""
    fixed = finding.fixed or "none published yet"
    title = f"Engine advisory {finding.identifier}: the pinned vLLM {version} is affected"
    body = "\n".join(
        [
            "The scheduled advisory check found the pinned engine inside the affected range of an advisory",
            "that GitHub's advisory database rates high or critical.",
            "",
            f"- Advisory: {finding.identifier}",
            f"- Pinned version: {version}",
            f"- Fixed version: {fixed}",
            "",
            "Turning this into backlog work is the owner's step, through the backlog tooling.",
        ]
    )
    return title, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version-file", type=Path, default=VERSION_FILE)
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL") or DEFAULT_API)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--open-issues", action="store_true", help="open one issue per serious advisory")
    args = parser.parse_args(argv)

    try:
        version = args.version_file.read_text(encoding="utf-8").strip().removeprefix("v")
        version_key(version)
        if args.open_issues and not args.repository:
            raise CheckError("--open-issues needs --repository or GITHUB_REPOSITORY")
        github = GitHub(args.api_url, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        findings = serious(github.advisories(), version)
        for finding in findings:
            print(f"{finding.identifier}: the pinned vllm {version} is affected (fixed in {finding.fixed or 'none'})")
        if args.open_issues and findings:
            open_texts = github.open_issue_texts(args.repository)
            for finding in findings:
                if any(finding.identifier in text for text in open_texts):
                    print(f"{finding.identifier}: an open issue already names it")
                    continue
                github.open_issue(args.repository, *issue_text(finding, version))
                print(f"{finding.identifier}: issue opened")
    except (CheckError, OSError) as error:
        print(f"advisory check failed: {error}", file=sys.stderr)
        return 2
    if not findings:
        print(f"no high or critical advisory affects the pinned vllm {version}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
