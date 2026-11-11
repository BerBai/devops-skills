"""Integration tests for ssh_execute v1.0 — runs real ssh to a real host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_execute_integration.py -v -m live_ssh

The target host comes from ``POC_HOST`` (there is no committed default). See
``tests/README.md`` for prerequisites.

Each test re-runs one of the PoC hypotheses (H1-H6) from
``.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/findings.md``,
but against the v1.0 ``ssh_execute.py`` (not the spike code).

Exit codes come from the product's own table
(``plugins/ssh-core/.../scripts/lib/exit_codes.py``). Two of them used to be
asserted as OpenSSH's raw 255: an unresolved *alias* is `4 alias_not_found`
and an unresolvable *hostname* is `102 network` — ssh's client-failure code is
translated, not passed through (GAP-002).

If ssh-agent is empty or the host is unreachable, tests skip cleanly
rather than failing — these tests gate quality, not infrastructure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import live_support  # noqa: E402
import pytest

# ``lib`` is pinned to ssh-core's own package by tests/conftest.py.
from lib import exit_codes as _codes  # noqa: E402
from live_support import require_live_ssh  # noqa: E402

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
POC_HOST = live_support.poc_host()
POC_CONFIG = live_support.poc_ssh_config()

# Exit codes, read from lib/exit_codes.py where the table has a name for them.
EXIT_OK = _codes.EXIT_OK
EXIT_INVALID_ARGUMENTS = _codes.EXIT_INVALID_ARGUMENTS
EXIT_TIMEOUT = _codes.EXIT_TIMEOUT
EXIT_ALIAS_NOT_FOUND = _codes.EXIT_CODES["alias_not_found"]
EXIT_NETWORK = _codes.EXIT_NETWORK
EXIT_AUTH = _codes.EXIT_AUTH

#: Codes that are the *remote command's* own status and must pass through
#: untouched, so they never appear in ``EXIT_CODES``. ``false`` exits 1 and
#: ``ssh_execute`` forwards a remote status verbatim; the envelope's
#: ``failure_class="remote_error"`` is what distinguishes "the command failed"
#: from the transport-level 104 the table reserves for ssh's own 255.
EXIT_REMOTE_COMMAND_FAILED = 1
EXIT_CUSTOM = 42
EXIT_MIXED_IO = 3
EXIT_NO_COMMAND = 127

# Default --timeout used by ssh_execute.py main(); checked in dispatch tests.
TIMEOUT_DEFAULT = 120
#: The timeout case asks for ``--timeout 3`` and must come back in roughly that
#: budget. ``elapsed_s == 3.0`` was a float-equality assertion that measured
#: 3.031 on the first real host it met (GAP-002); this is a range instead.
TIMEOUT_BUDGET_MIN_S = 3.0
TIMEOUT_BUDGET_MAX_S = 3.5


# -----------------------------------------------------------------------------
# Skip guards: bail out cleanly if the test machine isn't set up for live ssh.
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Module-level precondition; the shared guard names what is missing."""
    require_live_ssh()


def _run_envelope(*args: str, timeout: int = 30) -> dict:
    """Invoke ssh_execute.py as a subprocess; return parsed JSON envelope."""
    argv = [sys.executable, str(SSH_EXECUTE), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False,
        encoding="utf-8", errors="replace",
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
    """H1-2: `false` propagates exit 1, classified as remote_error.

    1 here is the *remote command's* status (``false``), passed through
    verbatim; ``failure_class="remote_error"`` is what separates "the command
    failed" from the transport-level 104 the table reserves for ssh's own 255.
    """
    result = _run_envelope(POC_HOST, "false")
    assert result["success"] is False
    assert result["exit_code"] == EXIT_REMOTE_COMMAND_FAILED
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

    The derived config mirrors whatever ``ssh -G POC_HOST`` resolved -- same
    HostName/User/IdentityFile/Port -- and only replaces the known-hosts file,
    so the 'Permanently added' warning the filter must drop really happens.
    The old version hardcoded ``192.168.2.115``/``root`` plus macOS-only
    ``UseKeychain yes``, which the Windows ssh client rejects outright.
    """
    resolved = live_support.resolved_ssh_options(POC_HOST)
    if not resolved.get("hostname"):
        pytest.skip(f"`ssh -G {POC_HOST}` did not resolve a hostname")

    known_hosts = tmp_path / "known_hosts"
    cfg = tmp_path / "ssh-config"
    lines = [
        f"Host {POC_HOST}-fresh",
        f"    HostName {resolved['hostname']}",
        f"    User {resolved.get('user') or 'root'}",
        f"    Port {resolved.get('port') or '22'}",
        f"    UserKnownHostsFile {known_hosts}",
        "    StrictHostKeyChecking accept-new",
    ]
    if resolved.get("identityfile"):
        lines.append(f"    IdentityFile {resolved['identityfile']}")
        # Without IdentitiesOnly the agent's other keys are tried first, which
        # is fine — but pinning it makes the derived config behave like the
        # original alias instead of like "whatever keys I happen to hold".
        lines.append("    IdentitiesOnly yes")
    cfg.write_text("\n".join(lines) + "\n", encoding="ascii")

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
    """H3-2: alias with no IdentityFile resolves via ssh-agent.

    Needs a hand-written ``Host poc-via-agent`` stanza whose whole point is the
    *absence* of an IdentityFile, so the only possible credential is the agent
    identity. A generated config cannot express that, so the shared guard skips
    with the missing stanza's name instead of fabricating one and failing.
    """
    cfg = live_support.require_ssh_config_stanza("poc-via-agent")
    result = _run_envelope(
        "poc-via-agent", "true", "--ssh-config", str(cfg)
    )
    assert result["success"] is True, result
    assert result["exit_code"] == EXIT_OK


def test_h3_undefined_alias_classified_alias_not_found() -> None:
    """H3-3: a name that is neither an alias nor an address → 4, not 255.

    ``bogus-not-defined-anywhere-xyz`` has no dot and is not an IP, so ssh
    treats it as a literal hostname and fails to resolve it. The product reads
    that shape as a mistyped *alias* (FA-22), which points the operator at
    ~/.ssh/config instead of at DNS; the exit code is therefore 4
    ``alias_not_found``, not OpenSSH's raw 255.
    """
    result = _run_envelope("bogus-not-defined-anywhere-xyz", "uptime", timeout=15)
    assert result["success"] is False
    # Some resolvers NAT-redirect instead of answering NXDOMAIN, in which case
    # the name is not reported as unresolved at all; skip *before* asserting the
    # code, so a resolver quirk cannot masquerade as a product regression.
    if result["data"]["failure_class"] != "alias_not_found":
        pytest.skip(
            "DNS resolver does not report NXDOMAIN for a bare hostname "
            "(NAT redirect / split-horizon DNS); cannot test the "
            f"alias-not-found path here. envelope: {result!r}"
        )
    assert result["exit_code"] == EXIT_ALIAS_NOT_FOUND, result
    assert result["data"]["failure_class"] == "alias_not_found"


# -----------------------------------------------------------------------------
# H4 — Failure classification
# -----------------------------------------------------------------------------


def test_h4_network_class_on_dns_failure() -> None:
    """H4-1: DNS-unresolvable host → 102 network.

    ``.invalid.local`` is a dotted name, so it is a *hostname* rather than an
    alias and lands in the ``network`` class (102) instead of
    ``alias_not_found`` (4). Some resolvers (macOS in certain network configs,
    corporate split-horizon) NAT-redirect arbitrary names to a sinkhole IP,
    which yields 'Connection closed' instead; if that happens we skip.
    """
    result = _run_envelope("nonexistent.invalid.local", "uptime", timeout=15)
    assert result["success"] is False
    if result["data"]["failure_class"] != "network":
        pytest.skip(
            "DNS resolver does not return NXDOMAIN for nonexistent.invalid.local "
            "(NAT redirect / split-horizon DNS); cannot test network class here. "
            f"envelope: {result!r}"
        )
    assert result["exit_code"] == EXIT_NETWORK, result
    assert result["data"]["failure_class"] == "network"


def test_h4_auth_class_on_publickey_denial() -> None:
    """H4-2: IdentityFile=/dev/null + IdentitiesOnly=yes → auth failure.

    Like H3-2 this needs a stanza only a human can write: ``Host poc-noauth``
    must pin an unusable identity so the publickey offer is denied on purpose.
    """
    cfg = live_support.require_ssh_config_stanza("poc-noauth")
    result = _run_envelope(
        "poc-noauth", "uptime", "--ssh-config", str(cfg), "--timeout", "15"
    )
    assert result["success"] is False
    if result["data"]["failure_class"] != "auth":
        pytest.skip(
            "the `poc-noauth` stanza did not produce a publickey denial "
            f"(is another credential available for that host?): {result!r}"
        )
    assert result["exit_code"] == EXIT_AUTH, result
    assert result["data"]["failure_class"] == "auth"


def test_h4_remote_error_residual() -> None:
    """H4-3: remote command returning non-zero (not 255) → remote_error."""
    result = _run_envelope(POC_HOST, "false")
    assert result["exit_code"] == EXIT_REMOTE_COMMAND_FAILED
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
    # ADR-001 D6: elapsed_s tracks the timeout budget. A range, not equality:
    # process teardown lands a few ms past the budget on every real host
    # (3.031 measured), and float equality turns that into a false failure.
    elapsed = result["data"]["elapsed_s"]
    assert TIMEOUT_BUDGET_MIN_S <= elapsed <= TIMEOUT_BUDGET_MAX_S, (
        f"elapsed_s={elapsed!r} outside "
        f"[{TIMEOUT_BUDGET_MIN_S},{TIMEOUT_BUDGET_MAX_S}]"
    )


# -----------------------------------------------------------------------------
# H6 — Local route
# -----------------------------------------------------------------------------


@pytest.mark.needs_posix
def test_h6_local_simple_command() -> None:
    """H6-1: `local ls /tmp` returns exit 0.

    A POSIX shell built this command; on Windows the local route only starts an
    ``.exe``/``.cmd``/``.bat`` from PATH, so ``ls`` is explicitly unsupported
    there (and the product says so, exit 127). The ``cmd /c`` form is the
    documented Windows spelling, so this case is POSIX-gated rather than
    rewritten to test something else.
    """
    result = _run_envelope("local", "ls /tmp")
    assert result["exit_code"] == EXIT_OK
    assert result["data"]["route"] == "local"


@pytest.mark.needs_posix
def test_h6_local_quoted_string_preserved() -> None:
    """H6-4: `echo \"hello world\"` keeps the space inside one arg."""
    result = _run_envelope("local", 'echo "hello world"')
    assert result["exit_code"] == EXIT_OK
    assert "hello world" in result["stdout"]


def test_h6_local_refuses_pipe_loudly() -> None:
    """H6-3 (v1.0 refinement): pipe is refused, not silently mis-handled.

    The local route deliberately runs without a shell, so the metacharacter is
    rejected *before* anything is spawned: 2 ``invalid_arguments`` (the argv
    cannot be honoured as given), not the generic 1 this case used to assert.
    Platform-independent, so it runs on Windows too.
    """
    result = _run_envelope("local", "ls /tmp | head -3")
    assert result["exit_code"] == EXIT_INVALID_ARGUMENTS, result
    assert result["data"]["refused_metachar"] == "|"
    assert result["data"]["failure_class"] == "invalid_arguments"


def test_h6_local_refuses_redirect_loudly(tmp_path: Path) -> None:
    """H6-5 (v1.0 refinement): redirect is refused before it can silently
    misbehave (the worst spike behavior — exit 0 but no file written)."""
    sentinel = tmp_path / "should_not_be_created.tmp"
    result = _run_envelope("local", f"echo hi > {sentinel}")
    assert result["exit_code"] == EXIT_INVALID_ARGUMENTS, result
    assert result["data"]["refused_metachar"] == ">"
    assert result["data"]["failure_class"] == "invalid_arguments"
    assert not sentinel.exists()
