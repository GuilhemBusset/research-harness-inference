"""The factory's own GitHub identity (ADR 0001, amendment 2): `app_token.py` mints an
installation token of the factory's GitHub App, every `gh` the engine starts gets one with at
least ten minutes left, and git gets one from a credential helper.

GitHub is a stub on the loopback interface that answers the App's two endpoints and verifies the
signature of each request with the public half of a key `openssl` generates here. `gh` is a
script that records the token it was started with. git is the real one. Nothing reaches the
network, and no key or token is written in this file: both are made at run time."""

import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

from helpers import SHIP, TempRepo, git, issue_body
from implement_loop import app_token, cli, environment, live, sandbox, verify
from implement_loop.agents import FakeAgents, Script
from implement_loop.config import Caps, Config
from implement_loop.engine import Engine
from implement_loop.github import FakeGitHub, GhCli, GitHubError, GitHubUnavailable
from implement_loop.graph import build_plan
from implement_loop.state import RunStore
from implement_loop.workspace import Workspace

APP_ID, INSTALLATION_ID = "4242", "777"
# What ADR 0001 grants the factory: no workflows, no administration.
GRANTED = {"contents": "write", "pull_requests": "write", "issues": "write", "checks": "read", "statuses": "read",
           "actions": "read", "metadata": "read"}
FAKE_GH = """#!/usr/bin/env bash
printf '%s\\t%s\\n' "${GH_TOKEN-}" "$*" >> "$FAKE_GH_LOG"
case "$*" in
  "api user"*) echo octo-owner ;;
  *check-runs*) if [ -n "${FAKE_GH_NO_CHECKS-}" ]; then echo "gh: Resource not accessible (HTTP 403)" >&2; exit 1; fi; echo 0 ;;
  "repo view"*) echo o/r ;;
  "api --method"*) echo '{}' ;;
  *) echo ok ;;
esac
"""


class Stub:
    """GitHub's API for an App: the installation and its tokens, for requests signed with the key."""

    def __init__(self, public_key: Path, scratch: Path):
        self.public_key, self.scratch = public_key, scratch
        self.permissions: dict[str, str] = dict(GRANTED)
        self.selection = "selected"
        self.status: int | None = None   # answer every request with this HTTP status instead
        self.later = 0.0                 # how far the clock has moved, in seconds (`pass_time`)
        self.minted: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, method: str) -> None:
                code, body = stub.answer(method, self.path, self.headers.get("Authorization") or "")
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.answer("GET")

            def do_POST(self):
                self.answer("POST")

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def signed_by_the_app(self, authorization: str) -> bool:
        """An RS256 JWT of the App, at most ten minutes long, that the public key verifies."""
        try:
            head, claims, signature = authorization.removeprefix("Bearer ").split(".")
            said = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        except ValueError:
            return False
        sig = self.scratch / f"sig-{secrets.token_hex(4)}"
        sig.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.public_key), "-signature", str(sig)],
                                  input=f"{head}.{claims}".encode(), capture_output=True, check=False).returncode == 0
        return verified and said["iss"] == int(APP_ID) and said["exp"] - said["iat"] <= 600

    def answer(self, method: str, path: str, authorization: str) -> tuple[int, dict]:
        if self.status is not None:
            return self.status, {"message": "the stub was told to refuse"}
        if not self.signed_by_the_app(authorization):
            return 401, {"message": "a JSON web token could not be decoded"}
        if (method, path) == ("GET", f"/app/installations/{INSTALLATION_ID}"):
            return 200, {"app_slug": "factory-app", "permissions": self.permissions, "repository_selection": self.selection}
        if (method, path) == ("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens"):
            self.minted.append("ghs_" + secrets.token_hex(18))   # the shape of an installation token
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + self.later + 3600))
            return 201, {"token": self.minted[-1], "expires_at": expires, "permissions": self.permissions}
        return 404, {"message": "Not Found"}


@pytest.fixture(scope="module")
def key(tmp_path_factory) -> Path:
    """An RSA private key generated here; its public half is beside it, for the stub."""
    path = tmp_path_factory.mktemp("app") / "app.private-key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(path), "2048"], capture_output=True, check=True)
    subprocess.run(["openssl", "rsa", "-in", str(path), "-pubout", "-out", str(path.with_suffix(".pub"))],
                   capture_output=True, check=True)
    return path


@pytest.fixture
def host(tmp_path, key):
    """A host with a `gh` that records the token it gets, the stub in GitHub's place, an owner
    whose own git credential helper answers for every host, and no App configured yet."""
    stub = Stub(key.with_suffix(".pub"), tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text(FAKE_GH)
    (fake_bin / "gh").chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text('[credential]\n\thelper = "!f() { echo username=owner; echo password=the-owners-login; }; f"\n')
    env = {k: v for k, v in os.environ.items()
           if k not in (*app_token.VARS, *sandbox.GITHUB_VARS) and not k.startswith("GIT_CONFIG_")}
    env |= {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            app_token.API_VAR: stub.url, "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0"}
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(app_token, "_held", []):
        yield stub
    stub.server.shutdown()
    stub.server.server_close()


@pytest.fixture
def app(host, key, monkeypatch):
    """The same host with the factory's App named by the three variables."""
    for name, value in zip(app_token.VARS, (APP_ID, INSTALLATION_ID, str(key)), strict=True):
        monkeypatch.setenv(name, value)
    return host


def pass_time(stub: Stub, monkeypatch, minutes: float) -> None:
    """Move the clock of the engine's process and of GitHub. A new process still reads the real one."""
    stub.later = minutes * 60
    monkeypatch.setattr(app_token, "_now", lambda: time.time() + stub.later)


def gh_calls() -> list[tuple[str, str]]:
    """(the GH_TOKEN it was started with, its arguments) of every `gh` process so far."""
    log = Path(os.environ["FAKE_GH_LOG"])
    return [(line.split("\t")[0], line.split("\t")[1]) for line in log.read_text().splitlines()] if log.exists() else []


def git_asks(host_name: str) -> dict[str, str]:
    """What a real git process gets for an https URL on `host_name`, from its credential helpers."""
    proc = subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host_name}\n\n",
                          capture_output=True, text=True, check=False)
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line) | {"exit": str(proc.returncode)}


def as_a_run_does(tmp: Path) -> None:
    """The engine's own environment as `cli.cmd_run` sets it, before its first git process."""
    cli.git_as_the_app()
    os.environ.update(sandbox.engine_git_env(os.environ, tmp / "no-hooks"))


def doctor(root: Path, slug: str | None = None) -> dict[str, tuple[str, str]]:
    """The doctor's lines, with `gh` (the recording script) and git for real."""
    real = cli._out
    with mock.patch.object(cli, "_out", lambda cmd: real(cmd) if cmd[0] in ("gh", "git") else (0, "stub 1.0")), \
         mock.patch.object(cli.environment, "probe", return_value=[]), \
         mock.patch.object(cli, "sandbox_check", return_value=(True, "sandbox", "ok")):
        return {name: (status, detail) for status, name, detail in cli.doctor_checks(root, slug)}


@pytest.fixture
def checkout(tmp_path) -> Path:
    """A repository whose `origin` is an https URL on github.com. Nothing here contacts it."""
    root = tmp_path / "checkout"
    git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    git("remote", "add", "origin", "https://github.com/o/r.git", cwd=root)
    return root


# ---------------------------------------------------------------- minting, renewing, the helper

def test_a_token_is_minted_from_the_key_with_openssl_and_the_standard_library(app, tmp_path, monkeypatch):
    assert app_token.token() == app.minted[0], "the stub verified the signature and the App ID before it answered"
    isolated = subprocess.run([sys.executable, "-I", "-S", app_token.__file__, "credential", "store"],
                              input="", capture_output=True, text=True, check=False)
    assert isolated.returncode == 0, f"the module needs more than the standard library: {isolated.stderr}"
    monkeypatch.setenv("PATH", str(tmp_path / "no-openssl-here"))
    with pytest.raises(app_token.AppTokenError, match="openssl"):
        app_token.jwt(app_token.App(APP_ID, INSTALLATION_ID, os.environ[app_token.VARS[2]]))


def test_a_token_with_less_than_ten_minutes_left_is_renewed_for_gh(app, monkeypatch):
    gh = cli.github("o/r")
    gh.get_issue(1)
    pass_time(app, monkeypatch, 49)   # eleven minutes left: kept
    gh.get_issue(1)
    assert cli.repo_slug() == "o/r"
    pass_time(app, monkeypatch, 51)   # nine minutes left: replaced
    gh.get_issue(1)
    gh.failed_log("0" * 40)     # one more `gh api`: the fake lists no failed run
    assert cli._out(["gh", "auth", "status"]) == (0, "ok")
    first, second = app.minted
    assert [token for token, _ in gh_calls()] == [first, first, first, second, second, second]
    assert "GH_TOKEN" not in os.environ, "the token goes to each `gh`, never into the engine's own environment"


def test_the_credential_helper_answers_git_with_the_renewed_token(app, tmp_path, monkeypatch):
    as_a_run_does(tmp_path)
    first = app_token.token()
    pass_time(app, monkeypatch, 51)   # the token the engine holds is nine minutes from its end
    assert app_token.token() == app.minted[1] != first
    answer = git_asks("github.com")
    assert answer["username"] == "x-access-token"
    assert answer["password"] == app.minted[-1] != first, "git gets a token minted after the first neared its end"
    assert answer["password"] != "the-owners-login", "the helpers of git's own files are dropped for github.com"
    assert git_asks("example.com")["password"] == "the-owners-login", "the App's helper answers for github.com only"
    before = dict(os.environ)
    cli.git_as_the_app()        # a run the supervisor launched inherits the helper and adds nothing
    assert dict(os.environ) == before


def test_a_token_that_cannot_be_minted_is_an_outage_or_an_error_and_git_does_not_prompt(app, tmp_path):
    slept: list[float] = []
    gh = GhCli("o/r", sleep=slept.append, env=app_token.gh_env)
    app.status = 503
    with pytest.raises(GitHubUnavailable, match="HTTP 503"):
        gh.get_issue(1)
    assert slept == [5, 15, 45], "GitHub may answer the next time: retried like any call that was not sent"
    app.status = 401
    with pytest.raises(GitHubError, match="HTTP 401") as refused:
        gh.get_issue(1)
    assert not isinstance(refused.value, GitHubUnavailable) and len(slept) == 3
    assert "the stub was told" not in str(refused.value), "GitHub's own text is not repeated"
    assert gh_calls() == [], "no `gh` runs with the owner's login in the App's place"
    as_a_run_does(tmp_path)
    answer = git_asks("github.com")
    assert "password" not in answer and answer["exit"] != "0", "git stops: no other helper, no prompt"


# ---------------------------------------------------------------- where the token is not

class EnvDumpingAgents(FakeAgents):
    """Fake agents whose check command also writes the environment it runs in to a file."""

    def __init__(self, scripts, dump: Path):
        super().__init__(scripts)
        self.dump = dump

    def _test_script(self, n: int) -> str:
        return super()._test_script(n) + f"env > '{self.dump}'\n"


def test_the_token_stays_with_gh_and_git(app, key, tmp_path):
    as_a_run_does(tmp_path)
    by_cli = cli.github("o/r")
    repo = TempRepo()
    try:
        class Spied(FakeGitHub):   # each read of an issue also starts a `gh`, as the real client would
            def get_issue(self, n):
                by_cli.get_issue(n)
                return super().get_issue(n)

        gh = Spied(on_merge=repo.merge)
        gh.add_issue(2, "Issue 2", issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["The test passes on main."]),
                     labels=("size:S",))
        gh.add_issue(1, "Epic", "An epic.\n", subs=(2,))
        store = RunStore.for_root(repo.work, 1)
        engine_log = store.dir / "engine.log"

        def log(message: str) -> None:
            with engine_log.open("a") as f:
                f.write(message + "\n")

        check_env = repo.tmp / "check-env.txt"
        agents = EnvDumpingAgents({2: Script(files={"docs/a.txt": "alpha"})}, check_env)
        engine = Engine(build_plan(gh, 1, repo.work), store, gh, Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir),
                        agents, Config(repo="test/test", caps=Caps(ci_poll_s=0, queue_poll_s=0)), repo_root=repo.work,
                        log=log, sleep=lambda s: None, policy={"lacks": []})
        assert engine.run()["phases"] == {"done": 1}

        # with `gh` and git: every `gh` of the run was started with the token, and git gets one
        tokens = {token for token, _ in gh_calls()}
        assert tokens and tokens <= set(app.minted)
        assert git_asks("github.com")["password"] in app.minted
        secret = [*app.minted, *key.read_text().splitlines()[1:-1]]   # every token, every line of the key

        def clean(text: str) -> bool:
            return not any(s in text for s in secret)

        # not in the engine's own environment, so not in what any other process inherits from it:
        # the doctor's and the run's probe of the test runner, a tool the doctor asks for its version
        assert clean(json.dumps(dict(os.environ)))
        probed: list[dict] = []

        def runner(cmd, env=None, **kwargs):
            probed.append(dict(os.environ if env is None else env))
            return subprocess.CompletedProcess(cmd, 0, "pytest 8.3.5", "")
        environment.probe(repo.work, runner)
        code, inherited = cli._out([sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"])
        assert code == 0 and probed and clean(json.dumps(probed)) and clean(inherited)
        # not with agents, not with checks: what each is started with, and what a check of the run saw
        for vendor in ("claude", "codex"):
            assert clean(json.dumps(live.agent_env(repo.tmp / "no-gh", {}, vendor)))
        assert clean(json.dumps(verify.scrubbed_env(dict(os.environ), repo.tmp / "check")))
        assert "PATH=" in check_env.read_text() and clean(check_env.read_text())
        # not in a state file, an event or a log: no file the run left, its worktrees included
        files = [p for p in repo.tmp.rglob("*") if p.is_file()]
        assert {"state.json", "events.jsonl", "engine.log"} <= {p.name for p in files if store.dir in p.parents}
        assert [str(p) for p in files if not clean(p.read_text(errors="replace"))] == []
        # not in what the engine published
        assert clean(json.dumps([gh.issues, gh.comments, gh.prs], default=str))
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- without the App, and the doctor

def test_without_the_variables_gh_keeps_its_login_and_the_doctor_names_it(host, key, checkout, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "what-the-shell-holds")
    before = dict(os.environ)
    cli.git_as_the_app()
    cli.github("o/r").get_issue(1)
    assert dict(os.environ) == before and git_asks("github.com")["password"] == "the-owners-login"
    assert gh_calls() == [("what-the-shell-holds", mock.ANY)] and host.minted == []
    status, detail = doctor(checkout)["github identity"]
    assert status == "PASS" and "octo-owner, the login `gh` has" in detail

    monkeypatch.setenv(app_token.VARS[0], APP_ID)   # one or two of the three: never a silent fall back
    monkeypatch.setenv(app_token.VARS[2], str(key))
    status, detail = doctor(checkout)["github identity"]
    assert status == "FAIL" and f"{app_token.VARS[1]} not set" in detail
    with pytest.raises(GitHubError, match="not set"):
        cli.github("o/r").get_issue(1)


def test_the_doctor_names_the_app_it_runs_as(app, checkout):
    named = f"the GitHub App factory-app, installation {INSTALLATION_ID}, on selected repositories"
    assert doctor(checkout)["github identity"] == ("PASS", named)
    assert doctor(checkout)["gh auth"][0] == "PASS" and gh_calls()[0][0] in app.minted, "the doctor's `gh` runs as the App"
    app.selection = "all"
    assert doctor(checkout)["github identity"][0] == "WARN"
    app.selection = "selected"
    git("remote", "set-url", "origin", "git@github.com:o/r.git", cwd=checkout)
    status, detail = doctor(checkout)["github identity"]
    assert status == "FAIL" and "git would not push as the App" in detail


def test_doctor_refuses_a_workflows_permission(app, checkout):
    app.permissions["workflows"] = "write"
    status, detail = doctor(checkout)["github identity"]
    assert status == "FAIL"
    assert "`workflows` permission (write)" in detail and "factory-app" in detail


@pytest.mark.parametrize(("change", "said"), [({"administration": "read"}, "`administration` permission (read)"),
                                              ({"checks": None}, "cannot read checks")])
def test_doctor_refuses_administration_and_an_installation_that_cannot_read_checks(app, checkout, change, said):
    app.permissions = {k: v for k, v in (app.permissions | change).items() if v}
    status, detail = doctor(checkout)["github identity"]
    assert status == "FAIL" and said in detail


def test_doctor_fails_when_the_identity_cannot_read_check_runs(host, monkeypatch):
    repo = TempRepo()   # an `origin` with a pushed `main`, on this disk
    try:
        assert doctor(repo.work, "o/r")["checks access"][0] == "PASS"
        monkeypatch.setenv("FAKE_GH_NO_CHECKS", "1")   # logged in, and refused on check runs: the fine-grained token
        lines = doctor(repo.work, "o/r")
        assert lines["gh auth"][0] == "PASS" and lines["github identity"][0] == "PASS"
        status, detail = lines["checks access"]
        assert status == "FAIL" and "HTTP 403" in detail and "`required`" in detail
    finally:
        repo.cleanup()
