# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Unit tests for scan_software v1.0.

Every test mocks ``subprocess.run`` at the module-level call site
(``scan_software.subprocess.run``) so no real ssh is ever invoked.
``_DEVOPS_HOME`` / ``_CACHE_DIR`` / ``_USER_CATALOG_DEFAULT`` are
re-pointed under ``tmp_path`` via ``monkeypatch`` so the real
``~/.devops`` is never touched.

Test groups (per design.md §12 D10 — 26 cases):

- Default catalog × 5: help / 33-entry total / category counts /
  default no-user-catalog / per-entry version_regex parametrized
- Catalog override × 3: user override-by-name / default-path missing
  silent / --catalog missing → 75 / non-JSON → 75
- Cache I/O × 4: atomic tempfile+replace / read-missing returns None /
  corrupt → 77 then rescan / write OSError → 76
- TTL × 3: fresh skip SSH / stale auto-rescan / --no-refresh-stale
- Refresh × 1: --refresh ignores fresh
- --has × 3: present exit 0 / absent exit 1 / unknown-tool exit 2
- --name × 2: cache hit single record / cache miss triggers scan
- Remote × 3: batch ssh fail → 78 / ssh timeout → 124 / NDJSON line
  corrupt → 79 + partial cache preserved
- Xilinx × 1: /opt/Xilinx/Vivado/2023.2 → version "2023.2"
- Output × 1: non-json summary table grouped by category
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import scan_software as ss  # noqa: E402

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect ~/.devops to tmp_path so tests never touch the real one."""
    devops = tmp_path / ".devops"
    cache = devops / "cache"
    catalog = devops / "software_catalog.json"
    devops.mkdir()
    cache.mkdir()
    monkeypatch.setattr(ss, "_DEVOPS_HOME", devops)
    monkeypatch.setattr(ss, "_CACHE_DIR", cache)
    monkeypatch.setattr(ss, "_USER_CATALOG_DEFAULT", catalog)
    return SimpleNamespace(home=devops, cache=cache, catalog=catalog, tmp=tmp_path)


def _ssh_envelope_stdout(ndjson_text: str, *, exit_code: int = 0, stderr: str = "") -> str:
    """Build the envelope that ssh_execute.py --json would emit."""
    return json.dumps(
        {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": ndjson_text,
            "stderr": stderr,
            "data": {},
        }
    )


def _make_completed(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


def _all_present_ndjson(version_raw: str = "1.0.0") -> str:
    """Return NDJSON with every catalog entry present.

    Xilinx entries get a path-shaped version_raw so the slicer can pull
    the trailing version segment.
    """
    lines = []
    for entry in ss._DEFAULT_CATALOG:
        name = entry["name"]
        if entry.get("scan_method") == "xilinx_dir":
            sub = entry["xilinx_subdir"]
            lines.append(
                json.dumps(
                    {
                        "name": name,
                        "present": True,
                        "version_raw": f"/opt/Xilinx/{sub}/2023.2",
                    }
                )
            )
        else:
            # Match the version_regex with a generic version string. Use
            # the canonical "<tool> version X" prefix for tools that
            # anchor on it; the fallback regexes accept bare numerics.
            raw = f"{name} version {version_raw}"
            lines.append(
                json.dumps({"name": name, "present": True, "version_raw": raw})
            )
    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# Default catalog (5)
# -----------------------------------------------------------------------------


def test_help_smoke():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "scan_software.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0
    out = proc.stdout
    for flag in ("--refresh", "--no-refresh-stale", "--name", "--has", "--catalog", "--json"):
        assert flag in out


def test_default_catalog_size():
    # 11 lang + 5 build + 2 gpu + 5 containers + 6 iac + 5 shell + 3 xilinx = 37.
    # (PRD prose said "33" but 11+5+2+5+6+5+3 = 37 — the implementation
    # carries the correct arithmetic.)
    assert len(ss._DEFAULT_CATALOG) == 37
    assert len(ss._DEFAULT_BY_NAME) == 37


def test_default_catalog_category_counts():
    counts: dict[str, int] = {}
    for entry in ss._DEFAULT_CATALOG:
        counts[entry["category"]] = counts.get(entry["category"], 0) + 1
    assert counts == {
        "lang": 11,
        "build": 5,
        "gpu": 2,
        "containers": 5,
        "iac": 6,
        "shell": 5,
        "xilinx": 3,
    }


def test_default_catalog_schema_valid():
    """Every entry passes _validate_entry without raising."""
    for entry in ss._DEFAULT_CATALOG:
        ss._validate_entry(entry)


@pytest.mark.parametrize(
    "name,version_raw,expected",
    [
        ("python", "Python 3.11.4", "3.11.4"),
        ("python3", "Python 3.11.4", "3.11.4"),
        ("pip", "pip 23.2.1 from /usr/local/lib", "23.2.1"),
        ("conda", "conda 23.5.0", "23.5.0"),
        ("nodejs", "v18.17.0", "18.17.0"),
        ("npm", "9.6.7", "9.6.7"),
        ("java", 'openjdk version "17.0.8"', "17.0.8"),
        ("mvn", "Apache Maven 3.9.4", "3.9.4"),
        ("gradle", "Gradle 8.2.1", "8.2.1"),
        ("gcc", "gcc (GCC) 11.4.0", "11.4.0"),
        ("clang", "clang version 14.0.6", "14.0.6"),
        ("cmake", "cmake version 3.27.4", "3.27.4"),
        ("make", "GNU Make 4.3", "4.3"),
        ("cuda", "nvcc: NVIDIA (R) Cuda compiler driver Cuda release 12.2, V12.2.91", "12.2"),
        ("docker", "Docker version 24.0.5, build abcdef", "24.0.5"),
        ("podman", "podman version 4.6.1", "4.6.1"),
        ("kubectl", "Client Version: v1.28.4", "1.28.4"),
        ("terraform", "Terraform v1.5.7", "1.5.7"),
        ("tofu", "OpenTofu v1.6.0", "1.6.0"),
        ("argocd", "argocd: v2.8.4+abcdef", "2.8.4"),
        ("git", "git version 2.42.0", "2.42.0"),
        ("jq", "jq-1.6", "1.6"),
        ("rsync", "rsync  version 3.2.7  protocol version 31", "3.2.7"),
        ("tmux", "tmux 3.3a", "3.3"),
    ],
)
def test_version_regex_parametrized(name, version_raw, expected):
    catalog = {name: ss._DEFAULT_BY_NAME[name]}
    assert ss._extract_version(name, version_raw, catalog) == expected


# -----------------------------------------------------------------------------
# Catalog override (3 — collapses into a few asserts each)
# -----------------------------------------------------------------------------


def test_user_catalog_override_by_name(isolated_home):
    payload = [
        {
            "name": "kubectl",
            "category": "containers",
            "probe_cmd": "true",
            "version_cmd": "echo kubectl 99.99.99",
            "version_regex": r"kubectl\s+([0-9.]+)",
        }
    ]
    isolated_home.catalog.write_text(json.dumps(payload))
    catalog = ss._load_catalog(None)
    assert catalog["kubectl"]["version_regex"] == r"kubectl\s+([0-9.]+)"
    # Other default entries still present
    assert "docker" in catalog
    assert "vivado" in catalog


def test_user_catalog_default_missing_is_silent(isolated_home):
    # No user catalog file written; default catalog is the result.
    catalog = ss._load_catalog(None)
    assert len(catalog) == 37


def test_explicit_catalog_missing_raises(isolated_home):
    with pytest.raises(ss.CatalogLoadError):
        ss._load_catalog(str(isolated_home.tmp / "no-such-catalog.json"))


def test_user_catalog_invalid_json_raises(isolated_home):
    isolated_home.catalog.write_text("{ not valid json ")
    with pytest.raises(ss.CatalogLoadError):
        ss._load_catalog(None)


def test_user_catalog_versioned_shape(isolated_home):
    payload = {
        "version": 1,
        "patterns": [
            {
                "name": "ourtool",
                "category": "shell",
                "probe_cmd": "command -v ourtool",
                "version_cmd": "ourtool --version",
                "version_regex": r"v?([0-9.]+)",
            }
        ],
    }
    isolated_home.catalog.write_text(json.dumps(payload))
    catalog = ss._load_catalog(None)
    assert "ourtool" in catalog
    assert catalog["ourtool"]["category"] == "shell"


def test_user_catalog_missing_field_raises(isolated_home):
    payload = [{"name": "broken", "category": "shell"}]  # no probe_cmd etc.
    isolated_home.catalog.write_text(json.dumps(payload))
    with pytest.raises(ss.CatalogLoadError):
        ss._load_catalog(None)


# -----------------------------------------------------------------------------
# Cache I/O (4)
# -----------------------------------------------------------------------------


def test_cache_read_missing_returns_none(isolated_home):
    assert ss._cache_read("nonexistent-alias") is None


def test_cache_write_then_read_roundtrip(isolated_home):
    payload = {
        "version": 1,
        "scanned_at": "2026-05-25T07:42:00Z",
        "server": "pai",
        "tools": {"git": {"present": True, "version": "2.42.0",
                          "category": "shell", "scan_method": "command_v"}},
    }
    ss._cache_write("pai", payload)
    assert (isolated_home.cache / "pai" / "software.json").exists()
    loaded = ss._cache_read("pai")
    assert loaded == payload


def test_cache_write_is_atomic_via_tempfile(isolated_home):
    """Verify _cache_write uses .<pid>.tmp + os.replace flow."""
    payload = {"version": 1, "scanned_at": "2026-05-25T07:42:00Z",
               "server": "pai", "tools": {}}
    ss._cache_write("pai", payload)
    cache_dir = isolated_home.cache / "pai"
    # No leftover tmp file should remain
    leftovers = [p for p in cache_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert (cache_dir / "software.json").is_file()


def test_cache_read_corrupt_raises(isolated_home):
    alias_dir = isolated_home.cache / "pai"
    alias_dir.mkdir()
    (alias_dir / "software.json").write_text("{ broken")
    with pytest.raises(ss.CacheCorruptError):
        ss._cache_read("pai")


def test_cache_read_wrong_version_raises(isolated_home):
    alias_dir = isolated_home.cache / "pai"
    alias_dir.mkdir()
    (alias_dir / "software.json").write_text(json.dumps({"version": 2, "tools": {}}))
    with pytest.raises(ss.CacheCorruptError):
        ss._cache_read("pai")


def test_cache_write_oserror_propagates(isolated_home, monkeypatch):
    payload = {"version": 1, "scanned_at": "2026-05-25T07:42:00Z",
               "server": "pai", "tools": {}}

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(ss.os, "replace", boom)
    with pytest.raises(OSError):
        ss._cache_write("pai", payload)


# -----------------------------------------------------------------------------
# TTL (3)
# -----------------------------------------------------------------------------


def _ts_iso(seconds_ago: int) -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(seconds=seconds_ago)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_is_stale_fresh_returns_false():
    payload = {"scanned_at": _ts_iso(60)}
    assert ss._is_stale(payload) is False


def test_is_stale_after_ttl_returns_true():
    payload = {"scanned_at": _ts_iso(8 * 24 * 3600)}
    assert ss._is_stale(payload) is True


def test_is_stale_missing_field_returns_true():
    assert ss._is_stale({}) is True


def test_fresh_cache_skips_ssh(isolated_home, capsys):
    payload = {
        "version": 1,
        "scanned_at": _ts_iso(60),
        "server": "pai",
        "tools": {n: {"present": True, "version": "1.0", "category": e["category"],
                      "scan_method": e.get("scan_method", "command_v")}
                  for n, e in ss._DEFAULT_BY_NAME.items()},
    }
    ss._cache_write("pai", payload)

    with patch.object(ss.subprocess, "run") as mock_run:
        rc = ss.main(["pai", "--json"])
    assert rc == 0
    assert mock_run.call_count == 0  # cache hit means NO ssh call
    out = capsys.readouterr()
    env = json.loads(out.out)
    assert env["success"] is True
    assert env["data"]["cache_stale"] is False


def test_no_refresh_stale_keeps_stale(isolated_home, capsys):
    """Stale cache + --no-refresh-stale → reuse stale, exit 0, warn."""
    payload = {
        "version": 1,
        "scanned_at": _ts_iso(8 * 24 * 3600),
        "server": "pai",
        "tools": {n: {"present": True, "version": "1.0", "category": e["category"],
                      "scan_method": e.get("scan_method", "command_v")}
                  for n, e in ss._DEFAULT_BY_NAME.items()},
    }
    ss._cache_write("pai", payload)

    with patch.object(ss.subprocess, "run") as mock_run:
        rc = ss.main(["pai", "--no-refresh-stale", "--json"])
    assert rc == 0
    assert mock_run.call_count == 0
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["cache_stale"] is True


def test_refresh_ignores_fresh_cache(isolated_home, capsys):
    payload = {
        "version": 1,
        "scanned_at": _ts_iso(60),
        "server": "pai",
        "tools": {},
    }
    ss._cache_write("pai", payload)

    with patch.object(ss.subprocess, "run") as mock_run:
        mock_run.return_value = _make_completed(_ssh_envelope_stdout(_all_present_ndjson()))
        rc = ss.main(["pai", "--refresh", "--json"])
    assert rc == 0
    # --refresh forces a remote call even though cache was fresh
    assert mock_run.call_count == 1


# -----------------------------------------------------------------------------
# --has (3)
# -----------------------------------------------------------------------------


def test_has_present_exit_0(isolated_home, capsys):
    payload = {
        "version": 1,
        "scanned_at": _ts_iso(60),
        "server": "pai",
        "tools": {"bash": {"present": True, "version": "5.2.0",
                            "category": "shell", "scan_method": "command_v"}},
    }
    # Inject a custom catalog entry for "bash" via tmp catalog file so
    # _dispatch_has accepts the name.
    payload["tools"]["git"] = {"present": True, "version": "2.42.0",
                                "category": "shell", "scan_method": "command_v"}
    ss._cache_write("pai", payload)
    rc = ss.main(["pai", "--has", "git", "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["success"] is True


def test_has_absent_exit_1(isolated_home, capsys):
    payload = {
        "version": 1,
        "scanned_at": _ts_iso(60),
        "server": "pai",
        "tools": {"docker": {"present": False, "category": "containers",
                              "scan_method": "command_v"}},
    }
    ss._cache_write("pai", payload)
    rc = ss.main(["pai", "--has", "docker", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["success"] is False


def test_has_unknown_tool_exit_2(isolated_home, capsys):
    rc = ss.main(["pai", "--has", "no-such-tool-xyz", "--json"])
    assert rc == 2
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["failure_class"] == "unknown_tool"


# -----------------------------------------------------------------------------
# --name (2)
# -----------------------------------------------------------------------------


def test_name_cache_hit_returns_record(isolated_home, capsys):
    payload = {
        "version": 1,
        "scanned_at": _ts_iso(60),
        "server": "pai",
        "tools": {"kubectl": {"present": True, "version": "1.28.4",
                                "version_raw": "Client Version: v1.28.4",
                                "category": "containers",
                                "scan_method": "command_v"}},
    }
    ss._cache_write("pai", payload)
    with patch.object(ss.subprocess, "run") as mock_run:
        rc = ss.main(["pai", "--name", "kubectl", "--json"])
    assert rc == 0
    assert mock_run.call_count == 0  # cache hit
    env = json.loads(capsys.readouterr().out)
    rec = env["data"]["tools"]["kubectl"]
    assert rec["present"] is True
    assert rec["version"] == "1.28.4"


def test_name_cache_miss_triggers_scan(isolated_home, capsys):
    # No cache → must scan
    with patch.object(ss.subprocess, "run") as mock_run:
        mock_run.return_value = _make_completed(_ssh_envelope_stdout(_all_present_ndjson()))
        rc = ss.main(["pai", "--name", "kubectl", "--json"])
    assert rc == 0
    assert mock_run.call_count == 1
    env = json.loads(capsys.readouterr().out)
    assert "kubectl" in env["data"]["tools"]


def test_name_unknown_tool_exit_1(isolated_home, capsys):
    rc = ss.main(["pai", "--name", "no-such-tool", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["failure_class"] == "unknown_tool"


# -----------------------------------------------------------------------------
# Remote (3)
# -----------------------------------------------------------------------------


def test_ssh_batch_failed_exit_78(isolated_home, capsys):
    with patch.object(ss.subprocess, "run") as mock_run:
        mock_run.return_value = _make_completed(
            _ssh_envelope_stdout("", exit_code=255, stderr="Connection refused"),
            returncode=255,
        )
        rc = ss.main(["pai", "--json"])
    assert rc == 78
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["failure_class"] == "ssh_batch_failed"


def test_ssh_timeout_exit_124(isolated_home, capsys):
    with patch.object(ss.subprocess, "run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=70)
        rc = ss.main(["pai", "--json"])
    assert rc == 124
    env = json.loads(capsys.readouterr().out)
    # 124 path uses "timeout" failure_class label in data
    assert env["exit_code"] == 124


def test_ndjson_corrupt_line_partial_cache_exit_79(isolated_home, capsys):
    # Build NDJSON with one broken line followed by valid ones.
    ndjson_lines: list[str] = ["{ broken json"]
    for entry in ss._DEFAULT_CATALOG:
        name = entry["name"]
        if entry.get("scan_method") == "xilinx_dir":
            sub = entry["xilinx_subdir"]
            ndjson_lines.append(
                json.dumps({"name": name, "present": True,
                            "version_raw": f"/opt/Xilinx/{sub}/2023.2"})
            )
        else:
            ndjson_lines.append(
                json.dumps({"name": name, "present": True,
                            "version_raw": f"{name} version 1.0.0"})
            )
    bad_ndjson = "\n".join(ndjson_lines) + "\n"

    with patch.object(ss.subprocess, "run") as mock_run:
        mock_run.return_value = _make_completed(_ssh_envelope_stdout(bad_ndjson))
        rc = ss.main(["pai", "--json"])
    assert rc == 79
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["failure_class"] == "tool_probe_corrupted"
    assert env["data"]["corrupted_lines"]
    # Partial cache must still be written
    cache_file = isolated_home.cache / "pai" / "software.json"
    assert cache_file.exists()


# -----------------------------------------------------------------------------
# Xilinx (1)
# -----------------------------------------------------------------------------


def test_xilinx_version_slice():
    catalog = {n: ss._DEFAULT_BY_NAME[n] for n in ("vivado", "vitis", "vivado_hls")}
    assert ss._extract_version("vivado", "/opt/Xilinx/Vivado/2023.2", catalog) == "2023.2"
    assert ss._extract_version("vitis", "/opt/Xilinx/Vitis/2024.1/", catalog) == "2024.1"
    assert ss._extract_version("vivado_hls", "/opt/Xilinx/Vivado_HLS/2020.1", catalog) == "2020.1"


# -----------------------------------------------------------------------------
# Output (1)
# -----------------------------------------------------------------------------


def test_human_summary_groups_by_category(isolated_home, capsys):
    payload = {
        "version": 1,
        "scanned_at": _ts_iso(60),
        "server": "pai",
        "tools": {n: {"present": True, "version": "1.0.0",
                      "category": e["category"],
                      "scan_method": e.get("scan_method", "command_v")}
                  for n, e in ss._DEFAULT_BY_NAME.items()},
    }
    ss._cache_write("pai", payload)
    rc = ss.main(["pai"])  # human mode (no --json)
    assert rc == 0
    out = capsys.readouterr().out
    # Each category header present
    for cat in ("lang", "build", "gpu", "containers", "iac", "shell", "xilinx"):
        assert f"[{cat}]" in out
    # 37 entries means a bunch of "+" lines
    assert out.count("\n  + ") == 37


# -----------------------------------------------------------------------------
# Catalog load_failed surfaced via main()
# -----------------------------------------------------------------------------


def test_main_catalog_load_failed_exit_75(isolated_home, capsys):
    rc = ss.main(["pai", "--catalog", str(isolated_home.tmp / "missing.json"), "--json"])
    assert rc == 75
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["failure_class"] == "catalog_load_failed"


# -----------------------------------------------------------------------------
# Remote shell builder smoke
# -----------------------------------------------------------------------------


def test_build_remote_script_contains_all_blocks():
    script = ss._build_remote_script(ss._DEFAULT_BY_NAME)
    # Non-Xilinx tools show up as `if command -v X` blocks
    assert "command -v kubectl" in script
    assert "command -v docker" in script
    # Xilinx tools dispatched via scan_xilinx
    assert "scan_xilinx 'Vivado'" in script
    assert "scan_xilinx 'Vitis'" in script
    assert "scan_xilinx 'Vivado_HLS'" in script


# -----------------------------------------------------------------------------
# Cache corrupt → auto rescan rewrites cache (success path, exit 0)
# -----------------------------------------------------------------------------


def test_corrupt_cache_triggers_rescan(isolated_home, capsys):
    alias_dir = isolated_home.cache / "pai"
    alias_dir.mkdir()
    (alias_dir / "software.json").write_text("{ corrupt")

    with patch.object(ss.subprocess, "run") as mock_run:
        mock_run.return_value = _make_completed(_ssh_envelope_stdout(_all_present_ndjson()))
        rc = ss.main(["pai", "--json"])
    assert rc == 0
    assert mock_run.call_count == 1
    # Cache file rewritten and now parseable
    reloaded = ss._cache_read("pai")
    assert reloaded is not None
    assert reloaded["version"] == 1
