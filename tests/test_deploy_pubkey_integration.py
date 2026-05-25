"""Integration tests for deploy_pubkey v1.0 — minimal, non-mutating.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_deploy_pubkey_integration.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

By design (per PRD D7), this suite does **not** invoke a real
``ssh-copy-id`` against POC_HOST. Running ssh-copy-id against POC_HOST
would just re-trip the ``already_installed`` path (the dev's key is
already there) and provide low signal. End-to-end "bootstrap a fresh
user" is left to the e2e validation report. Here we exercise only the
three cheap, non-mutating paths:

1. ``--help`` exits 0 (smoke; would also be caught by test_manifests).
2. Non-existent alias fails fast with ``failure_class="network"`` —
   ssh-copy-id resolves the alias before any auth and exits with a DNS
   error.
3. ``--key /nonexistent`` fails in preflight with
   ``failure_class="key_file_missing"`` and never spawns ssh-copy-id.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_PUBKEY = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "deploy_pubkey.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")

# Exit codes (named so PLR2004 doesn't complain).
EXIT_OK = 0
EXIT_KEY_FILE_MISSING = 11
EXIT_NETWORK = 15
EXIT_TOOL_MISSING = 10

# Cap each subprocess call so a hung ssh-copy-id can't wedge the test
# session. All cases here are designed to complete in < 5 s; we double
# that as the wall-clock budget.
RUN_TIMEOUT = 30


# -----------------------------------------------------------------------------
# Skip guards
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_ssh_copy_id() -> None:
    """Skip the whole module unless ssh-copy-id is on PATH.

    Unlike the ssh_execute integration suite, we do NOT require a
    reachable POC_HOST or a loaded ssh-agent identity — none of our
    three cases need either. The DNS-failure case in particular must
    work even when no host on the network is reachable, since that's
    the path under test.
    """
    if shutil.which("ssh-copy-id") is None:
        pytest.skip("ssh-copy-id not on PATH; install openssh-client (linux) or openssh (macOS via brew)")


def _run_deploy_pubkey(*cli_args: str, timeout: int = RUN_TIMEOUT) -> tuple[int, dict]:
    """Invoke deploy_pubkey.py as a subprocess; return (rc, parsed_json)."""
    argv = [sys.executable, str(DEPLOY_PUBKEY), *cli_args, "--json"]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as e:
        pytest.fail(
            f"deploy_pubkey produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise  # unreachable, satisfies type checkers
    return proc.returncode, payload


# -----------------------------------------------------------------------------
# Cases
# -----------------------------------------------------------------------------


def test_help_exits_zero() -> None:
    """`deploy_pubkey.py --help` exits 0 and prints the standard usage line.

    Manifest-style smoke. Verifies argparse builds without crashing under
    a real subprocess invocation (not just an in-process import).
    """
    proc = subprocess.run(
        [sys.executable, str(DEPLOY_PUBKEY), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()
    # D5 surface flags present.
    for flag in (
        "--key",
        "--connect-timeout",
        "--timeout",
        "--ssh-config",
        "--accept-new-hostkey",
        "--json",
    ):
        assert flag in proc.stdout, f"missing flag {flag} in --help output"
    # And the v0.2-only flag is gone.
    assert "--strip-password" not in proc.stdout


def test_nonexistent_alias_returns_network() -> None:
    """ssh-copy-id against an unknown alias fails fast on DNS → failure_class=network.

    The randomized suffix avoids any chance of the alias being defined
    in this developer's ~/.ssh/config by accident. We pass
    ``--connect-timeout=2`` to keep the whole case under a second.

    On some networks the resolver NAT-redirects unresolvable names to a
    sinkhole IP, in which case ssh-copy-id may report a different
    failure (Connection closed / auth). We accept either ``network`` or
    skip with a clear message — matching the precedent set by
    ``test_h4_network_class_on_dns_failure`` in ssh_execute integration.
    """
    bogus = f"deploy-pubkey-bogus-{uuid.uuid4().hex[:8]}"
    rc, payload = _run_deploy_pubkey(
        bogus, "--connect-timeout=2", "--timeout=10", timeout=20
    )

    # If a real key file existed and the network failure path is taken,
    # exit code is 15 and failure_class is "network".
    if payload.get("data", {}).get("failure_class") != "network":
        pytest.skip(
            "DNS resolver does not return NXDOMAIN for arbitrary hostnames "
            "(NAT redirect / split-horizon DNS); cannot test network "
            f"class here. failure_class={payload.get('data', {}).get('failure_class')!r}, "
            f"stderr={payload.get('stderr')!r}"
        )

    assert rc == EXIT_NETWORK
    assert payload["data"]["failure_class"] == "network"
    assert payload["success"] is False
    # Preflight passed (ssh-copy-id was actually spawned), so argv is populated.
    assert isinstance(payload["data"]["ssh_copy_id_argv"], list)
    assert payload["data"]["ssh_copy_id_argv"], "expected non-empty argv after preflight passed"
    assert payload["data"]["ssh_copy_id_argv"][0] == "ssh-copy-id"
    # And the elapsed budget stayed reasonable.
    assert payload["data"]["elapsed_s"] < 10  # noqa: PLR2004


def test_missing_key_returns_key_file_missing() -> None:
    """`--key /nonexistent.pub` short-circuits in preflight.

    The critical invariant: this case must NEVER reach the network. We
    verify that by:
    1. Asserting exit_code == 11 (key_file_missing).
    2. Asserting data.ssh_copy_id_argv == [] (preflight refused, never spawned subprocess).
    3. Asserting elapsed_s == 0.0 (preflight is O(1) on a file stat).

    This is also the canonical "deploy_pubkey is safe to run against any
    arbitrary alias" smoke — the missing-key guard fires before any
    DNS / connect / auth attempt could touch POC_HOST.
    """
    missing_key = f"/tmp/deploy_pubkey_missing_key_{uuid.uuid4().hex[:8]}.pub"
    # Defensive: the path must not exist (the random suffix ensures it,
    # but assert it explicitly in case TMPDIR weirdness ever bites).
    assert not Path(missing_key).exists()

    rc, payload = _run_deploy_pubkey(POC_HOST, "--key", missing_key)

    assert rc == EXIT_KEY_FILE_MISSING
    assert payload["data"]["failure_class"] == "key_file_missing"
    assert payload["data"]["ssh_copy_id_argv"] == [], (
        "preflight must short-circuit before any subprocess spawn"
    )
    assert payload["data"]["elapsed_s"] == 0.0
    # And the human hint points at the missing path.
    assert "public key not found" in payload["stderr"] or "doesn't look like" in payload["stderr"]


def test_missing_key_never_touches_network() -> None:
    """Belt-and-suspenders: even when --connect-timeout is set, the
    preflight refusal must fire fast (< 1 s) — proving the refusal
    happens before any network operation."""
    missing_key = f"/tmp/deploy_pubkey_safety_{uuid.uuid4().hex[:8]}.pub"
    rc, payload = _run_deploy_pubkey(
        POC_HOST, "--key", missing_key, "--connect-timeout=2"
    )
    assert rc == EXIT_KEY_FILE_MISSING
    assert payload["data"]["elapsed_s"] == 0.0
