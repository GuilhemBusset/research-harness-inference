"""What the CLI keeps on the student's machine (system design, section 6.4).

Two things, in the folder `config_dir()` names:

- the configuration, `config.json`: the endpoint's address, given once with
  `rhi login --endpoint`, and the identifier of this installation, made at its first login;
- the key, in the operating system's keychain when there is one, and otherwise in the file `key`,
  which only its owner can read. A key file that others can read is refused, not used.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import keyring
import keyring.errors
from keyring.backends import fail

# The keychain entry: one key per installation, so one entry per user of the machine.
SERVICE = "rhi"
USERNAME = "api-key"

CONFIG_FILE = "config.json"
KEY_FILE = "key"


class StoreError(Exception):
    """What the CLI keeps cannot be read or written as it must be."""


def config_dir() -> Path:
    """The CLI's folder: `$XDG_CONFIG_HOME/rhi`, or `~/.config/rhi`."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "rhi"


def _private_folder() -> Path:
    folder = config_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _write_private(path: Path, text: str) -> None:
    """Write a file that only its owner can read or write, whatever the umask or an older mode."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _read_private(path: Path) -> str | None:
    """The text of a file that only its owner can read; None when there is no file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StoreError(f"{path} cannot be read.") from error
    try:
        status = os.fstat(fd)
        if status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) & 0o077:
            raise StoreError(f"{path} can be read by other users: it is not used. Delete it and run rhi login again.")
        with os.fdopen(fd, encoding="utf-8", closefd=False) as file:
            return file.read()
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Config:
    """The endpoint's address and this installation's identifier, each None until it is known."""

    endpoint: str | None = None
    installation: str | None = None


def load_config() -> Config:
    """The stored configuration; an empty one before the first login."""
    path = config_dir() / CONFIG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    try:
        data = json.loads(text)
    except ValueError as error:
        raise StoreError(f"{path} is not valid JSON. Delete it and run rhi login --endpoint again.") from error
    if not isinstance(data, dict):
        raise StoreError(f"{path} is not a JSON object. Delete it and run rhi login --endpoint again.")
    endpoint, installation = data.get("endpoint"), data.get("installation")
    return Config(
        endpoint=endpoint if isinstance(endpoint, str) else None,
        installation=installation if isinstance(installation, str) else None,
    )


def save_config(config: Config) -> None:
    """Store the configuration."""
    body = {"endpoint": config.endpoint, "installation": config.installation}
    _write_private(_private_folder() / CONFIG_FILE, json.dumps(body, indent=2) + "\n")


def has_keychain() -> bool:
    """Whether the operating system offers a keychain that keyring recommends."""
    backend = keyring.get_keyring()
    return not isinstance(backend, fail.Keyring) and backend.priority > 0


def save_key(key: str) -> str:
    """Store the key, replacing any earlier one; say where: "the keychain" or the file's path."""
    path = config_dir() / KEY_FILE
    if has_keychain():
        try:
            keyring.set_password(SERVICE, USERNAME, key)
        except keyring.errors.KeyringError:
            pass
        else:
            path.unlink(missing_ok=True)  # an older key left in the file by an earlier login
            return "the keychain"
        try:  # the keychain refused the new key: drop the one it holds, which is now superseded
            keyring.delete_password(SERVICE, USERNAME)
        except keyring.errors.KeyringError:
            pass
    _write_private(_private_folder() / KEY_FILE, key)
    return str(path)


def load_key() -> str | None:
    """The stored key, None if there is none.

    The file exists only when the last login could not use the keychain (`save_key` removes it
    whenever the keychain takes the key), so a key in the file is the latest and comes first; the
    keychain is read otherwise. A key file that other users can read raises `StoreError`.
    """
    key = _read_private(config_dir() / KEY_FILE)
    if key is not None:
        return key
    if has_keychain():
        try:
            key = keyring.get_password(SERVICE, USERNAME)
        except keyring.errors.KeyringError:
            key = None
        return key or None
    return None


def delete_key() -> None:
    """Remove the stored key, from the file and from the keychain; nothing when there is none.

    `StoreError` unless the key is known to be gone: when the file cannot be removed, when the
    keychain still holds a key, and when a keychain that refused the removal cannot be read either
    (a locked one, say), since it may still hold the key.
    """
    path = config_dir() / KEY_FILE
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise StoreError(f"{path} cannot be removed: delete it yourself.") from error
    if not has_keychain():
        return
    try:
        keyring.delete_password(SERVICE, USERNAME)
    except keyring.errors.KeyringError:
        pass  # no entry, which is the aim, or a keychain that refuses: what it holds now says which
    else:
        return
    try:
        kept = keyring.get_password(SERVICE, USERNAME)
    except keyring.errors.KeyringError as error:
        raise StoreError(
            f"The keychain cannot be opened, and may still hold the key: unlock it and remove its entry `{SERVICE}`."
        ) from error
    if kept:
        raise StoreError(f"The keychain still holds the key: remove its entry `{SERVICE}` yourself.")
