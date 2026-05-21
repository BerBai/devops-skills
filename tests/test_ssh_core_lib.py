"""Unit tests for ssh-core/scripts/lib helpers.

Covers the helpers that v1.0 ssh_execute.py depends on:
    filter_ssh_noise(stderr) -> (real_stderr, noise_lines)
    classify_failure(returncode, stderr) -> None | "network" | "auth" | "remote_error"

The five pre-existing helpers (json_result, emit, msys_safe_env,
alias_state_path, unimplemented) are exercised indirectly by
test_manifests.py::test_script_help and are not re-tested here.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(LIB_DIR))

from lib import classify_failure, filter_ssh_noise  # noqa: E402, I001


# -----------------------------------------------------------------------------
# filter_ssh_noise
# -----------------------------------------------------------------------------


def test_filter_ssh_noise_strips_known_hosts_warning() -> None:
    stderr = "Warning: Permanently added '192.168.2.115' (ED25519) to the list of known hosts.\n"
    real, noise = filter_ssh_noise(stderr)
    assert real == ""
    assert len(noise) == 1
    assert "Permanently added" in noise[0]


def test_filter_ssh_noise_preserves_unknown_warnings() -> None:
    """Fail-open: any line we don't recognize stays in real_stderr."""
    stderr = "Some unknown ssh warning we have never seen before.\n"
    real, noise = filter_ssh_noise(stderr)
    assert "unknown ssh warning" in real
    assert noise == []


def test_filter_ssh_noise_preserves_trailing_newline() -> None:
    """Regression: the spike used splitlines+join and silently dropped the
    final newline. v1.0 must preserve it iff the input had one and we
    kept at least one real line."""
    stderr = "real error line\n"
    real, _ = filter_ssh_noise(stderr)
    assert real.endswith("\n")
    assert real == "real error line\n"


def test_filter_ssh_noise_empty_stderr() -> None:
    real, noise = filter_ssh_noise("")
    assert real == ""
    assert noise == []


def test_filter_ssh_noise_mixed_known_and_unknown() -> None:
    stderr = (
        "Warning: Permanently added 'foo' (ED25519) to the list of known hosts.\n"
        "real error: something went wrong\n"
    )
    real, noise = filter_ssh_noise(stderr)
    assert "real error" in real
    assert real.endswith("\n")  # trailing newline preserved
    assert len(noise) == 1
    assert "Permanently added" in noise[0]


# -----------------------------------------------------------------------------
# classify_failure
# -----------------------------------------------------------------------------


def test_classify_failure_success_returns_none() -> None:
    assert classify_failure(0, "") is None
    assert classify_failure(0, "anything in stderr at exit 0 is still success") is None


def test_classify_failure_network_on_dns_error() -> None:
    stderr = "ssh: Could not resolve hostname nonexistent.invalid.local: nodename nor servname provided, or not known\r\n"
    assert classify_failure(255, stderr) == "network"


def test_classify_failure_auth_on_publickey_denial() -> None:
    stderr = "root@1.2.3.4: Permission denied (publickey).\r\n"
    assert classify_failure(255, stderr) == "auth"


def test_classify_failure_remote_error_default() -> None:
    """exit 1 from a remote command (e.g. `false`) → remote_error."""
    assert classify_failure(1, "") == "remote_error"
    assert classify_failure(127, "bash: nosuch: command not found\n") == "remote_error"


def test_classify_failure_skips_auth_for_remote_permission_denied() -> None:
    """Regression for PoC findings.md H4 caveat: a remote command that
    legitimately prints "Permission denied" in its OWN stderr (e.g.
    `cat /etc/shadow` as a non-root user) must not be mis-classified as
    an ssh-client auth failure. Auth scoping requires returncode == 255."""
    stderr = "cat: /etc/shadow: Permission denied\n"
    assert classify_failure(1, stderr) == "remote_error"


def test_classify_failure_network_only_at_255() -> None:
    """Same scoping rule for network: a remote command that prints
    'Connection refused' in its own output (e.g. a curl error captured
    by a script that then exits 1) must not be mis-classified."""
    stderr = "curl: (7) Failed to connect to localhost port 8080: Connection refused\n"
    assert classify_failure(1, stderr) == "remote_error"
