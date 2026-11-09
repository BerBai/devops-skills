"""Unit tests for the ssh-core ``lib`` helpers that survived the sync.

The 1.4.0 sync deleted ``test_ssh_core_lib.py`` (11 cases) and with it the only
local coverage of ``filter_ssh_noise`` / ``classify_failure`` — the two helpers
every route depends on to tell "the transport broke" apart from "the remote
command exited non-zero".

These tests are pure functions: no ssh, no host, no subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import lib  # noqa: E402

SSH_CLIENT_FAILURE = 255


# -----------------------------------------------------------------------------
# filter_ssh_noise
# -----------------------------------------------------------------------------


def test_noise_line_is_dropped() -> None:
    stderr = (
        "Warning: Permanently added 'pai,10.0.0.2' (ED25519) to the list of known hosts.\n"
    )
    real, noise = lib.filter_ssh_noise(stderr)
    assert real == ""
    assert len(noise) == 1
    assert noise[0].startswith("Warning: Permanently added")


def test_real_error_is_preserved() -> None:
    stderr = "Permission denied (publickey).\n"
    real, noise = lib.filter_ssh_noise(stderr)
    assert real == "Permission denied (publickey).\n"
    assert noise == []


def test_mixed_output_keeps_order_and_trailing_newline() -> None:
    stderr = (
        "Warning: Permanently added 'x' (ED25519) to the list of known hosts.\n"
        "ssh: connect to host x port 22: Connection refused\n"
    )
    real, noise = lib.filter_ssh_noise(stderr)
    assert real == "ssh: connect to host x port 22: Connection refused\n"
    assert len(noise) == 1


def test_fail_open_keeps_unknown_warnings() -> None:
    """Missing a real error is worse than echoing an extra warning."""
    stderr = "Warning: something we have never seen before\n"
    real, noise = lib.filter_ssh_noise(stderr)
    assert real == "Warning: something we have never seen before\n"
    assert noise == []


def test_empty_input_is_empty_output() -> None:
    assert lib.filter_ssh_noise("") == ("", [])


def test_missing_trailing_newline_is_preserved() -> None:
    real, _ = lib.filter_ssh_noise("boom")
    assert real == "boom"


# -----------------------------------------------------------------------------
# classify_failure
# -----------------------------------------------------------------------------


def test_success_is_none() -> None:
    assert lib.classify_failure(0, "") is None


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Host key verification failed.", "host_key"),
        ("REMOTE HOST IDENTIFICATION HAS CHANGED!", "host_key"),
        ("no matching key exchange method found", "kex"),
        ("Permission denied (publickey,password).", "auth"),
        ("ssh: Could not resolve hostname nope: Name or service not known", "alias_not_found"),
        ("ssh: connect to host 10.0.0.9 port 22: Connection refused", "network"),
    ],
)
def test_ssh_client_failures_are_classified(stderr: str, expected: str) -> None:
    assert lib.classify_failure(SSH_CLIENT_FAILURE, stderr) == expected


def test_non_255_exit_is_remote_error() -> None:
    """A remote command's own stderr must not be misread as a transport fault."""
    assert lib.classify_failure(1, "Permission denied") == "remote_error"
    assert lib.classify_failure(2, "no matching key exchange method found") == "remote_error"


def test_unclassified_255_is_remote_error() -> None:
    assert lib.classify_failure(SSH_CLIENT_FAILURE, "something entirely new") == "remote_error"


def test_host_key_wins_over_auth_wording() -> None:
    """A host-key change also mentions "denied"; the fix is known_hosts."""
    stderr = "Host key verification failed.\nPermission denied (publickey)."
    assert lib.classify_failure(SSH_CLIENT_FAILURE, stderr) == "host_key"


def test_classify_never_returns_timeout() -> None:
    """Only the caller knows a timeout happened; this helper must not guess."""
    for code in (0, 1, 124, SSH_CLIENT_FAILURE):
        assert lib.classify_failure(code, "timed out") != "timeout"


# -----------------------------------------------------------------------------
# split_login_banner (SSHCORE-017: banner peeled off stderr)
# -----------------------------------------------------------------------------


def test_login_banner_is_reported_separately() -> None:
    """A banner host writes a blank line, then the banner, then real stderr.

    Only the first banner line is peeled (a multi-line banner leaves its tail
    behind on purpose: eating a real error is worse than echoing banner text).
    """
    command_output = "Linux pai 6.1.0\n"
    stderr = (
        "\n"
        "Authorized users only. All activity may be monitored.\n"
        "boom: command not found\n"
    )
    real, banner = lib.split_login_banner(stderr, command_output=command_output)
    assert "boom: command not found" in real
    assert banner == ["Authorized users only. All activity may be monitored."]


def test_stream_without_a_leading_blank_line_is_not_a_banner() -> None:
    """Banner hosts always start with a blank line; anything else is real."""
    stderr = "Authorized users only. boom: command not found\n"
    real, banner = lib.split_login_banner(stderr, command_output="")
    assert real == stderr
    assert banner == []


def test_no_banner_means_no_banner_lines() -> None:
    real, banner = lib.split_login_banner(
        "/bin/sh: nope: not found\n", command_output=""
    )
    assert real == "/bin/sh: nope: not found\n"
    assert banner == []
