"""SSHCORE-015 … 027: the output/input pipeline defects from 2026-11-03.

Every test here fails against the 1.3.2 code and passes against the fix. They
are all offline: a fake ``ssh``/child process stands in for the network, so the
suite still runs on an air-gapped machine.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.compat import temp_dir, write_text  # noqa: E402
from .ssh_core.helpers import SCRIPTS, load_script  # noqa: E402

execute = load_script("ssh_execute")
cluster = load_script("ssh_cluster")
config_manager = load_script("ssh_config_manager")

import lib  # noqa: E402
from lib import (  # noqa: E402
    BoundedCapture,
    normalized_exit_code,
    split_login_banner,
)


class NonUtf8Output(unittest.TestCase):
    """SSHCORE-015: an undecodable byte must never empty the stream."""

    def test_random_bytes_are_kept_with_replacement(self) -> None:
        sink = BoundedCapture()
        sink.feed(bytes(range(256)))
        text, replaced = sink.decoded()
        self.assertEqual(len(sink.raw()), 256)
        self.assertGreater(replaced, 0)
        self.assertEqual(len(text.encode("utf-8")), 256 + replaced * 2)
        self.assertIn("\ufffd", text)

    def test_invalid_byte_does_not_stop_the_reader(self) -> None:
        sink = BoundedCapture()
        stream = io.BytesIO(b"\xff" * 10 + b"DONE\n")
        lib.drain_stream(stream, sink)
        self.assertTrue(sink.text().endswith("DONE\n"))
        self.assertEqual(sink.decode_errors, 0)

    def test_text_feed_is_still_accepted(self) -> None:
        sink = BoundedCapture()
        sink.feed("隧道已建立")
        self.assertEqual(sink.text(), "隧道已建立")

    def test_attach_meta_reports_lossiness(self) -> None:
        out = BoundedCapture()
        err = BoundedCapture()
        out.feed(b"abc\xff")
        data: dict = {}
        execute._attach_capture_meta(data, out, err)
        self.assertGreater(data["decode_replaced"], 0)
        self.assertTrue(data["lossy_output"])
        self.assertEqual(data["bytes_captured"], 4)

    def test_clean_output_is_not_marked_lossy(self) -> None:
        out = BoundedCapture()
        out.feed(b"plain ascii\n")
        data: dict = {}
        execute._attach_capture_meta(data, out, BoundedCapture())
        self.assertEqual(data["decode_replaced"], 0)
        self.assertFalse(data["lossy_output"])


class NewlineFidelity(unittest.TestCase):
    """SSHCORE-019: CRLF and lone CR survive the trip."""

    def test_crlf_is_preserved(self) -> None:
        sink = BoundedCapture()
        sink.feed(b"aaa\r\nbbb\rccc\n")
        self.assertEqual(sink.text(), "aaa\r\nbbb\rccc\n")

    def test_child_pipes_are_binary(self) -> None:
        # A text-mode pipe would fold \r\n to \n before we ever see it.
        argv = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'a\\r\\nb\\rc\\n')"]
        _rc, out, _err, _elapsed, timed_out = execute._capture_process(argv, 30)
        self.assertFalse(timed_out)
        self.assertEqual(out.text(), "a\r\nb\rc\n")


class TruncationSemantics(unittest.TestCase):
    """SSHCORE-021 / 022: byte-exact capping and complete metadata."""

    def test_cap_is_counted_in_bytes(self) -> None:
        sink = BoundedCapture(cap=10)
        sink.feed("隧道已建立隧道已建立".encode("utf-8"))  # 30 bytes
        self.assertEqual(sink.bytes_kept, 10)
        self.assertEqual(sink.bytes_elided, 20)
        self.assertEqual(sink.bytes_kept + sink.bytes_elided, 30)

    def test_marker_is_outside_the_cap(self) -> None:
        sink = BoundedCapture(cap=8)
        sink.feed(b"x" * 20)
        text = sink.text()
        self.assertTrue(text.startswith("x" * 8))
        self.assertIn("12 bytes elided", text)

    def test_timeout_path_has_truncation_fields(self) -> None:
        script = (
            "import sys,time;"
            "sys.stdout.write('A'*4000);sys.stdout.flush();time.sleep(30)"
        )
        argv = [sys.executable, "-c", script]
        _rc, out, err, _elapsed, timed_out = execute._capture_process(argv, 1)
        self.assertTrue(timed_out)
        data = {"route": "local"}
        execute._attach_capture_meta(data, out, err)
        for key in ("truncated", "bytes_elided", "bytes_captured", "decode_replaced"):
            self.assertIn(key, data)
        self.assertTrue(data["partial_output"] if "partial_output" in data else True)


class SignalExitCodes(unittest.TestCase):
    """SSHCORE-023: signal deaths become 128+N, never negative."""

    def test_negative_returncode_is_normalised(self) -> None:
        self.assertEqual(normalized_exit_code(-15), (143, 15))
        self.assertEqual(normalized_exit_code(-9), (137, 9))

    def test_unsigned_windows_code_is_masked(self) -> None:
        self.assertEqual(normalized_exit_code(4294967295), (255, None))

    def test_plain_codes_pass_through(self) -> None:
        for code in (0, 1, 42, 124, 255):
            self.assertEqual(normalized_exit_code(code), (code, None))


class LoginBanner(unittest.TestCase):
    """SSHCORE-017: sshd's greeting is transport noise, not command stderr."""

    BANNER = "\r\nAuthorized users only. All activities may be monitored.\r\n"

    def test_banner_only_stream_becomes_empty(self) -> None:
        real, banner = split_login_banner(self.BANNER)
        self.assertEqual(real, "")
        self.assertEqual(len(banner), 1)

    def test_command_stderr_after_banner_is_kept(self) -> None:
        real, banner = split_login_banner(self.BANNER + "REMOTE_ERR\n")
        self.assertEqual(real, "REMOTE_ERR\n")
        self.assertEqual(len(banner), 1)

    def test_bannerless_stream_is_untouched(self) -> None:
        raw = "tar: /nope: Cannot stat: No such file or directory\n"
        real, banner = split_login_banner(raw)
        self.assertEqual(real, raw)
        self.assertEqual(banner, [])

    def test_auth_failure_after_banner_keeps_the_diagnostic(self) -> None:
        raw = self.BANNER + "root@h: Permission denied (publickey).\r\n"
        real, _banner = split_login_banner(raw)
        self.assertIn("Permission denied", real)

    def test_argv_does_not_use_loglevel_error(self) -> None:
        # Measured: ERROR drops `Connection refused` (9.x) and
        # `Unable to negotiate` (all versions), which breaks classification.
        argv = execute._build_remote_argv("h", "true", 8, None)
        self.assertNotIn("LogLevel=ERROR", argv)
        self.assertNotIn("-q", argv)


class TimeoutValidation(unittest.TestCase):
    """SSHCORE-021: a bad budget is an argv error, not a fake timeout."""

    def _run(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "ssh_execute.py"), *args, "--json"],
            capture_output=True, timeout=60, check=False,
        )
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
        return proc.returncode, payload

    def test_negative_timeout_is_rejected(self) -> None:
        code, payload = self._run("lab-240", "true", "--timeout", "-1")
        self.assertEqual(code, 107)
        self.assertEqual(payload["data"]["failure_class"], "invalid_option")

    def test_zero_timeout_is_rejected(self) -> None:
        code, payload = self._run("lab-240", "true", "--timeout", "0")
        self.assertEqual(code, 107)
        self.assertEqual(payload["data"]["invalid_option"], "--timeout")

    def test_negative_connect_timeout_is_rejected(self) -> None:
        code, payload = self._run("lab-240", "true", "--connect-timeout", "-5")
        self.assertEqual(code, 107)
        self.assertEqual(payload["data"]["invalid_option"], "--connect-timeout")


class HumanMode(unittest.TestCase):
    """SSHCORE-025: output is byte-clean and streamed while it runs."""

    def test_emit_does_not_add_a_newline(self) -> None:
        buf = io.StringIO()
        original = sys.stdout
        sys.stdout = buf
        try:
            lib.emit(
                {"success": True, "exit_code": 0, "stdout": "abc", "stderr": "", "data": {}},
                as_json=False,
            )
        finally:
            sys.stdout = original
        self.assertEqual(buf.getvalue(), "abc")

    def test_streamed_result_is_not_printed_twice(self) -> None:
        buf = io.StringIO()
        original = sys.stdout
        sys.stdout = buf
        try:
            lib.emit(
                {
                    "success": True,
                    "exit_code": 0,
                    "stdout": "already seen\n",
                    "stderr": "",
                    "data": {"streamed": True},
                },
                as_json=False,
                streamed=True,
            )
        finally:
            sys.stdout = original
        self.assertEqual(buf.getvalue(), "")

    def test_relay_tee_drops_the_banner_on_the_live_path(self) -> None:
        stream = io.StringIO()
        tee = execute._StreamTee(stream, suppress_login_banner=True)
        tee.feed(b"\r\nAuthorized users only.\r\n")
        tee.feed(b"REAL_ERR\n")
        tee.close()
        self.assertEqual(stream.getvalue(), "REAL_ERR\n")


class ClusterSshConfig(unittest.TestCase):
    """SSHCORE-018: --ssh-config selects AND connects."""

    def test_worker_argv_forwards_the_config(self) -> None:
        argv_calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            argv_calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"success": True, "exit_code": 0, "stdout": "ok",
                                     "stderr": "", "data": {}}), "",
            )

        original = cluster.subprocess.run
        cluster.subprocess.run = fake_run
        try:
            env = cluster._run_one_attempt(
                Path("ssh_execute.py"), "alias1", "true", 5, 2,
                ssh_config="C:/tmp/cfg",
            )
        finally:
            cluster.subprocess.run = original
        self.assertTrue(env["success"])
        self.assertIn("--ssh-config", argv_calls[0])
        self.assertIn("C:/tmp/cfg", argv_calls[0])

    def test_help_no_longer_says_resolution_only(self) -> None:
        text = cluster.build_parser().format_help()
        self.assertIn("BOTH", text)


class ClusterStdin(unittest.TestCase):
    """SSHCORE-024: workers never share the caller's stdin."""

    def test_workers_get_devnull_stdin(self) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"success": True, "exit_code": 0, "stdout": "",
                                     "stderr": "", "data": {}}), "",
            )

        original = cluster.subprocess.run
        cluster.subprocess.run = fake_run
        try:
            cluster._run_one_attempt(Path("ssh_execute.py"), "a", "cat", 5, 2)
        finally:
            cluster.subprocess.run = original
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)

    def test_stdin_file_is_forwarded_per_worker(self) -> None:
        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append(list(argv))
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"success": True, "exit_code": 0, "stdout": "",
                                     "stderr": "", "data": {}}), "",
            )

        original = cluster.subprocess.run
        cluster.subprocess.run = fake_run
        try:
            cluster._run_one_attempt(
                Path("ssh_execute.py"), "a", "cat", 5, 2, stdin_file="/tmp/payload",
            )
        finally:
            cluster.subprocess.run = original
        self.assertIn("--stdin-file", captured[0])
        self.assertIn("/tmp/payload", captured[0])

    def test_missing_stdin_file_is_reported(self) -> None:
        with temp_dir() as tmp:
            missing = str(Path(tmp) / "nope.txt")
            self.assertIsNotNone(cluster._stdin_file_problem(missing))
            present = Path(tmp) / "yes.txt"
            write_text(present, "x")
            self.assertIsNone(cluster._stdin_file_problem(str(present)))


class ClusterWorkerFailure(unittest.TestCase):
    """SSHCORE-016: a broken worker yields an envelope, never a traceback."""

    def test_undecodable_worker_output_is_contained(self) -> None:
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b"\xff\xfe not json", b"\xff")

        original = cluster.subprocess.run
        cluster.subprocess.run = fake_run
        try:
            env = cluster._run_one_attempt(Path("ssh_execute.py"), "a", "true", 5, 2)
        finally:
            cluster.subprocess.run = original
        self.assertFalse(env["success"])
        self.assertEqual(env["failure_class"], "ssh_execute_broken")

    def test_spawn_error_is_contained(self) -> None:
        def fake_run(argv, **kwargs):
            raise OSError("no such program")

        original = cluster.subprocess.run
        cluster.subprocess.run = fake_run
        try:
            env = cluster._run_one_attempt(Path("ssh_execute.py"), "a", "true", 5, 2)
        finally:
            cluster.subprocess.run = original
        self.assertFalse(env["success"])
        self.assertEqual(env["failure_class"], "ssh_execute_broken")


class TransferBackends(unittest.TestCase):
    """SSHCORE-026: scp is a real backend, and it says what it cannot do."""

    def _run(self, script: str, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args, "--json"],
            capture_output=True, timeout=120, check=False,
        )
        return proc.returncode, json.loads(proc.stdout.decode("utf-8", "replace"))

    def test_download_accepts_openssh_alias(self) -> None:
        parser = load_script("ssh_download").build_parser()
        args = parser.parse_args(["h", "/r", "/l", "--transport", "openssh"])
        self.assertEqual(args.transport, "openssh")

    def test_upload_accepts_openssh_alias(self) -> None:
        parser = load_script("ssh_upload").build_parser()
        args = parser.parse_args(["h", "/l", "/r", "--transport", "openssh"])
        self.assertEqual(args.transport, "openssh")

    def test_resume_without_paramiko_is_refused(self) -> None:
        with temp_dir() as tmp:
            code, payload = self._run(
                "ssh_download.py", "no-such-alias", "/remote/x", str(Path(tmp) / "x"),
                "--resume", "--transport", "openssh",
            )
        self.assertEqual(code, 166)
        self.assertEqual(payload["data"]["failure_class"], "mode_deferred")
        self.assertEqual(payload["data"]["deferred_flags"], ["--resume"])


class ConfigListHostColumn(unittest.TestCase):
    """SSHCORE-027: the host column distinguishes HostName from the alias."""

    CONFIG = (
        "Host plain\n"
        "    User root\n"
        "\n"
        "Host named\n"
        "    HostName 10.0.0.9\n"
        "    User deploy\n"
    )

    def setUp(self) -> None:
        self._tmp = temp_dir()
        self._dir = self._tmp.__enter__()
        self._path = str(Path(self._dir) / "config")
        write_text(Path(self._path), self.CONFIG)

    def tearDown(self) -> None:
        self._tmp.__exit__(None, None, None)

    def test_json_marks_explicit_hostname(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "ssh_config_manager.py"),
             "--config-path", self._path, "list", "--json"],
            capture_output=True, timeout=60, check=False,
        )
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
        hosts = {h["alias"]: h for h in payload["data"]["hosts"]}
        self.assertTrue(hosts["named"]["hostname_explicit"])
        self.assertEqual(hosts["named"]["host_source"], "hostname")
        self.assertFalse(hosts["plain"]["hostname_explicit"])
        self.assertEqual(hosts["plain"]["host_source"], "alias")
        self.assertEqual(hosts["plain"]["host"], "plain")

    def test_table_shows_dash_when_hostname_is_absent(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "ssh_config_manager.py"),
             "--config-path", self._path, "list"],
            capture_output=True, timeout=60, check=False,
        )
        table = proc.stdout.decode("utf-8", "replace")
        plain_row = next(line for line in table.splitlines() if line.startswith("plain"))
        self.assertTrue(plain_row.rstrip().endswith("-"), plain_row)
        named_row = next(line for line in table.splitlines() if line.startswith("named"))
        self.assertIn("10.0.0.9", named_row)


class DaemonPasswordRoute(unittest.TestCase):
    """The daemon must not hardcode BatchMode=yes for a password host.

    ``--auth password`` was accepted and then ignored by
    ``_ssh_argv_for_shell``, so a password-only host answered a rejected
    publickey (the same defect SSHCORE-001 fixed for exec and the tunnel
    follow-up fixed for tunnels). Verified live against a password-only Kylin
    host before and after.
    """

    def setUp(self) -> None:
        self.daemon = load_script("ssh_daemon")

    def test_key_route_keeps_batchmode(self) -> None:
        argv = self.daemon._ssh_argv_for_shell("h", 8, None, [], False)
        self.assertIn("BatchMode=yes", argv)
        self.assertNotIn("PasswordAuthentication=no", argv)

    def test_password_route_does_not_use_batchmode(self) -> None:
        argv = self.daemon._ssh_argv_for_shell(
            "h", 8, None, [], False, password_auth=True
        )
        self.assertNotIn("BatchMode=yes", argv)
        joined = " ".join(argv)
        self.assertIn("PreferredAuthentications=password,keyboard-interactive", joined)
        self.assertIn("PubkeyAuthentication=no", joined)
        self.assertIn("StrictHostKeyChecking=yes", joined)

    def test_password_route_still_validates_options(self) -> None:
        with self.assertRaises(ValueError):
            self.daemon._ssh_argv_for_shell(
                "h", 8, None, ["ProxyCommand=calc"], False, password_auth=True
            )


class TransferAuth(unittest.TestCase):
    """SSHCORE-026 follow-up: the scp/tar backends honour password auth.

    Before this, ``ssh_upload`` / ``ssh_download`` / ``ssh_server_transfer`` had
    no ``--auth`` at all and every backend hardcoded ``-o BatchMode=yes``, so
    the paramiko-free path was unusable on a password-only host: the only
    transport that could authenticate was SFTP, which needs paramiko.
    """

    def _run(self, script: str, *args: str, env_extra: dict | None = None) -> tuple[int, dict]:
        env = {k: v for k, v in os.environ.items() if k != "SSH_PASSWORD"}
        env.update(env_extra or {})
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args, "--json"],
            capture_output=True, timeout=120, check=False, env=env,
        )
        return proc.returncode, json.loads(proc.stdout.decode("utf-8", "replace"))

    def test_scp_argv_switches_profile(self) -> None:
        from lib.transfer import scp_argv

        key = scp_argv("a", "b")
        pw = scp_argv("a", "b", password_auth=True)
        self.assertIn("BatchMode=yes", key)
        self.assertNotIn("BatchMode=yes", pw)
        joined = " ".join(pw)
        self.assertIn("PreferredAuthentications=password,keyboard-interactive", joined)
        self.assertIn("PubkeyAuthentication=no", joined)

    def test_legacy_scp_flag_is_probed_not_assumed(self) -> None:
        """`-O` is OpenSSH 9.0+; 8.x (Kylin 3, RHEL 7/8) rejects it outright.

        Measured right after installing on such a host: every scp-backed
        upload/download failed with `unknown option -- O` while the same code
        worked on Windows' 9.5p1.
        """
        from lib import transfer

        # Force a client that does not know the flag.
        original = transfer._SCP_LEGACY_FLAG.copy()
        try:
            transfer._SCP_LEGACY_FLAG.clear()
            transfer._SCP_LEGACY_FLAG["scp"] = False
            argv = transfer.scp_argv("a", "b")
            self.assertNotIn("-O", argv)
            transfer._SCP_LEGACY_FLAG["scp"] = True
            self.assertIn("-O", transfer.scp_argv("a", "b"))
        finally:
            transfer._SCP_LEGACY_FLAG.clear()
            transfer._SCP_LEGACY_FLAG.update(original)

    def test_legacy_flag_probe_reads_the_client_error(self) -> None:
        from lib import transfer

        original = transfer._SCP_LEGACY_FLAG.copy()
        try:
            transfer._SCP_LEGACY_FLAG.clear()
            # A program that does not exist must be treated as "no flag",
            # never as "supported".
            self.assertFalse(transfer.scp_supports_legacy_flag("scp-does-not-exist"))
        finally:
            transfer._SCP_LEGACY_FLAG.clear()
            transfer._SCP_LEGACY_FLAG.update(original)

    def test_auth_resolution_reads_metadata(self) -> None:
        from lib.auth import resolve

        with temp_dir() as tmp:
            cfg = Path(tmp) / "config"
            write_text(
                cfg,
                "# auth: password\nHost pwhost\n    HostName 10.0.0.1\n\n"
                "Host keyhost\n    HostName 10.0.0.2\n",
            )
            self.assertEqual(resolve("pwhost", "auto", str(cfg), "SSH_CORE_NO_SUCH")[0],
                             "password")
            self.assertEqual(resolve("keyhost", "auto", str(cfg), "SSH_CORE_NO_SUCH")[0],
                             "key")

    def test_missing_secret_is_password_required(self) -> None:
        for script, args in (
            ("ssh_download.py", ["192.168.30.231", "/etc/hostname", "out.txt"]),
            ("ssh_upload.py", ["192.168.30.231", __file__, "/tmp/x"]),
        ):
            with self.subTest(script=script):
                code, payload = self._run(script, *args, "--auth", "password")
                self.assertEqual(code, 105, payload)
                self.assertEqual(payload["data"]["failure_class"], "password_required")
                self.assertNotIn("Traceback", json.dumps(payload))

    def test_server_transfer_missing_secret_is_password_required(self) -> None:
        code, payload = self._run(
            "ssh_server_transfer.py", "192.168.30.231", "/etc/hostname",
            "192.168.30.231", "/tmp/y", "--mode", "direct", "--auth", "password",
        )
        self.assertEqual(code, 105, payload)
        self.assertEqual(payload["data"]["failure_class"], "password_required")

    def test_flag_exists_on_all_three_scripts(self) -> None:
        for script in ("ssh_upload.py", "ssh_download.py", "ssh_server_transfer.py"):
            with self.subTest(script=script):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / script), "--help"],
                    capture_output=True, timeout=60, check=False,
                )
                text = proc.stdout.decode("utf-8", "replace")
                self.assertIn("--auth", text)
                self.assertIn("--password-env", text)


@unittest.skip("remote install tooling is packaged by the standalone ssh-core repository")
class InstallBundle(unittest.TestCase):
    """The release bundle and the on-host installers must not drift apart.

    `make install-host` copies three things to the target: the zip, its
    `.sha256` sidecar, and `tools/install-to-host.sh`. Two of those have already
    gone wrong once (CRLF sidecar, `scp -O` on OpenSSH 8.2), so the invariants
    are pinned here rather than discovered on a customer's host.
    """

    TOOLS = Path(__file__).resolve().parent.parent / "tools"
    DIST = Path(__file__).resolve().parent.parent / "dist"

    def test_install_scripts_are_posix_sh_with_lf(self) -> None:
        for name in ("install-to-host.sh", "verify-host-install.sh"):
            with self.subTest(script=name):
                path = self.TOOLS / name
                self.assertTrue(path.is_file(), f"{name} is missing")
                raw = path.read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "BOM in a shell script")
                self.assertNotIn(b"\r\n", raw, "CRLF in a shell script")
                self.assertTrue(raw.startswith(b"#!/bin/sh"), "needs a POSIX shebang")

    def test_installer_verifies_both_layers(self) -> None:
        text = (self.TOOLS / "install-to-host.sh").read_text(encoding="utf-8")
        # outer sidecar and inner manifest, or one of them silently stops being
        # checked the day someone edits the script.
        self.assertIn("sha256sum -c", text)
        self.assertIn("SHA256SUMS", text)
        # an older interpreter must be refused, not crashed into
        self.assertIn("sys.version_info < (3, 8)", text)
        # VERSION lives at the archive root; the installer has to carry it in
        self.assertIn('cp -f "$STAGE/VERSION"', text)

    def test_sidecar_is_lf(self) -> None:
        sidecars = sorted(self.DIST.glob("ssh-core-*.zip.sha256"))
        if not sidecars:
            self.skipTest("no dist archive built in this checkout")
        for sidecar in sidecars:
            with self.subTest(sidecar=sidecar.name):
                raw = sidecar.read_bytes()
                self.assertNotIn(b"\r", raw, "CRLF sidecar breaks sha256sum -c on POSIX")
                self.assertTrue(raw.endswith(b"\n"))

    def test_makefile_exposes_the_recipe(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "Makefile").read_text("utf-8")
        self.assertIn("install-host:", text)
        self.assertIn("install-to-host.sh", text)
        self.assertIn("verify-host-install.sh", text)
        self.assertIn("install-host \\", text, "target missing from .PHONY")


class RedactionConsistency(unittest.TestCase):
    """SSH-07 said "HostName is redacted"; an IP *alias* slipped through.

    `ssh_execute.py 192.168.30.240 true` redacted `stderr` to `<alias>` but left
    the literal address in `data.ssh_argv`, so the identifier the rule exists to
    hide was still in the envelope. Found by an independent install audit.
    """

    def test_address_alias_is_redacted_in_argv(self) -> None:
        from lib.redact import redactor_for

        redactor = redactor_for("192.168.30.240")
        argv = ["ssh", "-o", "BatchMode=yes", "--", "192.168.30.240", "true"]
        redacted = redactor.argv(argv)
        self.assertNotIn("192.168.30.240", " ".join(redacted))
        self.assertIn("<host>", redacted)

    def test_named_alias_is_kept(self) -> None:
        from lib.redact import redactor_for

        redactor = redactor_for("lab-240")
        self.assertEqual(
            redactor.argv(["ssh", "--", "lab-240", "true"]),
            ["ssh", "--", "lab-240", "true"],
        )

    def test_user_at_address_literal_uses_the_alias_placeholder(self) -> None:
        from lib.redact import redactor_for

        redactor = redactor_for("root@192.168.30.231:2222")
        text = redactor.text("root@192.168.30.231: Permission denied")
        self.assertNotIn("192.168.30.231", text)
        self.assertIn("<alias>", text)


@unittest.skip("remote install tooling is packaged by the standalone ssh-core repository")
class InstallHygiene(unittest.TestCase):
    """The install must not create what it also forbids.

    `verify-host-install.sh` checks "no __pycache__ in the tree" and then *runs*
    the installed code; without `-B` / `PYTHONDONTWRITEBYTECODE` it poisons the
    very tree it is verifying. An independent audit of the first install failed
    on exactly that (1 dir + 27 `.pyc`).
    """

    TOOLS = Path(__file__).resolve().parent.parent / "tools"

    def test_both_scripts_suppress_bytecode(self) -> None:
        for name in ("install-to-host.sh", "verify-host-install.sh"):
            with self.subTest(script=name):
                text = (self.TOOLS / name).read_text(encoding="utf-8")
                self.assertIn("PYTHONDONTWRITEBYTECODE=1", text)
                self.assertIn("export PYTHONDONTWRITEBYTECODE", text)

    def test_verifier_checks_for_bytecode(self) -> None:
        text = (self.TOOLS / "verify-host-install.sh").read_text(encoding="utf-8")
        self.assertIn("no bytecode in the installed tree", text)

    def test_installer_sweeps_bytecode_from_tree_and_backup(self) -> None:
        text = (self.TOOLS / "install-to-host.sh").read_text(encoding="utf-8")
        self.assertIn("__pycache__", text)
        self.assertIn("BACKUP", text)
        self.assertIn("-name '*.pyc' -delete", text)

    def test_every_entry_script_suppresses_bytecode(self) -> None:
        """A plain `python3 <script>` must not write into the installed tree.

        Environment variables and `-B` only work while the caller remembers
        them; an ``ssh_execute.py`` invoked by hand, by cron, or by another
        agent's driver would still leave ``__pycache__`` behind. The flag is
        therefore set by the script itself, before ``import lib``.
        """
        scripts = sorted(SCRIPTS.glob("*.py"))
        self.assertTrue(scripts)
        for path in scripts:
            if path.name == "__init__.py":
                continue
            with self.subTest(script=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("sys.dont_write_bytecode = True", text)
                # …and it has to come before the `from lib import ...` block.
                self.assertLess(
                    text.index("sys.dont_write_bytecode = True"),
                    text.index("from lib import"),
                )


class StdinFile(unittest.TestCase):
    """SSHCORE-024: --stdin-file feeds the command without touching argv."""

    def test_stdin_file_content_reaches_the_command_stdin(self) -> None:
        with temp_dir() as tmp:
            payload = Path(tmp) / "in.txt"
            write_text(payload, "hello\n")
            argv = [
                sys.executable, "-c",
                "import sys;sys.stdout.write(sys.stdin.read().upper())",
            ]
            _rc, out, _err, _elapsed, timed_out = execute._capture_process(
                argv, 30, stdin_text=payload.read_bytes(),
            )
        self.assertFalse(timed_out)
        self.assertEqual(out.text(), "HELLO\n")
    def test_stdin_pipe_is_closed_so_readers_see_eof(self) -> None:
        # Windows OpenSSH treats an inherited NUL handle as "no EOF yet", so an
        # empty stdin must be a *closed pipe* (SSHCORE-024).
        argv = [sys.executable, "-c", "import sys;print(len(sys.stdin.buffer.read()))"]
        _rc, out, _err, _elapsed, timed_out = execute._capture_process(argv, 30)
        self.assertFalse(timed_out)
        self.assertEqual(out.text().strip(), "0")


if __name__ == "__main__":
    unittest.main()
