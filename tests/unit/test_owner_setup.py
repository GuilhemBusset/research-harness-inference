"""The owner's walkthrough (setup/owner-setup.sh), run in a scratch copy of the repository.

`aws`, `gh`, `cdk` and `npm` are stand-ins that record every call, so no test reaches AWS or
GitHub. The `gh` stand-in answers for an App a test has registered with it: it applies the query
of the call, with `jq`, to the App as GitHub's API would describe it. `uv` is a stand-in too,
which runs the configuration module on the test's own interpreter and, as uv does, makes the
project environment where `UV_PROJECT_ENVIRONMENT` says, or else in `.venv` in the tree. It
records the module that stores the reporter's secret and never runs it. One test runs the real uv
instead, offline. After every run the scratch copy is compared with what it was, byte for byte.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import botocore.exceptions
import pytest

from rhi import config, owner_config, owner_secret

REPO = Path(__file__).resolve().parents[2]
# Not files of the repository: git's own folder, and what the tools leave behind as they run.
LEFT_OUT = shutil.ignore_patterns(".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache")
EXAMPLE = (REPO / config.EXAMPLE_FILE).read_text(encoding="utf-8")
# Part of every bad value below, so a test can tell that a refusal does not repeat the value.
MARK = "7701"
SECRET = "rhi/reporter/github-app"
# What stands for the reporter's private key: no real key, and a mark no output may repeat.
KEY_MARK = "stand-in-for-a-key"
KEY = f"{KEY_MARK} first line\n{KEY_MARK} last line\n"


def snapshot(tree: Path) -> dict[str, bytes | None]:
    """Every file of the tree with its bytes, and every folder."""
    return {
        str(path.relative_to(tree)): None if path.is_dir() else path.read_bytes() for path in sorted(tree.rglob("*"))
    }


class Walkthrough:
    """The scratch copy, a folder outside it for the owner's files, and the stand-ins."""

    def __init__(self, tmp_path: Path) -> None:
        self.tree = tmp_path / "repository"
        shutil.copytree(REPO, self.tree, ignore=LEFT_OUT)
        self.outside = tmp_path / "owner"
        self.outside.mkdir()
        (tmp_path / "tmp").mkdir()
        self.tmp_path = tmp_path
        self.calls = tmp_path / "calls"
        self.stand_ins = tmp_path / "bin"
        self.stand_ins.mkdir()
        record = f'echo "$(basename "$0") $*" >> {shlex.quote(str(self.calls))}'
        for name in ("cdk", "npm"):
            self.stand_in(name, record)
        # `gh api apps/<slug> --jq <query>`: the query, applied to the App a test registered.
        self.apps = tmp_path / "apps"
        self.apps.mkdir()
        registered = f'{shlex.quote(str(self.apps))}/"${{2#apps/}}".json'
        self.stand_in("gh", f'{record}\n[ "$1 $3" != "api --jq" ] || [ ! -f {registered} ] || jq -r "$4" {registered}')
        # The one answer the walkthrough reads from AWS: a quota's code and its value.
        self.stand_in("aws", f"{record}\n[ \"$2\" != list-service-quotas ] || printf 'L-EXAMPLE\\t0.0\\n'")
        self.stand_in(
            "uv",
            f"{record}\n"
            '[ "$1 $2 $3 $4 $5" = "run --locked --no-dev python -m" ] || exit 64\n'
            'mkdir -p "${UV_PROJECT_ENVIRONMENT:-$PWD/.venv}"\n'
            # Recorded, never run: the two writers would look for AWS.
            '[ "$6" != rhi.owner_secret ] || exit 0\n'
            '[ "$6" = rhi.owner_config ] || exit 64\n'
            '[ "$7" != write ] || exit 0\n'
            "shift 4\n"
            f'exec {shlex.quote(sys.executable)} "$@"',
        )

    def stand_in(self, name: str, body: str) -> None:
        path = self.stand_ins / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def app(self, slug: str, app_id: int, **permissions: str) -> None:
        """Registers an App with the `gh` stand-in, in the shape of GitHub's `GET /apps/{slug}`."""
        described = {"id": app_id, "slug": slug, "permissions": permissions, "events": []}
        (self.apps / f"{slug}.json").write_text(json.dumps(described), encoding="utf-8")

    def owner_file(self, text: str = EXAMPLE, name: str = "owner.toml") -> Path:
        path = self.outside / name
        path.write_text(text, encoding="utf-8")
        return path

    def run(self, *arguments: object, answers: str = "", real_uv: bool = False) -> subprocess.CompletedProcess[str]:
        """Run the walkthrough in the scratch copy, and check that the copy is as it was."""
        if real_uv:
            (self.stand_ins / "uv").unlink()
        # Nothing the caller's shell sets may keep bytecode or an environment out of the tree: the
        # script has to. The real uv finds everything it installs in its cache.
        dropped = ("PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")
        env = {name: value for name, value in os.environ.items() if name not in dropped}
        env |= {
            "PATH": f"{self.stand_ins}{os.pathsep}{env['PATH']}",
            "TMPDIR": str(self.tmp_path / "tmp"),
            "UV_OFFLINE": "1",
        }
        self.calls.write_text("", encoding="utf-8")
        before = snapshot(self.tree)
        done = subprocess.run(
            ["bash", "setup/owner-setup.sh", *map(str, arguments)],
            cwd=self.tree,
            env=env,
            input=answers,
            capture_output=True,
            text=True,
            check=False,
        )
        after = snapshot(self.tree)
        assert sorted(name for name in before.keys() | after.keys() if before.get(name, 0) != after.get(name, 0)) == []
        return done

    def called(self, *tools: str) -> list[str]:
        """The calls the stand-ins of these tools recorded in the last run, one per line."""
        lines = self.calls.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if line.split()[0] in tools]


@pytest.fixture
def walkthrough(tmp_path: Path) -> Walkthrough:
    return Walkthrough(tmp_path)


def commands(output: str) -> list[str]:
    """The commands a run printed."""
    return [line.strip().removeprefix("$ ") for line in output.splitlines() if line.strip().startswith("$ ")]


def quota_requests(output: str) -> dict[str, int]:
    """The vCPUs of each quota request a run printed, by the family its quota is named after."""
    requests: dict[str, int] = {}
    family = ""
    for command in commands(output):
        named = re.search(r"Running On-Demand (.+?) instances", command)
        if named:
            family = named[1]
        elif "request-service-quota-increase" in command:
            requests[family] = int(command.split("--desired-value ")[1])
    return requests


def test_the_dry_run_prints_the_nine_steps_in_order(walkthrough: Walkthrough) -> None:
    done = walkthrough.run("--dry-run")

    assert done.returncode == 0, done.stderr
    headings = re.findall(r"^== Step (\d) of 9: (.+) ==$", done.stdout, flags=re.MULTILINE)
    assert [int(number) for number, _ in headings] == list(range(1, 10))
    # The order of section 8.6, by a word of each step.
    words = "quota, domain, GitHub App, configuration, billing, bootstrap, connection, ruleset, roster"
    for word, (_, title) in zip(words.split(", "), headings, strict=True):
        assert word.lower() in title.lower()
    # Every command of a real run is printed, and nothing was started: not AWS, not GitHub.
    printed = commands(done.stdout)
    started = "aws service-quotas, aws ssm, aws cloudformation, cdk bootstrap, gh repo edit, setup/apply-ruleset.sh"
    started += f", gh api 'apps/<slug>', rhi.owner_secret {SECRET}, aws secretsmanager"
    for tool in started.split(", "):
        assert any(tool in command for command in printed), tool
    assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []


@pytest.mark.parametrize(
    ("options", "requests"),
    [
        (["--max-instances", "2"], {"G and VT": 24}),
        (["--max-instances", "2", "--upgrade-pool"], {"G and VT": 120}),
        (["--max-instances", "4"], {"G and VT": 40}),
    ],
)
def test_the_g_family_request(walkthrough: Walkthrough, options: list[str], requests: dict[str, int]) -> None:
    done = walkthrough.run("--dry-run", "--step", "quotas", *options)

    assert done.returncode == 0, done.stderr
    assert quota_requests(done.stdout) == requests  # and no P-family request: the xl pool is not wanted


def test_a_p_family_quota_only_for_the_xl_pool(walkthrough: Walkthrough) -> None:
    done = walkthrough.run("--dry-run", "--step", "quotas", "--max-instances", "2", "--xl-pool")

    requests = quota_requests(done.stdout)
    assert requests.pop("P") > 0
    assert requests == {"G and VT": 24}


def test_a_command_runs_only_after_a_yes(walkthrough: Walkthrough) -> None:
    # The two pools are declined; then each command of the step is asked about.
    done = walkthrough.run("--step", "quotas", "--max-instances", "2", answers="n\nn\nn\n")
    assert done.returncode == 0, done.stderr
    assert walkthrough.called("aws") == []

    done = walkthrough.run("--step", "quotas", "--max-instances", "2", answers="n\nn\ny\nn\n")
    assert [call.split()[2] for call in walkthrough.called("aws")] == ["list-service-quotas"]

    done = walkthrough.run("--step", "quotas", "--max-instances", "2", answers="n\nn\ny\ny\n")
    assert done.returncode == 0, done.stderr
    asked, requested = walkthrough.called("aws")
    assert "list-service-quotas" in asked
    assert requested.endswith("--service-code ec2 --quota-code L-EXAMPLE --desired-value 24")
    assert "request-service-quota-increase" in requested


def test_the_apps_step_names_the_permissions_and_the_page(walkthrough: Walkthrough) -> None:
    done = walkthrough.run("--dry-run", "--step", "apps")

    assert done.returncode == 0, done.stderr
    assert "https://github.com/settings/apps/new" in done.stdout
    assert done.stdout.count("Permissions:") == 3  # one list for each App
    assert "login_client_id" in done.stdout  # where the login App's client ID goes
    assert "stays on the factory's host" in done.stdout
    # One read-back for each App, with a placeholder for the slug; nothing was started.
    assert sum(command.startswith("gh api 'apps/<slug>'") for command in commands(done.stdout)) == 3
    assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []


def test_each_app_is_read_back_and_judged(walkthrough: Walkthrough) -> None:
    said = "the permissions of section 8.6"
    walkthrough.app("course-login", 101)
    walkthrough.app("course-reporter", 202, statuses="write", metadata="read")

    # The login App and the reporter, each read after a yes. No installation yet, no factory App.
    done = walkthrough.run("--step", "apps", answers="course-login\ny\ncourse-reporter\ny\n\n\n")
    assert done.returncode == 0, done.stderr
    login, reporter = walkthrough.called("gh")
    assert login.startswith("gh api apps/course-login --jq ")
    assert reporter.startswith("gh api apps/course-reporter --jq ")
    assert "ID: 101" in done.stdout
    assert "ID: 202" in done.stdout
    assert "metadata=read statuses=write" in done.stdout
    assert done.stdout.count(f"These are {said}") == 2
    assert f"NOT {said}" not in done.stdout
    assert walkthrough.called("aws", "uv") == []

    # A permission too many on the first two; the factory's App as the step prints it.
    walkthrough.app("course-login", 101, metadata="read")
    walkthrough.app("course-reporter", 202, statuses="write", metadata="read", contents="read")
    factory = {"contents": "write", "pull_requests": "write", "issues": "write"}
    factory |= {"statuses": "read", "checks": "read", "actions": "read", "metadata": "read"}
    walkthrough.app("course-factory", 303, **factory)
    done = walkthrough.run("--step", "apps", answers="course-login\ny\ncourse-reporter\ny\n\ncourse-factory\ny\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.count(f"NOT {said}") == 2
    assert done.stdout.count(f"These are {said}") == 1
    assert done.stdout.index(f"NOT {said}") < done.stdout.index("ID: 303") < done.stdout.index(f"These are {said}")

    # Without a yes no App is read.
    done = walkthrough.run("--step", "apps", answers="course-login\nn\n")
    assert done.returncode == 0, done.stderr
    assert walkthrough.called("gh") == []


def test_the_reporter_key_comes_from_outside_the_tree(walkthrough: Walkthrough) -> None:
    def step(file: Path | str) -> subprocess.CompletedProcess[str]:
        return walkthrough.run("--dry-run", "--step", "apps", "--reporter-key", file)

    # A key file outside the tree: the writer's command names the secret and the file. The file is
    # not opened, so one that nobody may read is accepted too, and its content is never printed.
    key = walkthrough.owner_file(KEY, "reporter.pem")
    for mode in (0o000, 0o600):
        key.chmod(mode)
        accepted = step(key)
        assert accepted.returncode == 0, accepted.stderr
        (writer,) = [command for command in commands(accepted.stdout) if "rhi.owner_secret" in command]
        assert f" {SECRET} " in writer
        assert writer.endswith(f"--private-key-file {key}")
        assert KEY_MARK not in accepted.stdout + accepted.stderr
        assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []

    # The same file inside the tree, and a link in the tree to the file outside: refused.
    (walkthrough.tree / "reporter.pem").write_text(KEY, encoding="utf-8")
    (walkthrough.tree / "link.pem").symlink_to(key)
    for inside in (walkthrough.tree / "reporter.pem", "reporter.pem", walkthrough.tree / "link.pem"):
        refused = step(inside)
        assert refused.returncode != 0
        assert "inside the working tree" in refused.stderr
        assert "rhi.owner_secret" not in refused.stdout  # the writing is not offered
        assert KEY_MARK not in refused.stdout + refused.stderr
        assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []


def test_the_reporter_step_asks_before_it_stores(walkthrough: Walkthrough) -> None:
    walkthrough.app("course-reporter", 202, statuses="write", metadata="read")
    key = walkthrough.owner_file(KEY, "reporter.pem")
    # No login App yet; the reporter is read back; then the installation ID and the key file.
    asked = f"\ncourse-reporter\ny\n404\n{key}\n"

    # No to the writing: nothing is started, and the check of the secret is not offered.
    done = walkthrough.run("--step", "apps", answers=f"{asked}n\ny\n")
    assert done.returncode == 0, done.stderr
    assert walkthrough.called("uv", "aws") == []

    # Yes to the writing, no to the check: the module gets the App's ID that GitHub gave.
    done = walkthrough.run("--step", "apps", answers=f"{asked}y\nn\n")
    assert done.returncode == 0, done.stderr
    (stored,) = walkthrough.called("uv")
    assert stored.endswith(f"rhi.owner_secret {SECRET} --app-id 202 --installation-id 404 --private-key-file {key}")
    assert walkthrough.called("aws") == []
    assert KEY_MARK not in done.stdout + done.stderr
    assert list((walkthrough.tmp_path / "tmp").glob("rhi-owner-setup.*")) == []

    # A key file inside the tree is refused in a real run too, whichever way it is named.
    (walkthrough.tree / "reporter.pem").write_text(KEY, encoding="utf-8")
    done = walkthrough.run("--step", "apps", answers=f"\ncourse-reporter\ny\n404\n{walkthrough.tree}/reporter.pem\ny\n")
    assert done.returncode != 0
    assert "inside the working tree" in done.stderr
    done = walkthrough.run("--step", "apps", "--reporter-key", "reporter.pem", answers="\ncourse-reporter\ny\n404\ny\n")
    assert done.returncode != 0
    assert walkthrough.called("uv", "aws") == []


class Secrets:
    """A Secrets Manager client, as far as the module uses one: it records what is written."""

    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.written: list[tuple[str, dict[str, Any]]] = []

    def create_secret(self, **request: Any) -> dict[str, Any]:
        if self.exists:
            raise botocore.exceptions.ClientError({"Error": {"Code": "ResourceExistsException"}}, "CreateSecret")
        self.written.append(("create_secret", request))
        return {}

    def put_secret_value(self, **request: Any) -> dict[str, Any]:
        self.written.append(("put_secret_value", request))
        return {}


def test_the_module_stores_the_three_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    file = tmp_path / "reporter.pem"
    file.write_text(KEY, encoding="utf-8")
    arguments = [SECRET, "--app-id", "202", "--installation-id", "404", "--private-key-file", str(file)]
    absent, present, untouched = Secrets(exists=False), Secrets(exists=True), Secrets(exists=False)

    assert owner_secret.main(arguments, absent) == 0
    assert owner_secret.main(arguments, present) == 0
    # Created when it is absent, and its value replaced when it exists: the same three fields.
    ((created, creation),) = absent.written
    ((updated, update),) = present.written
    assert (created, creation["Name"]) == ("create_secret", SECRET)
    assert (updated, update["SecretId"]) == ("put_secret_value", SECRET)
    assert creation["SecretString"] == update["SecretString"]
    assert json.loads(creation["SecretString"]) == {"app_id": "202", "installation_id": "404", "private_key": KEY}

    # A key file that cannot be read stores nothing. Nothing printed holds the key.
    arguments[-1] = str(tmp_path / "absent.pem")
    assert owner_secret.main(arguments, untouched) == 1
    assert untouched.written == []
    printed = capsys.readouterr()
    assert SECRET in printed.out
    assert KEY_MARK not in printed.out + printed.err


def test_the_configuration_comes_from_outside_the_tree(walkthrough: Walkthrough) -> None:
    def step(file: Path | str) -> subprocess.CompletedProcess[str]:
        return walkthrough.run("--dry-run", "--step", "configuration", "--config", file)

    # A file outside the tree that the loader accepts: checked, and nothing is written.
    accepted = step(walkthrough.owner_file())
    assert accepted.returncode == 0, accepted.stderr
    assert [call.split()[7] for call in walkthrough.called("uv")] == ["check"]
    assert walkthrough.called("aws") == []

    # The same file inside the tree, and a link in the tree to the file outside: refused unread.
    (walkthrough.tree / "owner.toml").write_text(EXAMPLE, encoding="utf-8")
    (walkthrough.tree / "link.toml").symlink_to(walkthrough.outside / "owner.toml")
    for inside in (walkthrough.tree / "owner.toml", "owner.toml", walkthrough.tree / "link.toml"):
        refused = step(inside)
        assert refused.returncode != 0
        assert "inside the working tree" in refused.stderr
        assert walkthrough.called("uv") == []

    # Files outside the tree that the loader refuses: a session window, and a missing key.
    with_a_window = EXAMPLE.replace("session_windows = []", f'session_windows = ["Monday 09:00 {MARK}"]')
    without_the_domain = EXAMPLE.replace('domain = "example.com"', "").replace("admin@", f"admin{MARK}@")
    for text, key in ((with_a_window, "session_windows"), (without_the_domain, "domain")):
        assert text != EXAMPLE
        refused = step(walkthrough.owner_file(text, "refused.toml"))
        assert refused.returncode != 0
        assert repr(key) in refused.stderr  # the loader's own refusal, which names the key
        assert MARK not in refused.stdout + refused.stderr


def test_the_configuration_step_asks_before_it_writes(walkthrough: Walkthrough) -> None:
    file = walkthrough.owner_file()

    # No to the check: the writing is not offered, whatever the answers that follow.
    done = walkthrough.run("--step", "configuration", "--config", file, answers="n\ny\ny\n")
    assert done.returncode == 0, done.stderr
    assert walkthrough.called("uv", "aws") == []

    # Yes to the check, no to the writing.
    done = walkthrough.run("--step", "configuration", "--config", file, answers="y\nn\n")
    assert done.returncode == 0, done.stderr
    assert [call.split()[7] for call in walkthrough.called("uv")] == ["check"]

    # Yes to both, no to the read that lists the parameters' names: the module is asked to write.
    done = walkthrough.run("--step", "configuration", "--config", file, answers="y\ny\nn\n")
    assert done.returncode == 0, done.stderr
    assert [call.split()[7:] for call in walkthrough.called("uv")] == [["check", str(file)], ["write", str(file)]]
    assert walkthrough.called("aws") == []

    # A file the loader refuses stops the step before the writing is offered.
    refused = walkthrough.owner_file(EXAMPLE.replace("pools = []", "pools = [1]"), "refused.toml")
    done = walkthrough.run("--step", "configuration", "--config", refused, answers="y\ny\ny\n")
    assert done.returncode != 0
    assert [call.split()[7] for call in walkthrough.called("uv")] == ["check"]


class Parameters:
    """An SSM client, as far as the writer uses one: it records what is put."""

    def __init__(self) -> None:
        self.put: list[dict[str, Any]] = []

    def put_parameter(self, **request: Any) -> dict[str, Any]:
        self.put.append(request)
        return {"Version": 1}


def test_the_module_writes_what_the_configuration_writer_writes(tmp_path: Path) -> None:
    file = tmp_path / "owner.toml"
    file.write_text(EXAMPLE.replace("example.com", "example.org"), encoding="utf-8")
    expected, written, untouched = Parameters(), Parameters(), Parameters()
    config.write_ssm(config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(file)}), expected)

    assert owner_config.main(["write", str(file)], written) == 0
    assert written.put == expected.put
    assert {"Name": "/rhi/config/domain", "Value": "example.org", "Type": "String", "Overwrite": True} in written.put

    # A check writes nothing, and a file the loader refuses is refused before anything is written.
    assert owner_config.main(["check", str(file)], untouched) == 0
    file.write_text(EXAMPLE.replace("pools = []", "pools = [1]"), encoding="utf-8")
    assert owner_config.main(["write", str(file)], untouched) == 1
    assert untouched.put == []


def test_the_scratch_copy_is_byte_identical_afterwards(walkthrough: Walkthrough) -> None:
    # The whole dry run, with every option and a file to check. `run` compares the copy.
    file, key = walkthrough.owner_file(), walkthrough.owner_file(KEY, "reporter.pem")
    options = ["--max-instances", "2", "--upgrade-pool", "--xl-pool", "--config", file, "--reporter-key", key]
    done = walkthrough.run("--dry-run", *options)

    assert done.returncode == 0, done.stderr
    assert [call.split()[7] for call in walkthrough.called("uv")] == ["check"]
    assert walkthrough.called("aws", "gh", "cdk", "npm") == []
    # What the tools wrote as they ran went to a temporary folder, which is gone.
    assert list((walkthrough.tmp_path / "tmp").glob("rhi-owner-setup.*")) == []


def test_the_real_uv_leaves_no_environment_in_the_tree(walkthrough: Walkthrough) -> None:
    # In a fresh checkout `uv run --locked` would make `.venv` in the tree, and Python would keep
    # bytecode beside the modules it imports. Offline: uv installs from its cache, which the
    # `uv run --locked pytest` that runs this test has filled with the same libraries.
    assert not (walkthrough.tree / ".venv").exists()
    file = walkthrough.owner_file()
    done = walkthrough.run("--dry-run", "--step", "configuration", "--config", file, real_uv=True)

    assert done.returncode == 0, done.stderr
    assert "accepts the file" in done.stdout
    assert list((walkthrough.tmp_path / "tmp").glob("rhi-owner-setup.*")) == []
