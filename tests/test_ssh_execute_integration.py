"""Integration tests for ssh_execute v1.0 — runs real ssh to a real host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_execute_integration.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

Each test re-runs one of the PoC hypotheses (H1-H6) from
``.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/findings.md``,
but against the v1.0 ``ssh_execute.py`` (not the spike code).

If ssh-agent is empty or the host is unreachable, tests skip cleanly
rather than failing — these tests gate quality, not infrastructure.
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
SSH_EXECUTE = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_execute.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")
POC_CONFIG = "/tmp/poc-ssh-config"

# Well-known exit codes used across the assertions below.
EXIT_OK = 0
EXIT_REMOTE_ERROR = 1
EXIT_CUSTOM = 42
EXIT_MIXED_IO = 3
EXIT_TIMEOUT = 124
EXIT_NO_COMMAND = 127
EXIT_SSH_CLIENT_FAILURE = 255

# Default --timeout used by ssh_execute.py main(); checked in dispatch tests.
TIMEOUT_DEFAULT = 120
TIMEOUT_BUDGET = 3.0


# -----------------------------------------------------------------------------
# Skip guards: bail out cleanly if the test machine isn't set up for live ssh.
# -----------------------------------------------------------------------------


def _ssh_agent_has_key() -> bool:
    """Return True iff ssh-add -l reports at least one loaded identity."""
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
    # `ssh-add -l` returns 0 when at least one key is loaded, 1 when none,
    # 2 when the agent is unreachable. Be permissive: 0 means we have keys.
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _host_reachable(host: str, ssh_config: str | None = None) -> bool:
    """Quick `ssh <host> true` probe with a short timeout."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if ssh_config and Path(ssh_config).exists():
        argv += ["-F", ssh_config]
    argv += [host, "true"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Skip the whole module unless agent + host are usable."""
    if not _ssh_agent_has_key():
        pytest.skip(
            "ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)"
        )
    if not _host_reachable(POC_HOST):
        pytest.skip(
            f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)"
        )


def _run_envelope(*args: str, timeout: int = 30) -> dict:
    """Invoke ssh_execute.py as a subprocess; return parsed JSON envelope."""
    argv = [sys.executable, str(SSH_EXECUTE), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"ssh_execute produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise  # unreachable, satisfies type checkers


# -----------------------------------------------------------------------------
# H1 — Remote exit code propagation
# -----------------------------------------------------------------------------


def test_h1_true_returns_exit_0() -> None:
    """H1-1: `true` propagates exit 0 with failure_class=None."""
    result = _run_envelope(POC_HOST, "true")
    assert result["success"] is True
    assert result["exit_code"] == EXIT_OK
    assert result["data"]["failure_class"] is None


def test_h1_false_returns_exit_1_remote_error() -> None:
    """H1-2: `false` propagates exit 1, classified as remote_error (non-255)."""
    result = _run_envelope(POC_HOST, "false")
    assert result["success"] is False
    assert result["exit_code"] == EXIT_REMOTE_ERROR
    assert result["data"]["failure_class"] == "remote_error"


def test_h1_arbitrary_exit_code_propagates() -> None:
    """H1-3: remote `bash -c 'exit 42'` surfaces exit_code=42."""
    result = _run_envelope(POC_HOST, "bash -c 'exit 42'")
    assert result["exit_code"] == EXIT_CUSTOM
    assert result["data"]["failure_class"] == "remote_error"


def test_h1_nonexistent_command_127() -> None:
    """H1-4: missing remote command returns exit 127."""
    result = _run_envelope(POC_HOST, "nonexistent_command_xyz_zzz")
    assert result["exit_code"] == EXIT_NO_COMMAND
    assert result["data"]["failure_class"] == "remote_error"


def test_h1_mixed_stdio_splits_correctly() -> None:
    """H1-5: stdout and stderr arrive on separate streams."""
    result = _run_envelope(
        POC_HOST, "bash -c 'echo TO_OUT; echo TO_ERR >&2; exit 3'"
    )
    assert result["exit_code"] == EXIT_MIXED_IO
    assert "TO_OUT" in result["stdout"]
    assert "TO_ERR" in result["stderr"]


# -----------------------------------------------------------------------------
# H2 — Known-hosts noise filtering
# -----------------------------------------------------------------------------


def test_h2_normal_call_has_no_noise() -> None:
    """H2-1: against a known host, ssh_noise_lines is empty."""
    result = _run_envelope(POC_HOST, "true")
    assert result["data"]["ssh_noise_lines"] == []


def test_h2_filters_known_hosts_warning_via_fresh_config(tmp_path: Path) -> None:
    """H2-3: force the known_hosts warning by pointing at a fresh file.

    Creates a temporary ssh config that mirrors POC_HOST but pins
    UserKnownHostsFile to a fresh path so ssh emits the
    'Permanently added' warning we want to filter.
    """
    if not Path(POC_CONFIG).exists():
        pytest.skip(f"{POC_CONFIG} (PoC scratch config) missing on this machine")

    known_hosts = tmp_path / "known_hosts"
    cfg = tmp_path / "ssh-config"
    base = Path(POC_CONFIG).read_text()
    cfg.write_text(
        base
        + (
            "\n"
            f"Host {POC_HOST}-fresh\n"
            "    HostName 192.168.2.115\n"
            "    User root\n"
            "    IdentityFile ~/.ssh/id_ed25519\n"
            "    UseKeychain yes\n"
            "    AddKeysToAgent yes\n"
            f"    UserKnownHostsFile {known_hosts}\n"
            "    StrictHostKeyChecking accept-new\n"
        )
    )

    result = _run_envelope(
        f"{POC_HOST}-fresh", "true", "--ssh-config", str(cfg)
    )
    if result["exit_code"] != 0:
        pytest.skip(
            f"could not provoke known_hosts warning: {result['stderr']!r}"
        )
    # The noise line(s) should now be in data, not in top-level stderr.
    noise = result["data"]["ssh_noise_lines"]
    assert any("Permanently added" in line for line in noise), result
    assert "Permanently added" not in result["stderr"]
    assert "Permanently added" in result["data"]["raw_stderr"]


# -----------------------------------------------------------------------------
# H3 — Alias resolution via ~/.ssh/config (no Python-side parsing)
# -----------------------------------------------------------------------------


def test_h3_alias_via_agent() -> None:
    """H3-2: alias with no IdentityFile resolves via ssh-agent."""
    if not Path(POC_CONFIG).exists():
        pytest.skip(f"{POC_CONFIG} missing on this machine")
    result = _run_envelope(
        "poc-via-agent", "true", "--ssh-config", POC_CONFIG
    )
    assert result["success"] is True, result
    assert result["exit_code"] == 0


def test_h3_undefined_alias_classified_network() -> None:
    """H3-3: alias not in any config → ssh treats as literal hostname,
    fails DNS, classified as network. Skip if the resolver NAT-redirects
    instead of returning NXDOMAIN."""
    result = _run_envelope("bogus-not-defined-anywhere-xyz", "uptime", timeout=15)
    assert result["success"] is False
    assert result["exit_code"] == EXIT_SSH_CLIENT_FAILURE
    if result["data"]["failure_class"] != "network":
        pytest.skip(
            "DNS resolver does not return NXDOMAIN for arbitrary hostnames "
            "(NAT redirect / split-horizon DNS); cannot test alias-as-hostname "
            f"path here. stderr: {result['stderr']!r}"
        )
    assert result["data"]["failure_class"] == "network"


# -----------------------------------------------------------------------------
# H4 — Failure classification
# -----------------------------------------------------------------------------


def test_h4_network_class_on_dns_failure() -> None:
    """H4-1: DNS-unresolvable host → failure_class=network.

    Some resolvers (e.g. macOS in certain network configs) NAT-redirect
    arbitrary hostnames to a sinkhole IP, which yields 'Connection closed'
    instead of 'Could not resolve hostname'. ``.invalid`` and ``.local``
    are the most reliably-unresolvable TLDs in practice. If the env still
    NATs them we skip rather than fail.
    """
    result = _run_envelope("nonexistent.invalid.local", "uptime", timeout=15)
    assert result["exit_code"] == EXIT_SSH_CLIENT_FAILURE
    if result["data"]["failure_class"] != "network":
        pytest.skip(
            "DNS resolver does not return NXDOMAIN for nonexistent.invalid.local "
            "(NAT redirect / split-horizon DNS); cannot test network class here. "
            f"stderr: {result['stderr']!r}"
        )
    assert result["data"]["failure_class"] == "network"


def test_h4_auth_class_on_publickey_denial() -> None:
    """H4-2: IdentityFile=/dev/null + IdentitiesOnly=yes → auth failure."""
    if not Path(POC_CONFIG).exists():
        pytest.skip(f"{POC_CONFIG} missing on this machine")
    result = _run_envelope(
        "poc-noauth", "uptime", "--ssh-config", POC_CONFIG, "--timeout", "15"
    )
    assert result["exit_code"] == EXIT_SSH_CLIENT_FAILURE
    assert result["data"]["failure_class"] == "auth", result


def test_h4_remote_error_residual() -> None:
    """H4-3: remote command returning non-zero (not 255) → remote_error."""
    result = _run_envelope(POC_HOST, "false")
    assert result["exit_code"] == EXIT_REMOTE_ERROR
    assert result["data"]["failure_class"] == "remote_error"


# -----------------------------------------------------------------------------
# H5 — Timeout
# -----------------------------------------------------------------------------


def test_h5_timeout_returns_124(tmp_path: Path) -> None:
    """H5: subprocess.TimeoutExpired → exit_code=124, failure_class=timeout."""
    result = _run_envelope(
        POC_HOST, "sleep 60", "--timeout", "3", timeout=8
    )
    assert result["exit_code"] == EXIT_TIMEOUT, result
    assert result["data"]["failure_class"] == "timeout"
    # ADR-001 D6: elapsed_s == timeout budget on timeout
    assert result["data"]["elapsed_s"] == TIMEOUT_BUDGET


# -----------------------------------------------------------------------------
# H6 — Local route
# -----------------------------------------------------------------------------


def test_h6_local_simple_command() -> None:
    """H6-1: `local ls /tmp` returns exit 0."""
    result = _run_envelope("local", "ls /tmp")
    assert result["exit_code"] == EXIT_OK
    assert result["data"]["route"] == "local"


def test_h6_local_quoted_string_preserved() -> None:
    """H6-4: `echo \"hello world\"` keeps the space inside one arg."""
    result = _run_envelope("local", 'echo "hello world"')
    assert result["exit_code"] == EXIT_OK
    assert "hello world" in result["stdout"]


def test_h6_local_refuses_pipe_loudly() -> None:
    """H6-3 (v1.0 refinement): pipe is refused, not silently mis-handled."""
    result = _run_envelope("local", "ls /tmp | head -3")
    assert result["exit_code"] == EXIT_REMOTE_ERROR
    assert result["data"]["refused_metachar"] == "|"


def test_h6_local_refuses_redirect_loudly(tmp_path: Path) -> None:
    """H6-5 (v1.0 refinement): redirect is refused before it can silently
    misbehave (the worst spike behavior — exit 0 but no file written)."""
    sentinel = tmp_path / "should_not_be_created.tmp"
    result = _run_envelope("local", f"echo hi > {sentinel}")
    assert result["exit_code"] == EXIT_REMOTE_ERROR
    assert result["data"]["refused_metachar"] == ">"
    assert not sentinel.exists()
