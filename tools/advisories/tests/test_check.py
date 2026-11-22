"""The advisory check: the comparison on recorded advisories, and the issues it opens on a stub of GitHub."""

from __future__ import annotations

import ast
import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import check

RECORDED: list[dict[str, Any]] = json.loads((Path(__file__).with_name("advisories.json")).read_text(encoding="utf-8"))
REPOSITORY = "OWNER/REPOSITORY"
CRITICAL = "GHSA-cccc-2222-3333"


class StubGitHub:
    """GitHub's advisory and issue endpoints, in process on the loopback, two advisories a page."""

    def __init__(self, advisories: list[dict[str, Any]]) -> None:
        self.advisories = advisories
        self.issues: list[dict[str, Any]] = [{"title": "A pull request", "body": "", "pull_request": {}}]
        self.posted: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def reply(self, status: int, body: Any, link: str | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if link:
                    self.send_header("Link", f'<{link}>; rel="next"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                if url.path == "/advisories" and query.get("ecosystem") == "pip" and query.get("affects") == "vllm":
                    page = int(query.get("page", "1"))
                    items = stub.advisories[2 * (page - 1) : 2 * page]
                    more = len(stub.advisories) > 2 * page
                    link = f"{stub.url}/advisories?ecosystem=pip&affects=vllm&page={page + 1}" if more else None
                    self.reply(200, items, link)
                elif url.path == f"/repos/{REPOSITORY}/issues" and query.get("state") == "open":
                    self.reply(200, stub.issues)
                else:
                    self.reply(404, {"message": "Not Found"})

            def do_POST(self) -> None:
                if urlsplit(self.path).path != f"/repos/{REPOSITORY}/issues":
                    self.reply(404, {"message": "Not Found"})
                    return
                issue = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.posted.append(issue)
                stub.issues.append(issue)
                self.reply(201, {"number": len(stub.issues)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubGitHub]:
    # The stub is on the loopback: a proxy the environment names must not carry the requests.
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "127.0.0.1")
    stub = StubGitHub(RECORDED)
    thread = threading.Thread(target=stub.server.serve_forever, daemon=True)
    thread.start()
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


def run(github: StubGitHub, tmp_path: Path, version: str, *options: str) -> int:
    version_file = tmp_path / "VERSION"
    version_file.write_text(f"{version}\n", encoding="utf-8")
    argv = ["--version-file", str(version_file), "--api-url", github.url, "--repository", REPOSITORY, *options]
    return check.main(argv)


@pytest.mark.parametrize(
    ("version", "affected", "inside"),
    [
        ("0.33.0", ">= 0.30.0, < 0.33.1", True),
        ("0.33.1", ">= 0.30.0, < 0.33.1", False),
        ("0.29.9", ">= 0.30.0, < 0.33.1", False),
        ("0.9.2", "< 0.10.0", True),  # numeric, not text, comparison
        ("0.33.1rc1", "< 0.33.1", True),  # a pre-release is below its release
        ("0.34", "<= 0.34.0", True),
        ("0.34.0", "> 0.34.0", False),
        ("0.33.0", "= 0.33.0", True),
    ],
)
def test_range_comparison(version: str, affected: str, inside: bool) -> None:
    assert check.in_range(version, affected) is inside


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.33.0", [(CRITICAL, "0.33.1")]),  # also inside the moderate one, which does not count
        ("0.30.5", [("GHSA-hhhh-2222-3333", "0.31.2"), (CRITICAL, "0.33.1")]),
        ("0.34.0", []),  # inside the moderate one only
        ("0.41.0", [("GHSA-wwww-2222-3333", None)]),
    ],
)
def test_serious_advisories_on_recorded_ones(version: str, expected: list[tuple[str, str | None]]) -> None:
    assert [(found.identifier, found.fixed) for found in check.serious(RECORDED, version)] == expected


PUBLISHED = "GHSA-94f4-hr76-p5j6"  # a published vLLM advisory, recorded from the database


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.21.1", [(PUBLISHED, "0.22.0")]), ("0.22.0", []), ("0.2.9", []), ("0.3.0", [(PUBLISHED, "0.22.0")])],
)
def test_serious_on_a_published_advisory(version: str, expected: list[tuple[str, str | None]]) -> None:
    published = [advisory for advisory in RECORDED if advisory["ghsa_id"] == PUBLISHED]
    assert [(found.identifier, found.fixed) for found in check.serious(published, version)] == expected


def test_an_affected_version_fails_and_opens_one_plain_issue_once(
    github: StubGitHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(github, tmp_path, "0.33.0", "--open-issues") == 1
    assert CRITICAL in capsys.readouterr().out

    [issue] = github.posted
    text = f"{issue['title']}\n{issue['body']}"
    for value in (CRITICAL, "Pinned version: 0.33.0", "Fixed version: 0.33.1"):
        assert value in text
    critical = next(advisory for advisory in RECORDED if advisory["ghsa_id"] == CRITICAL)
    for copied in ("summary", "description", "html_url", "cve_id"):
        assert critical[copied] not in text

    # The next run finds the open issue that names the advisory: it still fails, and opens none.
    assert run(github, tmp_path, "0.33.0", "--open-issues") == 1
    assert len(github.posted) == 1


def test_no_issue_without_the_option(github: StubGitHub, tmp_path: Path) -> None:
    assert run(github, tmp_path, "0.33.0") == 1
    assert github.posted == []


@pytest.mark.parametrize("version", ["0.33.1", "0.34.0"], ids=["above-the-fixed-version", "moderate-only"])
def test_passes_and_opens_nothing(github: StubGitHub, tmp_path: Path, version: str) -> None:
    assert run(github, tmp_path, version, "--open-issues") == 0
    assert github.posted == []


def test_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(check.__file__).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert {name.split(".")[0] for name in names} <= set(sys.stdlib_module_names)
