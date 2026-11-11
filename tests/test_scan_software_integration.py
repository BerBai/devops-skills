# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Integration tests for scan_software v1.0 — runs real SSH to a host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_scan_software_integration.py -v -m live_ssh

The target host comes from ``POC_HOST`` (no committed default). See
``tests/README.md`` for prerequisites.

The catalog size is *read from the shipped script* rather than pinned to a
literal: the suite used to assert "exactly 37 tools", so adding one catalog
entry turned a healthy scan into a failure (GAP-002). What matters is that
every entry the script knows about comes back, and that a real tool carries a
real version.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import live_support  # noqa: E402
import pytest
from live_support import require_live_ssh  # noqa: E402

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
POC_HOST = live_support.poc_host()

#: The script's own exit table (its docstring: 0 ok, 1 generic, 2 --has on a
#: tool that is not in the catalog). Kept as literals with names because
#: ssh-guarded has no shared ``exit_codes`` module to import -- the numbers are
#: part of *its* per-script namespace (75-79), not the ssh-core table.
EXIT_OK = 0
EXIT_HAS_ABSENT = 1
EXIT_HAS_NOT_IN_CATALOG = 2


def _catalog_names() -> list[str]:
    """Return the names in the script's *default* catalog.

    Loaded by path so the test never hardcodes a count or a spelling: the
    script is the authority on what it scans, and a new entry must not require
    editing an assertion.
    """
    spec = importlib.util.spec_from_file_location(
        "scan_software_under_test", SCAN_SOFTWARE
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        pytest.fail(f"cannot load {SCAN_SOFTWARE} to read its catalog")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [entry["name"] for entry in module._DEFAULT_CATALOG]


def _isolated_home(monkeypatch, tmp_path: Path) -> dict[str, str]:
    """Point the child process's ``~`` at ``tmp_path`` and return the env.

    ``scan_software`` resolves ``~/.devops`` with ``os.path.expanduser`` at
    import time, so the *process environment* is what matters. Setting only
    ``HOME`` was not enough on Windows (``expanduser`` prefers ``USERPROFILE``),
    so the scan kept writing to and reading from the developer's real cache —
    including the fake tool an earlier ``--has`` subtest had injected, which is
    how a leftover entry turned this assertion red for reasons unrelated to the
    host. Both variables are set, and the child gets an explicit env so the
    redirect cannot silently stop working again.
    """
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    env = dict(os.environ)
    env["HOME"] = home
    env["USERPROFILE"] = home
    return env


def test_full_scan_on_pai(tmp_path, monkeypatch):
    """Full scan: SSH to the host, isolated cache, assert the catalog surfaces."""
    catalog = _catalog_names()
    assert len(catalog) >= 30, (
        f"the shipped catalog shrank to {len(catalog)} entries; the live scan "
        "coverage assumes a broad tool list"
    )

    env_for_child = _isolated_home(monkeypatch, tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCAN_SOFTWARE), POC_HOST, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=env_for_child,
    )
    assert proc.returncode == EXIT_OK, (
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    env = json.loads(proc.stdout)
    assert env["success"] is True
    tools = env["data"]["tools"]
    # Every catalog entry surfaces, regardless of present/absent -- compared to
    # the script's own list, so adding an entry cannot break this assertion.
    missing = sorted(set(catalog) - set(tools))
    assert not missing, f"scan dropped catalog entries: {missing}"
    assert len(tools) == len(catalog), (
        f"envelope has {len(tools)} tools, catalog has {len(catalog)}: "
        f"extra={sorted(set(tools) - set(catalog))}"
    )
    # git is in the default catalog and on any Linux host this suite talks to.
    assert tools["git"]["present"] is True
    assert tools["git"]["version"]  # non-empty string
    # The scan really wrote its cache under the isolated home.
    cache_file = tmp_path / ".devops" / "cache" / str(POC_HOST) / "software.json"
    assert cache_file.is_file(), (
        f"no cache under the isolated home ({cache_file}); the redirect is "
        "not reaching the child process"
    )


def test_has_subcommand_on_pai(tmp_path, monkeypatch):
    """--has dispatch on a real host.

    1. ``--has git`` exit 0 (git is on every Linux box pai-like)
    2. ``--has totally-fake-tool-<rand>`` exit 2 (not in catalog)
    3. user catalog adds ``my-fake-tool`` → exit 1 (in catalog, absent)

    Every child gets the isolated home from :func:`_isolated_home`: the user
    catalog subtest writes a cache entry for a tool that does not exist
    anywhere, and without the redirect it landed in the developer's real
    ``~/.devops/cache`` and poisoned later scans.
    """
    env_for_child = _isolated_home(monkeypatch, tmp_path)

    # Subtest 1: in-catalog + present (use git as a safer bet than bash)
    proc = subprocess.run(
        [sys.executable, str(SCAN_SOFTWARE), POC_HOST, "--has", "git"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=env_for_child,
    )
    assert proc.returncode == EXIT_OK, (
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
        encoding="utf-8",
        errors="replace",
        env=env_for_child,
    )
    assert proc.returncode == EXIT_HAS_NOT_IN_CATALOG

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
            encoding="utf-8",
            errors="replace",
            env=env_for_child,
        )
        assert proc.returncode == EXIT_HAS_ABSENT, (
            f"absent tool should exit {EXIT_HAS_ABSENT}; got {proc.returncode}, "
            f"stderr={proc.stderr!r}"
        )
    finally:
        Path(catalog_path).unlink(missing_ok=True)
