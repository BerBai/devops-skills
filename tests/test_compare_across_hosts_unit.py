"""Unit tests for remote-debug/scripts/compare_across_hosts.py v1.0.

The script is exercised through its in-process API (``_unified``,
``_fetch_cell``, ``_assemble_comparison``, ``_build_packages_comparison``,
``main``) with ``subprocess.run`` mocked at the module level. The
integration counterpart lives in
``tests/test_compare_across_hosts_integration.py`` (marker: ``live_ssh``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "remote-debug" / "skills" / "remote-debug" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import compare_across_hosts as cah  # noqa: E402, I001

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124
NO_PKG_EXIT = 99


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _envelope_json(
    success: bool = True,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    data: dict | None = None,
) -> str:
    return json.dumps(
        {
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "data": data or {},
        }
    )


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _mock_responses(monkeypatch: pytest.MonkeyPatch, by_argv3: dict[str, str]) -> None:
    """Patch subprocess.run to dispatch based on the remote command (argv[3])."""

    def fake_run(argv, **_kwargs):
        cmd = argv[3]
        payload = by_argv3.get(cmd)
        if payload is None:
            # Fallback: empty success envelope so we don't hide bad keys
            payload = _envelope_json(exit_code=0, stdout="")
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(cah.subprocess, "run", fake_run)


def _mock_per_host(
    monkeypatch: pytest.MonkeyPatch, by_alias: dict[str, str]
) -> None:
    """Patch subprocess.run to dispatch based on the host alias (argv[2])."""

    def fake_run(argv, **_kwargs):
        alias = argv[2]
        payload = by_alias.get(alias)
        if payload is None:
            payload = _envelope_json(exit_code=0, stdout="")
        # Try to mirror remote exit_code into subprocess exit_code so the
        # script's success-check logic (which keys off envelope.exit_code)
        # sees the right thing.
        rc = json.loads(payload)["exit_code"]
        return _completed(rc, stdout=payload, stderr="")

    monkeypatch.setattr(cah.subprocess, "run", fake_run)


# -----------------------------------------------------------------------------
# _unified
# -----------------------------------------------------------------------------


def test_unified_identical_returns_empty() -> None:
    assert cah._unified("a\nb\n", "a\nb\n", "x", "y", 3) == ""


def test_unified_one_change_emits_diff() -> None:
    diff = cah._unified("a\nb\n", "a\nB\n", "host1:f", "host2:f", 3)
    assert diff != ""
    assert "host1:f" in diff
    assert "host2:f" in diff
    assert "-b" in diff
    assert "+B" in diff


def test_unified_empty_inputs() -> None:
    assert cah._unified("", "", "x", "y", 3) == ""


# -----------------------------------------------------------------------------
# _cmd_cat_file shell-quotes
# -----------------------------------------------------------------------------


def test_cmd_cat_file_quotes_special_chars() -> None:
    cmd = cah._cmd_cat_file("/etc/foo; rm -rf /")
    assert cmd.startswith("cat ")
    assert "'/etc/foo; rm -rf /'" in cmd


# -----------------------------------------------------------------------------
# _fetch_cell
# -----------------------------------------------------------------------------


def test_fetch_cell_success(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(success=True, exit_code=0, stdout="contents\n")
    monkeypatch.setattr(
        cah.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )
    content, info = cah._fetch_cell(Path("/x"), "pai", "cat /a", 8)
    assert content == "contents\n"
    assert info["remote_exit_code"] == 0
    assert info["failure_class"] is None


def test_fetch_cell_remote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=False, exit_code=1,
        stderr="cat: /missing: No such file",
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        cah.subprocess, "run",
        lambda *_a, **_k: _completed(1, stdout=payload, stderr=""),
    )
    content, info = cah._fetch_cell(Path("/x"), "pai", "cat /missing", 8)
    assert content is None
    assert info["remote_exit_code"] == EXIT_FAIL
    assert info["failure_class"] == "remote_error"
    assert "No such file" in info["remote_stderr"]


def test_fetch_cell_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=False, exit_code=255,
        data={"failure_class": "auth"},
    )
    monkeypatch.setattr(
        cah.subprocess, "run",
        lambda *_a, **_k: _completed(255, stdout=payload, stderr=""),
    )
    content, info = cah._fetch_cell(Path("/x"), "pai", "cat /a", 8)
    assert content is None
    assert info["failure_class"] == "auth"


def test_fetch_cell_handles_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(cah.subprocess, "run", raise_timeout)
    content, info = cah._fetch_cell(Path("/x"), "pai", "cat /a", 8)
    assert content is None
    assert info["failure_class"] == "timeout"


# -----------------------------------------------------------------------------
# _fetch_packages_cell: marker parsing
# -----------------------------------------------------------------------------


def test_fetch_packages_dpkg_extracts_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=True, exit_code=0,
        stdout="__PKG_MANAGER=dpkg\nbash=5.1\ncoreutils=8.32\n",
    )
    monkeypatch.setattr(
        cah.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )
    content, manager, _ = cah._fetch_packages_cell(Path("/x"), "pai", 8)
    assert manager == "dpkg"
    assert content == "bash=5.1\ncoreutils=8.32\n"


def test_fetch_packages_rpm_extracts_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=True, exit_code=0,
        stdout="__PKG_MANAGER=rpm\nbash=5.1\nglibc=2.34\n",
    )
    monkeypatch.setattr(
        cah.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )
    content, manager, _ = cah._fetch_packages_cell(Path("/x"), "pai", 8)
    assert manager == "rpm"
    assert content == "bash=5.1\nglibc=2.34\n"


def test_fetch_packages_no_manager_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=False, exit_code=NO_PKG_EXIT,
        stderr="__PKG_MANAGER=none",
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        cah.subprocess, "run",
        lambda *_a, **_k: _completed(NO_PKG_EXIT, stdout=payload, stderr=""),
    )
    content, manager, info = cah._fetch_packages_cell(Path("/x"), "pai", 8)
    assert content is None
    assert manager is None
    assert info["pkg_manager"] == "none"


# -----------------------------------------------------------------------------
# _build_file_comparison
# -----------------------------------------------------------------------------


def test_build_file_comparison_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="alpha.example.com\n"),
        "local": _envelope_json(exit_code=0, stdout="alpha.example.com\n"),
    })
    cell = cah._build_file_comparison(
        Path("/x"), "pai", "local", "/etc/hostname", 8, 3
    )
    assert cell["kind"] == "file"
    assert cell["target"] == "/etc/hostname"
    assert cell["differs"] is False
    assert cell["unified_diff"] == ""
    assert "error" not in cell


def test_build_file_comparison_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="alpha\n"),
        "local": _envelope_json(exit_code=0, stdout="bravo\n"),
    })
    cell = cah._build_file_comparison(
        Path("/x"), "pai", "local", "/etc/hostname", 8, 3
    )
    assert cell["differs"] is True
    assert "pai:/etc/hostname" in cell["unified_diff"]
    assert "local:/etc/hostname" in cell["unified_diff"]


def test_build_file_comparison_baseline_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(
            success=False, exit_code=1,
            stderr="cat: /missing: No such file",
            data={"failure_class": "remote_error"},
        ),
        "local": _envelope_json(exit_code=0, stdout="bravo\n"),
    })
    cell = cah._build_file_comparison(
        Path("/x"), "pai", "local", "/missing", 8, 3
    )
    assert cell["differs"] is False
    assert "error" in cell
    assert cell["error"]["side"] == "baseline"
    assert "No such file" in cell["error"]["remote_stderr"]


def test_build_file_comparison_other_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="alpha\n"),
        "local": _envelope_json(
            success=False, exit_code=1,
            stderr="cat: /missing: No such file",
            data={"failure_class": "remote_error"},
        ),
    })
    cell = cah._build_file_comparison(
        Path("/x"), "pai", "local", "/missing", 8, 3
    )
    assert cell["error"]["side"] == "other"


def test_build_file_comparison_both_error(monkeypatch: pytest.MonkeyPatch) -> None:
    err_payload = _envelope_json(
        success=False, exit_code=1, stderr="err",
        data={"failure_class": "remote_error"},
    )
    _mock_per_host(monkeypatch, {"pai": err_payload, "local": err_payload})
    cell = cah._build_file_comparison(
        Path("/x"), "pai", "local", "/missing", 8, 3
    )
    assert cell["error"]["side"] == "both"


# -----------------------------------------------------------------------------
# _build_command_comparison
# -----------------------------------------------------------------------------


def test_build_command_comparison_same(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="Linux\n"),
        "local": _envelope_json(exit_code=0, stdout="Linux\n"),
    })
    cell = cah._build_command_comparison(
        Path("/x"), "pai", "local", "uname -s", 8, 3
    )
    assert cell["kind"] == "command"
    assert cell["target"] == "uname -s"
    assert cell["differs"] is False


def test_build_command_comparison_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="Linux\n"),
        "local": _envelope_json(exit_code=0, stdout="Darwin\n"),
    })
    cell = cah._build_command_comparison(
        Path("/x"), "pai", "local", "uname -s", 8, 3
    )
    assert cell["differs"] is True
    assert "Linux" in cell["unified_diff"]
    assert "Darwin" in cell["unified_diff"]


# -----------------------------------------------------------------------------
# _build_packages_comparison
# -----------------------------------------------------------------------------


def test_build_packages_comparison_same_distro_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg_dpkg = _envelope_json(
        exit_code=0,
        stdout="__PKG_MANAGER=dpkg\nbash=5.1\ncoreutils=8.32\n",
    )
    _mock_per_host(monkeypatch, {"pai": pkg_dpkg, "local": pkg_dpkg})
    cell = cah._build_packages_comparison(Path("/x"), "pai", "local", 8, 3)
    assert cell["differs"] is False
    assert cell["baseline_pkg_manager"] == "dpkg"
    assert cell["other_pkg_manager"] == "dpkg"


def test_build_packages_comparison_same_distro_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(
            exit_code=0, stdout="__PKG_MANAGER=dpkg\nbash=5.1\n"
        ),
        "local": _envelope_json(
            exit_code=0, stdout="__PKG_MANAGER=dpkg\nbash=5.2\n"
        ),
    })
    cell = cah._build_packages_comparison(Path("/x"), "pai", "local", 8, 3)
    assert cell["differs"] is True
    assert "bash=5.1" in cell["unified_diff"]
    assert "bash=5.2" in cell["unified_diff"]


def test_build_packages_comparison_cross_distro_marks_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(
            exit_code=0, stdout="__PKG_MANAGER=dpkg\nbash=5.1\n"
        ),
        "local": _envelope_json(
            exit_code=0, stdout="__PKG_MANAGER=rpm\nbash=5.1\n"
        ),
    })
    cell = cah._build_packages_comparison(Path("/x"), "pai", "local", 8, 3)
    assert cell["differs"] is True
    assert "distro mismatch" in cell["unified_diff"]
    assert cell["baseline_pkg_manager"] == "dpkg"
    assert cell["other_pkg_manager"] == "rpm"


def test_build_packages_comparison_no_manager_one_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(
            exit_code=0, stdout="__PKG_MANAGER=dpkg\nbash=5.1\n"
        ),
        "local": _envelope_json(
            success=False, exit_code=NO_PKG_EXIT,
            stderr="__PKG_MANAGER=none",
            data={"failure_class": "remote_error"},
        ),
    })
    cell = cah._build_packages_comparison(Path("/x"), "pai", "local", 8, 3)
    assert "error" in cell
    assert cell["error"]["side"] == "other"


# -----------------------------------------------------------------------------
# _summarise
# -----------------------------------------------------------------------------


def test_summarise_counts() -> None:
    comparisons = [
        {"kind": "file", "differs": True},
        {"kind": "file", "differs": False},
        {"kind": "command", "differs": True},
        {"kind": "packages", "differs": False, "error": {"side": "both"}},
    ]
    summary = cah._summarise(comparisons)
    expected_total = 4
    expected_differs = 2
    expected_errors = 1
    assert summary["total"] == expected_total
    assert summary["differs_count"] == expected_differs
    assert summary["error_count"] == expected_errors
    assert summary["by_kind"]["file"] == 1
    assert summary["by_kind"]["command"] == 1
    assert summary["by_kind"].get("packages", 0) == 0


# -----------------------------------------------------------------------------
# ssh_execute.py discovery + precondition
# -----------------------------------------------------------------------------


def test_ssh_execute_path_discovery_finds_file() -> None:
    path = cah._ssh_execute_path()
    assert path.name == "ssh_execute.py"
    assert path.exists()


def test_main_precondition_when_ssh_execute_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cah, "_ssh_execute_path", lambda: Path("/nope/ssh_execute.py"))

    def fail(*_a, **_k):
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(cah.subprocess, "run", fail)
    rc = cah.main(["pai", "local", "--files", "/etc/hostname", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["failure_class"] == "precondition"


# -----------------------------------------------------------------------------
# main(): argv error paths
# -----------------------------------------------------------------------------


def test_main_rejects_single_alias(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cah.main(["pai", "--files", "/etc/hostname"])
    assert rc == EXIT_ARGS
    assert "at least two" in capsys.readouterr().err


def test_main_rejects_no_comparison_mode(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cah.main(["pai", "local"])
    assert rc == EXIT_ARGS
    assert "--files" in capsys.readouterr().err


# -----------------------------------------------------------------------------
# main(): happy paths
# -----------------------------------------------------------------------------


def test_main_files_identical_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="alpha\n"),
        "local": _envelope_json(exit_code=0, stdout="alpha\n"),
    })
    rc = cah.main([
        "pai", "local", "--files", "/etc/hostname", "--json",
    ])
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"]["summary"]["differs_count"] == 0
    assert parsed["data"]["summary"]["total"] == 1


def test_main_files_differs_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="alpha\n"),
        "local": _envelope_json(exit_code=0, stdout="bravo\n"),
    })
    rc = cah.main(["pai", "local", "--files", "/etc/hostname", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    assert parsed["data"]["summary"]["differs_count"] == 1
    assert parsed["data"]["comparisons"][0]["differs"] is True


def test_main_commands_with_two_others(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """3 aliases, 1 command -> 2 comparisons (baseline vs alias[1], vs alias[2])."""
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="Linux\n"),
        "h2": _envelope_json(exit_code=0, stdout="Linux\n"),
        "h3": _envelope_json(exit_code=0, stdout="Darwin\n"),
    })
    rc = cah.main([
        "pai", "h2", "h3", "--commands", "uname -s", "--json",
    ])
    assert rc == EXIT_FAIL  # h3 differs from baseline
    parsed = json.loads(capsys.readouterr().out)
    expected_total = 2
    assert parsed["data"]["summary"]["total"] == expected_total
    diffs = [c for c in parsed["data"]["comparisons"] if c["differs"]]
    assert len(diffs) == 1
    assert diffs[0]["other_host"] == "h3"


def test_main_packages_mode_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pkg_payload = _envelope_json(
        exit_code=0,
        stdout="__PKG_MANAGER=dpkg\nbash=5.1\n",
    )
    _mock_per_host(monkeypatch, {"pai": pkg_payload, "local": pkg_payload})
    rc = cah.main(["pai", "local", "--packages", "--json"])
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    cell = parsed["data"]["comparisons"][0]
    assert cell["kind"] == "packages"
    assert cell["baseline_pkg_manager"] == "dpkg"
    assert cell["other_pkg_manager"] == "dpkg"


def test_main_mixed_files_commands_packages(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All three modes at once -> N=3 comparisons for one (baseline,other) pair."""
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(
            exit_code=0,
            # Same stdout regardless of command; this is fine for the smoke
            # test since we only count cells, not validate diff contents.
            stdout="__PKG_MANAGER=dpkg\nbash=5.1\n",
        ),
        "local": _envelope_json(
            exit_code=0,
            stdout="__PKG_MANAGER=dpkg\nbash=5.1\n",
        ),
    })
    rc = cah.main([
        "pai", "local",
        "--files", "/etc/hostname",
        "--commands", "uname -s",
        "--packages",
        "--json",
    ])
    parsed = json.loads(capsys.readouterr().out)
    expected_total = 3
    assert parsed["data"]["summary"]["total"] == expected_total
    kinds = {c["kind"] for c in parsed["data"]["comparisons"]}
    assert kinds == {"file", "command", "packages"}
    # All payloads are identical, so all should mark differs=false (even
    # though the "command stdout" coincidentally equals the package list --
    # that's fine for an isolated smoke check).
    assert rc == EXIT_OK


def test_main_envelope_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="x\n"),
        "local": _envelope_json(exit_code=0, stdout="x\n"),
    })
    cah.main(["pai", "local", "--files", "/etc/hostname", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert set(parsed.keys()) >= {"success", "exit_code", "stdout", "stderr", "data"}
    data = parsed["data"]
    for key in ("baseline", "others", "comparisons", "summary"):
        assert key in data


def test_main_non_json_writes_human_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mock_per_host(monkeypatch, {
        "pai": _envelope_json(exit_code=0, stdout="alpha\n"),
        "local": _envelope_json(exit_code=0, stdout="bravo\n"),
    })
    rc = cah.main(["pai", "local", "--files", "/etc/hostname"])
    assert rc == EXIT_FAIL
    out = capsys.readouterr().out
    assert "file" in out
    assert "differs" in out
    assert "/etc/hostname" in out
