"""Integration tests for ssh-core/scripts/ssh_config_manager.py v1.0.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_ssh_config_manager_integration.py -v -m live_ssh

These exercise the **read-only** paths against the real ``~/.ssh/config``.
No mutation, ever -- both for safety (the user's real config) and because
the prd's acceptance criteria explicitly carve out read-only integration
coverage. Cross-checks against ``ssh -G <alias>`` ensure our parsed
values match what OpenSSH itself derives from the same file.

POC_HOST defaults to ``pai``; override via env (``POC_HOST=other-alias``)
if you need to point at a different alias in your real config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
SCM_PATH = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_config_manager.py"
)
REAL_CONFIG = Path("~/.ssh/config").expanduser()
POC_HOST = os.environ.get("POC_HOST", "pai")

EXIT_OK = 0


@pytest.fixture(scope="module", autouse=True)
def _require_real_config() -> None:
    """Skip the whole module unless the real ~/.ssh/config exists and is
    readable. The integration tests are NOT meant to gate CI -- they are
    a sanity check that ssh_config_manager talks to real OpenSSH-format
    files correctly."""
    if not REAL_CONFIG.exists():
        pytest.skip(f"{REAL_CONFIG} does not exist")
    try:
        REAL_CONFIG.read_text(encoding="utf-8")
    except PermissionError:
        pytest.skip(f"{REAL_CONFIG} is not readable")
    # Also need `ssh` available for the ssh -G cross-check.
    if shutil.which("ssh") is None:
        pytest.skip("ssh binary not on PATH")


def _run_envelope(*args: str, timeout: int = 10) -> dict:
    """Invoke ssh_config_manager.py as a subprocess; return parsed JSON."""
    argv = [sys.executable, str(SCM_PATH), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"ssh_config_manager produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise  # unreachable; satisfies type checkers


# Number of (key, value) parts a well-formed `ssh -G` line splits into.
_FIELD_PARTS = 2


def _ssh_g_field(alias: str, field: str) -> str | None:
    """Read one `ssh -G <alias>` field, lowercased.

    Returns None if `ssh -G` fails or the field isn't present.
    """
    try:
        proc = subprocess.run(
            ["ssh", "-G", alias],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == _FIELD_PARTS and parts[0].lower() == field.lower():
            return parts[1].strip()
    return None


# -----------------------------------------------------------------------------
# Live read-only tests
# -----------------------------------------------------------------------------


def test_list_includes_pai() -> None:
    """`ssh_config_manager.py list` against the real ~/.ssh/config must
    surface every alias the user has, including the PoC reference host."""
    result = _run_envelope("list")
    assert result["success"] is True, result
    assert result["exit_code"] == EXIT_OK
    aliases = [h["alias"] for h in result["data"]["hosts"]]
    if POC_HOST not in aliases:
        pytest.skip(
            f"alias {POC_HOST!r} not present in {REAL_CONFIG}; "
            "override POC_HOST or add the alias to test this case"
        )
    assert POC_HOST in aliases


def test_find_pai_matches_ssh_dash_G() -> None:
    """`ssh_config_manager.py find pai` must report the same host/user as
    `ssh -G pai` derives from the same file. This is the integration
    contract: our parser is consistent with OpenSSH's own."""
    result = _run_envelope("find", POC_HOST)
    if not result["success"]:
        pytest.skip(
            f"alias {POC_HOST!r} not present in {REAL_CONFIG}; "
            f"got failure_class={result['data'].get('failure_class')}"
        )
    data = result["data"]
    # Cross-check the fields that ssh -G always emits.
    ssh_g_host = _ssh_g_field(POC_HOST, "hostname")
    ssh_g_user = _ssh_g_field(POC_HOST, "user")
    if ssh_g_host is None or ssh_g_user is None:
        pytest.skip(
            f"ssh -G {POC_HOST} did not return hostname/user; "
            "cannot cross-check"
        )
    assert data["host"] == ssh_g_host, (
        f"host mismatch: our find says {data['host']!r}, "
        f"ssh -G says {ssh_g_host!r}"
    )
    assert data["user"] == ssh_g_user, (
        f"user mismatch: our find says {data['user']!r}, "
        f"ssh -G says {ssh_g_user!r}"
    )
    # If the user pinned a Port, our value should match too. If not, ssh
    # -G returns its default ("22") and our find returns None -- both are
    # fine and we just don't assert.
    if data["port"] is not None:
        ssh_g_port = _ssh_g_field(POC_HOST, "port")
        assert ssh_g_port is not None
        assert int(ssh_g_port) == data["port"], (
            f"port mismatch: our find says {data['port']!r}, "
            f"ssh -G says {ssh_g_port!r}"
        )


def test_find_pai_json_structure() -> None:
    """The `find <alias> --json` envelope must contain every field the prd
    promises (alias / host / user / port / key / proxy_jump / environment
    / tags / location / description / raw_lines)."""
    result = _run_envelope("find", POC_HOST)
    if not result["success"]:
        pytest.skip(f"alias {POC_HOST!r} missing from {REAL_CONFIG}")
    assert result["exit_code"] == EXIT_OK
    assert result["success"] is True

    required = {
        "alias", "host", "user", "port", "key", "proxy_jump",
        "environment", "tags", "location", "description", "raw_lines",
        "failure_class",
    }
    missing = required - set(result["data"].keys())
    assert not missing, f"data envelope missing keys: {missing}"

    # alias must echo back to what we asked for.
    assert result["data"]["alias"] == POC_HOST
    # raw_lines must include the original `Host <alias>` directive.
    assert any(
        ln.strip().lower() == f"host {POC_HOST}".lower()
        for ln in result["data"]["raw_lines"]
    ), f"raw_lines does not contain `Host {POC_HOST}`: {result['data']['raw_lines']}"
    # failure_class is None on success.
    assert result["data"]["failure_class"] is None
