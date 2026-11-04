"""DV-15 / DV-16: real health checks with TCP probes, and inventory export."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import SCRIPTS, load_script  # noqa: E402
from .ssh_core.compat import temp_dir  # noqa: E402
from lib.probe import (  # noqa: E402
    ProbeSpec,
    parse_probe,
    probe_alias,
    resolve_endpoint,
    summarize,
    tcp_probe,
)

CONFIG_MANAGER = SCRIPTS / "ssh_config_manager.py"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _closed_port() -> int:
    """A port nothing listens on (bound, then released)."""
    port = _free_port()
    return port


class ProbeParsing(unittest.TestCase):
    """DV-15: `--probe tcp:22` / `--probe port:3306` / bare `22`."""

    def test_forms(self) -> None:
        self.assertEqual(parse_probe("tcp:22"), ProbeSpec("tcp", 22, "tcp:22"))
        self.assertEqual(parse_probe("port:3306").port, 3306)
        self.assertEqual(parse_probe("22").port, 22)
        self.assertEqual(parse_probe("TCP:2222").kind, "tcp")

    def test_rejects_nonsense(self) -> None:
        for bad in ("", "udp:22", "tcp:", "tcp:http", "0", "70000", "22:33"):
            with self.assertRaises(ValueError, msg=bad):
                parse_probe(bad)


class TcpProbeBehaviour(unittest.TestCase):
    """DV-15: the probe reports what a socket connect actually did."""

    def test_open_port_is_ok(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            record = tcp_probe("127.0.0.1", port, timeout=2.0)
            self.assertTrue(record["ok"], record)
            self.assertIsNone(record["error"])
            self.assertEqual(record["port"], port)
            self.assertIsInstance(record["latency_ms"], float)
        finally:
            server.close()

    def test_closed_port_is_not_ok(self) -> None:
        record = tcp_probe("127.0.0.1", _closed_port(), timeout=1.0)
        self.assertFalse(record["ok"])
        self.assertIn("error", record)
        self.assertIsNotNone(record["error"])

    @unittest.skipUnless(os.environ.get("SSH_CORE_NETWORK_TESTS") == "1", "external DNS test disabled by default")
    def test_unknown_host_does_not_raise(self) -> None:
        record = tcp_probe("no-such-host.invalid", 22, timeout=0.5)
        self.assertFalse(record["ok"])

    def test_summarize_folds_records(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        open_port = server.getsockname()[1]
        try:
            records = [
                tcp_probe("127.0.0.1", open_port, timeout=1.0),
                tcp_probe("127.0.0.1", _closed_port(), timeout=0.5),
            ]
        finally:
            server.close()
        folded = summarize(records)
        self.assertFalse(folded["ok"])
        self.assertEqual(folded["probes"], 2)
        self.assertEqual(folded["failed"], 1)

    def test_resolve_endpoint_reads_the_config(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            config = Path(tmp) / "config"
            config.write_text(
                "Host probed\n    HostName 127.0.0.1\n    Port 2022\n",
                encoding="utf-8",
            )
            host, port = resolve_endpoint("probed", str(config))
        self.assertEqual((host, port), ("127.0.0.1", 2022))

    def test_resolve_endpoint_falls_back_to_the_literal(self) -> None:
        host, port = resolve_endpoint("deploy@203.0.113.9:2222")
        self.assertEqual((host, port), ("203.0.113.9", 2222))
        host, port = resolve_endpoint("[2001:db8::1]:2200")
        self.assertEqual((host, port), ("2001:db8::1", 2200))

    def test_probe_alias_attaches_the_alias(self) -> None:
        record = probe_alias("127.0.0.1", parse_probe(f"tcp:{_closed_port()}"),
                             timeout=0.5)
        self.assertEqual(record["alias"], "127.0.0.1")
        self.assertIn("resolved_host", record)


class ClusterHealthCheckSurface(unittest.TestCase):
    """DV-15: the CLI exposes the new probes and validates them early."""

    def setUp(self) -> None:
        self.module = load_script("ssh_cluster")

    def test_flags_are_registered(self) -> None:
        text = self.module.build_parser().format_help()
        self.assertIn("--health-check-command", text)
        self.assertIn("--probe", text)
        self.assertIn("--probe-timeout", text)
        self.assertIn("--retry", text)

    def test_default_health_command_is_still_true(self) -> None:
        args = self.module.build_parser().parse_args(["uptime", "--hosts", "a"])
        self.assertEqual(args.health_check_command, "true")

    def test_bad_probe_is_rejected_before_any_ssh(self) -> None:
        rc = self.module.main(["true", "--hosts", "a", "--probe", "udp:53", "--json"])
        self.assertEqual(rc, 2)

    def test_health_check_marks_a_closed_port_unhealthy(self) -> None:
        """SSH 'succeeds' (stubbed) but the probed port is closed -> unhealthy."""
        closed = _closed_port()

        def fake_run_one(*_args, **_kwargs):
            return {
                "alias": "a", "success": True, "exit_code": 0, "stdout": "",
                "stderr": "", "elapsed_ms": 1, "failure_class": None, "skipped": False,
            }

        original = self.module._run_one
        self.module._run_one = fake_run_one  # type: ignore[assignment]
        try:
            alive, dead, _extra = self.module._health_check(
                Path("ssh_execute.py"), ["a"], 5, 1, probes=[parse_probe(f"tcp:{closed}")],
                probe_timeout=0.5,
            )
        finally:
            self.module._run_one = original  # type: ignore[assignment]
        self.assertEqual(alive, [])
        self.assertIn("a", dead)
        self.assertEqual(dead["a"]["failure_class"], "health_check_failed")
        self.assertTrue(dead["a"]["skipped"])
        self.assertFalse(dead["a"]["probe"]["ok"])

    def test_health_check_command_failure_marks_the_host_dead(self) -> None:
        def fake_run_one(*_args, **_kwargs):
            return {
                "alias": "a", "success": False, "exit_code": 3, "stdout": "",
                "stderr": "inactive", "elapsed_ms": 1,
                "failure_class": "remote_error", "skipped": False,
            }

        original = self.module._run_one
        self.module._run_one = fake_run_one  # type: ignore[assignment]
        try:
            alive, dead, _extra = self.module._health_check(
                Path("ssh_execute.py"), ["a"], 5, 1,
                command="systemctl is-active nginx",
            )
        finally:
            self.module._run_one = original  # type: ignore[assignment]
        self.assertEqual(alive, [])
        self.assertIn("a", dead)

    def test_per_host_retry_repeats_only_transient_failures(self) -> None:
        calls: list[int] = []
        results = [
            {"alias": "a", "success": False, "exit_code": 102,
             "stdout": "", "stderr": "", "elapsed_ms": 1,
             "failure_class": "network", "skipped": False},
            {"alias": "a", "success": True, "exit_code": 0,
             "stdout": "", "stderr": "", "elapsed_ms": 1,
             "failure_class": None, "skipped": False},
        ]

        def fake_attempt(*_args, **_kwargs):
            calls.append(1)
            return results[min(len(calls) - 1, len(results) - 1)]

        original = self.module._run_one_attempt
        self.module._run_one_attempt = fake_attempt  # type: ignore[assignment]
        try:
            from lib.retry import RetryPolicy

            env = self.module._run_one(
                Path("ssh_execute.py"), "a", "true", 5, 5,
                retry=RetryPolicy(attempts=3, delay=0.0, backoff=1.0),
            )
        finally:
            self.module._run_one_attempt = original  # type: ignore[assignment]
        self.assertTrue(env["success"])
        self.assertEqual(env["attempts"], 2)
        self.assertEqual(len(calls), 2)

    def test_per_host_retry_does_not_repeat_an_auth_failure(self) -> None:
        calls: list[int] = []

        def fake_attempt(*_args, **_kwargs):
            calls.append(1)
            return {
                "alias": "a", "success": False, "exit_code": 100,
                "stdout": "", "stderr": "denied", "elapsed_ms": 1,
                "failure_class": "auth", "skipped": False,
            }

        original = self.module._run_one_attempt
        self.module._run_one_attempt = fake_attempt  # type: ignore[assignment]
        try:
            from lib.retry import RetryPolicy

            env = self.module._run_one(
                Path("ssh_execute.py"), "a", "true", 5, 5,
                retry=RetryPolicy(attempts=5, delay=0.0, backoff=1.0),
            )
        finally:
            self.module._run_one_attempt = original  # type: ignore[assignment]
        self.assertFalse(env["success"])
        self.assertEqual(len(calls), 1)


class InventoryExport(unittest.TestCase):
    """DV-16: `list --format ansible|yaml|ssh-config|json` is consumable."""

    CONFIG = (
        "# description: web tier\n"
        "# environment: production\n"
        "# tags: web,nginx\n"
        "Host web-01\n"
        "    HostName 10.0.1.11\n"
        "    User deploy\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "\n"
        "# environment: staging\n"
        "# tags: db\n"
        "Host db-01\n"
        "    HostName 10.0.2.5\n"
        "    User admin\n"
        "    Port 2222\n"
        "\n"
        "Host *\n"
        "    ServerAliveInterval 30\n"
    )

    def setUp(self) -> None:
        self._tmp = temp_dir(ignore_cleanup_errors=True)
        self.config = Path(self._tmp.name) / "config"
        self.config.write_text(self.CONFIG, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CONFIG_MANAGER), "--config-path", str(self.config), *args],
            capture_output=True, text=True, timeout=120,
        )

    def test_ansible_inventory_is_valid_json_with_all_and_hostvars(self) -> None:
        proc = self._run("list", "--format", "ansible")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inventory = json.loads(proc.stdout)
        self.assertIn("all", inventory)
        self.assertIn("_meta", inventory)
        hosts = inventory["all"]["hosts"]
        self.assertEqual(set(hosts), {"web-01", "db-01"})
        self.assertEqual(hosts["web-01"]["ansible_host"], "10.0.1.11")
        self.assertEqual(hosts["web-01"]["ansible_user"], "deploy")
        self.assertEqual(hosts["db-01"]["ansible_port"], 2222)
        self.assertEqual(hosts["web-01"]["ssh_core_environment"], "production")
        self.assertEqual(hosts["web-01"]["ssh_core_tags"], ["web", "nginx"])

    def test_ansible_envelope_carries_the_inventory(self) -> None:
        proc = self._run("list", "--format", "ansible", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["data"]["format"], "ansible")
        self.assertIn("all", payload["data"]["inventory"])

    def test_yaml_export_is_parseable_when_pyyaml_is_available(self) -> None:
        proc = self._run("list", "--format", "yaml")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("all:", proc.stdout)
        self.assertIn("web-01:", proc.stdout)
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            self.skipTest("PyYAML is not installed; syntax is covered by inspection")
        parsed = yaml.safe_load(proc.stdout)
        self.assertEqual(parsed["all"]["hosts"]["web-01"]["ansible_host"], "10.0.1.11")

    def test_ssh_config_export_drops_metadata_comments_and_wildcards(self) -> None:
        proc = self._run("list", "--format", "ssh-config")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = proc.stdout
        self.assertIn("Host web-01", text)
        self.assertIn("HostName 10.0.1.11", text)
        self.assertNotIn("# environment:", text)
        self.assertNotIn("Host *", text)
        # The exported text must itself be a valid config.
        target = Path(self._tmp.name) / "exported"
        target.write_text(text, encoding="utf-8")
        check = subprocess.run(
            ["ssh", "-F", str(target), "-G", "web-01"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_json_format_matches_the_envelope(self) -> None:
        proc = self._run("list", "--format", "json", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["data"]["format"], "json")
        inner = json.loads(payload["stdout"])
        self.assertEqual(len(inner), len(payload["data"]["hosts"]))

    def test_table_remains_the_default(self) -> None:
        proc = self._run("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("alias", proc.stdout)
        self.assertIn("web-01", proc.stdout)

    def test_format_is_validated_by_argparse(self) -> None:
        proc = self._run("list", "--format", "xml", "--json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid", proc.stdout.lower())


class TransferMirrorSurface(unittest.TestCase):
    """DV-19: --preserve and --delete exist, and --delete needs --yes."""

    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            capture_output=True, text=True, timeout=60,
        )

    def test_upload_help_lists_the_new_flags(self) -> None:
        for script in ("ssh_upload.py", "ssh_download.py"):
            with self.subTest(script=script):
                proc = self._run(script, "--help")
                for flag in ("--preserve", "--delete", "--yes", "--retry"):
                    self.assertIn(flag, proc.stdout, f"{script} {flag}")

    def test_delete_without_yes_is_refused(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            source = Path(tmp) / "tree"
            source.mkdir()
            (source / "a.txt").write_text("x", encoding="utf-8")
            proc = self._run(
                "ssh_upload.py", "no-such-alias", str(source), "/tmp/dest",
                "--recursive", "--delete", "--json",
            )
            self.assertEqual(proc.returncode, 109)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["data"]["failure_class"], "confirmation_required")
            self.assertEqual(payload["data"]["requires"], "--yes")

    def test_delete_requires_recursive(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            source = Path(tmp) / "a.txt"
            source.write_text("x", encoding="utf-8")
            proc = self._run(
                "ssh_upload.py", "no-such-alias", str(source), "/tmp/dest",
                "--delete", "--yes", "--json",
            )
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
