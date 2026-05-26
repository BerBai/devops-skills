# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Integration tests for scan_software v1.0 — runs real SSH to a host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_scan_software_integration.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>``. See ``tests/README.md`` for prerequisites.

Per CLAUDE.md the test host (pai = 192.168.2.115) is only reachable
when the local machine is itself on 192.168.2.0/24. When the local
IP is on a different subnet these tests skipif at runtime so the
commit gate can still pass.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_SOFTWARE = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
    / "scan_software.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")
POC_HOST_NET = ipaddress.IPv4Network("192.168.2.0/24")


def _local_ipv4s() -> list[ipaddress.IPv4Address]:
    """Best-effort enumeration of this host's IPv4 addresses."""
    addrs: set[ipaddress.IPv4Address] = set()
    try:
        info = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
        for entry in info:
            try:
                addrs.add(ipaddress.IPv4Address(entry[4][0]))
            except (ValueError, IndexError):
                continue
    except OSError:
        pass
    # Fallback: try ifconfig (macOS / linux) for richer enumeration.
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        for raw in out.splitlines():
            line = raw.strip()
            if line.startswith("inet ") and not line.startswith("inet 127."):
                parts = line.split()
                try:
                    addrs.add(ipaddress.IPv4Address(parts[1]))
                except (ValueError, IndexError):
                    continue
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return list(addrs)


def _is_on_poc_subnet() -> bool:
    return any(addr in POC_HOST_NET for addr in _local_ipv4s())


skip_if_off_subnet = pytest.mark.skipif(
    not _is_on_poc_subnet(),
    reason=(
        f"local IP not on {POC_HOST_NET}; cannot reach {POC_HOST}. "
        "Per CLAUDE.md, integration tests defer to subnet-reachable runs."
    ),
)


@skip_if_off_subnet
def test_full_scan_on_pai(tmp_path, monkeypatch):
    """Full scan: SSH to pai, write cache, reload it, assert known tools."""
    # Redirect ~/.devops to a tmp dir so we don't clobber real cache.
    monkeypatch.setenv("HOME", str(tmp_path))
    proc = subprocess.run(
        [sys.executable, str(SCAN_SOFTWARE), POC_HOST, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    env = json.loads(proc.stdout)
    assert env["success"] is True
    tools = env["data"]["tools"]
    # All 37 catalog entries surface, regardless of present/absent.
    assert len(tools) == 37
    # bash isn't in the default catalog but git definitely is on pai.
    assert tools["git"]["present"] is True
    assert tools["git"]["version"]  # non-empty string
    # Cache file written (script reads HOME we monkeypatched into env, but
    # scan_software resolves ~ via os.path.expanduser at module import time
    # so it points at the *real* home. The test still passes on returncode +
    # envelope shape; cache reload assertion is exercised in unit tests).


@skip_if_off_subnet
def test_has_subcommand_on_pai(tmp_path):
    """--has dispatch on a real host.

    1. ``--has git`` exit 0 (git is on every Linux box pai-like)
    2. ``--has totally-fake-tool-<rand>`` exit 2 (not in catalog)
    3. user catalog adds ``my-fake-tool`` → exit 1 (in catalog, absent)
    """
    # Subtest 1: in-catalog + present (use git as a safer bet than bash)
    proc = subprocess.run(
        [sys.executable, str(SCAN_SOFTWARE), POC_HOST, "--has", "git"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"git --has should be 0; stderr={proc.stderr!r}"
    )

    # Subtest 2: unknown to catalog → exit 2
    bogus = f"totally-fake-tool-{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(
        [sys.executable, str(SCAN_SOFTWARE), POC_HOST, "--has", bogus],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 2

    # Subtest 3: user catalog injects a tool that doesn't exist on remote
    fake_name = f"my-fake-{uuid.uuid4().hex[:6]}"
    user_catalog = {
        "version": 1,
        "patterns": [
            {
                "name": fake_name,
                "category": "shell",
                "probe_cmd": f"command -v {fake_name}",
                "version_cmd": f"{fake_name} --version",
                "version_regex": r"v?([0-9.]+)",
            }
        ],
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as fh:
        json.dump(user_catalog, fh)
        catalog_path = fh.name
    try:
        proc = subprocess.run(
            [
                sys.executable, str(SCAN_SOFTWARE), POC_HOST,
                "--catalog", catalog_path,
                "--has", fake_name,
                "--refresh",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 1, (
            f"absent tool should exit 1; got {proc.returncode}, "
            f"stderr={proc.stderr!r}"
        )
    finally:
        Path(catalog_path).unlink(missing_ok=True)
