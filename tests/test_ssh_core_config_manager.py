"""SSH-02 / SSH-03 / SSH-04 / SSH-05 / SSH-20 / SSH-21 / SSH-22: config manager."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.compat import temp_dir  # noqa: E402
from .ssh_core.helpers import SCRIPTS, load_script  # noqa: E402

cfgmgr = load_script("ssh_config_manager")

BASE_CONFIG = """# description: keeper
Host good
    HostName 10.0.0.1
    User deploy

Host victim
    HostName 10.0.0.2
    User root
"""


def _run_cli(args: list[str], config: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "ssh_config_manager.py"),
         "--config-path", str(config), *args, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
    )


class RenderRefusesValuelessDirectives(unittest.TestCase):
    """SSH-02: `--user ''` must delete the field, never emit `User `."""

    def test_blank_fields_are_omitted(self) -> None:
        doc = cfgmgr.parse_config(BASE_CONFIG, Path("config"))
        host = doc.find("victim")
        assert host is not None
        host.user = ""
        host.host = ""
        host.identity_file = ""
        host.proxy_jump = ""
        rendered = cfgmgr.render(doc)
        self.assertNotIn("User \n", rendered)
        for keyword in ("User", "HostName", "IdentityFile", "ProxyJump"):
            for line in rendered.splitlines():
                if line.strip().startswith(keyword):
                    self.assertGreater(
                        len(line.split()), 1, f"valueless directive: {line!r}"
                    )

    def test_valueless_directive_is_detected(self) -> None:
        problem = cfgmgr.valueless_directive("Host a\n    User \n")
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertEqual(problem[1], "User")

    def test_empty_update_keeps_other_hosts_connectable(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text(BASE_CONFIG, encoding="utf-8")
            result = _run_cli(["update", "--alias", "victim", "--user="], config)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = config.read_text(encoding="utf-8")
            self.assertIsNone(cfgmgr.valueless_directive(text))
            # The untouched host still resolves.
            proc = subprocess.run(
                ["ssh", "-F", str(config), "-G", "good"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("hostname 10.0.0.1", proc.stdout)


class BackupUniqueness(unittest.TestCase):
    """SSH-04: five writes in the same second produce five distinct backups."""

    def test_five_backups_are_distinct(self) -> None:
        from lib.configfile import atomic_write_with_backup

        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text(BASE_CONFIG, encoding="utf-8")
            backups = [
                atomic_write_with_backup(config, f"{BASE_CONFIG}\n# {index}\n")
                for index in range(5)
            ]
            self.assertEqual(len(set(backups)), 5)
            for backup in backups:
                self.assertTrue(backup.exists())


class ConcurrentWrites(unittest.TestCase):
    """SSH-03: 20 concurrent creates must all land."""

    def test_twenty_concurrent_creates(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text(BASE_CONFIG, encoding="utf-8")

            results: list[subprocess.CompletedProcess] = []
            lock = threading.Lock()

            def worker(index: int) -> None:
                proc = _run_cli(
                    ["create", "--alias", f"h{index}", "--host", f"10.9.0.{index}",
                     "--user", "u"],
                    config,
                )
                with lock:
                    results.append(proc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=180)

            failures = [r for r in results if r.returncode != 0]
            self.assertEqual(failures, [], [r.stderr for r in failures])
            text = config.read_text(encoding="utf-8")
            for index in range(20):
                self.assertIn(f"Host h{index}", text)
            self.assertIsNone(cfgmgr.valueless_directive(text))


class WildcardProtection(unittest.TestCase):
    """SSH-20: `Host *` is visible but not deletable by accident."""

    CONFIG = (
        "Host *\n    ServerAliveInterval 30\n\n"
        "Host web-*\n    User web\n\n"
        "Host real\n    HostName 10.1.1.1\n    User me\n"
    )

    def test_list_marks_wildcards(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text(self.CONFIG, encoding="utf-8")
            plain = _run_cli(["list"], config)
            self.assertNotIn('"*"', plain.stdout)
            marked = _run_cli(["list", "--include-wildcards"], config)
            self.assertIn('"wildcard": true', marked.stdout)

    def test_delete_refuses_without_flag(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text(self.CONFIG, encoding="utf-8")
            refused = _run_cli(["delete", "--alias", "*"], config)
            self.assertEqual(refused.returncode, 180, refused.stderr)
            self.assertIn("Host *", config.read_text(encoding="utf-8"))
            allowed = _run_cli(["delete", "--alias", "*", "--allow-wildcard"], config)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)


class CaseInsensitiveAliases(unittest.TestCase):
    """SSH-21: WIN-PROD-01 is the same host as win-prod-01."""

    def test_create_collides_case_insensitively(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text("", encoding="utf-8")
            self.assertEqual(
                _run_cli(["create", "--alias", "win-prod-01", "--host", "10.0.0.1",
                          "--user", "u"], config).returncode,
                0,
            )
            clash = _run_cli(["create", "--alias", "WIN-PROD-01", "--host",
                              "10.0.0.2", "--user", "u"], config)
            self.assertEqual(clash.returncode, 5, clash.stderr)
            found = _run_cli(["find", "WIN-PROD-01"], config)
            self.assertEqual(found.returncode, 0, found.stderr)


class CreateMakesMissingFile(unittest.TestCase):
    """SSH-22: a fresh machine works without hand-creating ~/.ssh/config."""

    def test_create_creates_the_file(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            self.assertFalse(config.exists())
            result = _run_cli(["create", "--alias", "fresh", "--host", "10.0.0.9",
                               "--user", "u"], config)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config.exists())


class RestoreAndDryRun(unittest.TestCase):
    """SSH-05: roll back to a backup, and preview without writing."""

    def test_restore_round_trip(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text(BASE_CONFIG, encoding="utf-8")
            listing = _run_cli(["restore", "--list-backups"], config)
            self.assertEqual(listing.returncode, 0, listing.stderr)
            update = _run_cli(["update", "--alias", "victim", "--user", "ops"], config)
            self.assertEqual(update.returncode, 0, update.stderr)
            self.assertIn("User ops", config.read_text(encoding="utf-8"))
            restored = _run_cli(["restore", "--alias", "victim"], config)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertIn("User root", config.read_text(encoding="utf-8"))

    def test_dry_run_does_not_write(self) -> None:
        with temp_dir() as tmp:
            config = Path(tmp) / "config"
            config.write_text(BASE_CONFIG, encoding="utf-8")
            before = config.read_text(encoding="utf-8")
            preview = _run_cli(
                ["update", "--alias", "good", "--port", "2222", "--dry-run", "--diff"],
                config,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("Port 2222", preview.stdout)
            self.assertEqual(config.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
