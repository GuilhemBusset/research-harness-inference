"""Tests of the hygiene scanner (tools/hygiene/scan.py).

Every value a rule must catch is assembled at run time (`j("AK", "IA", ...)`), never written out,
so this file passes the scanner itself and trips no other secret scanner. The parametrized cases
carry ids for the same reason: a test report never prints a value.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import scan

SCAN = Path(scan.__file__).resolve()
REPO = SCAN.parents[2]


def j(*parts: str) -> str:
    return "".join(parts)


# Values that look real. None of them is.
ACCOUNT = j("4444", "5555", "6666")
OTHER_ACCOUNT = j("9012", "3456", "7890")
ACCESS_KEY = j("AK", "IA", "Q7XM2P4KD9WT6ZRB")
ROLE_ID = j("AR", "OA", "Q7XM2P4KD9WT6ZRB")
SECRET_KEY = j("q7Xm2P4kD9", "wT6zRbV1nC", "8hJ5sL3fG0", "yU2eA4oI6p")
GITHUB_TOKEN = j("gh", "p_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8")
GITHUB_PAT = j("github", "_pat_", "11ABCDEFG0", "abcdefghij_klmnopqrst", "UVWXYZ0123456789abcd")
ANTHROPIC_KEY = j("sk", "-ant-", "api03-", "aBcDeFgHiJkLmNoPqRsTuVwXyZ-012345")
OPENAI_PROJECT_KEY = j("sk", "-proj-", "aBcDeFgHiJkLmNoPqRsTuVwXyZ_012345")
OPENAI_LEGACY_KEY = j("sk", "-", "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345", "6789abcdEFGH")
HF_TOKEN = j("hf", "_", "aBcDeFgHiJkLmNoPqRsTuVwXyZ01234567")
GRAFANA_TOKEN = j("gl", "sa_", "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345", "_", "0a1b2c3d")
PEM_HEADER = j("-----BEGIN RSA ", "PRIVATE KEY", "-----")
OPENSSH_HEADER = j("-----BEGIN OPENSSH ", "PRIVATE KEY", "-----")
PKCS8_HEADER = j("-----BEGIN ", "PRIVATE KEY", "-----")
PKCS8_FOOTER = j("-----END ", "PRIVATE KEY", "-----")
KEY_BODY = j("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "BKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj")
URL_PASSWORD = j("hunter", "2-s3cr3t", "!")
API_HOST = j("a1b2c3d4e5", ".execute-api", ".eu-north-1.amazonaws.com")
CLOUDFRONT_HOST = j("d3k9x8example1", ".cloudfront", ".net")
ALB_HOST = j("gateway-1234567890", ".us-east-1", ".elb", ".amazonaws.com")
NLB_HOST = j("pool-0a1b2c3d4e5f6a7b", ".elb", ".us-east-1.amazonaws.com")
FUNCTION_URL = j("abcdefghij0123456789abcdefghij01", ".lambda-url", ".us-east-1.on.aws")
PROMETHEUS_WORKSPACE = j("ws", "-0a1b2c3d", "-4e5f-6a7b-8c9d", "-0e1f2a3b4c5d")
HOSTED_ZONE = j("hostedzone", "/", "Z0123456ABCDEFGHIJKL")
INSTANCE_ID = j("i", "-", "0a1b2c3d4e5f6a7b8")
SUBNET_ID = j("subnet", "-", "0a1b2c3d4e5f6a7b8")
EMAIL = j("jane.doe", "@", "corp-mail.io")
NUMERIC_DOMAIN_EMAIL = j("someone", "@", "163.com")
LINUX_HOME = j("/ho", "me/", "alice")
MAC_HOME = j("/Us", "ers/", "alice")
WSL_HOME = j("/mnt/c", "/Us", "ers/", "alice")
WINDOWS_HOME = j("C:", "\\Us", "ers\\", "alice")

POSITIVES = [
    ("account-in-arn", j("arn:aws:iam::", ACCOUNT, ":role/deploy"), "iam arn"),
    ("account-in-arn", j("arn:aws-us-gov:s3:us-gov-west-1:", ACCOUNT, ":thing"), "other partition"),
    ("account-id", f"account = {ACCOUNT}", "bare"),
    ("account-id", f'"{ACCOUNT}"', "quoted"),
    ("account-id", f"cdk-hnb659fds-assets-{ACCOUNT}-us-east-1", "in a bucket name"),
    ("account-id", f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/engine", "registry host"),
    ("account-id", f"arn%3Aaws%3Aiam%3A%3A{ACCOUNT}%3Arole", "url-encoded arn"),
    ("account-id", f"pool{ACCOUNT}", "glued to a word"),
    ("account-id", f"Account ID: {ACCOUNT[:4]}-{ACCOUNT[4:8]}-{ACCOUNT[8:]}", "as the console shows it"),
    ("account-id", f"logs-{ACCOUNT[:4]}-{ACCOUNT[4:8]}-{ACCOUNT[8:]}", "console form inside a name"),
    ("account-id", f"logs-1.{ACCOUNT}.s3.amazonaws.com", "after a digit and a dot, in a host name"),
    ("access-key", f"key={ACCESS_KEY}", "access key id"),
    ("access-key", f"principal {ROLE_ID} assumed", "role unique id"),
    ("access-key", f"aws_secret_access_key = {SECRET_KEY}", "secret key, ini"),
    ("access-key", f'"AWS_SECRET_ACCESS_KEY": "{SECRET_KEY}"', "secret key, json"),
    ("access-key", f'"SecretAccessKey": "{SECRET_KEY}"', "secret key, as the CLI prints it"),
    ("access-key", f"--aws-secret-access-key {SECRET_KEY}", "secret key, as an option"),
    ("access-key", f"aws_secret_access_key={SECRET_KEY}=", "secret key, with padding"),
    ("access-key", f"aws_session_token = {SECRET_KEY * 4}", "session token"),
    ("token", f"GH_TOKEN={GITHUB_TOKEN}", "github classic"),
    ("token", f'"header\\n{GITHUB_TOKEN}"', "github, after an escape"),
    ("token", f"TOKEN_{GITHUB_TOKEN}", "github, glued to a word"),
    ("token", f'"header\\n{ANTHROPIC_KEY}"', "anthropic, after an escape"),
    ("token", j("PuTTY-User-Key-", "File-3: ssh-ed25519"), "putty"),
    ("token", GITHUB_PAT, "github fine-grained"),
    ("token", f"x-api-key: {ANTHROPIC_KEY}", "anthropic"),
    ("token", f"OPENAI_API_KEY={OPENAI_PROJECT_KEY}", "openai project"),
    ("token", OPENAI_LEGACY_KEY, "openai legacy"),
    ("token", f"HF_TOKEN={HF_TOKEN}", "hugging face"),
    ("token", GRAFANA_TOKEN, "grafana"),
    ("token", PEM_HEADER, "pem rsa"),
    ("token", OPENSSH_HEADER, "openssh"),
    ("token", PKCS8_HEADER, "pkcs8"),
    ("token", f"https://deploy:{URL_PASSWORD}@10.0.0.5/repo.git", "credentials in a url, host an address"),
    ("token", f"postgres://app:{URL_PASSWORD}@db/x", "credentials in a url, host one label"),
    ("token", f"redis://:{URL_PASSWORD}@cache:6379", "credentials in a url, no user"),
    ("token", j("https://deploy:pa", "$$w0rd9", "@", "git.internal/repo"), "credentials in a url, odd characters"),
    ("endpoint", f"https://{API_HOST}/prod", "api gateway"),
    ("endpoint", j("a1b2c3d4e5", ".execute-api", ".${AWS::Region}.amazonaws.com"), "api gateway, region as a template"),
    ("endpoint", CLOUDFRONT_HOST, "cloudfront"),
    ("endpoint", f"dualstack.{ALB_HOST}", "alb"),
    ("endpoint", NLB_HOST, "nlb"),
    ("endpoint", f"https://{FUNCTION_URL}/", "function url"),
    ("endpoint", f"/workspaces/{PROMETHEUS_WORKSPACE}/api/v1/remote_write", "prometheus workspace"),
    ("endpoint", f"/{HOSTED_ZONE}", "hosted zone"),
    ("resource-id", f"InstanceId: {INSTANCE_ID}", "instance"),
    ("resource-id", f'"{SUBNET_ID}"', "subnet"),
    ("resource-id", f"prod-{SUBNET_ID}", "at the end of a name"),
    ("email", f"contact {EMAIL} for access", "address"),
    ("email", f"<{NUMERIC_DOMAIN_EMAIL}>", "numeric domain"),
    ("email", j("jane.doe", "@", "163.vip.com"), "numeric first label, three labels"),
    ("email", j("jane", "@", "münchen-mail.de"), "a domain in another script"),
    ("email", j("jane", "@", "corp-mail.xn--p1ai"), "an encoded top-level domain"),
    ("email", f"https://deploy:{j('hunter2', '@', 'git.corp-mail.io')}/repo", "credentials in a url"),
    ("home-path", f"cd {LINUX_HOME}/projects", "linux"),
    ("home-path", f"'{LINUX_HOME}'", "linux, no trailing slash"),
    ("home-path", f"file://{MAC_HOME}/x", "macos"),
    ("home-path", f"{WSL_HOME}/Desktop", "wsl"),
    ("home-path", f"{WINDOWS_HOME}\\Desktop", "windows"),
    ("home-path", j("/ho", "me/", "émile/x"), "a name in another script"),
    ("home-path", j("\\\\wsl$\\Ubuntu\\ho", "me\\", "alice"), "wsl, seen from windows"),
    # More cases, and what stays quiet, are in test_console_and_badges.py.
    ("console-link", j("https", "://", "eu-north-1.console", ".aws.amazon.com/ec2/home"), "regional console"),
    ("build-badge", j("https", "://", "codebuild", ".eu-north-1.amazonaws.com/badges?uuid=example"), "codebuild"),
]

PLACEHOLDERS = [
    ("arn:aws:iam::123456789012:role/deploy", "arn, first documented account"),
    ("arn:aws:sns:us-east-1:111122223333:alerts", "arn, second documented account"),
    ("account = 123456789012", "bare documented account"),
    ("Account ID: 1234-5678-9012 or 1111-2222-3333", "documented accounts as the console shows them"),
    ("arn:aws:iam::aws:policy/ReadOnlyAccess", "arn of an AWS-managed policy"),
    ("arn:aws:s3:::model-store/weights", "arn without an account"),
    ("AKIAIOSFODNN7EXAMPLE", "documented access key"),
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "documented secret key"),
    ("https://d111111abcdef8.cloudfront.net/index.html", "documented cloudfront host"),
    ("i-1234567890abcdef0 in vpc-1234567890abcdef0 from ami-0abcdef1234567890", "documented resource ids"),
    ("admin@example.com, a.student@example.org, ops@mail.example.net", "reserved domains"),
    ("ship-test@example.invalid and https://user:pw@example.test", "reserved top-level domains"),
    ("Co-Authored-By: Claude <noreply@anthropic.com>", "agent trailer"),
    ("12345678+octocat@users.noreply.github.com", "github no-reply"),
    ("git remote add origin git@github.com:owner/repo.git", "ssh remote"),
    ("/home/user/project, /home/runner/work, /home/ec2-user/, /home/ubuntu/.cache", "generic homes"),
]

NOT_FINDINGS = [
    (j("sha256:9f86d081884c7d65", "9a2f", ACCOUNT, "0015ad"), "twelve digits inside a hash"),
    (f"blob {j('00', ACCOUNT, '11')}", "twelve digits inside a 16-digit hexadecimal run"),
    (f"started_at_ms = 1{ACCOUNT}", "thirteen digits"),
    (f"cost_usd = 0.{ACCOUNT}", "the fraction of a decimal number"),
    (f"ratios 1.{ACCOUNT}, 0.{ACCOUNT}e-05 and 2.{ACCOUNT}", "more decimal numbers"),
    (f"id 0a1b2c3d-4e5f-6a7b-8c9d-{ACCOUNT}", "the last group of a UUID"),
    (f"id 0a1b2c3d-{ACCOUNT[:4]}-{ACCOUNT[4:8]}-{ACCOUNT[8:]}-0a1b2c3d4e5f", "the middle groups of a UUID"),
    (f"card {ACCOUNT[:4]}-{ACCOUNT[4:8]}-{ACCOUNT[8:]}-0000 and build 2026-1111", "other dashed digit groups"),
    ("uvx ruff@0.17.3 check and uses: actions/checkout@v7.0.1", "tool versions"),
    ("npm i @scope/pkg@1.x.beta aws-cdk@2.1000.0-alpha.next", "package versions"),
    ('"import pytest\\n\\n@pytest.mark.parametrize(\'w\', [1])\\n"', "a decorator after an escape"),
    ("@pytest.mark.skip(reason='later') and @dataclass.field", "decorators"),
    ("background: url(logo@2x.png) url(logo@2x.avif) and bundle@1.x.js", "file names"),
    (j("C:", "\\Us", "ers\\", "user is the documented folder"), "a generic windows user"),
    ("image@sha256:0123abcd and user@host", "no dotted domain"),
    ("*.execute-api.*.amazonaws.com, *.cloudfront.net, *.elb.amazonaws.com, hostedzone/Z…", "the rule list"),
    ("f'{api_id}.execute-api.{region}.amazonaws.com'", "a template of a host"),
    ("elasticloadbalancing.us-east-1.amazonaws.com", "a service endpoint"),
    ("/home/<name>/ and /Users/<name>/ and /home/$USER/x and $HOME/x", "home placeholders"),
    ("https://docs.example.com/home/index.html and docs/home/page.md", "a path that contains /home/"),
    ("ask-implementation-loop-supervisor-about-this", "a word that contains sk-"),
    (f"the task-{j('a' * 40)} identifier", "a long word after sk-"),
    ("ghp_secret123 and gh pr merge", "too short for a token"),
    ("AKIA and ASIA are the access-key prefixes", "prefixes alone"),
    ("aws_secret_access_key = <your key>", "a secret key placeholder"),
    ("https://x-access-token:{token}@github.com/o/r.git and postgres://app:${DB_PASSWORD}@db.internal/x",
     "templates for credentials in a url"),
    ("https://user:<password>@git.internal/x and https://user:****@git.internal/x", "masked credentials in a url"),
    ("https://example.com:443/path?q=a@b and docker://registry:5000/image@sha256:0123abcd", "a port, not a password"),
    ("-----BEGIN CERTIFICATE----- and -----BEGIN PUBLIC KEY-----", "public material"),
    ("i-0a1b2c3 and sg-12345678 and api-0a1b2c3d4e5f6a7b8", "not the 17-character resource form"),
    ("lookup of hostedzone/ZONE", "a short zone placeholder"),
]


@pytest.fixture(scope="module")
def cfg() -> scan.Config:
    return scan.load_config()


def rules(text: str, cfg: scan.Config, path: str = "file.txt") -> list[str]:
    return [f.rule for f in scan.scan_text(text, path, cfg)]


# ---------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("rule", "text"), [p[:2] for p in POSITIVES], ids=[f"{p[0]}: {p[2]}" for p in POSITIVES])
def test_rule_fires_on_a_positive(rule: str, text: str, cfg: scan.Config) -> None:
    assert rule in rules(text, cfg)


def test_every_content_rule_has_a_positive() -> None:
    assert {p[0] for p in POSITIVES} == set(scan.RULES) - {"forbidden-file", "local-value"}


@pytest.mark.parametrize("text", [p[0] for p in PLACEHOLDERS], ids=[p[1] for p in PLACEHOLDERS])
def test_documented_placeholders_are_quiet(text: str, cfg: scan.Config) -> None:
    assert scan.scan_text(text, "file.txt", cfg) == []


@pytest.mark.parametrize("text", [p[0] for p in NOT_FINDINGS], ids=[p[1] for p in NOT_FINDINGS])
def test_lookalikes_are_quiet(text: str, cfg: scan.Config) -> None:
    assert scan.scan_text(text, "file.txt", cfg) == []


def test_an_account_in_an_arn_is_one_finding(cfg: scan.Config) -> None:
    assert rules(j("arn:aws:iam::", ACCOUNT, ":root"), cfg) == ["account-in-arn"]


def test_findings_carry_path_and_line(cfg: scan.Config) -> None:
    text = f"first line\nsecond {ACCOUNT}\n\nfourth {EMAIL}\n"
    found = scan.scan_text(text, "docs/notes.md", cfg)
    assert [(f.path, f.line, f.rule) for f in found] == [
        ("docs/notes.md", 2, "account-id"), ("docs/notes.md", 4, "email")]
    assert found[0].render() == "docs/notes.md:2: account-id: second [redacted account-id]"


def test_an_excerpt_never_shows_a_value(cfg: scan.Config) -> None:
    line = f"{ACCOUNT} then {EMAIL} then {GITHUB_TOKEN} then {LINUX_HOME}/x and {OTHER_ACCOUNT}"
    found = scan.scan_text(line, "f", cfg)
    assert sorted(f.rule for f in found) == ["account-id", "account-id", "email", "home-path", "token"]
    for finding in found:
        for value in (ACCOUNT, OTHER_ACCOUNT, EMAIL, GITHUB_TOKEN, "alice"):
            assert value not in finding.render()


def test_a_name_with_spaces_or_accents_is_masked_whole(cfg: scan.Config) -> None:
    [windows] = scan.scan_text(j("cd C:", "\\Us", "ers\\", "Jane Doe\\Documents"), "f", cfg)
    assert windows.render() == "f:1: home-path: cd [redacted home-path]\\Documents"
    [address] = scan.scan_text(j("ask jörg.müller", "@", "corp-mail.io today"), "f", cfg)
    assert address.render() == "f:1: email: ask [redacted email] today"


def test_an_excerpt_is_cropped_around_its_finding(cfg: scan.Config) -> None:
    [finding] = scan.scan_text("x" * 500 + f" {ACCOUNT} " + "y" * 500, "f", cfg)
    assert len(finding.excerpt) < 130
    assert finding.excerpt.startswith("…")
    assert finding.excerpt.endswith("…")
    assert "[redacted account-id]" in finding.excerpt


def test_lockfiles_skip_only_the_account_rule(cfg: scan.Config) -> None:
    text = f"size = {ACCOUNT}\ntoken = {GITHUB_TOKEN}\n"
    assert rules(text, cfg, "uv.lock") == ["token"]
    assert rules(text, cfg, "infra/package-lock.json") == ["token"]
    assert rules(text, cfg, "notes/uv.lock.md") == ["account-id", "token"]


# ---------------------------------------------------------------------------------------------
# Allowing one line
# ---------------------------------------------------------------------------------------------

def test_an_inline_marker_allows_one_finding_of_its_rule(cfg: scan.Config) -> None:
    assert rules(f"x = {ACCOUNT}  # hygiene: allow account-id (a number of the test vector)", cfg) == []


def test_an_inline_marker_covers_one_finding_only(cfg: scan.Config) -> None:
    line = f"{ACCOUNT} {OTHER_ACCOUNT}  # hygiene: allow account-id (first one is a vector)"
    assert rules(line, cfg) == ["account-id"]
    twice = line + " hygiene: allow account-id (so is the second)"
    assert rules(twice, cfg) == []


def test_an_inline_marker_does_not_cover_another_rule_or_line(cfg: scan.Config) -> None:
    assert rules(f"{ACCOUNT} {EMAIL}  # hygiene: allow email (a list address)", cfg) == ["account-id"]
    assert rules(f"# hygiene: allow account-id (wrong line)\n{ACCOUNT}\n", cfg) == ["account-id"]


def test_an_inline_marker_stops_at_any_line_ending(cfg: scan.Config) -> None:
    for ending in ("\n", "\r\n", "\r"):
        text = f"# hygiene: allow account-id (elsewhere){ending}name = 1{ending}account = {ACCOUNT}{ending}"
        assert [(f.line, f.rule) for f in scan.scan_text(text, "f", cfg)] == [(3, "account-id")]


def test_a_long_unclosed_marker_costs_nothing(cfg: scan.Config) -> None:
    line = f"{ACCOUNT}  # hygiene: allow account-id (" + "reason " * 60_000
    started = time.perf_counter()
    assert rules(line, cfg) == ["account-id"]
    assert time.perf_counter() - started < 5


@pytest.mark.parametrize("marker", ["hygiene: allow account-id", "hygiene: allow account-id ()",
                                    "hygiene: allow account-id ( )", "hygiene: allow (no rule)"],
                         ids=["no reason", "empty reason", "blank reason", "no rule"])
def test_an_inline_marker_needs_a_rule_and_a_reason(marker: str, cfg: scan.Config) -> None:
    assert rules(f"{ACCOUNT}  # {marker}", cfg) == ["account-id"]


# ---------------------------------------------------------------------------------------------
# Values only the owner knows
# ---------------------------------------------------------------------------------------------

REAL_DOMAIN = j("inference", ".", "corp-campus", ".", "io")


def local_config(tmp_path: Path, text: str) -> scan.Config:
    values = tmp_path / "hygiene-values"
    values.write_text(text)
    return scan.load_config(local_values=values)


def test_local_values_are_found_in_any_case_and_any_file(tmp_path: Path) -> None:
    cfg = local_config(tmp_path, f"# the domain\n\n  {REAL_DOMAIN}  \nClass-Roster-2026\n")
    found = scan.scan_text(f"endpoint https://api.{REAL_DOMAIN.upper()}/v1 for class-roster-2026", "uv.lock", cfg)
    assert [f.rule for f in found] == ["local-value", "local-value"]
    assert REAL_DOMAIN.upper() not in found[0].render()
    assert scan.redact(f"host {REAL_DOMAIN}", cfg) == "host [redacted local-value]"
    (tmp_path / "blob.bin").write_bytes(b"\0\0" + REAL_DOMAIN.encode() + b"\0")
    assert [f.rule for f in scan.scan_paths(["blob.bin"], tmp_path, cfg)] == ["local-value"]


def test_a_local_values_file_with_a_byte_order_mark_loses_nothing(tmp_path: Path) -> None:
    values = tmp_path / "hygiene-values"
    values.write_bytes(b"\xef\xbb\xbf" + f"{REAL_DOMAIN}\nclass-roster-2026\n".encode())
    cfg = scan.load_config(local_values=values)
    assert len(cfg.local_values) == 2
    assert rules(f"see {REAL_DOMAIN}", cfg) == ["local-value"]


def test_a_local_value_cannot_be_allowed_inline(tmp_path: Path) -> None:
    cfg = local_config(tmp_path, f"{REAL_DOMAIN}\n")
    assert rules(f"{REAL_DOMAIN}  # hygiene: allow local-value (it is fine)", cfg) == ["local-value"]


def test_without_a_local_values_file_the_rule_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYGIENE_LOCAL_VALUES")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert scan.load_config().local_values == ()
    folder = tmp_path / "config" / "research-harness-inference"
    folder.mkdir(parents=True)
    (folder / "hygiene-values").write_text(f"{REAL_DOMAIN}\n")
    assert len(scan.load_config().local_values) == 1


@pytest.mark.parametrize(("text", "message"), [("ab\n", "shorter than 4"), (None, "cannot read")],
                         ids=["too short", "missing although named"])
def test_a_wrong_local_values_file_is_an_error(text: str | None, message: str, tmp_path: Path) -> None:
    values = tmp_path / "hygiene-values"
    if text is not None:
        values.write_text(text)
    with pytest.raises(scan.ScanError, match=message):
        scan.load_config(local_values=values)


def test_cli_reads_the_local_values_named_in_the_environment(tmp_path: Path) -> None:
    (tmp_path / "values").write_text(f"{REAL_DOMAIN}\n")
    (tmp_path / "doc.md").write_text(f"see https://{REAL_DOMAIN}/status\n")
    env = clean_env() | {"HYGIENE_LOCAL_VALUES": str(tmp_path / "values")}
    result = subprocess.run([sys.executable, str(SCAN), "--paths", "doc.md"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert result.stdout == "doc.md:1: local-value: see https://[redacted local-value]/status\n"
    assert "1 local values checked" in result.stderr
    assert REAL_DOMAIN not in result.stdout + result.stderr
    missing = subprocess.run([sys.executable, str(SCAN), "--paths", "doc.md"], cwd=tmp_path,
                             env=env | {"HYGIENE_LOCAL_VALUES": str(tmp_path / "absent")},
                             capture_output=True, text=True, check=False)
    assert missing.returncode == 2


# ---------------------------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------------------------

FORBIDDEN_NAMES = [
    "cdk.context.json", "infra/cdk.context.json", "cdk.out/manifest.json", "infra/cdk.out/Stack.template.json",
    ".env", ".env.local", "services/gateway/.env.production", ".envrc", "deploy.pem", "certs/server.key",
    "bundle.p12", "id_ed25519", "terraform.tfstate", "terraform.tfstate.backup", ".aws/credentials",
    "home/.aws/config",
]
ALLOWED_NAMES = [
    ".env.example", "services/gateway/.env.example", "docs/key.md", "environment.py", "tools/keys.py",
    "cdk.json", "docs/cdk.out.md", "pem.txt", "tests/test_env.py", "aws/config.py",
]


@pytest.mark.parametrize("path", FORBIDDEN_NAMES)
def test_forbidden_file_names(path: str, tmp_path: Path, cfg: scan.Config) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("nothing wrong inside\n")
    assert [(f.path, f.line, f.rule) for f in scan.scan_paths(["."], tmp_path, cfg)] == [(path, 0, "forbidden-file")]


@pytest.mark.parametrize("path", ALLOWED_NAMES)
def test_allowed_file_names(path: str, tmp_path: Path, cfg: scan.Config) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("nothing wrong inside\n")
    assert scan.scan_paths(["."], tmp_path, cfg) == []


def test_an_example_env_file_is_still_read(tmp_path: Path, cfg: scan.Config) -> None:
    (tmp_path / ".env.example").write_text(f"AWS_ACCOUNT={ACCOUNT}\n")
    assert [f.rule for f in scan.scan_paths([".env.example"], tmp_path, cfg)] == ["account-id"]


# ---------------------------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------------------------

def test_redact_masks_every_value_and_keeps_the_rest(cfg: scan.Config) -> None:
    text = f"deployed to {ACCOUNT} by {EMAIL}\nrole arn:aws:iam::{OTHER_ACCOUNT}:role/x\nplaceholder 123456789012\n"
    assert scan.redact(text, cfg) == (
        "deployed to [redacted account-id] by [redacted email]\n"
        "role arn:aws:iam::[redacted account-in-arn]:role/x\n"
        "placeholder 123456789012\n"
    )


def test_a_private_key_is_masked_whole(cfg: scan.Config) -> None:
    block = f"{PKCS8_HEADER}\n{KEY_BODY}\n{KEY_BODY}\n{PKCS8_FOOTER}\n"
    assert scan.redact(f"before\n{block}after\n", cfg) == "before\n[redacted token]\nafter\n"
    assert scan.redact(f"cut off\n{PKCS8_HEADER}\n{KEY_BODY}", cfg) == "cut off\n[redacted token]"
    one_line = f'{{"private_key": "{PKCS8_HEADER}\\n{KEY_BODY}\\n{PKCS8_FOOTER}\\n", "x": 1}}'
    [finding] = scan.scan_text(one_line, "key.json", cfg)
    assert finding.rule == "token"
    assert KEY_BODY[:8] not in finding.render()
    assert KEY_BODY[:8] not in scan.redact(one_line, cfg)
    assert [f.line for f in scan.scan_text(f"x\n{block}", "id", cfg)] == [2]


def test_credentials_in_a_url_are_masked_whole(cfg: scan.Config) -> None:
    [finding] = scan.scan_text(f"remote = https://deploy:{URL_PASSWORD}@10.0.0.5/repo.git", "f", cfg)
    assert finding.render() == "f:1: token: remote = https://[redacted token]@10.0.0.5/repo.git"


def test_redact_ignores_inline_markers(cfg: scan.Config) -> None:
    assert ACCOUNT not in scan.redact(f"{ACCOUNT}  # hygiene: allow account-id (not for publication)", cfg)


def test_redact_and_scan_text_use_the_checked_in_configuration_by_default() -> None:
    assert scan.redact(f"id {ACCOUNT}, example 111122223333") == "id [redacted account-id], example 111122223333"
    assert [f.rule for f in scan.scan_text(f"id {ACCOUNT}")] == ["account-id"]


def test_scan_paths_reads_files_directories_and_link_targets(tmp_path: Path, cfg: scan.Config) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(f"ACCOUNT = '{ACCOUNT}'\n")
    (tmp_path / "clean.md").write_text("nothing\n")
    (tmp_path / "link").symlink_to(f"{LINUX_HOME}/secrets")
    (tmp_path / "linked-dir").symlink_to("src", target_is_directory=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(f"email = {EMAIL}\n")
    found = scan.scan_paths(["."], tmp_path, cfg)
    assert sorted((f.path, f.rule) for f in found) == [("link", "home-path"), ("src/app.py", "account-id")]
    assert [f.path for f in scan.scan_paths(["src/app.py"], tmp_path, cfg)] == ["src/app.py"]


def test_scan_paths_rejects_a_missing_path(tmp_path: Path, cfg: scan.Config) -> None:
    with pytest.raises(scan.ScanError, match="no such file"):
        scan.scan_paths(["absent.txt"], tmp_path, cfg)


def test_binary_files_are_read_for_every_rule_but_addresses(tmp_path: Path, cfg: scan.Config) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\0\x01\x02 " + f"{ACCOUNT} {EMAIL} {GITHUB_TOKEN}".encode() + b"\xff\0")
    found = scan.scan_paths(["blob.bin"], tmp_path, cfg)
    assert [f.rule for f in found] == ["account-id", "token"]
    for finding in found:   # the address is not reported, and not shown either
        assert EMAIL not in finding.render()
        assert "[redacted email]" in finding.excerpt


def test_a_binary_full_of_nul_bytes_is_not_mistaken_for_wide_text(tmp_path: Path, cfg: scan.Config) -> None:
    compiled = b"".join(bytes([i % 251 + 1, 0, i % 7 + 1, 9, 0]) for i in range(400))   # 40% NUL, at any position
    (tmp_path / "module.pyc").write_bytes(compiled + f" {EMAIL} ".encode() + compiled)
    assert scan.scan_paths(["module.pyc"], tmp_path, cfg) == []


def test_one_stray_nul_does_not_make_a_text_file_binary(tmp_path: Path, cfg: scan.Config) -> None:
    (tmp_path / "notes.txt").write_bytes(("x" * 2000 + f"\n{ACCOUNT} {EMAIL}\n").encode() + b"\0")
    assert [f.rule for f in scan.scan_paths(["notes.txt"], tmp_path, cfg)] == ["account-id", "email"]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_wide_encodings_are_read(encoding: str, tmp_path: Path, cfg: scan.Config) -> None:
    (tmp_path / "notes.txt").write_bytes(f"first\naccount {ACCOUNT} mail {EMAIL}\n".encode(encoding))
    found = scan.scan_paths(["notes.txt"], tmp_path, cfg)
    assert [(f.line, f.rule) for f in found] == [(2, "account-id"), (2, "email")]


def test_a_byte_order_mark_does_not_hide_plain_text(tmp_path: Path, cfg: scan.Config) -> None:
    for name, mark in (("le.txt", b"\xff\xfe"), ("be.txt", b"\xfe\xff"), ("utf8.txt", b"\xef\xbb\xbf")):
        (tmp_path / name).write_bytes(mark + f"{ACCOUNT} {GITHUB_TOKEN}\n".encode())
        assert [f.rule for f in scan.scan_paths([name], tmp_path, cfg)] == ["account-id", "token"]


def test_a_value_in_a_file_name_is_a_finding_and_is_never_printed(tmp_path: Path, cfg: scan.Config) -> None:
    folder = tmp_path / "AWSLogs" / ACCOUNT
    folder.mkdir(parents=True)
    (folder / "readme.txt").write_text(f"written by {EMAIL}\n")
    (tmp_path / f"{EMAIL}.txt").write_text("clean\n")
    found = scan.scan_paths(["."], tmp_path, cfg)
    assert sorted((f.path, f.line, f.rule) for f in found) == [
        ("AWSLogs/[redacted account-id]/readme.txt", 0, "account-id"),
        ("AWSLogs/[redacted account-id]/readme.txt", 1, "email"),
        ("[redacted email].txt", 0, "email"),
    ]


def test_a_file_outside_the_root_is_judged_on_its_content_and_its_own_name(tmp_path: Path) -> None:
    cfg = scan.load_config(write_config(tmp_path, '[exclude]\npaths = ["**"]\n[lockfiles]\npaths = ["**/uv.lock"]\n'))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.md").write_text(f"{ACCOUNT}\n")
    assert scan.scan_paths(["inside.md"], repo, cfg) == []   # excluded by the configuration
    elsewhere = tmp_path / f"tmp-{ACCOUNT}"   # a folder that is not the repository's to judge
    elsewhere.mkdir()
    (elsewhere / "E1-01.md").write_text(f"clean title\n\ndeploys to {ACCOUNT}\n")
    (elsewhere / "uv.lock").write_text(f"size = {ACCOUNT}\n")
    (elsewhere / "clean.md").write_text("nothing\n")
    found = scan.scan_paths([elsewhere / "E1-01.md", elsewhere / "uv.lock", elsewhere / "clean.md"], repo, cfg)
    assert [(f.line, f.rule) for f in found] == [(3, "account-id"), (1, "account-id")]
    for finding in found:
        assert ACCOUNT not in finding.render()
        assert finding.path.endswith(("/tmp-[redacted account-id]/E1-01.md", "/tmp-[redacted account-id]/uv.lock"))


def test_odd_file_names_cannot_break_the_output(tmp_path: Path, cfg: scan.Config) -> None:
    (tmp_path / "two\nlines.txt").write_text(f"{ACCOUNT}\n")
    os.symlink("target", os.fsencode(tmp_path) + b"/caf\xe9.pem")
    result = run_scan(tmp_path, "--paths", ".")
    assert result.returncode == 1
    assert sorted(result.stdout.splitlines()) == [
        "caf .pem:0: forbidden-file: a key or certificate file",
        "two lines.txt:1: account-id: [redacted account-id]",
    ]


def test_utf16_text_is_read(tmp_path: Path, cfg: scan.Config) -> None:
    (tmp_path / "notes.txt").write_bytes(f"account {ACCOUNT}\n".encode("utf-16"))
    assert [f.rule for f in scan.scan_paths(["notes.txt"], tmp_path, cfg)] == ["account-id"]


# ---------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------

def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "hygiene.toml"
    path.write_text(text)
    return path


def test_the_checked_in_configuration_holds_placeholders_only(cfg: scan.Config) -> None:
    assert cfg.account_ids == {"123456789012", "111122223333"}
    assert cfg.exclude == ()


def test_configuration_placeholders_and_excludes_apply(tmp_path: Path) -> None:
    cfg = scan.load_config(write_config(tmp_path, f"""
[placeholders]
account_ids = ["{ACCOUNT}"]
email_domains = ["corp-mail.io"]
home_users = ["alice"]
[exclude]
paths = ["fixtures/**", "*.rec"]
"""))
    assert scan.scan_text(f"{ACCOUNT} {EMAIL} {LINUX_HOME}/x", "f", cfg) == []
    assert [f.rule for f in scan.scan_text(OTHER_ACCOUNT, "f", cfg)] == ["account-id"]
    for name in ("fixtures/deep/run.json", "top.rec", "kept/run.json", "kept/deep.rec"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(OTHER_ACCOUNT)
    assert sorted(f.path for f in scan.scan_paths(["fixtures", "top.rec", "kept"], tmp_path, cfg)) == [
        "kept/deep.rec", "kept/run.json"]


@pytest.mark.parametrize(("text", "message"), [
    ("[placeholders]\naccount_id = []\n", "unknown key"),
    ("[allow]\npaths = []\n", "unknown table"),
    ("[placeholders]\naccount_ids = [\"1234\"]\n", "not 12 digits"),
    ("[exclude]\npaths = \"docs/**\"\n", "must be a list"),
    ("[exclude\n", "cannot read"),
], ids=["unknown key", "unknown table", "bad account", "not a list", "not toml"])
def test_a_wrong_configuration_is_an_error(text: str, message: str, tmp_path: Path) -> None:
    with pytest.raises(scan.ScanError, match=message):
        scan.load_config(write_config(tmp_path, text))


def test_a_missing_configuration_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(scan.ScanError, match="cannot read"):
        scan.load_config(tmp_path / "absent.toml")


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------

def clean_env() -> dict[str, str]:
    """No git variable of the caller (a hook sets several), and no user or system git configuration."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "HYGIENE_LOCAL_VALUES": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    return env


def run_scan(cwd: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCAN), *args], cwd=cwd, env=clean_env(), input=stdin,
                          capture_output=True, text=True, check=False)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=clean_env(), capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "--quiet", "--initial-branch=main")
    return path


def test_cli_paths_exit_statuses_and_output(tmp_path: Path) -> None:
    (tmp_path / "clean.txt").write_text("nothing\n")
    (tmp_path / "leak.txt").write_text(f"one\n{ACCOUNT}\n")
    clean = run_scan(tmp_path, "--paths", "clean.txt")
    assert (clean.returncode, clean.stdout) == (0, "")
    assert "1 files scanned, 0 findings" in clean.stderr
    leak = run_scan(tmp_path, "--paths", "clean.txt", "leak.txt")
    assert (leak.returncode, leak.stdout) == (1, "leak.txt:2: account-id: [redacted account-id]\n")
    assert ACCOUNT not in leak.stdout + leak.stderr


@pytest.mark.parametrize("args", [["--no-such-option"], ["--staged", "--paths", "x"], ["--paths"],
                                  ["--paths", "absent.txt"], ["--config", "absent.toml"]],
                         ids=["unknown option", "two sources", "no path", "missing path", "missing config"])
def test_cli_usage_errors_exit_2(args: list[str], tmp_path: Path) -> None:
    result = run_scan(tmp_path, *args)
    assert result.returncode == 2
    assert result.stdout == ""


def test_cli_outside_a_git_work_tree_exits_2(tmp_path: Path) -> None:
    result = run_scan(tmp_path)
    assert result.returncode == 2
    assert "not inside a git work tree" in result.stderr


def test_cli_redact_filters_stdin(tmp_path: Path) -> None:
    result = run_scan(tmp_path, "--redact", stdin=f"deploy failed in {ACCOUNT}\n")
    assert (result.returncode, result.stdout) == (0, "deploy failed in [redacted account-id]\n")


def test_cli_show_allowed_lists_inline_markers(tmp_path: Path) -> None:
    (tmp_path / "vector.txt").write_text(f"{ACCOUNT}  hygiene: allow account-id (a test vector)\n")
    result = run_scan(tmp_path, "--show-allowed", "--paths", "vector.txt")
    assert (result.returncode, result.stdout) == (0, "")
    assert "allowed inline: vector.txt:1: account-id" in result.stderr
    assert "1 allowed inline" in result.stderr


def test_cli_quiet_prints_nothing_unless_there_is_a_finding(tmp_path: Path) -> None:
    (tmp_path / "clean.txt").write_text("nothing\n")
    (tmp_path / "leak.txt").write_text(f"{ACCOUNT}\n")
    clean = run_scan(tmp_path, "--quiet", "--paths", "clean.txt")
    assert (clean.returncode, clean.stdout, clean.stderr) == (0, "", "")
    leak = run_scan(tmp_path, "-q", "--paths", "leak.txt")
    assert leak.returncode == 1
    assert "1 findings" in leak.stderr


def test_cli_default_scans_what_git_tracks_or_would_add(repo: Path) -> None:
    (repo / ".gitignore").write_text("ignored.txt\n.env\n")
    (repo / "tracked.txt").write_text(f"{ACCOUNT}\n")
    (repo / "untracked.txt").write_text(f"{EMAIL}\n")
    (repo / "ignored.txt").write_text(f"{OTHER_ACCOUNT}\n")
    (repo / ".env").write_text("SAFE=1\n")
    git(repo, "add", "tracked.txt", ".gitignore")
    result = run_scan(repo)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["tracked.txt:1: account-id: [redacted account-id]",
                                          "untracked.txt:1: email: [redacted email]"]
    git(repo, "add", "--force", ".env")
    assert ".env:0: forbidden-file:" in run_scan(repo).stdout


def test_cli_default_scans_the_whole_work_tree_from_a_subdirectory(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "top.txt").write_text(f"{ACCOUNT}\n")
    result = run_scan(repo / "sub")
    assert result.stdout.splitlines() == ["top.txt:1: account-id: [redacted account-id]"]


def test_cli_staged_reads_the_index_not_the_work_tree(repo: Path) -> None:
    (repo / "a.txt").write_text(f"{ACCOUNT}\n")
    (repo / "b.txt").write_text("clean\n")
    git(repo, "add", "a.txt", "b.txt")
    (repo / "a.txt").write_text("fixed in the work tree only\n")
    (repo / "b.txt").write_text(f"dirty in the work tree only {OTHER_ACCOUNT}\n")
    result = run_scan(repo, "--staged")   # also the first commit of a repository: there is no HEAD yet
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["a.txt:1: account-id: [redacted account-id]"]


def test_cli_staged_reads_only_what_the_commit_changes(repo: Path) -> None:
    (repo / "old.txt").write_text(f"{ACCOUNT}\n")
    (repo / "kept.txt").write_text("clean\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "first")
    (repo / "new.txt").write_text("clean\n")
    git(repo, "add", "new.txt")
    assert run_scan(repo, "--staged").returncode == 0
    (repo / "kept.txt").write_text(f"{EMAIL}\n")
    git(repo, "rm", "--quiet", "--cached", "old.txt")
    git(repo, "add", "kept.txt")
    result = run_scan(repo, "--staged")
    assert result.stdout.splitlines() == ["kept.txt:1: email: [redacted email]"]
    assert "2 files scanned" in result.stderr


def test_cli_rev_reads_the_tree_of_a_commit(repo: Path) -> None:
    (repo / "a.txt").write_text(f"{ACCOUNT}\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "leak")
    (repo / "a.txt").write_text("clean\n")
    git(repo, "commit", "--quiet", "-am", "fix")
    (repo / "a.txt").write_text(f"work tree {OTHER_ACCOUNT}\n")
    assert run_scan(repo, "--rev", "HEAD").returncode == 0
    old = run_scan(repo, "--rev", "HEAD~1")
    assert (old.returncode, old.stdout) == (1, "a.txt:1: account-id: [redacted account-id]\n")
    assert run_scan(repo, "--rev", "no-such-rev").returncode == 2
    assert run_scan(repo, "--rev", "--output=x").returncode == 2


def test_cli_history_reads_every_version(repo: Path) -> None:
    assert run_scan(repo, "--history").returncode == 0   # no commit yet
    (repo / "a.txt").write_text(f"{ACCOUNT}\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "leak")
    blob = git(repo, "rev-parse", "HEAD:a.txt").strip()
    (repo / "a.txt").write_text("clean\n")
    git(repo, "commit", "--quiet", "-am", "fix")
    git(repo, "switch", "--quiet", "-c", "side")
    (repo / "b.txt").write_text("clean too\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "side work")
    assert run_scan(repo, "--rev", "HEAD").returncode == 0
    result = run_scan(repo, "--history")
    assert (result.returncode, result.stdout) == (1, f"a.txt@{blob[:12]}:1: account-id: [redacted account-id]\n")
    assert run_scan(repo, "--history", "main..side").returncode == 0


def test_cli_history_reads_commit_identities_and_messages(repo: Path) -> None:
    (repo / "a.txt").write_text("clean\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "clean identity, clean message")
    clean = run_scan(repo, "--history")
    assert clean.returncode == 0
    assert "1 files and 1 commits scanned" in clean.stderr
    (repo / "a.txt").write_text("still clean\n")
    git(repo, "commit", "--quiet", "-am",
        f"deploy fix\n\nfailed in account {ACCOUNT}\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    in_message = git(repo, "rev-parse", "HEAD").strip()[:12]
    (repo / "a.txt").write_text("clean again\n")
    git(repo, "commit", "--quiet", f"--author=Jane <{EMAIL}>", "-am", "a commit under a personal address")
    in_identity = git(repo, "rev-parse", "HEAD").strip()[:12]
    result = run_scan(repo, "--history")
    assert result.returncode == 1
    assert sorted(result.stdout.splitlines()) == sorted([
        f"<commit>@{in_message}:6: account-id: failed in account [redacted account-id]",
        f"<commit>@{in_identity}:1: email: Author: Jane <[redacted email]>",
    ])
    assert "3 files and 3 commits scanned" in result.stderr


def test_cli_history_honours_no_marker_in_a_commit(repo: Path) -> None:
    (repo / "a.txt").write_text("clean\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", f"--author=hygiene: allow email (me) <{EMAIL}>", "-m",
        f"account {ACCOUNT} hygiene: allow account-id (it is fine)")
    result = run_scan(repo, "--history")
    assert result.returncode == 1
    assert sorted(line.split(": ")[1] for line in result.stdout.splitlines()) == ["account-id", "email"]


def test_cli_history_reads_a_commit_whatever_its_message_contains(repo: Path) -> None:
    (repo / "a.txt").write_text("clean\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", f"odd \x01 separator\n\naccount {ACCOUNT}\n")
    result = run_scan(repo, "--history")
    assert result.returncode == 1
    assert "account-id: account [redacted account-id]" in result.stdout


def test_cli_history_remembers_a_forbidden_name_after_a_rename(repo: Path) -> None:
    (repo / ".env").write_text("SAFE=1\n")
    git(repo, "add", "--force", ".env")
    git(repo, "commit", "--quiet", "-m", "an environment file")
    git(repo, "mv", ".env", "settings.txt")
    git(repo, "commit", "--quiet", "-m", "renamed")
    assert run_scan(repo, "--rev", "HEAD").returncode == 0
    result = run_scan(repo, "--history")
    assert result.returncode == 1
    assert ".env:0: forbidden-file:" in result.stdout


# ---------------------------------------------------------------------------------------------
# This repository
# ---------------------------------------------------------------------------------------------

def test_this_repository_scans_clean() -> None:
    result = subprocess.run([sys.executable, str(SCAN)], cwd=REPO, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
