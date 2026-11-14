"""The doctor's `commit identity` and `git config` lines. The engine commits as the repository's git identity, a
commit publishes its author's and its committer's name and address for good, and the history
scan (CI's `history` job, and the engine's own check before a push) reads both on every commit.
With an identity the hygiene scanner refuses, no branch of the loop could ever land: the doctor
fails first, and a run does not start. The scanner itself judges, with this repository's
allow-list; nothing of that list is repeated in the engine.

And the repository's git configuration, which agents read inside the sandbox, holds no
credential: a login belongs in a credential helper, which the sandbox hides."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import git, identify
from implement_loop import cli
from test_standing_checks import ACCOUNT, add_scanner

# An address under no allow-listed domain, never written out in a file.
REFUSED = "someone" + "@" + "corp-mail.org"
IDENTITY_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL")


class IdentityCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-identity-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "work"
        git("init", "-q", "-b", "main", str(self.root), cwd=self.tmp)
        identify(self.root)   # Loop Test, at an address under `.invalid`: a reserved name, allowed
        add_scanner(self.root)
        clean = {k: v for k, v in os.environ.items() if k not in IDENTITY_VARS}
        patcher = mock.patch.dict(os.environ, clean, clear=True)   # whatever the developer's shell sets
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_identity_the_scanner_allows_passes_and_is_shown(self):
        self.assertEqual(cli.identity_check(self.root), (True, "commit identity", "Loop Test <loop-test@example.invalid>"))

    def test_a_github_no_reply_address_passes(self):
        git("config", "user.email", "12345+someone@users.noreply.github.com", cwd=self.root)
        ok, _, detail = cli.identity_check(self.root)
        self.assertTrue(ok, detail)

    def test_an_address_the_scanner_refuses_fails_and_is_never_shown(self):
        git("config", "user.email", REFUSED, cwd=self.root)
        ok, name, detail = cli.identity_check(self.root)
        self.assertEqual((ok, name), (False, "commit identity"))
        self.assertIn("the author git would commit as here is not one the hygiene scanner allows", detail)
        self.assertIn("no-reply address", detail)
        self.assertIn("docs/factory/green-light.md", detail)
        self.assertNotIn(REFUSED, detail)
        self.assertNotIn("corp-mail", detail)

    def test_what_the_environment_puts_in_its_place_is_what_counts(self):
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_EMAIL": REFUSED}):
            ok, _, detail = cli.identity_check(self.root)
        self.assertFalse(ok)
        self.assertIn("the committer git would commit as", detail, "the author, from the configuration, passed")
        git("config", "user.email", REFUSED, cwd=self.root)
        allowed = {"GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_EMAIL": "c@example.com"}
        with mock.patch.dict(os.environ, allowed):
            self.assertEqual(cli.identity_check(self.root),
                             (True, "commit identity", "Loop Test <a@example.com>; Loop Test <c@example.com>"))

    def test_a_name_the_scanner_refuses_fails_too(self):
        git("config", "user.name", f"Owner of {ACCOUNT}", cwd=self.root)
        ok, _, detail = cli.identity_check(self.root)
        self.assertFalse(ok)
        self.assertNotIn(ACCOUNT, detail)

    def test_the_allow_list_is_the_repositorys_own_file(self):
        git("config", "user.email", REFUSED, cwd=self.root)
        self.assertFalse(cli.identity_check(self.root)[0])
        allow_list = self.root / "tools" / "hygiene" / "hygiene.toml"
        text = allow_list.read_text()
        self.assertIn('"users.noreply.github.com",', text)
        allow_list.write_text(text.replace('"users.noreply.github.com",', '"users.noreply.github.com", "corp-mail.org",'))
        self.assertTrue(cli.identity_check(self.root)[0], "what that file allows, the doctor allows: it holds no copy")

    def test_without_the_scanner_nothing_vouches_for_the_identity(self):
        shutil.rmtree(self.root / "tools")
        ok, _, detail = cli.identity_check(self.root)
        self.assertFalse(ok)
        self.assertIn("tools/hygiene/scan.py is missing", detail)

    def test_a_scanner_that_cannot_answer_fails_closed(self):
        (self.root / "tools" / "hygiene" / "hygiene.toml").write_text("[placeholders\n")
        ok, _, detail = cli.identity_check(self.root)
        self.assertFalse(ok)
        self.assertIn("the hygiene scanner could not be asked", detail)

    def doctor(self) -> dict[str, tuple[str, str]]:
        real = cli._out
        with mock.patch.object(cli, "_out", lambda cmd: real(cmd) if cmd[0] == "git" else (0, "stub 1.0")), \
             mock.patch.object(cli.environment, "probe", return_value=[]), \
             mock.patch.object(cli, "sandbox_check", return_value=(True, "sandbox", "ok")), \
             mock.patch.object(cli.shutil, "which", lambda name: f"/usr/bin/{name}"):
            return {name: (status, detail) for status, name, detail in cli.doctor_checks(self.root, None)}

    def test_the_doctor_prints_the_line_and_it_can_fail(self):
        self.assertEqual(self.doctor()["commit identity"], ("PASS", "Loop Test <loop-test@example.invalid>"))
        git("config", "user.email", REFUSED, cwd=self.root)
        status, detail = self.doctor()["commit identity"]
        self.assertEqual(status, "FAIL", "a failure, not a warning: `run` and `supervise` refuse to start on it")
        self.assertNotIn(REFUSED, detail)


class GitConfigCheckTests(unittest.TestCase):
    """Agents read the repository's git configuration inside the sandbox: it must hold no credential."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-gitconfig-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "work"
        git("init", "-q", "-b", "main", str(self.root), cwd=self.tmp)
        identify(self.root)

    def test_bare_urls_and_a_helper_pass(self):
        for url in ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo.git",
                    str(self.tmp / "remote.git")):
            git("config", "remote.origin.url", url, cwd=self.root)
            with self.subTest(url=url):
                self.assertEqual(cli.git_config_check(self.root),
                                 (True, "git config", "no credential in the repository's configuration"))
        git("config", "credential.helper", "!gh auth git-credential", cwd=self.root)
        self.assertTrue(cli.git_config_check(self.root)[0], "a helper is where the login belongs")

    def test_a_credential_in_a_url_or_a_header_fails_and_is_not_shown(self):
        token = "ghp_" + "a1B2" * 9
        cases = {"remote.origin.url": "https://x-access-token:" + token + "@github.com/owner/repo.git",
                 "remote.origin.pushurl": "https://" + token + "@github.com/owner/repo.git",
                 "remote.mirror.url": "https://owner:" + "hunter2@git.example.com/owner/repo.git",
                 "http.https://github.com/.extraheader": "AUTHORIZATION: basic eDp5"}
        for key, value in cases.items():
            with self.subTest(key=key):
                git("config", key, value, cwd=self.root)
                ok, name, detail = cli.git_config_check(self.root)
                self.assertEqual((ok, name), (False, "git config"))
                self.assertIn(key, detail, "which setting, never its value")
                self.assertNotIn(token, detail)
                self.assertNotIn("hunter2", detail)
                self.assertIn("credential helper", detail)
                git("config", "--unset", key, cwd=self.root)
        self.assertTrue(cli.git_config_check(self.root)[0])

    def test_the_doctor_prints_the_line(self):
        real = cli._out
        with mock.patch.object(cli, "_out", lambda cmd: real(cmd) if cmd[0] == "git" else (0, "stub 1.0")), \
             mock.patch.object(cli.environment, "probe", return_value=[]), \
             mock.patch.object(cli, "sandbox_check", return_value=(True, "sandbox", "ok")), \
             mock.patch.object(cli.shutil, "which", lambda name: f"/usr/bin/{name}"):
            lines = {name: status for status, name, _ in cli.doctor_checks(self.root, None)}
        self.assertEqual(lines["git config"], "PASS")


if __name__ == "__main__":
    unittest.main()
