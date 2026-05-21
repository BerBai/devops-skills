"""Integration tests for diagnose_host v1.0 -- real ssh to a real host.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_diagnose_host_integration.py -v -m live_ssh

These exercise the full shell-out chain:

    pytest -> diagnose_host.py (subprocess) -> ssh_execute.py (subprocess)
           -> native ssh -> remote shell

i.e. the architectural claim from CONTRIBUTING.md line 29 that diagnostic
plugins talk to ssh-core through the CLI rather than via Python imports.

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

If ssh-agent is empty or the host is unreachable, the whole module is
skipped cleanly rather than failing -- these tests gate code quality,
not infrastructure.
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
DIAGNOSE_HOST = (
    REPO_ROOT
    / "plugins"
    / "remote-debug"
    / "skills"
    / "remote-debug"
    / "scripts"
    / "diagnose_host.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")
EXPECTED_PROBES = {"uptime", "load", "disk", "mem", "zombie"}

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SSH_FAIL = 255
DISK_PCT_MAX = 100


# -----------------------------------------------------------------------------
# Skip guards (mirror tests/test_ssh_execute_integration.py)
# -----------------------------------------------------------------------------


def _ssh_agent_has_key() -> bool:
    if shutil.which("ssh-add") is None:
        return False
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _host_reachable(host: str) -> bool:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        host,
        "true",
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    if not _ssh_agent_has_key():
        pytest.skip(
            "ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)"
        )
    if not _host_reachable(POC_HOST):
        pytest.skip(
            f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)"
        )


def _run_envelope(*args: str, timeout: int = 60) -> dict:
    """Invoke diagnose_host.py as a subprocess; return parsed JSON envelope."""
    argv = [sys.executable, str(DIAGNOSE_HOST), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"diagnose_host produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise  # unreachable, satisfies type-checkers


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_diagnose_host_pai_returns_healthy_envelope() -> None:
    """End-to-end: diagnose_host pai --json. Every probe must succeed and
    severity must not be crit on a healthy host."""
    result = _run_envelope(POC_HOST)

    assert result["success"] is True, result
    assert result["exit_code"] == EXIT_OK
    assert set(result["data"]["probes"].keys()) >= EXPECTED_PROBES

    for name, probe in result["data"]["probes"].items():
        assert probe["success"] is True, f"probe {name} failed: {probe}"
        assert probe["exit_code"] == EXIT_OK, f"probe {name}: {probe}"

    assert result["data"]["severity"] in {"ok", "warn"}, result["data"]

    # Each probe with structured output has parsed fields populated.
    load_parsed = result["data"]["probes"]["load"]["parsed"]
    assert "cores" in load_parsed
    assert "load_1m" in load_parsed
    assert load_parsed["cores"] >= 1

    disk_parsed = result["data"]["probes"]["disk"]["parsed"]
    assert "use_pct" in disk_parsed
    assert 0 <= disk_parsed["use_pct"] <= DISK_PCT_MAX

    mem_parsed = result["data"]["probes"]["mem"]["parsed"]
    assert "total_mb" in mem_parsed
    assert mem_parsed["total_mb"] > 0

    zombie_parsed = result["data"]["probes"]["zombie"]["parsed"]
    assert "count" in zombie_parsed
    assert zombie_parsed["count"] >= 0


def test_diagnose_host_invalid_host_classifies_network() -> None:
    """End-to-end against an unresolvable host: every probe surfaces
    failure_class=network (propagated up from ssh_execute).

    NOTE: split-horizon DNS / NAT redirectors may resolve arbitrary names
    to a sinkhole IP; in that case ssh emits 'Connection closed' rather
    than 'Could not resolve hostname'. We skip rather than fail so the
    test still passes on networks that NAT-redirect.
    """
    result = _run_envelope("nonexistent.invalid.local")

    assert result["success"] is False, result
    assert result["exit_code"] == EXIT_FAIL

    probes = result["data"]["probes"]
    assert set(probes.keys()) >= EXPECTED_PROBES

    classes = {p["failure_class"] for p in probes.values()}
    if classes != {"network"}:
        pytest.skip(
            "DNS resolver does not return NXDOMAIN for nonexistent.invalid.local "
            f"(observed classes: {classes}); cannot test network class here."
        )

    for name, probe in probes.items():
        assert probe["success"] is False, f"probe {name} unexpectedly ok: {probe}"
        assert probe["exit_code"] == EXIT_SSH_FAIL, f"probe {name}: {probe}"
        assert probe["failure_class"] == "network"


def test_diagnose_host_check_filter_runs_only_listed() -> None:
    """--check uptime,load against a real host produces exactly two probes."""
    result = _run_envelope(POC_HOST, "--check", "uptime,load")
    assert result["success"] is True, result
    assert set(result["data"]["probes"].keys()) == {"uptime", "load"}
    assert result["data"]["checks_run"] == ["uptime", "load"]
