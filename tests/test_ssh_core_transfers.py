"""SSH-10 / SSH-11 / SSH-14: deploy_pubkey and the transfer backends."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import load_script  # noqa: E402
from .ssh_core.compat import temp_dir, write_text  # noqa: E402

import lib.transfer as transfer  # noqa: E402

deploy = load_script("deploy_pubkey")


PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY test@host"


class DeployPubkeyInstall(unittest.TestCase):
    """SSH-10: install without ssh-copy-id, idempotently, with verification."""

    def test_install_command_is_idempotent(self) -> None:
        command = deploy._remote_install_commands(PUBKEY)
        # The install snippet now comes from lib.pubkey.build_install_script,
        # so the assertions describe behaviour rather than one spelling.
        self.assertIn("grep -qF", command)
        self.assertIn("SSHCORE:already_installed", command)
        self.assertIn("SSHCORE:installed", command)
        self.assertIn('chmod 700 "$SSH_DIR"', command)
        self.assertIn('chmod 600 "$AUTH"', command)
        self.assertIn("umask 077", command)
        self.assertNotIn("ssh-copy-id", command)

    def test_install_command_quotes_the_key(self) -> None:
        command = deploy._remote_install_commands(PUBKEY)
        self.assertIn(f"'{PUBKEY}'", command)

    def test_install_command_matches_the_shared_library(self) -> None:
        """One implementation: the library's, not a private copy."""
        from lib.pubkey import build_install_script, pubkey_from_text

        self.assertEqual(
            deploy._remote_install_commands(PUBKEY),
            build_install_script(pubkey_from_text(PUBKEY)),
        )

    def test_preflight_never_requires_ssh_copy_id(self) -> None:
        import argparse

        args = argparse.Namespace(
            alias="h", key=str(Path(__file__).resolve()), password=True,
            ssh_config=None, password_env="SSH_PASSWORD", timeout=30,
            connect_timeout=8, accept_new_hostkey=False, json=True,
        )
        with mock.patch.object(deploy.shutil, "which", return_value=None) if hasattr(
            deploy, "shutil"
        ) else mock.patch("shutil.which", return_value=None):
            refusal = deploy._preflight(args)
        # The file exists but is not a public key → key_file_missing, never
        # tool_missing (that was the Windows bug).
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["data"]["failure_class"], "key_file_missing")

    def test_pubkey_detection(self) -> None:
        import tempfile

        with temp_dir() as tmp:
            good = Path(tmp) / "id.pub"
            good.write_text(PUBKEY + "\n", encoding="utf-8")
            self.assertTrue(deploy._looks_like_openssh_pubkey(good))
            private = Path(tmp) / "id_ed25519"
            private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
            self.assertFalse(deploy._looks_like_openssh_pubkey(private))


class TransferFallback(unittest.TestCase):
    """SSH-11: scp exists independently of paramiko."""

    def test_scp_argv_is_modern_and_quoted(self) -> None:
        argv = transfer.scp_argv(
            "/local/file with space.txt", "host:/tmp/x", recursive=False,
            port=2222, identity="~/.ssh/id_ed25519",
        )
        self.assertEqual(argv[0], "scp")
        self.assertIn("-O", argv)
        self.assertIn("-P", argv)
        self.assertIn("2222", argv)
        self.assertEqual(argv[-2:], ["/local/file with space.txt", "host:/tmp/x"])

    def test_scp_argv_recursive(self) -> None:
        argv = transfer.scp_argv("/a", "h:/b", recursive=True)
        self.assertIn("-r", argv)

    def test_paramiko_detection_matches_importability(self) -> None:
        try:
            import paramiko  # noqa: F401
        except Exception:
            self.assertFalse(transfer.paramiko_available())
        else:
            self.assertTrue(transfer.paramiko_available())

    def test_sha256_of_a_known_file(self) -> None:
        import hashlib
        import tempfile

        with temp_dir() as tmp:
            path = Path(tmp) / "f.txt"
            # write_text (the 3.8-compatible shim) keeps the bytes identical on
            # Windows — no CRLF translation — which is the point of a hash check.
            write_text(path, "hello\n")
            self.assertEqual(
                transfer.sha256_file(path),
                hashlib.sha256(b"hello\n").hexdigest(),
            )

    def test_scp_stderr_is_classified(self) -> None:
        cases = {
            "scp: /root/x: Permission denied": "permission",
            "scp: /root/x: No such file or directory": "remote_path_missing",
            "Host key verification failed.": "host_key",
            "ssh: Could not resolve hostname nope": "network",
            "scp: /nope: Not a directory": "remote_path_invalid",
            "something else": "transfer_io",
        }
        for stderr, expected in cases.items():
            with self.subTest(stderr=stderr):
                self.assertEqual(transfer._classify(stderr), expected)


class ServerTransferModes(unittest.TestCase):
    """SSH-14: direct/hybrid/auto are implemented, not deferred."""

    def test_route_prefers_stream_when_paramiko_is_present(self) -> None:
        st = load_script("ssh_server_transfer")
        import argparse

        args = argparse.Namespace(
            mode="auto", use_rsync=False, src_alias="a", src_path="/a",
            dst_alias="b", dst_path="/b", recursive=False, bwlimit=None,
            ssh_config=None, accept_new_hostkey=False, timeout=30, json=True,
        )
        with mock.patch.object(st, "paramiko_available", return_value=True):
            mode, failure = st._route(args)
        self.assertEqual(mode, "stream")
        self.assertIsNone(failure)

    def test_route_falls_back_to_direct_without_paramiko(self) -> None:
        st = load_script("ssh_server_transfer")
        import argparse

        args = argparse.Namespace(
            mode="auto", use_rsync=False, src_alias="a", src_path="/a",
            dst_alias="b", dst_path="/b", recursive=False, bwlimit=None,
            ssh_config=None, accept_new_hostkey=False, timeout=30, json=True,
        )
        with mock.patch.object(st, "paramiko_available", return_value=False):
            mode, failure = st._route(args)
        self.assertEqual(mode, "direct")
        self.assertIsNone(failure)

    def test_explicit_stream_without_paramiko_fails_cleanly(self) -> None:
        st = load_script("ssh_server_transfer")
        import argparse

        args = argparse.Namespace(
            mode="stream", use_rsync=False, src_alias="a", src_path="/a",
            dst_alias="b", dst_path="/b", recursive=False, bwlimit=None,
            ssh_config=None, accept_new_hostkey=False, timeout=30, json=True,
        )
        with mock.patch.object(st, "paramiko_available", return_value=False):
            _, failure = st._route(args)
        self.assertIsNotNone(failure)
        self.assertEqual(failure["data"]["failure_class"], "dependency_missing")

    def test_rsync_runs_on_the_source_host(self) -> None:
        """FA-04: rsync cannot take a remote source *and* a remote destination."""
        with mock.patch.object(transfer, "have", return_value=True), mock.patch.object(
            transfer.subprocess, "run"
        ) as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            outcome = transfer.run_rsync("src", "/a", "dst", "/b", recursive=True)
        self.assertTrue(outcome.success)
        argv = run.call_args.args[0]
        # The outer command is ssh to the *source*; rsync runs there.
        self.assertEqual(argv[0], "ssh")
        self.assertIn("src", argv)
        remote_command = argv[-1]
        self.assertTrue(remote_command.startswith("rsync "))
        self.assertIn("'dst:/b'", remote_command)
        self.assertIn("'/a'", remote_command)
        # No bare `src:/a` operand can appear: rsync would reject two remotes.
        self.assertNotIn("src:/a", remote_command)


if __name__ == "__main__":
    unittest.main()
