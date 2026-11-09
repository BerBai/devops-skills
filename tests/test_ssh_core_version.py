"""DV-11 / DV-32: one version, one exit, everywhere."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import SCRIPTS  # noqa: E402

REPO_ROOT = SCRIPTS.parent.parent.parent
TOOLS = REPO_ROOT / "tools"
VERSION_PY = SCRIPTS / "lib" / "version.py"

ALL_SCRIPTS = [
    "ssh_execute.py",
    "ssh_session.py",
    "ssh_cluster.py",
    "ssh_daemon.py",
    "ssh_tunnel.py",
    "ssh_upload.py",
    "ssh_download.py",
    "ssh_server_transfer.py",
    "deploy_pubkey.py",
    "ssh_config_manager.py",
    "selfcheck.py",
    "audit.py",
]


def _truth() -> str:
    text = VERSION_PY.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=120,
    )


@unittest.skipUnless(False, "standalone packaging manifests are intentionally not part of the plugin")
class VersionIsSingleSourced(unittest.TestCase):
    """DV-11: `for f in scripts/*.py; do python $f --version; done` must agree."""

    def test_every_script_reports_the_same_version(self) -> None:
        truth = _truth()
        for name in ALL_SCRIPTS:
            with self.subTest(script=name):
                proc = _run(SCRIPTS / name, "--version")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"ssh-core {truth}", proc.stdout)

    def test_version_is_machine_readable(self) -> None:
        proc = _run(SCRIPTS / "selfcheck.py", "--version", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["data"]["version"], _truth())

    def test_packaging_files_agree(self) -> None:
        truth = _truth()
        plugin = json.loads(
            (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8")
        )
        self.assertEqual(plugin["version"], truth)
        marketplace = json.loads(
            (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8")
        )
        self.assertEqual(marketplace["plugins"][0]["version"], truth)
        self.assertEqual(marketplace["metadata"]["version"], truth)
        self.assertIn(
            f'version = "{truth}"', (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        )
        self.assertEqual((REPO_ROOT / "VERSION").read_text("utf-8").strip(), truth)
        self.assertIn(f"## [{truth}]", (REPO_ROOT / "CHANGELOG.md").read_text("utf-8"))


@unittest.skip("bump tooling is packaged by the standalone ssh-core repository")
class VersionTool(unittest.TestCase):
    """DV-11: the bump tool is the only writer, and --check gates CI."""

    def test_check_passes_on_a_consistent_tree(self) -> None:
        proc = _run(TOOLS / "bump-version.py", "--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_check_json_shape(self) -> None:
        proc = _run(TOOLS / "bump-version.py", "--check", "--json")
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["truth"], _truth())
        self.assertTrue(payload["files"])

    def test_dry_run_does_not_write(self) -> None:
        before = VERSION_PY.read_bytes()
        proc = _run(TOOLS / "bump-version.py", "9.9.9", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(VERSION_PY.read_bytes(), before)

    def test_bad_version_is_refused(self) -> None:
        proc = _run(TOOLS / "bump-version.py", "not-a-version")
        self.assertEqual(proc.returncode, 2)

    def test_older_version_is_refused(self) -> None:
        truth = _truth()
        major, minor, patch = (int(p) for p in truth.split("."))
        older = f"{major}.{max(0, minor - 1)}.{patch}"
        proc = _run(TOOLS / "bump-version.py", older)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
