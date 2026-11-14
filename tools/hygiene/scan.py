#!/usr/bin/env python3
"""Public-repository hygiene scanner (docs/adr/0001-software-factory.md, decision 4).

This repository is public, so nothing that identifies the AWS account it deploys to, and no
credential, may be committed. This scanner is the deterministic gate for that rule: the git hooks,
the implement loop's verifier and CI all run it (scripts/check.sh hygiene).

    python3 tools/hygiene/scan.py                 every file git tracks or would add
    python3 tools/hygiene/scan.py --staged        what the next commit adds or changes
    python3 tools/hygiene/scan.py --paths P...    these files or directories, as they are on disk
    python3 tools/hygiene/scan.py --rev REV       the tree of one commit
    python3 tools/hygiene/scan.py --history       every file version and every commit message in the history
    python3 tools/hygiene/scan.py --redact        copy stdin to stdout with every finding masked

Exit status: 0 clean, 1 findings (one per line on stdout, `path:line: rule: redacted excerpt`),
2 usage or environment error. Standard library only. The rules are code, below; the placeholders
they accept and the paths they skip are data, in hygiene.toml. See README.md beside this file.

One rule has no pattern: the real values only the owner knows (the domain, for one) cannot be
named in a public repository. On the owner's machine they are read, one per line, from a file
outside the repository ($HYGIENE_LOCAL_VALUES, default ~/.config/research-harness-inference/
hygiene-values) and reported as `local-value`. Where the file does not exist, the rule is off.
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "hygiene.toml"

RULES = (
    "account-in-arn", "account-id", "access-key", "token", "forbidden-file", "endpoint", "resource-id", "email",
    "home-path", "local-value", "console-link", "build-badge",
)
# Not applied to binary files, where an address is somebody else's metadata (a font, a PDF) or noise.
TEXT_ONLY_RULES = frozenset({"email"})
# Rules no inline marker can allow: a file that must not exist, a value the owner declared private.
NEVER_ALLOWED_INLINE = frozenset({"forbidden-file", "local-value"})
LOCAL_VALUES_ENV = "HYGIENE_LOCAL_VALUES"
LOCAL_VALUE_MIN = 4   # a shorter value would match half the repository


class ScanError(Exception):
    """The scan could not run (bad configuration, no git work tree, unreadable input): exit 2."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int   # 1-based; 0 when the finding is about the path itself
    rule: str
    excerpt: str   # redacted: safe to print, log and publish

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.excerpt}"


@dataclass(frozen=True)
class Config:
    """hygiene.toml: the documented placeholders the rules accept, and the paths they skip."""

    account_ids: frozenset[str]
    email_domains: tuple[str, ...]
    emails: frozenset[str]
    home_users: frozenset[str]
    resource_ids: frozenset[str]
    strings: frozenset[str]
    lockfiles: tuple[re.Pattern[str], ...]
    exclude: tuple[re.Pattern[str], ...]
    local_values: tuple[re.Pattern[str], ...] = ()   # never from hygiene.toml: see load_local_values

    def is_lockfile(self, path: str) -> bool:
        return any(p.fullmatch(path) for p in self.lockfiles)

    def is_excluded(self, path: str) -> bool:
        return any(p.fullmatch(path) for p in self.exclude)


def _glob(pattern: str) -> re.Pattern[str]:
    """A repository-relative glob: `*` and `?` stay inside one path segment, `**` crosses them."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


_SCHEMA = {
    "placeholders": ("account_ids", "email_domains", "emails", "home_users", "resource_ids", "strings"),
    "lockfiles": ("paths",),
    "exclude": ("paths",),
}


def load_local_values(path: Path | None = None) -> tuple[re.Pattern[str], ...]:
    """The owner's real values, one per line (`#` starts a comment line), matched anywhere and in
    any letter case. The file lives outside the repository and outside the agents' sandbox. With
    no path given: $HYGIENE_LOCAL_VALUES, which must exist when set, else the default location,
    which may be absent (then there are no values: CI, a contributor's machine, the sandbox)."""
    explicit = path or (Path(os.environ[LOCAL_VALUES_ENV]) if os.environ.get(LOCAL_VALUES_ENV) else None)
    if explicit is None:
        try:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        except RuntimeError:   # no home directory to look in
            return ()
        path = base / "research-harness-inference" / "hygiene-values"
        if not path.exists():
            return ()
    else:
        path = explicit
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ScanError(f"cannot read the local values file: {exc.__class__.__name__}") from exc
    values = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if any(len(value) < LOCAL_VALUE_MIN for value in values):
        raise ScanError(f"the local values file has a value shorter than {LOCAL_VALUE_MIN} characters")
    return tuple(re.compile(re.escape(value), re.IGNORECASE) for value in dict.fromkeys(values))


def load_config(path: Path | None = None, local_values: Path | None = None) -> Config:
    """Read hygiene.toml strictly: an unknown table or key is an error, never ignored, so a typo
    cannot switch a placeholder list off or an exclude on without anyone noticing."""
    path = path or DEFAULT_CONFIG
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ScanError(f"cannot read the hygiene configuration {path.name}: {exc}") from exc
    values: dict[tuple[str, str], list[str]] = {}
    for table, keys in _SCHEMA.items():
        section = raw.pop(table, {})
        if not isinstance(section, dict):
            raise ScanError(f"{path.name}: [{table}] must be a table")
        for key in keys:
            value = section.pop(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ScanError(f"{path.name}: [{table}] {key} must be a list of non-empty strings")
            values[table, key] = value
        if section:
            raise ScanError(f"{path.name}: unknown key(s) in [{table}]: {', '.join(sorted(section))}")
    if raw:
        raise ScanError(f"{path.name}: unknown table(s): {', '.join(sorted(raw))}")
    for account in values["placeholders", "account_ids"]:
        if not re.fullmatch(r"[0-9]{12}", account):
            raise ScanError(f"{path.name}: an account_ids placeholder is not 12 digits")
    return Config(
        account_ids=frozenset(values["placeholders", "account_ids"]),
        email_domains=tuple(v.lower() for v in values["placeholders", "email_domains"]),
        emails=frozenset(v.lower() for v in values["placeholders", "emails"]),
        home_users=frozenset(values["placeholders", "home_users"]),
        resource_ids=frozenset(v.lower() for v in values["placeholders", "resource_ids"]),
        strings=frozenset(values["placeholders", "strings"]),
        lockfiles=tuple(_glob(v) for v in values["lockfiles", "paths"]),
        exclude=tuple(_glob(v) for v in values["exclude", "paths"]),
        local_values=load_local_values(local_values),
    )


# --------------------------------------------------------------------------------------------
# Content rules. Each takes the whole text and yields (rule, start, end) for every match that is
# not a documented placeholder. No pattern can match across a line break.
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Hit:
    rule: str
    start: int
    end: int


_DIGITS = frozenset("0123456789")
_HEX = frozenset("0123456789abcdefABCDEF")

# 1. account-in-arn: an ARN whose account field is filled, in any partition.
_ARN = re.compile(r"arn:aws[a-z-]*:[A-Za-z0-9-]*:[a-z0-9-]*:([0-9]{12}):")
# 2. account-id: exactly twelve digits. What it is embedded in decides (see _account_ids).
_TWELVE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
# As the console shows it, alone or inside a name; not when it continues a run of hexadecimal
# groups (the middle of a UUID) or of digit groups (a card number).
_DASHED = re.compile(r"(?<![0-9])(?<![0-9A-Fa-f]-)[0-9]{4}-[0-9]{4}-[0-9]{4}(?![0-9])(?!-[0-9A-Fa-f])")
_UUID_HEAD = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-$")
_HASH_RUN = 16   # twelve digits inside this many hexadecimal characters or more are part of a hash
# 3. access-key: the unique-ID prefixes of IAM (access keys, users, roles, groups, policies,
#    instance profiles, certificates, public keys), and a long value given to a secret key or a
#    session token by name, in any spelling (`aws_secret_access_key`, `SecretAccessKey`, ...).
_ACCESS_KEY = re.compile(
    r"(?<![A-Za-z0-9])(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|AGPA|ABIA|ACCA|APKA|ASCA)[A-Z0-9]{16}(?![A-Za-z0-9])")
_SECRET_KEY = re.compile(
    r"(?i:secret[_-]?(?:access[_-]?)?key|session[_-]?token)[\"']?[ \t]*[:= \t][ \t]*[\"']?([A-Za-z0-9/+=]{40,400})")
# 4. token: the credentials this project's tools and its design use, and private keys.
_WORD_START = r"(?:(?<![A-Za-z0-9_-])|(?<=\\[ntr]))"   # not inside a word; `\n` before it is an escape, not a word
_TOKENS = tuple(re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{36,}",                                  # GitHub: classic, OAuth, app, refresh
    r"github_pat_[A-Za-z0-9_]{36,}",                                # GitHub: fine-grained
    _WORD_START + r"sk-ant-[A-Za-z0-9_-]{24,}",                     # Anthropic
    _WORD_START + r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{24,}",  # OpenAI: project, service, admin
    _WORD_START + r"sk-[A-Za-z0-9]{32,}",                           # OpenAI: legacy
    r"hf_[A-Za-z0-9]{30,}",                                         # Hugging Face (the model store)
    r"glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8}",                         # Grafana service account
    # PEM and OpenPGP private keys: the header and whatever shares its line (a key kept on one
    # line, as in a JSON document). The lines below a header are masked by redact().
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[^\n]*",
    r"PuTTY-User-Key-File-[0-9]+:[^\n]*",                           # PuTTY private key
))
_PEM_BLOCK = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----.*?(?:-----END \1-----|\Z)", re.DOTALL)
# `scheme://<user>:<password>@host`. A template in the password's place is not a password.
_URL_CREDENTIALS = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{1,15}://([^\s/:@'\"]{0,64}:([^\s/@'\"]{3,128}))@([\w.-]+)")
_TEMPLATE = re.compile(r"\$\{?\w+\}?|\{+[^{}]*\}+|<[^<>]*>|%\w|%\(\w+\)\w|[*xX.]+")
# 6. endpoint: hostnames and identifiers that name one account's resources.
_LABELS = r"(?<![A-Za-z0-9.-])(?:[a-z0-9-]+\.)"   # one or more host labels, from the start of the host name
_REGION = r"(?:[a-z0-9-]+|\$?\{[^{}\s]{1,40}\})"    # a region, or a template for one: the rest still names a resource
_ENDPOINTS = tuple(re.compile(p, re.IGNORECASE) for p in (
    _LABELS + r"+execute-api\." + _REGION + r"\.amazonaws\.com(?:\.cn)?",   # API Gateway
    _LABELS + r"+cloudfront\.net",                                           # CloudFront distribution
    _LABELS + r"{2,}elb\.amazonaws\.com(?:\.cn)?",                           # load balancer (ALB, CLB)
    _LABELS + r"+elb\." + _REGION + r"\.amazonaws\.com(?:\.cn)?",           # load balancer (NLB)
    _LABELS + r"+lambda-url\." + _REGION + r"\.on\.aws",                     # Lambda function URL
    r"(?<![A-Za-z0-9-])ws-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",      # Managed Prometheus workspace
)) + (re.compile(r"hostedzone/Z[A-Z0-9]{7,}"),)                              # Route 53 hosted zone
# 7. resource-id: EC2-style identifiers in their 17-character form, as cdk.context.json caches
#    them, alone or at the end of a name (`prod-vpc-...`). `i-` needs a clean start: `api-...` is a word.
_RESOURCE_IDS = (
    re.compile(r"(?<![A-Za-z0-9])(?:vpc|subnet|sg|ami|igw|nat|rtb|eni|eipalloc|vol|snap|lt|acl|vpce|tgw|pcx|cgw|vgw)"
               r"-([0-9a-f]{17})(?![0-9a-f])"),
    re.compile(r"(?<![A-Za-z0-9-])i-([0-9a-f]{17})(?![0-9a-f])"),
)
# 8. email: found from each `@`, so a long line costs one pass. Letters of any script count.
_EMAIL_LOCAL = re.compile(r"[\w.%+-]{1,64}$")
_EMAIL_DOMAIN = re.compile(r"[\w-]+(?:\.[\w-]+)+")
_VERSION_LABEL = re.compile(r"v?[0-9]+")
# `logo@2x.png`, `pkg@1.2.tar.gz`: a file name, not an address. None of these is a top-level domain.
_FILE_SUFFIXES = frozenset({
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "bmp", "tiff", "pdf", "ttf", "otf", "css", "scss",
    "js", "mjs", "cjs", "jsx", "ts", "tsx", "json", "html", "xml", "csv", "txt", "yaml", "yml", "toml", "lock",
    "gz", "whl",
})
# 9. home-path: a user's home directory on Linux, macOS, WSL (from either side) or Windows,
#    where a user name may hold spaces.
_HOME_PATHS = (
    re.compile(r"(?<![\w.~-])/(?:home|Users)/(\w[\w.-]*)"),
    re.compile(r"(?<![\w.~-])/mnt/[a-z]/Users/(\w[\w.-]*)"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}(\w[^\\/\n\"'<>|:*?]{0,63})"),
    re.compile(r"\\{1,2}home\\{1,2}(\w[\w.-]*)"),
)

# 11. console-link and 12. build-badge: a URL, with its scheme. The whole of it is the finding,
#     since its path and its query are what name the region, the resource or the project.
_URL = r"https?://"
_URL_TAIL = r"[^\s\"<>`\\]*"   # the rest of the URL; _url_end drops what belongs to the text around it
_CLOSERS = {")": "(", "]": "[", "}": "{"}
# The console and the sign-in page of each partition (aws, China, GovCloud), alone or after
# other labels: a region, an account alias, or a template for one.
_CONSOLE_LINK = re.compile(
    _URL + r"(?:" + _REGION + r"\.)*(?:console|signin)\.(?:aws\.amazon\.com|amazonaws\.cn|amazonaws-us-gov\.com)"
    r"(?![a-z0-9-]|\.[a-z0-9-])" + _URL_TAIL, re.IGNORECASE)
_BUILD_BADGES = tuple(re.compile(_URL + p + _URL_TAIL, re.IGNORECASE) for p in (
    r"codebuild\." + _REGION + r"\.amazonaws\.com(?:\.cn)?/badges(?![a-z0-9-])",          # CodeBuild project
    r"github\.com/[^\s/]+/[^\s/]+/(?:actions/)?workflows/[^\s/]+/badge\.svg(?![a-z0-9-])",   # GitHub Actions workflow
))
# shields.io serves badges of anything. The ones of a build or workflow status say so in their URL.
_SHIELDS_BADGE = re.compile(_URL + r"(?:img|raster)\.shields\.io(/" + _URL_TAIL + r")", re.IGNORECASE)
_BUILD_WORDS = re.compile(
    r"(?<![a-z])(?:(?:code)?builds?|workflows?|pipelines?|check-runs|checks-status)(?![a-z])", re.IGNORECASE)

_ALLOW = re.compile(r"hygiene:[ \t]{0,8}allow[ \t]{1,8}([a-z-]{1,40})[ \t]{0,8}\(([^()\n]{1,200})\)")


def _arns(text: str, cfg: Config) -> Iterator[_Hit]:
    for m in _ARN.finditer(text):
        if m.group(1) not in cfg.account_ids:
            yield _Hit("account-in-arn", m.start(1), m.end(1))


def _is_fraction(text: str, start: int, end: int) -> bool:
    """`0.123456789012` and `1.5e-05`-style numbers, not `logs-1.<account>.s3...` or `v1.<account>-x`."""
    if not (start >= 2 and text[start - 1] == "." and text[start - 2] in _DIGITS):
        return False
    after = text[end:end + 1]
    return not (after in (".", "-", "_") or (after.isalpha() and after not in "eE"))


def _account_ids(text: str, cfg: Config) -> Iterator[_Hit]:
    """Twelve digits that are not a placeholder, and not visibly something else: part of a hash
    (a run of 16 or more hexadecimal characters), the fraction of a decimal number, or the last
    group of a UUID. A longer run of digits never matches: the pattern wants exactly twelve.
    Also the form the AWS console displays, three groups of four digits, when it stands alone."""
    in_arn = {m.start(1) for m in _ARN.finditer(text)}
    for m in _TWELVE.finditer(text):
        start, end = m.span()
        if m.group() in cfg.account_ids or start in in_arn:
            continue
        left = start
        while left > 0 and start - left < _HASH_RUN and text[left - 1] in _HEX:
            left -= 1
        right = end
        while right < len(text) and right - left < _HASH_RUN and text[right] in _HEX:
            right += 1
        if right - left >= _HASH_RUN:
            continue
        if _is_fraction(text, start, end):
            continue
        if _UUID_HEAD.search(text, max(0, start - 24), start):
            continue
        yield _Hit("account-id", start, end)
    for m in _DASHED.finditer(text):
        if m.group().replace("-", "") not in cfg.account_ids:
            yield _Hit("account-id", m.start(), m.end())


def _access_keys(text: str, cfg: Config) -> Iterator[_Hit]:
    for m in _ACCESS_KEY.finditer(text):
        if m.group() not in cfg.strings:
            yield _Hit("access-key", m.start(), m.end())
    for m in _SECRET_KEY.finditer(text):
        if m.group(1) not in cfg.strings:
            yield _Hit("access-key", m.start(1), m.end(1))


def _is_placeholder_host(host: str, cfg: Config) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in cfg.email_domains)


def _tokens(text: str, cfg: Config) -> Iterator[_Hit]:
    for pattern in _TOKENS:
        for m in pattern.finditer(text):
            if m.group() not in cfg.strings:
                yield _Hit("token", m.start(), m.end())
    for m in _URL_CREDENTIALS.finditer(text):
        if not _TEMPLATE.fullmatch(m.group(2)) and not _is_placeholder_host(m.group(3), cfg):
            yield _Hit("token", m.start(1), m.end(1))


def _endpoints(text: str, cfg: Config) -> Iterator[_Hit]:
    documented = {s.lower() for s in cfg.strings}   # host names have no letter case
    for pattern in _ENDPOINTS:
        for m in pattern.finditer(text):
            if m.group().lower() not in documented:
                yield _Hit("endpoint", m.start(), m.end())


def _resource_ids(text: str, cfg: Config) -> Iterator[_Hit]:
    for pattern in _RESOURCE_IDS:
        for m in pattern.finditer(text):
            if m.group(1) not in cfg.resource_ids and m.group() not in cfg.strings:
                yield _Hit("resource-id", m.start(), m.end())


def _is_version(labels: list[str]) -> bool:
    """`ruff@0.17.3`, `checkout@v7.0.1`, `pkg@1.x.beta`: a version, not a mail domain. A domain
    whose first label is a number and whose second is a name (`163.com`) is a mail domain."""
    return bool(_VERSION_LABEL.fullmatch(labels[0])) and (labels[1][:1] in _DIGITS or labels[1] in ("x", "*"))


def _emails(text: str, cfg: Config) -> Iterator[_Hit]:
    at = text.find("@")
    while at != -1:
        local = _EMAIL_LOCAL.search(text, max(0, at - 64), at)
        domain = _EMAIL_DOMAIN.match(text, at + 1)
        at_next = text.find("@", at + 1)
        if local and domain:
            name = local.group()
            if text[at - len(name) - 1:at - len(name)] == "\\":   # `\n@pytest.mark...` in a string: an escape
                name = name[1:]
            name = name.lstrip(".")
            host = domain.group().lower()
            labels = host.split(".")
            allowed = f"{name}@{host}".lower() in cfg.emails or _is_placeholder_host(host, cfg)
            plausible = (
                bool(name)
                and ((len(labels[-1]) >= 2 and labels[-1].isalpha()) or labels[-1].startswith("xn--"))
                and labels[-1] not in _FILE_SUFFIXES
                and not _is_version(labels)
            )
            if plausible and not allowed:
                yield _Hit("email", at - len(name), domain.end())
        at = at_next


def _home_paths(text: str, cfg: Config) -> Iterator[_Hit]:
    for pattern in _HOME_PATHS:
        for m in pattern.finditer(text):
            if m.group(1).split()[0] not in cfg.home_users:   # the first word, where a name may hold spaces
                yield _Hit("home-path", m.start(), m.end())


def _local_values(text: str, cfg: Config) -> Iterator[_Hit]:
    """10. local-value: a value from the owner's own list, which no public pattern could name."""
    for pattern in cfg.local_values:
        for m in pattern.finditer(text):
            yield _Hit("local-value", m.start(), m.end())


def _url_end(text: str, start: int, end: int) -> int:
    """Where the URL matched in text[start:end] stops: before the punctuation of the sentence it
    is written in, and before a bracket that closes one opened ahead of it (a Markdown link)."""
    while end > start:
        last = text[end - 1]
        if last in ".,;:!?'" or (last in _CLOSERS and text.count(last, start, end) > text.count(_CLOSERS[last], start, end)):
            end -= 1
        else:
            break
    return end


def _console_links(text: str, cfg: Config) -> Iterator[_Hit]:
    for m in _CONSOLE_LINK.finditer(text):
        yield _Hit("console-link", m.start(), _url_end(text, m.start(), m.end()))


def _build_badges(text: str, cfg: Config) -> Iterator[_Hit]:
    for pattern in _BUILD_BADGES:
        for m in pattern.finditer(text):
            yield _Hit("build-badge", m.start(), _url_end(text, m.start(), m.end()))
    for m in _SHIELDS_BADGE.finditer(text):
        end = _url_end(text, m.start(), m.end())
        if _BUILD_WORDS.search(text, m.start(1), end):
            yield _Hit("build-badge", m.start(), end)


_CONTENT_RULES: tuple[Callable[[str, Config], Iterator[_Hit]], ...] = (
    _arns, _account_ids, _access_keys, _tokens, _endpoints, _resource_ids, _emails, _home_paths, _local_values,
    _console_links, _build_badges,
)


def _hits(text: str, cfg: Config) -> list[_Hit]:
    """Every match of every content rule, in order. What is reported may be fewer (_skipped);
    what is masked never is."""
    found = {hit for rule in _CONTENT_RULES for hit in rule(text, cfg)}
    return sorted(found, key=lambda h: (h.start, h.end, h.rule))


def _skipped(path: str, cfg: Config, binary: bool) -> frozenset[str]:
    """The rules not reported for this file: account IDs in a lockfile, addresses in a binary."""
    lockfile = frozenset({"account-id"}) if cfg.is_lockfile(path) else frozenset()
    return lockfile | (TEXT_ONLY_RULES if binary else frozenset())


def _mask(hit: _Hit) -> str:
    return f"[redacted {hit.rule}]"


def _printable(text: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _excerpts(line: str, offset: int, hits: Sequence[_Hit], context: int = 48) -> list[str]:
    """One excerpt per hit of a line: the text around it, with every hit of the line masked (so
    an excerpt never shows what another finding is about). `offset` is the line's start in the text."""
    pieces: list[str] = []
    positions: list[int] = []
    cursor = 0   # in the line
    length = 0   # of the masked line so far
    for hit in hits:
        start, end = hit.start - offset, hit.end - offset
        if start >= cursor:
            before = _printable(line[cursor:start])
            pieces += [before, _mask(hit)]
            positions.append(length + len(before))
            length += len(before) + len(_mask(hit))
        else:   # overlaps the previous hit: it shares that hit's mask
            positions.append(positions[-1])
        cursor = max(cursor, end)
    pieces.append(_printable(line[cursor:]))
    masked = "".join(pieces)
    out = []
    for hit, pos in zip(hits, positions, strict=True):
        lo, hi = max(0, pos - context), min(len(masked), pos + len(_mask(hit)) + context)
        out.append(("…" if lo else "") + masked[lo:hi].strip() + ("…" if hi < len(masked) else ""))
    return out


def _scan_content(text: str, path: str, cfg: Config, binary: bool = False,
                  markers: bool = True) -> tuple[list[Finding], list[Finding]]:
    """The findings of one text, and the findings an inline marker allowed. A line ends at a
    line feed or a carriage return, so a marker never reaches past its own line."""
    hits = _hits(text, cfg)
    skipped = _skipped(path, cfg, binary)
    if all(hit.rule in skipped for hit in hits):
        return [], []
    starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", text)]
    by_line: dict[int, list[_Hit]] = {}
    for hit in hits:
        by_line.setdefault(bisect.bisect_right(starts, hit.start) - 1, []).append(hit)
    findings: list[Finding] = []
    allowed: list[Finding] = []
    for index, line_hits in by_line.items():
        begin = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        line = text[begin:end].rstrip("\r\n")
        budget: dict[str, int] = {}
        for marker in _ALLOW.finditer(line if markers else ""):
            if marker.group(2).strip() and marker.group(1) not in NEVER_ALLOWED_INLINE:
                budget[marker.group(1)] = budget.get(marker.group(1), 0) + 1
        for hit, excerpt in zip(line_hits, _excerpts(line, begin, line_hits), strict=True):
            if hit.rule in skipped:
                continue   # masked in its neighbours' excerpts, not reported itself
            finding = Finding(path, index + 1, hit.rule, excerpt)
            if budget.get(hit.rule, 0) > 0:
                budget[hit.rule] -= 1
                allowed.append(finding)
            else:
                findings.append(finding)
    return findings, allowed


# --------------------------------------------------------------------------------------------
# Path rule
# --------------------------------------------------------------------------------------------

_KEY_FILES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".ppk")


def _forbidden(path: str) -> str | None:
    """5. forbidden-file: why a file of this name may never be tracked, or None."""
    parts = path.split("/")
    name = parts[-1]
    reason = None
    if name == "cdk.context.json":
        reason = "the CDK lookup cache records account and resource IDs"
    elif "cdk.out" in parts[:-1]:
        reason = "synthesized CDK output"
    elif ".aws" in parts[:-1]:
        reason = "AWS CLI configuration or credentials"
    elif name != ".env.example" and (name in (".env", ".envrc") or name.startswith(".env.")):
        reason = "an environment file; only .env.example, with placeholders, is tracked"
    elif name.endswith(_KEY_SUFFIXES) or name in _KEY_FILES:
        reason = "a key or certificate file"
    elif name.endswith(".tfstate") or ".tfstate." in name:
        reason = "infrastructure state records account and resource IDs"
    return reason


# --------------------------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------------------------

_default: Config | None = None


def _config(config: Config | None) -> Config:
    global _default
    if config is not None:
        return config
    if _default is None:
        _default = load_config()
    return _default


def _readings(data: bytes) -> list[tuple[str, bool]]:
    """Every way these bytes may hold text, each with whether to treat it as binary. Bytes that
    are not UTF-8 are replaced, which leaves every ASCII pattern findable. A file with NUL bytes
    is also read without them: that is how UTF-16 and UTF-32 text shows its ASCII, whatever git
    attribute or byte-order mark it carries. Such text has a NUL in most of its odd, or most of
    its even, positions; a compiled or compressed file has them anywhere."""
    plain = data.decode("utf-8", errors="replace")
    nul = data.count(0)
    if not nul:
        return [(plain, False)]
    binary = nul * 1000 > len(data)   # one stray NUL does not make a text file binary
    wide = max(data[0::2].count(0), data[1::2].count(0)) * 5 >= 3 * (len(data) // 2 or 1)
    return [(plain, binary), (data.replace(b"\0", b"").decode("utf-8", errors="replace"), binary and not wide)]


def _name(path: str, cfg: Config) -> tuple[list[str], str]:
    """The rules a path itself breaks, and the path as it may be printed: any value in it masked,
    nothing that breaks a line or a terminal. A name is read whole and without its last suffix,
    so that `<address>.txt` is an address and not a host called `.txt`."""
    stem, dot, suffix = path.rpartition(".")
    if not dot or "/" in suffix:
        stem = ""
    rules = dict.fromkeys(hit.rule for text in (path, stem) for hit in _hits(text, cfg))
    shown = redact(path, cfg)
    if stem and shown == path:
        shown = redact(stem, cfg) + "." + suffix
    return list(rules), _printable(shown)


def scan_text(text: str, path: str = "<text>", config: Config | None = None) -> list[Finding]:
    """The content findings of `text`. `path` labels them and selects per-path behaviour (a
    lockfile is not checked for account IDs); file names are scan_paths' business."""
    return _scan_content(text, path, _config(config))[0]


def redact(text: str, config: Config | None = None) -> str:
    """`text` with everything a content rule matches masked, whatever inline markers say, and
    private-key blocks masked whole. For anything about to be published: evidence, commit
    statuses, log excerpts."""
    text = _PEM_BLOCK.sub("[redacted token]", text)
    out: list[str] = []
    cursor = 0
    for hit in _hits(text, _config(config)):
        if hit.start >= cursor:
            out += [text[cursor:hit.start], _mask(hit)]
        cursor = max(cursor, hit.end)
    out.append(text[cursor:])
    return "".join(out)


@dataclass
class Report:
    findings: list[Finding]
    allowed: list[Finding]
    files: int = 0
    commits: int = 0

    def add(self, path: str, data: bytes | None, cfg: Config, outside: bool = False) -> None:
        """One file: its name, then its content (None: the name only, for a path with no blob).
        A value in the name is a finding like any other, and is masked wherever the path is printed.

        `outside`: the file is not under the scan's root (an issue body in a temporary file, say).
        The folders above it are not the repository's, so only its own name is judged, and the
        configuration's globs, which are relative to the repository, do not apply to it."""
        if not outside and cfg.is_excluded(path):
            return
        self.files += 1
        folder, slash, name = path.rpartition("/") if outside else ("", "", path)
        name_rules, shown = _name(name, cfg)
        shown = _printable(redact(folder, cfg)) + slash + shown
        reason = _forbidden(name)
        if reason:
            self.findings.append(Finding(shown, 0, "forbidden-file", reason))
        self.findings += [Finding(shown, 0, rule, "in the file name") for rule in name_rules]
        if data is not None:
            seen: set[Finding] = set()
            for text, binary in _readings(data):
                found, allowed = _scan_content(text, "" if outside else path, cfg, binary)
                for finding, into in [(f, self.findings) for f in found] + [(f, self.allowed) for f in allowed]:
                    finding = Finding(shown, finding.line, finding.rule, finding.excerpt)
                    if finding not in seen:
                        seen.add(finding)
                        into.append(finding)


def _walk(top: Path) -> Iterator[Path]:
    """Every file under `top`, and every symbolic link (a link to a directory is not followed:
    like any link, it is read as its target's name). `.git` directories are not entered."""
    for folder, dirs, files in os.walk(top):
        links = [d for d in dirs if (Path(folder) / d).is_symlink()]
        dirs[:] = sorted(d for d in dirs if d != ".git" and d not in links)
        for name in sorted(files + links):
            yield Path(folder) / name


def _read(path: Path) -> bytes | None:
    """A file's bytes; a symbolic link's target, as git stores it; None for anything else."""
    try:
        if path.is_symlink():
            return os.fsencode(path.readlink())
        if path.is_file():
            return path.read_bytes()
    except OSError as exc:
        raise ScanError(f"cannot read {path}: {exc}") from exc
    return None


def _report_paths(paths: Iterable[str | Path], root: Path | None, cfg: Config) -> Report:
    report = Report([], [])
    base = (root or Path.cwd()).resolve()
    for given in paths:
        top = Path(given) if Path(given).is_absolute() else base / given
        if not top.is_symlink() and not top.exists():
            raise ScanError(f"no such file or directory: {given}")
        for path in (_walk(top) if top.is_dir() and not top.is_symlink() else [top]):
            data = _read(path)
            if data is None:
                continue
            try:
                inside = path.resolve().relative_to(base) if not path.is_symlink() else path.relative_to(base)
            except ValueError:
                report.add(path.as_posix(), data, cfg, outside=True)
            else:
                report.add(inside.as_posix(), data, cfg)
    return report


def scan_paths(paths: Iterable[str | Path], root: Path | None = None, config: Config | None = None) -> list[Finding]:
    """The findings of these files and directories as they are on disk: names and content. Paths
    are shown relative to `root` (default: the current directory), which is also what the
    configuration's globs are matched against. A file outside `root` is scanned too: its content
    and its own name, with no exclude and no lockfile exemption."""
    return _report_paths(paths, root, _config(config)).findings


# --------------------------------------------------------------------------------------------
# Git sources
# --------------------------------------------------------------------------------------------

def _git(root: Path | None, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True, check=False)
    except OSError as exc:
        raise ScanError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        said = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ScanError(f"git {args[0]} failed: {said[-1] if said else proc.returncode}")
    return proc.stdout


def _toplevel() -> Path:
    try:
        return Path(os.fsdecode(_git(None, "rev-parse", "--show-toplevel")).rstrip("\n"))
    except ScanError as exc:
        raise ScanError(f"not inside a git work tree ({exc}); scan a plain directory with --paths") from exc


def _blobs(root: Path, oids: Sequence[str], batch: int = 500) -> Iterator[tuple[str, bytes]]:
    """The content of these blobs, a bounded number at a time."""
    for i in range(0, len(oids), batch):
        chunk = oids[i:i + batch]
        out = _git(root, "cat-file", "--batch", stdin="".join(f"{oid}\n" for oid in chunk).encode())
        pos = 0
        for oid in chunk:
            eol = out.index(b"\n", pos)
            header = out[pos:eol].split()
            if len(header) != 3 or header[1] != b"blob":
                raise ScanError(f"git cat-file: {oid[:12]} is not a readable blob")
            size = int(header[2])
            yield oid, out[eol + 1:eol + 1 + size]
            pos = eol + 1 + size + 1


def _report_blobs(root: Path, entries: Sequence[tuple[str, str]], cfg: Config, label: bool = False) -> Report:
    """Scan (path, blob id) pairs. `label` adds the blob id to the path, for history scans."""
    report = Report([], [])
    wanted = [(path, oid) for path, oid in entries if not cfg.is_excluded(path)]
    content = dict(_blobs(root, sorted({oid for _, oid in wanted})))
    for path, oid in wanted:
        before = len(report.findings), len(report.allowed)
        report.add(path, content[oid], cfg)
        if label:
            for found, since in ((report.findings, before[0]), (report.allowed, before[1])):
                found[since:] = [Finding(f"{f.path}@{oid[:12]}", f.line, f.rule, f.excerpt) for f in found[since:]]
    return report


def _report_worktree(cfg: Config) -> Report:
    """Every file git tracks or would add, read from the work tree."""
    root = _toplevel()
    listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    report = Report([], [])
    for name in dict.fromkeys(os.fsdecode(n) for n in listed.split(b"\0") if n):
        data = _read(root / name)
        if data is not None:   # a tracked file deleted from the work tree has nothing to scan
            report.add(name, data, cfg)
    return report


def _index_entries(root: Path) -> dict[str, str]:
    """path -> blob id for every index entry that is a file or a symbolic link."""
    entries: dict[str, str] = {}
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            meta, name = record.split(b"\t", 1)
            mode, oid, _stage = meta.split()
            if mode != b"160000":   # a submodule has no blob
                entries[os.fsdecode(name)] = oid.decode()
    return entries


def _report_staged(cfg: Config) -> Report:
    """The staged content of every file the next commit adds or changes (all of it, not only the
    changed lines: a file with a finding stays blocked until the finding is gone)."""
    root = _toplevel()
    changed = _git(root, "diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=ACMT")
    index = _index_entries(root)
    names = [os.fsdecode(n) for n in changed.split(b"\0") if n]
    return _report_blobs(root, [(name, index[name]) for name in names if name in index], cfg)


def _report_rev(rev: str, cfg: Config) -> Report:
    """The tree of one commit."""
    root = _toplevel()
    entries = []
    for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", "--end-of-options", rev).split(b"\0"):
        if record:
            meta, name = record.split(b"\t", 1)
            _mode, kind, oid = meta.split()
            if kind == b"blob":
                entries.append((os.fsdecode(name), oid.decode()))
    return _report_blobs(root, entries, cfg)


def _report_commits(root: Path, revs: Sequence[str], cfg: Config, report: Report) -> None:
    """What a commit publishes besides its files: the author's and committer's name and address,
    and the message with its trailers. Fields end with NUL, which no commit can contain."""
    logged = _git(root, "log", "--format=%H%x00%an <%ae>%x00%cn <%ce>%x00%B%x00", *revs)
    fields = logged.decode("utf-8", errors="replace").split("\x00")
    if len(fields) % 4 != 1 or fields[-1].strip():
        raise ScanError("git log: unexpected output; the commits were not scanned")
    for i in range(0, len(fields) - 1, 4):
        sha, author, committer, message = fields[i].strip(), *fields[i + 1:i + 4]
        text = f"Author: {author}\nCommitter: {committer}\n\n{message}"
        # No inline marker counts here: nobody reviews a commit message as they review a file.
        found, _ = _scan_content(text, f"<commit>@{sha[:12]}", cfg, markers=False)
        report.commits += 1
        report.findings += found


def _history_paths(root: Path, revs: Sequence[str]) -> list[str]:
    """Every path a commit of the history ever added: a blob is listed under one name only, and
    a forbidden name stays in the history after a rename."""
    listed = _git(root, "log", "--format=", "--name-only", "-z", "--no-renames", "--diff-filter=A", "-m", *revs)
    return list(dict.fromkeys(os.fsdecode(n) for n in listed.split(b"\0") if n.strip(b"\n")))


def _report_history(revs: Sequence[str], cfg: Config) -> Report:
    """Every version of every file in the commits `revs` select (default: all refs and HEAD), and
    those commits' own text."""
    root = _toplevel()
    if not revs:
        refs = _git(root, "for-each-ref", "--format=%(refname)").split()
        try:
            _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
            head = ["HEAD"]
        except ScanError:
            head = []
        if not refs and not head:
            return Report([], [])   # no commit yet: no history
        revs = ["--all", *head]
    else:
        revs = ["--end-of-options", *revs]
    listed = _git(root, "rev-list", "--objects", *revs).decode("utf-8", errors="surrogateescape").splitlines()
    named = [line.split(" ", 1) for line in listed if " " in line]
    oids = [oid for oid, _ in named]
    kinds = _git(root, "cat-file", "--batch-check=%(objecttype)", stdin="".join(f"{o}\n" for o in oids).encode())
    blobs = [(name, oid) for (oid, name), kind in zip(named, kinds.split(b"\n"), strict=False) if kind == b"blob"]
    report = _report_blobs(root, blobs, cfg, label=True)
    scanned = {name for name, _ in blobs}
    for name in _history_paths(root, revs):
        if name not in scanned:
            report.add(name, None, cfg)   # its content was read under another name
    _report_commits(root, revs, cfg, report)
    return report


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan.py",
        description="Public-repository hygiene scanner. With no option: every file git tracks or would add.",
        epilog="Exit status: 0 clean, 1 findings, 2 usage or environment error. Rules: " + ", ".join(RULES) + ".",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--staged", action="store_true", help="scan what the next commit adds or changes")
    source.add_argument("--paths", nargs="+", metavar="P", help="scan these files or directories as they are on disk")
    source.add_argument("--rev", metavar="REV", help="scan the tree of one commit")
    source.add_argument("--history", nargs="*", metavar="REVS",
                        help="scan every file version, commit message and commit identity in the history "
                             "(of REVS, such as main..HEAD; default: all)")
    source.add_argument("--redact", action="store_true", help="copy stdin to stdout with every finding masked")
    parser.add_argument("--config", type=Path, metavar="FILE", help="another hygiene.toml (default: beside scan.py)")
    parser.add_argument("--show-allowed", action="store_true", help="also list, on stderr, what inline markers allowed")
    parser.add_argument("-q", "--quiet", action="store_true", help="print nothing when there is no finding")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.redact:
            sys.stdout.write(redact(sys.stdin.read(), cfg))
            return 0
        if args.staged:
            report = _report_staged(cfg)
        elif args.paths:
            report = _report_paths(args.paths, None, cfg)
        elif args.rev:
            report = _report_rev(args.rev, cfg)
        elif args.history is not None:
            report = _report_history(args.history, cfg)
        else:
            report = _report_worktree(cfg)
    except ScanError as exc:
        print(f"hygiene: error: {exc}", file=sys.stderr)
        return 2
    for finding in sorted(report.findings, key=lambda f: (f.path, f.line, f.rule)):
        print(finding.render())
    sys.stdout.flush()   # findings first, also when both streams end in one place (a hook, a CI log)
    if args.show_allowed:
        for finding in sorted(report.allowed, key=lambda f: (f.path, f.line, f.rule)):
            print(f"allowed inline: {finding.render()}", file=sys.stderr)
    if report.findings or not args.quiet:
        scanned = f"{report.files} files" + (f" and {report.commits} commits" if report.commits else "")
        local = f", {len(cfg.local_values)} local values checked" if cfg.local_values else ""
        print(f"hygiene: {scanned} scanned, {len(report.findings)} findings, "
              f"{len(report.allowed)} allowed inline{local}", file=sys.stderr)
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
