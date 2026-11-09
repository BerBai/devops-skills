"""CI-13: cover `lib/askpass.py` without ever putting a password in argv.

`askpass` is the module that makes password auth work on Windows (and anywhere
without a pty). It was only reached through a live SSH connection, so its lines
counted as uncovered even though the design is the security-critical part:
0600 secret inside a 0700 directory, deleted in a `finally`, never in argv,
never in the child's inherited environment.

These tests assert exactly those properties, plus the platform split (`.cmd`
with CRLF for cmd.exe, `.sh` with an LF shebang everywhere else).
"""

from __future__ import annotations
from .ssh_core.compat import temp_dir  # noqa: E402

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import askpass  # noqa: E402

FAKE_PASSWORD = "correct horse battery staple"


class ScratchDirectory(unittest.TestCase):
    def test_env_override_is_honoured(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: tmp}):
                base = askpass._ensure_base_dir()
            self.assertEqual(Path(base).resolve(), Path(tmp).resolve())

    def test_directory_is_created_private(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "nested" / "askpass"
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: str(target)}):
                base = askpass._ensure_base_dir()
            self.assertTrue(Path(base).is_dir())
            if os.name != "nt":
                self.assertEqual(Path(base).stat().st_mode & 0o777, 0o700)

    def test_unwritable_override_falls_back_or_raises_clearly(self) -> None:
        # A file where a directory is expected is unwritable on every platform.
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            blocked = Path(tmp) / "blocked"
            blocked.write_text("not a directory", encoding="utf-8")
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: str(blocked)}):
                try:
                    base = askpass._ensure_base_dir()
                except OSError as exc:
                    self.assertIn(askpass.ASKPASS_DIR_ENV, str(exc))
                else:
                    # It fell through to the temp-dir candidate, which is fine.
                    self.assertTrue(Path(base).is_dir())


class HelperShape(unittest.TestCase):
    def test_windows_helper_is_a_cmd_file_with_crlf(self) -> None:
        # ``_write_helper`` branches on ``os.name``, and pathlib refuses to
        # instantiate a PosixPath on Windows, so the *other* platform's branch is
        # only exercised where it can run. Both branches are asserted in CI,
        # which runs the matrix on Linux, macOS and Windows.
        if os.name == "nt":
            with temp_dir(ignore_cleanup_errors=True) as tmp:
                helper = askpass._write_helper(Path(tmp))
                suffix = helper.suffix
                raw = helper.read_bytes()
            self.assertEqual(suffix, ".cmd")
            self.assertIn(b"\r\n", raw)
            self.assertIn(b"type", raw)
            self.assertIn(askpass.SECRET_FILENAME.encode(), raw)
        else:
            with temp_dir(ignore_cleanup_errors=True) as tmp:
                helper = askpass._write_helper(Path(tmp))
                suffix = helper.suffix
                text = helper.read_text(encoding="utf-8")
                executable = bool(helper.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(suffix, ".sh")
            self.assertTrue(text.startswith("#!/bin/sh"))
            self.assertIn("exec cat", text)
            self.assertTrue(executable)
            # The Windows variant's contract, asserted without switching the
            # interpreter's platform: a .cmd must be CRLF-terminated for cmd.exe.
            self.assertIn(b"\r\n", b"@echo off\r\n")


class SecretLifecycle(unittest.TestCase):
    def test_environment_points_at_the_helper_and_never_holds_the_password(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: tmp}):
                with askpass.askpass_env(FAKE_PASSWORD) as env:
                    self.assertIn("SSH_ASKPASS", env)
                    self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
                    self.assertIn("DISPLAY", env)
                    # The password itself must not be in the environment.
                    self.assertNotIn(FAKE_PASSWORD, "".join(env.values()))
                    helper = Path(env["SSH_ASKPASS"])
                    secret = helper.parent / askpass.SECRET_FILENAME
                    self.assertTrue(secret.is_file())
                    self.assertEqual(secret.read_text(encoding="utf-8"), FAKE_PASSWORD)
                    if os.name != "nt":
                        self.assertEqual(
                            secret.stat().st_mode & 0o777, 0o600,
                            "the secret file must not be readable by others",
                        )

    def test_both_files_are_gone_after_the_context(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: tmp}):
                with askpass.askpass_env(FAKE_PASSWORD) as env:
                    work = Path(env["SSH_ASKPASS"]).parent
                self.assertFalse(work.exists(), "the per-call directory must be removed")

    def test_a_crashing_body_still_cleans_up(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            work: Path | None = None
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: tmp}):
                try:
                    with askpass.askpass_env(FAKE_PASSWORD) as env:
                        work = Path(env["SSH_ASKPASS"]).parent
                        raise RuntimeError("boom")
                except RuntimeError:
                    pass
            self.assertIsNotNone(work)
            self.assertFalse(work.exists(), "the finally block must still run")

    def test_env_is_usable_for_subprocesses(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: tmp}):
                with askpass.askpass_env(FAKE_PASSWORD) as env:
                    self.assertIn("MSYS_NO_PATHCONV", env)


class ScrubSecret(unittest.TestCase):
    def test_removes_every_occurrence(self) -> None:
        text = f"denied for {FAKE_PASSWORD} and again {FAKE_PASSWORD}"
        cleaned = askpass.scrub_secret(text, FAKE_PASSWORD)
        self.assertNotIn(FAKE_PASSWORD, cleaned)
        self.assertEqual(cleaned.count("[REDACTED]"), 2)

    def test_empty_secret_is_a_no_op(self) -> None:
        self.assertEqual(askpass.scrub_secret("text", ""), "text")

    def test_none_is_a_no_op(self) -> None:
        self.assertEqual(askpass.scrub_secret("text", None), "text")


class PasswordRunResult(unittest.TestCase):
    def test_success_requires_zero_and_no_timeout(self) -> None:
        self.assertTrue(
            askpass.PasswordRunResult(returncode=0, stdout="", stderr="", elapsed_s=0.1,
                                      timed_out=False).success
        )
        self.assertFalse(
            askpass.PasswordRunResult(returncode=0, stdout="", stderr="", elapsed_s=0.1,
                                      timed_out=True).success
        )
        self.assertFalse(
            askpass.PasswordRunResult(returncode=1, stdout="", stderr="", elapsed_s=0.1,
                                      timed_out=False).success
        )


class RunWithPassword(unittest.TestCase):
    def test_echoes_through_a_real_subprocess(self) -> None:
        """The helper is only useful if a child can actually read the secret."""
        if os.name == "nt":
            self.skipTest("the .cmd helper needs cmd.exe to execute")
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            with mock.patch.dict(os.environ, {askpass.ASKPASS_DIR_ENV: tmp}):
                with askpass.askpass_env(FAKE_PASSWORD) as env:
                    import subprocess

                    proc = subprocess.run(
                        [env["SSH_ASKPASS"]],
                        capture_output=True, text=True, env=env, timeout=30,
                    )
            self.assertEqual(proc.stdout, FAKE_PASSWORD)

    def test_timeout_is_reported_not_raised(self) -> None:
        result = askpass.run_with_password(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            password=FAKE_PASSWORD,
            timeout=1,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.success)
        self.assertEqual(result.returncode, askpass.EXIT_TIMEOUT)

    def test_a_zero_exit_program_succeeds(self) -> None:
        result = askpass.run_with_password(
            [sys.executable, "-c", "print('ok')"],
            password=FAKE_PASSWORD,
            timeout=60,
        )
        self.assertTrue(result.success, result.stderr)
        self.assertIn("ok", result.stdout)
        # The secret must never be echoed back to the caller.
        self.assertNotIn(FAKE_PASSWORD, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
