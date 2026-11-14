"""Stand-in for the GitHub CLI in the backlog tests: `gh api` over a JSON file.

It speaks the part of GitHub's REST API that the publisher writes and the engine reads back:
the repository, its labels, its issues, sub-issues and issue dependencies. The state is the JSON
file in $FAKE_GH_STATE; every call is appended to $FAKE_GH_LOG as one JSON line, so a test can
assert that a command made no call, or no write.

Where the real API is strict, so is this one. A link takes the `id` of an issue, never its
number, and ids are kept far from numbers so that a mix-up is a 404. An issue has one parent.
Creating a label, a sub-issue link or a dependency that already exists is a 422.

To stand in for an outage, the state may hold `"fail_post": N`: the Nth POST fails with a 502.
With `"fail_after_effect": true` that POST takes effect before failing, as a lost answer would.
`"fail_get": "text"` makes every GET whose path holds that text fail with a 502.

`"branches"` maps each pushed branch to its head commit; an empty repository has none, and its
default branch is then a 404, as on GitHub.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

ID_BASE = 7_000_000


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def issue_view(state: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
    number = str(issue["number"])
    blocking = sum(issue["id"] in blockers for blockers in state["blocked"].values())
    return {
        **issue,
        "html_url": f"https://github.com/{state['repo']}/issues/{number}",
        "sub_issues_summary": {"total": len(state["subs"].get(number, [])), "completed": 0, "percent_completed": 0},
        "issue_dependencies_summary": {
            "blocked_by": len(state["blocked"].get(number, [])), "blocking": blocking,
            "total_blocked_by": len(state["blocked"].get(number, [])), "total_blocking": blocking},
    }


def by_id(state: dict[str, Any], issue_id: object) -> dict[str, Any]:
    if isinstance(issue_id, bool) or not isinstance(issue_id, int):
        raise Refused(422, "Invalid request: the id must be an integer")
    for issue in state["issues"].values():
        if issue["id"] == issue_id:
            return issue
    raise Refused(404, "Not Found")


def page(items: list[Any], query: dict[str, list[str]]) -> list[Any]:
    per_page = int(query.get("per_page", ["30"])[0])
    start = (int(query.get("page", ["1"])[0]) - 1) * per_page
    return items[start:start + per_page]


def handle(state: dict[str, Any], method: str, tail: str, query: dict[str, list[str]], payload: Any) -> Any:
    """One branch per endpoint; anything else is a 404."""
    issues: dict[str, dict[str, Any]] = state["issues"]
    parts = tail.split("/") if tail else []
    if not parts and method == "GET":
        return {"full_name": state.get("full_name", state["repo"]), "private": True,
                "has_issues": state.get("has_issues", True), "default_branch": state.get("default_branch", "main")}
    if parts[:1] == ["branches"] and len(parts) > 1 and method == "GET":
        name = "/".join(parts[1:])
        if name not in state.get("branches", {}):
            raise Refused(404, "Branch not found")
        return {"name": name, "commit": {"sha": state["branches"][name]}}
    if parts == ["labels"] and method == "GET":
        return page(state["labels"], query)
    if parts == ["labels"] and method == "POST":
        if any(label["name"].lower() == payload["name"].lower() for label in state["labels"]):
            raise Refused(422, "Validation Failed: already_exists")
        label = {"name": payload["name"], "color": payload["color"], "description": payload.get("description", "")}
        state["labels"].append(label)
        return label
    if parts == ["issues"] and method == "GET":
        listed = sorted(issues.values(), key=lambda issue: -issue["number"])
        return page([issue_view(state, issue) for issue in listed], query)
    if parts == ["issues"] and method == "POST":
        if not isinstance(payload.get("title"), str) or not isinstance(payload.get("body"), str):
            raise Refused(422, "Validation Failed: title and body must be strings")
        number = max([int(n) for n in issues] + [0]) + 1
        names = {label["name"].lower(): label["name"] for label in state["labels"]}
        for name in payload.get("labels", []):
            if name.lower() not in names:      # GitHub creates a missing label with a default colour
                state["labels"].append({"name": name, "color": "ededed", "description": ""})
                state.setdefault("labels_created_by_issues", []).append(name)
                names[name.lower()] = name
        issues[str(number)] = {"number": number, "id": ID_BASE + 13 * number, "title": payload["title"],
                               "body": payload["body"], "state": "open",    # a label keeps the case it was made with
                               "labels": [{"name": names[name.lower()]} for name in payload.get("labels", [])]}
        return issue_view(state, issues[str(number)])
    if len(parts) >= 2 and parts[0] == "issues" and parts[1] in issues:
        issue = issues[parts[1]]
        rest = parts[2:]
        if not rest and method == "GET":
            return issue_view(state, issue)
        if rest == ["sub_issues"] and method == "GET":
            return page([issue_view(state, by_id(state, i)) for i in state["subs"].get(parts[1], [])], query)
        if rest == ["sub_issues"] and method == "POST":
            child = by_id(state, payload.get("sub_issue_id"))
            if any(child["id"] in children for children in state["subs"].values()):
                raise Refused(422, "Validation Failed: the sub-issue already has a parent")
            state["subs"].setdefault(parts[1], []).append(child["id"])
            return issue_view(state, issue)
        if rest == ["dependencies", "blocked_by"] and method == "GET":
            return page([issue_view(state, by_id(state, i)) for i in state["blocked"].get(parts[1], [])], query)
        if rest == ["dependencies", "blocked_by"] and method == "POST":
            blocker = by_id(state, payload.get("issue_id"))
            if blocker["id"] in state["blocked"].get(parts[1], []):
                raise Refused(422, "Validation Failed: the dependency already exists")
            state["blocked"].setdefault(parts[1], []).append(blocker["id"])
            return issue_view(state, issue)
    raise Refused(404, "Not Found")


def main(argv: list[str]) -> int:
    state_path = Path(os.environ["FAKE_GH_STATE"])
    log_path = Path(os.environ["FAKE_GH_LOG"])
    method, path, has_input = "GET", "", False
    args = iter(argv[1:])
    for arg in args:
        if arg == "--method":
            method = next(args)
        elif arg in ("-H", "--hostname"):
            next(args)
        elif arg == "--input":
            has_input = next(args) == "-"
        else:
            path = arg
    payload = json.loads(sys.stdin.read()) if has_input else None
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "method": method, "path": path, "payload": payload}) + "\n")
    if argv[:1] != ["api"]:
        print(f"fake gh: only `gh api` is served, not {argv[:1]}", file=sys.stderr)
        return 1

    state = json.loads(state_path.read_text(encoding="utf-8"))
    route, _, query_text = path.partition("?")
    prefix = f"repos/{state['repo']}"
    fails = False
    if method == "POST":
        state["posts"] = state.get("posts", 0) + 1
        fails = state["posts"] == state.get("fail_post")
    answer, refusal = None, None
    try:
        if route != prefix and not route.startswith(prefix + "/"):
            refusal = Refused(404, "Not Found")
        elif (fails and not state.get("fail_after_effect")) or (
                method == "GET" and state.get("fail_get") and state["fail_get"] in path):
            refusal = Refused(502, "Bad Gateway")
        else:
            answer = handle(state, method, route[len(prefix) + 1:], urllib.parse.parse_qs(query_text), payload)
            if fails:
                refusal = Refused(502, "Bad Gateway")
    except Refused as refused:
        refusal = refused
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    if refusal is not None:
        print(json.dumps({"message": refusal.message, "status": str(refusal.status)}))
        print(f"gh: {refusal.message} (HTTP {refusal.status})", file=sys.stderr)
        return 1
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
