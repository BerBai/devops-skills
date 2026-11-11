"""SSH-18 / SSH-19 / SSH-30: the contract shared by every script."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import exit_codes  # noqa: E402

from .ssh_core.helpers import SCRIPTS  # noqa: E402

SCRIPTS_WITH_ARGV_ERRORS = [
    "deploy_pubkey.py",
    "ssh_cluster.py",
    "ssh_config_manager.py",
    "ssh_daemon.py",
    "ssh_download.py",
    "ssh_execute.py",
    "ssh_server_transfer.py",
    "ssh_session.py",
    "ssh_tunnel.py",
    "ssh_upload.py",
]


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, timeout=120,
    )


class JsonEnvelopeOnArgumentErrors(unittest.TestCase):
    """SSH-18: 9/9 scripts used to exit 2 with an empty stdout."""

    def test_every_script_emits_json_on_bad_argv(self) -> None:
        for script in SCRIPTS_WITH_ARGV_ERRORS:
            with self.subTest(script=script):
                proc = _run(script, "--json")
                self.assertEqual(proc.returncode, 2, proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["exit_code"], 2)
                self.assertEqual(payload["data"]["failure_class"], "invalid_arguments")
                self.assertIn("usage", payload["data"])


class VersionFlag(unittest.TestCase):
    """SSH-30 / DV-11: the version is queryable and never hardcoded in a test."""

    def test_every_script_answers_version(self) -> None:
        from lib.version import __version__

        for script in SCRIPTS_WITH_ARGV_ERRORS:
            with self.subTest(script=script):
                proc = _run(script, "--version")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"ssh-core {__version__}", proc.stdout)

    def test_version_is_machine_readable(self) -> None:
        from lib.version import __version__

        proc = _run("ssh_execute.py", "--version", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["data"]["version"], __version__)


class ExitCodeTable(unittest.TestCase):
    """SSH-19: one failure_class → one exit code."""

    def test_codes_are_unique(self) -> None:
        seen: dict[int, str] = {}
        for name, code in exit_codes.EXIT_CODES.items():
            if name in exit_codes.ALIASES:
                self.assertEqual(
                    exit_codes.EXIT_CODES[exit_codes.ALIASES[name]],
                    code,
                    f"{name} claims to alias {exit_codes.ALIASES[name]} but the "
                    "codes differ",
                )
                continue
            if code in seen:
                self.fail(
                    f"{name} and {seen[code]} share exit code {code}; the table "
                    "must be one class → one code (or declare an alias)"
                )
            seen[code] = name

    def test_known_mappings(self) -> None:
        self.assertEqual(exit_codes.exit_code_for(None), 0)
        self.assertEqual(exit_codes.exit_code_for("auth"), 100)
        self.assertEqual(exit_codes.exit_code_for("host_key"), 101)
        self.assertEqual(exit_codes.exit_code_for("network"), 102)
        self.assertEqual(exit_codes.exit_code_for("timeout"), 124)
        self.assertEqual(exit_codes.exit_code_for("dependency_missing"), 3)
        self.assertEqual(exit_codes.exit_code_for("password_required"), 105)

    def test_unknown_class_falls_back_to_internal(self) -> None:
        self.assertEqual(exit_codes.exit_code_for("not_a_class"), 1)

    def test_transfer_verify_failed_is_registered(self) -> None:
        # FA-16: emitted as a keyword by lib/sftp_transport; it used to fall
        # through to 1 while TransferResult said 9.
        self.assertEqual(
            exit_codes.exit_code_for("transfer_verify_failed"),
            exit_codes.EXIT_VERIFY_FAILED,
        )

    def test_mode_deferred_is_registered(self) -> None:
        # FA-05: the class used when --delete/--preserve cannot be honoured.
        self.assertEqual(
            exit_codes.exit_code_for("mode_deferred"),
            exit_codes.EXIT_MODE_UNSUPPORTED,
        )

    def test_alias_not_found_maps_to_not_found(self) -> None:
        # FA-22: an unknown alias must not be reported as a network failure.
        self.assertEqual(
            exit_codes.exit_code_for("alias_not_found"), exit_codes.EXIT_NOT_FOUND
        )


@unittest.skip("standalone packaging files are intentionally not part of the plugin")
class PluginMetadata(unittest.TestCase):
    """SSH-29 / SSH-30: packaging files exist and agree with the code."""

    def test_version_strings_agree(self) -> None:
        root = SCRIPTS.parent.parent.parent
        payload = json.loads((root / ".claude-plugin" / "plugin.json").read_text("utf-8"))
        from lib.version import __version__

        self.assertEqual(payload["version"], __version__)

    def test_packaging_files_exist(self) -> None:
        root = SCRIPTS.parent.parent.parent
        for name in ("README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml", ".gitignore"):
            with self.subTest(file=name):
                self.assertTrue((root / name).is_file(), f"{name} is missing")


if __name__ == "__main__":
    unittest.main()
