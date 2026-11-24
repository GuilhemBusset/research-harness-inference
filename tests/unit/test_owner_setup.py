"""The owner's walkthrough (setup/owner-setup.sh), run in a scratch copy of the repository.

`aws`, `gh`, `cdk` and `npm` are stand-ins that record every call, so no test reaches AWS or
GitHub. `uv` is a stand-in too, which runs the configuration module and the module that reads the
GitHub Apps back on the test's own interpreter and, as uv does, makes the project environment
where `UV_PROJECT_ENVIRONMENT` says, or else in `.venv` in the tree. The Apps are read from the
stand-in for GitHub of tests/unit/test_owner_apps.py, which answers 404 to anything but an App's
own token. The `uv` stand-in records the module that stores the reporter's secret and never runs
it. One test runs the real uv instead, offline. After every run the scratch copy is compared with
what it was, byte for byte.

The scanner's list of local values is a file in the test's temporary folder in every run: by
`HYGIENE_LOCAL_VALUES`, or, for the scanner's default file, under a home directory made there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions
import pytest

from rhi import config, owner_config, owner_secret

REPO = Path(__file__).resolve().parents[2]
# Not files of the repository: git's own folder, and what the tools leave behind as they run.
LEFT_OUT = shutil.ignore_patterns(".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache")
EXAMPLE = (REPO / config.EXAMPLE_FILE).read_text(encoding="utf-8")
# The example with its mock pool broken: a file the loader refuses.
REFUSED = EXAMPLE.replace("max_instances = 2\n", "max_instances = 0\n")
assert REFUSED != EXAMPLE
# Part of every bad value below, so a test can tell that a refusal does not repeat the value.
MARK = "7701"
SECRET = "rhi/reporter/github-app"
# What stands for the reporter's private key: no real key, and a mark no output may repeat.
KEY_MARK = "stand-in-for-a-key"
KEY = f"{KEY_MARK} first line\n{KEY_MARK} last line\n"
# The values of the example configuration's private keys, and one the owner enters at the prompt.
PRIVATE = sorted(str(getattr(config.load({"RHI_CONFIG": "example"}), key)) for key in config.PRIVATE_KEYS)
ENTERED = "Entered At The Prompt"


def _load_github_stand_in() -> Any:
    """The stand-in for GitHub that tests/unit/test_owner_apps.py defines, loaded from its file."""
    name = "owner_apps_github_stand_in"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_owner_apps.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass looks its module up while it is defined
    spec.loader.exec_module(module)
    return module


STAND_IN = _load_github_stand_in()
REPOSITORY = STAND_IN.REPOSITORY
FACTORY = STAND_IN.FACTORY


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
        # The list of local values of every run, unless the run names another. Its folder is absent.
        self.values = tmp_path / "scanner" / "hygiene-values"
        self.calls = tmp_path / "calls"
        self.stand_ins = tmp_path / "bin"
        self.stand_ins.mkdir()
        record = f'echo "$(basename "$0") $*" >> {shlex.quote(str(self.calls))}'
        for name in ("cdk", "npm"):
            self.stand_in(name, record)
        self.stand_in("gh", record)
        # GitHub, for the module that reads the Apps back. It answers to an App's own tokens only.
        self.github = STAND_IN.GitHub(tmp_path / "github")
        # The one answer the walkthrough reads from AWS: a quota's code and its value.
        self.stand_in("aws", f"{record}\n[ \"$2\" != list-service-quotas ] || printf 'L-EXAMPLE\\t0.0\\n'")
        self.stand_in(
            "uv",
            f"{record}\n"
            '[ "$1 $2 $3 $4 $5" = "run --locked --no-dev python -m" ] || exit 64\n'
            'mkdir -p "${UV_PROJECT_ENVIRONMENT:-$PWD/.venv}"\n'
            # Recorded, never run: the two writers would look for AWS.
            '[ "$6" != rhi.owner_secret ] || exit 0\n'
            '[ "$6" = rhi.owner_config ] || [ "$6" = rhi.owner_apps ] || exit 64\n'
            '[ "$7" != write ] || exit 0\n'
            "shift 4\n"
            f'exec {shlex.quote(sys.executable)} "$@"',
        )

    def stand_in(self, name: str, body: str) -> None:
        path = self.stand_ins / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def owner_file(self, text: str = EXAMPLE, name: str = "owner.toml") -> Path:
        path = self.outside / name
        path.write_text(text, encoding="utf-8")
        return path

    def run(
        self, *arguments: object, answers: str = "", real_uv: bool = False, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the walkthrough in the scratch copy, and check that the copy is as it was. `env` is
        added to the environment of the run, after the list of local values."""
        if real_uv:
            (self.stand_ins / "uv").unlink()
        # Nothing the caller's shell sets may keep bytecode or an environment out of the tree: the
        # script has to. The real uv finds everything it installs in its cache.
        # Nor may the region the caller's shell sets: a run names its own.
        dropped = ("PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")
        dropped += ("AWS_REGION", "AWS_DEFAULT_REGION")
        added = env or {}
        env = {name: value for name, value in os.environ.items() if name not in dropped}
        env |= {
            "PATH": f"{self.stand_ins}{os.pathsep}{env['PATH']}",
            "TMPDIR": str(self.tmp_path / "tmp"),
            "UV_OFFLINE": "1",
            "HYGIENE_LOCAL_VALUES": str(self.values),
            # The owner's own `gh` login, which no request to GitHub may carry.
            "GH_TOKEN": STAND_IN.OWNER_LOGIN,
        }
        env |= self.github.env
        env |= added
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
def walkthrough(tmp_path: Path) -> Any:
    walkthrough = Walkthrough(tmp_path)
    yield walkthrough
    walkthrough.github.server.shutdown()


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
    started += ", rhi.owner_apps reporter, rhi.owner_apps login, rhi.owner_apps factory"
    started += f", rhi.owner_secret {SECRET}, aws secretsmanager"
    started += ", rhi.owner_config values"
    for tool in started.split(", "):
        assert any(tool in command for command in printed), tool
    assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []
    # The list of local values is the second half of step 4: after the configuration, before step 5.
    in_order = r"^== Step 4 of 9: .*^== Step 4b of 9: [^\n]*values[^\n]* ==$.*^== Step 5 of 9: "
    assert re.search(in_order, done.stdout, flags=re.MULTILINE | re.DOTALL)
    assert not walkthrough.values.parent.exists()


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
    # One read-back for each App, with placeholders for what a real run asks; nothing was started.
    read = [command.split()[9:11] for command in commands(done.stdout) if "rhi.owner_apps" in command]
    assert read == [["reporter", "--app-id"], ["login", "--slug"], ["factory", "--app-id"]]
    assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []


def apps(github: Any, reporter: str = "selected", factory: str = "selected", **reporter_permissions: str) -> list[Path]:
    """The three Apps, registered with the stand-in for GitHub; the reporter's and the factory's
    keys. Each is installed on this repository, alone when its selection is `selected`."""
    reach = {"selected": [REPOSITORY], "all": [REPOSITORY, STAND_IN.OTHER]}
    permissions = reporter_permissions or STAND_IN.REPORTER
    keys = [github.app(202, "course-reporter", [(404, reporter, reach[reporter])], permissions)]
    keys.append(github.app(303, "course-factory", [(505, factory, reach[factory])], FACTORY))
    github.app(101, "course-login")
    return keys


def test_each_app_is_read_back_and_judged(walkthrough: Walkthrough) -> None:
    said = "the permissions of section 8.6"
    reporter, factory = apps(walkthrough.github)
    # The reporter's ID, key, this repository, a yes; no to storing; the login App; the factory's App.
    answers = f"202\n{reporter}\n{REPOSITORY}\ny\nn\ncourse-login\ny\n303\n{factory}\ny\n"

    done = walkthrough.run("--step", "apps", answers=answers)
    assert done.returncode == 0, done.stderr
    assert [call.split()[6:8] for call in walkthrough.called("uv")] == [
        ["rhi.owner_apps", "reporter"],
        ["rhi.owner_apps", "login"],
        ["rhi.owner_apps", "factory"],
    ]
    for app_id in (202, 101, 303):
        assert f"ID {app_id}" in done.stdout
    assert done.stdout.count(f"These are {said}") == 3
    assert f"Installation 404: selected repositories: {REPOSITORY}" in done.stdout
    assert "device flow is on" in done.stdout
    assert walkthrough.called("aws", "gh") == []

    # A permission too many on the reporter, a factory's App on every repository, and a login App
    # whose device flow is off: judged, refused and refused, and the step goes on to the end.
    apps(walkthrough.github, factory="all", statuses="write", contents="read")
    walkthrough.github.app(101, "course-login", device_flow=False)
    done = walkthrough.run("--step", "apps", answers=answers)
    assert done.returncode == 0, done.stderr
    assert done.stdout.count(f"NOT {said}") == 1
    assert done.stderr.count("refused: ") == 2
    assert "device flow is off" in done.stderr
    assert "installation 505 is on all repositories" in done.stderr

    # Without a yes no App is read.
    done = walkthrough.run("--step", "apps", answers=f"202\n{reporter}\n{REPOSITORY}\nn\n\n")
    assert done.returncode == 0, done.stderr
    assert walkthrough.called("uv") == []


def test_no_command_of_the_apps_step_is_a_call_with_the_owners_login(walkthrough: Walkthrough) -> None:
    reporter, factory = apps(walkthrough.github)
    keys = ["--reporter-key", reporter, "--factory-key", factory]

    dry = walkthrough.run("--dry-run", "--step", "apps", *keys)
    real = walkthrough.run("--step", "apps", *keys, answers=f"202\n{REPOSITORY}\ny\ny\ny\ncourse-login\ny\n303\ny\n")
    for done in (dry, real):
        assert done.returncode == 0, done.stderr
        assert not [command for command in commands(done.stdout) if command.split()[0] in ("gh", "curl")]
    assert walkthrough.called("gh") == []
    # GitHub was asked, by the module only, and never with the owner's login (`GH_TOKEN` was set):
    # each request carried an App's JWT or a token minted from one, but for the device code.
    github = walkthrough.github
    minted = set(github.tokens)
    assert minted
    assert STAND_IN.OWNER_LOGIN not in github.tokens_seen()
    assert {token for token in github.tokens_seen() if token.count(".") != 2} <= minted


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


def test_the_app_keys_come_from_outside_the_tree(walkthrough: Walkthrough) -> None:
    def step(option: str, file: Path | str) -> subprocess.CompletedProcess[str]:
        return walkthrough.run("--dry-run", "--step", "apps", option, file)

    # The reporter's key signs the reading of the reporter and of the login App; the factory's key
    # the reading of the factory's App. Neither is opened by the step, nor printed.
    key = walkthrough.owner_file(KEY, "app.pem")
    accepted = step("--reporter-key", key)
    assert accepted.returncode == 0, accepted.stderr
    reporter, login = [command for command in commands(accepted.stdout) if " reporter " in command or " login " in command]
    assert f"--key-file {key}" in reporter
    assert f"--key-file {key}" in login
    accepted = step("--factory-key", key)
    assert accepted.returncode == 0, accepted.stderr
    (factory,) = [command for command in commands(accepted.stdout) if "rhi.owner_apps factory" in command]
    assert f"--key-file {key}" in factory
    assert KEY_MARK not in accepted.stdout + accepted.stderr
    assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []

    # The factory's key inside the tree, or a link in the tree to the file outside: refused.
    (walkthrough.tree / "factory.pem").write_text(KEY, encoding="utf-8")
    (walkthrough.tree / "link.pem").symlink_to(key)
    for inside in (walkthrough.tree / "factory.pem", "factory.pem", walkthrough.tree / "link.pem"):
        refused = step("--factory-key", inside)
        assert refused.returncode != 0
        assert "inside the working tree" in refused.stderr
        assert "rhi.owner_apps factory" not in refused.stdout  # the reading is not offered
        assert KEY_MARK not in refused.stdout + refused.stderr
        assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []


def test_the_reporter_step_asks_before_it_stores(walkthrough: Walkthrough) -> None:
    reporter, _ = apps(walkthrough.github)
    # The reporter's ID, its key, this repository, and a yes to reading it; then the writing.
    asked = f"202\n{reporter}\n{REPOSITORY}\ny\n"

    # No to the writing: the check of the secret is not offered, and nothing stores.
    done = walkthrough.run("--step", "apps", answers=f"{asked}n\ny\n")
    assert done.returncode == 0, done.stderr
    assert not [call for call in walkthrough.called("uv") if "rhi.owner_secret" in call]
    assert walkthrough.called("aws") == []

    # Yes to the writing, no to the check: the module stores, and nothing is left behind.
    done = walkthrough.run("--step", "apps", answers=f"{asked}y\nn\n")
    assert done.returncode == 0, done.stderr
    (stored,) = [call for call in walkthrough.called("uv") if "rhi.owner_secret" in call]
    assert f"rhi.owner_secret {SECRET} " in stored
    assert walkthrough.called("aws") == []
    key_lines = [line for line in reporter.read_text(encoding="utf-8").splitlines() if "KEY" not in line]
    assert not [line for line in key_lines if line in done.stdout + done.stderr]
    assert list((walkthrough.tmp_path / "tmp").glob("rhi-owner-setup.*")) == []

    # A key file inside the tree is refused in a real run too, whichever way it is named.
    (walkthrough.tree / "reporter.pem").write_text(KEY, encoding="utf-8")
    done = walkthrough.run("--step", "apps", answers=f"202\n{walkthrough.tree}/reporter.pem\ny\n")
    assert done.returncode != 0
    assert "inside the working tree" in done.stderr
    done = walkthrough.run("--step", "apps", "--reporter-key", "reporter.pem", answers="202\ny\n")
    assert done.returncode != 0
    assert walkthrough.called("uv", "aws") == []


def test_the_reporter_step_stores_the_installation_github_reports(walkthrough: Walkthrough) -> None:
    reporter, _ = apps(walkthrough.github)
    asked = f"202\n{reporter}\n{REPOSITORY}\ny\n"

    # The App's ID and the installation's ID are GitHub's, and no answer gave the installation's.
    done = walkthrough.run("--step", "apps", answers=f"{asked}y\nn\n")
    assert done.returncode == 0, done.stderr
    (stored,) = [call for call in walkthrough.called("uv") if "rhi.owner_secret" in call]
    assert stored.endswith(f"rhi.owner_secret {SECRET} --app-id 202 --installation-id 404 --private-key-file {reporter}")

    # A reporter on every repository, or with no installation yet: nothing is offered to store.
    apps(walkthrough.github, reporter="all")
    done = walkthrough.run("--step", "apps", answers=f"{asked}y\nn\n")
    assert "refused: " in done.stderr
    walkthrough.github.app(202, "course-reporter", [], STAND_IN.REPORTER)
    done = walkthrough.run("--step", "apps", answers=f"{asked}y\nn\n")
    assert "no installation" in done.stdout
    assert not [call for call in walkthrough.called("uv") if "rhi.owner_secret" in call]


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


@pytest.mark.parametrize(
    ("error", "named"),
    [
        (botocore.exceptions.NoRegionError(), "must give a region"),
        (botocore.exceptions.MissingDependencyException(msg=f"no awscrt {MARK}"), "MissingDependencyException"),
    ],
)
def test_a_client_the_sdk_cannot_make_is_named_by_its_cause(
    monkeypatch: pytest.MonkeyPatch, error: Exception, named: str
) -> None:
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *_: (_ for _ in ()).throw(error))
    with pytest.raises(RuntimeError) as raised:
        owner_secret.store(SECRET, "{}")
    assert named in str(raised.value)
    assert MARK not in str(raised.value)  # the SDK's own text is not repeated
    if not isinstance(error, botocore.exceptions.NoRegionError):
        assert "region" not in str(raised.value)


def test_only_aws_region_gives_the_region_to_every_command(walkthrough: Walkthrough) -> None:
    file = walkthrough.owner_file()
    seen = walkthrough.tmp_path / "region"
    # The stand-in for uv records the region the SDK of a Python step would read, then goes on.
    uv = walkthrough.stand_ins / "uv"
    uv.write_text(
        uv.read_text(encoding="utf-8").replace(
            "#!/usr/bin/env bash\n", f'#!/usr/bin/env bash\necho "${{AWS_DEFAULT_REGION-unset}}" >> {seen}\n', 1
        ),
        encoding="utf-8",
    )
    region = {"AWS_REGION": "us-west-2"}
    for only, expected in ((region, "us-west-2"), (region | {"AWS_DEFAULT_REGION": "us-east-1"}, "us-east-1")):
        seen.write_text("", encoding="utf-8")
        done = walkthrough.run("--step", "configuration", "--config", file, answers="y\ny\nn\n", env=only)
        assert done.returncode == 0, done.stderr
        assert seen.read_text(encoding="utf-8").split() == [expected, expected]  # the check and the writing


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

    # Files outside the tree that the loader refuses: a session window, and a missing key. The
    # retired key goes first, before the pool tables, where TOML reads it as a key of the file.
    with_a_window = f'session_windows = ["Monday 09:00 {MARK}"]\n' + EXAMPLE
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
    refused = walkthrough.owner_file(REFUSED, "refused.toml")
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


class Zones:
    """An EC2 client, as far as the module's zone check uses one: a region of the example's four
    zones, each of which offers every instance type asked for (tests/unit/test_owner_zones.py
    holds what the check refuses)."""

    NAMES = tuple(config.load({"RHI_CONFIG": "example"}).zones or ())

    def describe_availability_zones(self, **_request: Any) -> dict[str, Any]:
        return {"AvailabilityZones": [{"ZoneName": name} for name in self.NAMES]}

    def describe_instance_type_offerings(self, **request: Any) -> dict[str, Any]:
        (asked,) = request["Filters"]
        offers = [{"InstanceType": kind, "Location": zone} for zone in self.NAMES for kind in asked["Values"]]
        return {"InstanceTypeOfferings": offers}


def test_the_module_writes_what_the_configuration_writer_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The zone check's client is a stand-in in every call, and the SDK is asked for none.
    def refuse(*_arguments: object, **_options: object) -> None:
        raise AssertionError("a test asked the SDK for a real client")

    monkeypatch.setattr(boto3, "client", refuse)
    file = tmp_path / "owner.toml"
    file.write_text(EXAMPLE.replace("example.com", "example.org"), encoding="utf-8")
    expected, written, untouched = Parameters(), Parameters(), Parameters()
    config.write_ssm(config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(file)}), expected)

    assert owner_config.main(["write", str(file)], written, Zones()) == 0
    assert written.put == expected.put
    assert {"Name": "/rhi/config/domain", "Value": "example.org", "Type": "String", "Overwrite": True} in written.put

    # A file that still holds the retired session windows, empty, writes the same parameters, and
    # nothing in the setup writes or asks for them.
    file.write_text("session_windows = []\n" + EXAMPLE.replace("example.com", "example.org"), encoding="utf-8")
    retired = Parameters()
    assert owner_config.main(["write", str(file)], retired, Zones()) == 0
    assert retired.put == expected.put
    assert not any("session_windows" in request["Name"] for request in retired.put)
    assert "session_windows" not in (REPO / "setup/owner-setup.sh").read_text(encoding="utf-8")

    # A check writes nothing, and a file the loader refuses is refused before anything is written.
    assert owner_config.main(["check", str(file)], untouched) == 0
    file.write_text(REFUSED, encoding="utf-8")
    assert owner_config.main(["write", str(file)], untouched, Zones()) == 1
    assert untouched.put == []


def scan(file: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The repository's own scanner, on one file, in this environment."""
    scanner = [sys.executable, str(REPO / "tools/hygiene/scan.py"), "--paths", str(file)]
    return subprocess.run(scanner, env=os.environ | env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("named", ["by the variable", "by default"])
def test_the_step_writes_each_private_value_once(walkthrough: Walkthrough, named: str) -> None:
    # The two ways the scanner finds its list. An empty variable is an unset one, for both.
    home = walkthrough.tmp_path / "home"
    if named == "by the variable":
        values, env = walkthrough.values, {"HYGIENE_LOCAL_VALUES": str(walkthrough.values)}
    else:
        values = home / ".config" / "research-harness-inference" / "hygiene-values"
        env = {"HOME": str(home), "HYGIENE_LOCAL_VALUES": "", "XDG_CONFIG_HOME": ""}
    file = walkthrough.owner_file()
    scratch = walkthrough.outside / "scratch.txt"
    scratch.write_text(f"served under {PRIVATE[0]}\n", encoding="utf-8")
    # No list yet, so no finding: the rule is off, or the scan refuses the absent file the variable names.
    assert scan(scratch, env).returncode == (2 if named == "by the variable" else 0)

    # The owner enters one value more and one the configuration holds already, then says yes.
    done = walkthrough.run("--step", "values", "--config", file, answers=f"{ENTERED}\n{PRIVATE[0]}\n\ny\n", env=env)
    assert done.returncode == 0, done.stderr
    assert sorted(values.read_text(encoding="utf-8").splitlines()) == sorted([*PRIVATE, ENTERED])
    assert stat.S_IMODE(values.stat().st_mode) == 0o600
    assert stat.S_IMODE(values.parent.stat().st_mode) == 0o700
    # It prints how many values the list holds, and no value.
    assert f"holds {len(PRIVATE) + 1} values" in done.stdout
    assert not [value for value in [*PRIVATE, ENTERED] if value in done.stdout + done.stderr]

    # Run again, the list is what it was. One that others could read is the owner's alone again.
    written = values.read_bytes()
    values.chmod(0o644)
    done = walkthrough.run("--step", "values", "--config", file, answers=f"{ENTERED.upper()}\n\ny\n", env=env)
    assert done.returncode == 0, done.stderr
    assert values.read_bytes() == written
    assert stat.S_IMODE(values.stat().st_mode) == 0o600
    assert f"holds {len(PRIVATE) + 1} values" in done.stdout

    # The scanner now reports the scratch file, under the rule, without showing the value.
    found = scan(scratch, env)
    assert found.returncode == 1, found.stderr
    assert f"{scratch}:1: local-value: " in found.stdout
    assert PRIVATE[0] not in found.stdout + found.stderr


def test_a_list_that_exists_keeps_its_lines(walkthrough: Walkthrough) -> None:
    walkthrough.values.parent.mkdir()
    walkthrough.values.write_text("# the owner's own\nkept by the owner", encoding="utf-8")  # no end of line

    done = walkthrough.run("--step", "values", "--config", walkthrough.owner_file(), answers="\ny\n")
    assert done.returncode == 0, done.stderr
    lines = walkthrough.values.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# the owner's own", "kept by the owner"]
    assert sorted(lines[2:]) == PRIVATE
    assert f"holds {len(PRIVATE) + 1} values" in done.stdout  # a comment is not a value


def test_the_values_step_asks_before_it_writes(walkthrough: Walkthrough) -> None:
    file = walkthrough.owner_file()

    # No further value, then no to the writing: nothing is started and there is no list.
    done = walkthrough.run("--step", "values", "--config", file, answers="\nn\n")
    assert done.returncode == 0, done.stderr
    assert walkthrough.called("uv") == []

    # A value the scanner would refuse to read, which would stop every scan: nothing is written,
    # and the refusal does not repeat it.
    done = walkthrough.run("--step", "values", "--config", file, answers=f"{MARK[:3]}\n\ny\n")
    assert done.returncode != 0
    assert "refused" in done.stderr
    assert MARK[:3] not in done.stdout + done.stderr
    assert not walkthrough.values.parent.exists()


def test_a_dry_run_of_the_values_step_writes_no_file(walkthrough: Walkthrough) -> None:
    done = walkthrough.run("--dry-run", "--step", "values", "--config", walkthrough.owner_file())

    assert done.returncode == 0, done.stderr
    # What it would do: the command, which names the list. It read no answer and started nothing.
    (writer,) = [command for command in commands(done.stdout) if "rhi.owner_config values" in command]
    assert writer.endswith(f"--list {walkthrough.values}")
    assert walkthrough.called("aws", "gh", "cdk", "npm", "uv") == []
    assert not walkthrough.values.parent.exists()  # neither the list nor its folder


def test_the_list_is_never_written_in_the_repository(walkthrough: Walkthrough) -> None:
    file = walkthrough.owner_file()
    # In the tree: by its path, by a path from the folder the step is started in, by a link in the
    # tree to a file outside it, and by a link outside the tree to a file in it.
    target = walkthrough.outside / "hygiene-values"
    target.write_text("", encoding="utf-8")
    (walkthrough.tree / "link-values").symlink_to(target)
    (walkthrough.outside / "link-values").symlink_to(walkthrough.tree / "hygiene-values")
    inside = (walkthrough.tree / "hygiene-values", "docs/hygiene-values", walkthrough.tree / "link-values")

    for named in (*inside, walkthrough.outside / "link-values"):
        for options in (["--dry-run"], []):
            # `run` checks that the scratch copy is as it was: no list in it.
            step = [*options, "--step", "values", "--config", file]
            refused = walkthrough.run(*step, answers="\ny\n", env={"HYGIENE_LOCAL_VALUES": str(named)})
            assert refused.returncode != 0, named
            assert "inside the working tree" in refused.stderr
            assert "rhi.owner_config" not in refused.stdout  # the writing is not offered
            assert walkthrough.called("uv") == []
    assert target.read_text(encoding="utf-8") == ""


def test_the_scratch_copy_is_byte_identical_afterwards(walkthrough: Walkthrough) -> None:
    # The whole dry run, with every option and a file to check. `run` compares the copy.
    file, key = walkthrough.owner_file(), walkthrough.owner_file(KEY, "reporter.pem")
    options = ["--max-instances", "2", "--upgrade-pool", "--xl-pool", "--config", file, "--reporter-key", key]
    options += ["--factory-key", key]
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
