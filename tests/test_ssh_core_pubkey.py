"""CI-13: bring `lib/pubkey.py` and `lib/ssh_config.py` under test.

Both modules existed since 1.1.0 with zero importers: `deploy_pubkey.py` had
grown its own private copies of "pick the default key", "does this look like a
public key", "quote it for sh" and "adopt key auth in ~/.ssh/config", while
`lib/pubkey.py` (312 lines) and `lib/ssh_config.py` (160 lines) sat unreachable.
That is the same "two implementations drift" failure the issue list calls out
for `PtySession` (CI-08) — just slower, because both halves worked.

The acceptance criterion is measured, not asserted: `tools/measure_coverage.py`
counts these two files as 0% before this change. So the fix has two halves:

* move the duplication out of `deploy_pubkey.py` (it now calls the library),
* pin the library's behaviour here, including the case that matters most — a
  **private** key passed as `--key` must never reach a remote
  ``authorized_keys``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import SCRIPTS, load_script  # noqa: E402
from .ssh_core.compat import temp_dir  # noqa: E402

from lib.pubkey import (  # noqa: E402
    DEFAULT_KEY,
    MARK_ALREADY,
    MARK_ERROR_PREFIX,
    MARK_HOME,
    MARK_INSTALLED,
    Pubkey,
    PubkeyError,
    build_install_script,
    load_pubkey,
    looks_like_pubkey_text,
    normalize_identity_path,
    parse_install_output,
    pick_default_pubkey,
    resolve_key_path,
    sh_quote,
)

PUBKEY_TEXT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY test@host"
PRIVATE_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


def _make_key(tmp: str, text: str = PUBKEY_TEXT, name: str = "id.pub") -> Path:
    path = Path(tmp) / name
    path.write_text(text + "\n", encoding="utf-8")
    return path


class KeyDiscovery(unittest.TestCase):
    def test_resolve_expands_home_and_makes_absolute(self) -> None:
        resolved = resolve_key_path("~/.ssh/id_ed25519.pub")
        self.assertTrue(resolved.is_absolute())
        self.assertNotIn("~", str(resolved))

    def test_pick_default_prefers_an_existing_candidate(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            fake_home = Path(tmp)
            (fake_home / ".ssh").mkdir()
            rsa = fake_home / ".ssh" / "id_rsa.pub"
            rsa.write_text(PUBKEY_TEXT, encoding="utf-8")
            original = os.environ.get("HOME")
            original_user = os.environ.get("USERPROFILE")
            os.environ["HOME"] = str(fake_home)
            os.environ["USERPROFILE"] = str(fake_home)
            try:
                path, source = pick_default_pubkey()
            finally:
                for name, value in (("HOME", original), ("USERPROFILE", original_user)):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        self.assertEqual(source, "auto")
        self.assertTrue(path.endswith("id_rsa.pub"))

    def test_pick_default_falls_back_to_the_modern_default(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            original = os.environ.get("HOME")
            original_user = os.environ.get("USERPROFILE")
            os.environ["HOME"] = tmp
            os.environ["USERPROFILE"] = tmp
            try:
                path, source = pick_default_pubkey()
            finally:
                for name, value in (("HOME", original), ("USERPROFILE", original_user)):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        self.assertEqual(source, "fallback")
        self.assertEqual(path, DEFAULT_KEY)


class KeyValidation(unittest.TestCase):
    def test_recognises_every_supported_type(self) -> None:
        for prefix in ("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256",
                       "sk-ssh-ed25519@openssh.com"):
            with self.subTest(prefix=prefix):
                self.assertTrue(looks_like_pubkey_text(f"{prefix} AAAA test"))

    def test_rejects_a_private_key(self) -> None:
        self.assertFalse(looks_like_pubkey_text(PRIVATE_HEADER + "\nb3BlbnNzaC1rZXk=\n"))
        self.assertFalse(looks_like_pubkey_text(""))
        self.assertFalse(looks_like_pubkey_text("not a key at all"))

    def test_load_reads_type_blob_and_comment(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            key = load_pubkey(_make_key(tmp))
        self.assertEqual(key.key_type, "ssh-ed25519")
        self.assertEqual(key.blob, "AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY")
        self.assertEqual(key.comment, "test@host")
        self.assertEqual(key.source, "explicit")
        self.assertIn("ssh-ed25519 AAAA", key.line)
        self.assertEqual(key.short_name, "id.pub")
        self.assertEqual(key.identity, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY")

    def test_missing_file_is_key_file_missing(self) -> None:
        with self.assertRaises(PubkeyError) as ctx:
            load_pubkey("/nonexistent/id.pub")
        self.assertEqual(ctx.exception.failure_class, "key_file_missing")

    def test_private_key_is_refused_with_a_public_message(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "id_ed25519"
            path.write_text(PRIVATE_HEADER + "\nb3BlbnNzaC1rZXk=\n", encoding="utf-8")
            with self.assertRaises(PubkeyError) as ctx:
                load_pubkey(path)
        self.assertEqual(ctx.exception.failure_class, "key_file_missing")
        self.assertIn("private key", str(ctx.exception))

    def test_incomplete_key_line_is_refused(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "id.pub"
            path.write_text("ssh-ed25519\n", encoding="utf-8")
            with self.assertRaises(PubkeyError):
                load_pubkey(path)

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "id.pub"
            path.write_text(
                "# leading comment\n\n" + PUBKEY_TEXT + "\n# trailing\n",
                encoding="utf-8",
            )
            key = load_pubkey(path)
        self.assertEqual(key.comment, "test@host")


class InstallScript(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Pubkey(
            path=Path("/tmp/id.pub"),
            line=PUBKEY_TEXT,
            key_type="ssh-ed25519",
            blob="AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY",
            comment="test@host",
        )

    def test_quoting_survives_an_apostrophe(self) -> None:
        self.assertEqual(sh_quote("it's"), "'it'\\''s'")

    def test_script_is_idempotent_and_dependency_light(self) -> None:
        script = build_install_script(self.key)
        self.assertIn("grep -qF", script)
        self.assertIn(MARK_ALREADY, script)
        self.assertIn(MARK_INSTALLED, script)
        self.assertIn("chmod 700", script)
        self.assertIn("chmod 600", script)
        self.assertIn("umask 077", script)
        self.assertIn(MARK_HOME, script)
        # No ssh-copy-id, no python, no base64: it must run on Kylin 3.
        for forbidden in ("ssh-copy-id", "python", "base64"):
            self.assertNotIn(forbidden, script)

    def test_script_quotes_the_key_verbatim(self) -> None:
        self.assertIn(f"'{PUBKEY_TEXT}'", build_install_script(self.key))

    def test_script_reports_each_failure_mode_as_a_marker(self) -> None:
        script = build_install_script(self.key)
        for reason in ("no-home", "mkdir", "touch", "not-writable", "append"):
            self.assertIn(f"{MARK_ERROR_PREFIX}{reason}", script)


class InstallOutput(unittest.TestCase):
    def test_installed(self) -> None:
        out = parse_install_output(f"{MARK_INSTALLED}\n{MARK_HOME}/home/u\n")
        self.assertEqual(out.outcome, "installed")
        self.assertEqual(out.home, "/home/u")
        self.assertIsNone(out.error)
        self.assertTrue(out.ok)

    def test_already_installed(self) -> None:
        out = parse_install_output(MARK_ALREADY)
        self.assertEqual(out.outcome, "already_installed")
        self.assertTrue(out.ok)

    def test_error_wins_over_outcome(self) -> None:
        out = parse_install_output(f"{MARK_ERROR_PREFIX}not-writable\n")
        self.assertEqual(out.error, "not-writable")
        self.assertFalse(out.ok)

    def test_garbage_yields_an_empty_result(self) -> None:
        out = parse_install_output("All keys were skipped because they already exist")
        self.assertIsNone(out.outcome)
        self.assertFalse(out.ok)


class IdentityPath(unittest.TestCase):
    def test_home_relative_path_is_shortened(self) -> None:
        home = Path(os.path.expanduser("~")).resolve()
        rendered = normalize_identity_path(home / ".ssh" / "id_ed25519")
        self.assertEqual(rendered, "~/.ssh/id_ed25519")

    def test_path_outside_home_stays_absolute(self) -> None:
        rendered = normalize_identity_path(Path("/etc/keys/prod_ed25519"))
        self.assertNotIn("~", rendered)
        self.assertIn("prod_ed25519", rendered)


class DeployPubkeyUsesTheLibrary(unittest.TestCase):
    """The point of the exercise: one implementation, not two."""

    def setUp(self) -> None:
        self.module = load_script("deploy_pubkey")

    def test_deploy_imports_the_shared_helpers(self) -> None:
        source = (SCRIPTS / "deploy_pubkey.py").read_text(encoding="utf-8")
        code = source.split('"""', 2)[-1]  # skip the module docstring
        self.assertIn("from lib.pubkey import", code)
        # The *knowledge* must not be duplicated: no second prefix table, no
        # second install-script body, no second marker constants.
        self.assertNotIn("_PUBKEY_PREFIXES", code)
        self.assertNotIn("_MARKER_ADDED = \"ssh-core", code)
        self.assertNotIn("umask 077", code)
        # A thin file-reading adapter is fine and is what `deploy_pubkey` tests
        # call, but it must delegate the judgement.
        self.assertIn("looks_like_pubkey_text(", code)

    def test_library_and_script_share_one_install_command(self) -> None:
        from lib.pubkey import build_install_script, pubkey_from_text

        self.assertEqual(
            self.module._install_command(PUBKEY_TEXT),
            build_install_script(pubkey_from_text(PUBKEY_TEXT)),
        )

    def test_private_key_is_refused_before_any_network(self) -> None:
        import argparse

        with temp_dir(ignore_cleanup_errors=True) as tmp:
            private = Path(tmp) / "id_ed25519"
            private.write_text(PRIVATE_HEADER + "\nx\n", encoding="utf-8")
            args = argparse.Namespace(
                alias="h", key=str(private), password=False, ssh_config=None,
                password_env="SSH_PASSWORD", timeout=30, connect_timeout=8,
                accept_new_hostkey=False, json=True, redact=True, audit=None,
            )
            refusal = self.module._preflight(args)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["data"]["failure_class"], "key_file_missing")


if __name__ == "__main__":
    unittest.main()
