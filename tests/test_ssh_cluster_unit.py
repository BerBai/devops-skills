"""Unit tests for ssh-core/scripts/ssh_cluster.py v1.0.

Mocks ``subprocess.run`` (the only external surface) and exercises
target resolution, the per-host worker, serial/parallel broadcast,
health-check phase, aggregation, and ``main()``'s end-to-end flow.

Integration counterpart lives in ``tests/test_ssh_cluster_integration.py``
(marker: ``live_ssh``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import ssh_cluster as sc  # noqa: E402, I001

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _ok_envelope(stdout: str = "hi", alias_hint: str = "h") -> str:
    """JSON string ssh_execute would print on a successful run."""
    return json.dumps({
        "success": True,
        "exit_code": 0,
        "stdout": stdout,
        "stderr": "",
        "data": {"alias": alias_hint, "failure_class": None},
    })


def _fail_envelope(failure_class: str = "remote_error", exit_code: int = 1) -> str:
    return json.dumps({
        "success": False,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": "boom",
        "data": {"failure_class": failure_class},
    })


# -----------------------------------------------------------------------------
# _parse_targets
# -----------------------------------------------------------------------------


def test_parse_targets_single() -> None:
    aliases, err = sc._parse_targets("pai", None, None)
    assert err is None
    assert aliases == ["pai"]


def test_parse_targets_multi_preserves_order() -> None:
    aliases, err = sc._parse_targets("pai,local,build", None, None)
    assert err is None
    assert aliases == ["pai", "local", "build"]


def test_parse_targets_dedup_preserves_first() -> None:
    aliases, err = sc._parse_targets("pai,local,pai,build,local", None, None)
    assert err is None
    assert aliases == ["pai", "local", "build"]


def test_parse_targets_whitespace_tolerant() -> None:
    aliases, err = sc._parse_targets("  pai , local  ,build  ", None, None)
    assert err is None
    assert aliases == ["pai", "local", "build"]


def test_parse_targets_empty_string_is_error() -> None:
    aliases, err = sc._parse_targets("", None, None)
    assert aliases is None
    assert err == "target_resolution_failed"


def test_parse_targets_only_whitespace_is_error() -> None:
    aliases, err = sc._parse_targets("   ,   ,  ", None, None)
    assert aliases is None
    assert err == "target_resolution_failed"


def test_parse_targets_none_is_error() -> None:
    aliases, err = sc._parse_targets(None, None, None)
    assert aliases is None
    assert err == "target_resolution_failed"


def test_parse_targets_tags_alone_is_error_at_target_level() -> None:
    # v1.0: tags arg accepted by argparse but resolution requires --hosts
    aliases, err = sc._parse_targets(None, "web", None)
    assert aliases is None
    assert err == "target_resolution_failed"


# -----------------------------------------------------------------------------
# _per_host_env
# -----------------------------------------------------------------------------


def test_per_host_env_has_eight_fields() -> None:
    env = sc._per_host_env(
        True, 0, "pai", elapsed_ms=42, stdout="x", stderr="y",
        failure_class=None, skipped=False,
    )
    expected_keys = {
        "alias", "success", "exit_code", "stdout", "stderr",
        "elapsed_ms", "failure_class", "skipped",
    }
    assert set(env.keys()) == expected_keys


def test_per_host_env_failure_class_passthrough() -> None:
    env = sc._per_host_env(
        False, 1, "pai", elapsed_ms=0, failure_class="auth",
    )
    assert env["failure_class"] == "auth"
    assert env["skipped"] is False


# -----------------------------------------------------------------------------
# _run_one: subprocess shell-out paths
# -----------------------------------------------------------------------------


def test_run_one_happy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ssh_exec = tmp_path / "ssh_execute.py"
    ssh_exec.write_text("# placeholder")
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["timeout"] = kw.get("timeout")
        return _completed(0, stdout=_ok_envelope("uptime line"))

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    env = sc._run_one(ssh_exec, "pai", "uptime", 30, 8)
    assert env["success"] is True
    assert env["alias"] == "pai"
    assert env["stdout"] == "uptime line"
    assert env["failure_class"] is None
    # argv should pass --timeout and --connect-timeout through
    assert "--timeout" in captured["argv"]
    assert "30" in captured["argv"]
    assert "--connect-timeout" in captured["argv"]
    assert "8" in captured["argv"]
    # Wall budget is timeout + connect + padding
    assert captured["timeout"] == 30 + 8 + sc.WORKER_TIMEOUT_PADDING


def test_run_one_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ssh_exec = tmp_path / "ssh_execute.py"
    ssh_exec.write_text("# placeholder")

    def raise_to(*args, **kw):
        raise subprocess.TimeoutExpired("ssh", 30)

    monkeypatch.setattr(sc.subprocess, "run", raise_to)
    env = sc._run_one(ssh_exec, "pai", "sleep 99", 30, 8)
    assert env["success"] is False
    assert env["exit_code"] == EXIT_TIMEOUT
    assert env["failure_class"] == "timeout"


def test_run_one_non_json_output_is_ssh_execute_broken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ssh_exec = tmp_path / "ssh_execute.py"
    ssh_exec.write_text("# placeholder")

    def fake_run(argv, **kw):
        return _completed(1, stdout="NOT JSON", stderr="meh")

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    env = sc._run_one(ssh_exec, "pai", "true", 30, 8)
    assert env["success"] is False
    assert env["failure_class"] == "ssh_execute_broken"
    assert env["stdout"] == "NOT JSON"


def test_run_one_ssh_execute_failure_propagates_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ssh_exec = tmp_path / "ssh_execute.py"
    ssh_exec.write_text("# placeholder")

    def fake_run(argv, **kw):
        return _completed(1, stdout=_fail_envelope("network"))

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    env = sc._run_one(ssh_exec, "pai", "true", 30, 8)
    assert env["success"] is False
    assert env["failure_class"] == "network"


# -----------------------------------------------------------------------------
# _broadcast: serial and parallel paths
# -----------------------------------------------------------------------------


def _patch_run_one(monkeypatch: pytest.MonkeyPatch, plan: dict[str, dict]):
    """Replace _run_one with a deterministic per-alias responder."""
    call_log: list[str] = []

    def fake_run_one(ssh_exec, alias, command, timeout, connect_timeout):
        call_log.append(alias)
        cfg = plan.get(alias, {"success": True})
        return sc._per_host_env(
            success=cfg.get("success", True),
            exit_code=cfg.get("exit_code", 0 if cfg.get("success", True) else 1),
            alias=alias,
            elapsed_ms=cfg.get("elapsed_ms", 10),
            stdout=cfg.get("stdout", "ok"),
            stderr=cfg.get("stderr", ""),
            failure_class=cfg.get("failure_class"),
        )

    monkeypatch.setattr(sc, "_run_one", fake_run_one)
    return call_log


def test_broadcast_max_workers_one_is_serial_preserving_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b", "c", "d"]
    call_log = _patch_run_one(monkeypatch, {})
    results = sc._broadcast(
        tmp_path / "ssh_execute.py", aliases, "uptime", 30, 8,
        max_workers=1, fail_fast=False,
    )
    assert list(results.keys()) == aliases
    assert call_log == aliases


def test_broadcast_parallel_runs_all_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b", "c", "d", "e", "f", "g", "h"]
    call_log = _patch_run_one(monkeypatch, {})
    results = sc._broadcast(
        tmp_path / "ssh_execute.py", aliases, "uptime", 30, 8,
        max_workers=4, fail_fast=False,
    )
    assert set(results.keys()) == set(aliases)
    assert sorted(call_log) == sorted(aliases)
    assert all(env["success"] for env in results.values())


def test_broadcast_serial_fail_fast_skips_remaining(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b", "c", "d"]
    plan = {"a": {"success": False, "failure_class": "remote_error"}}
    call_log = _patch_run_one(monkeypatch, plan)
    results = sc._broadcast(
        tmp_path / "ssh_execute.py", aliases, "uptime", 30, 8,
        max_workers=1, fail_fast=True,
    )
    # Only `a` was actually invoked; b/c/d were marked skipped without invoke
    assert call_log == ["a"]
    assert results["a"]["success"] is False
    for skip in ["b", "c", "d"]:
        assert results[skip]["failure_class"] == "skipped_fail_fast"
        assert results[skip]["skipped"] is True


def test_broadcast_all_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b"]
    _patch_run_one(monkeypatch, {})
    results = sc._broadcast(
        tmp_path / "ssh_execute.py", aliases, "uptime", 30, 8,
        max_workers=4, fail_fast=False,
    )
    assert all(env["success"] for env in results.values())


def test_broadcast_all_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b"]
    plan = {
        "a": {"success": False, "failure_class": "auth"},
        "b": {"success": False, "failure_class": "network"},
    }
    _patch_run_one(monkeypatch, plan)
    results = sc._broadcast(
        tmp_path / "ssh_execute.py", aliases, "uptime", 30, 8,
        max_workers=4, fail_fast=False,
    )
    assert not any(env["success"] for env in results.values())


def test_broadcast_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b"]
    plan = {"a": {"success": False, "failure_class": "network"}}
    _patch_run_one(monkeypatch, plan)
    results = sc._broadcast(
        tmp_path / "ssh_execute.py", aliases, "uptime", 30, 8,
        max_workers=4, fail_fast=False,
    )
    assert results["a"]["success"] is False
    assert results["b"]["success"] is True


# -----------------------------------------------------------------------------
# _health_check
# -----------------------------------------------------------------------------


def test_health_check_all_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b"]
    _patch_run_one(monkeypatch, {})
    alive, dead = sc._health_check(
        tmp_path / "ssh_execute.py", aliases, connect_timeout=8, max_workers=4,
    )
    assert alive == aliases
    assert dead == {}


def test_health_check_mixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b", "c"]
    plan = {"b": {"success": False, "failure_class": "network"}}
    _patch_run_one(monkeypatch, plan)
    alive, dead = sc._health_check(
        tmp_path / "ssh_execute.py", aliases, connect_timeout=8, max_workers=4,
    )
    assert alive == ["a", "c"]
    assert set(dead.keys()) == {"b"}
    assert dead["b"]["failure_class"] == "health_check_failed"
    assert dead["b"]["skipped"] is True


def test_health_check_all_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    aliases = ["a", "b"]
    plan = {
        "a": {"success": False, "failure_class": "auth"},
        "b": {"success": False, "failure_class": "network"},
    }
    _patch_run_one(monkeypatch, plan)
    alive, dead = sc._health_check(
        tmp_path / "ssh_execute.py", aliases, connect_timeout=8, max_workers=4,
    )
    assert alive == []
    assert set(dead.keys()) == {"a", "b"}


# -----------------------------------------------------------------------------
# _classify_top / _summarize
# -----------------------------------------------------------------------------


def test_classify_top_all_ok() -> None:
    results = {
        "a": sc._per_host_env(True, 0, "a", elapsed_ms=10),
        "b": sc._per_host_env(True, 0, "b", elapsed_ms=10),
    }
    success, fc = sc._classify_top(results)
    assert success is True
    assert fc is None


def test_classify_top_partial() -> None:
    results = {
        "a": sc._per_host_env(True, 0, "a", elapsed_ms=10),
        "b": sc._per_host_env(False, 1, "b", elapsed_ms=10),
    }
    success, fc = sc._classify_top(results)
    assert success is False
    assert fc == "partial_failure"


def test_classify_top_all_fail() -> None:
    results = {
        "a": sc._per_host_env(False, 1, "a", elapsed_ms=10),
        "b": sc._per_host_env(False, 1, "b", elapsed_ms=10),
    }
    success, fc = sc._classify_top(results)
    assert success is False
    assert fc == "all_hosts_failed"


def test_classify_top_empty_marks_target_failure() -> None:
    success, fc = sc._classify_top({})
    assert success is False
    assert fc == "target_resolution_failed"


def test_summarize_counts() -> None:
    results = {
        "a": sc._per_host_env(True, 0, "a", elapsed_ms=10),
        "b": sc._per_host_env(False, 1, "b", elapsed_ms=20),
        "c": sc._per_host_env(False, 1, "c", elapsed_ms=0, skipped=True),
    }
    summary = sc._summarize(results, started_at=time.monotonic() - 0.5)
    assert summary["total"] == 3  # noqa: PLR2004
    assert summary["ok"] == 1
    assert summary["fail"] == 1
    assert summary["skipped"] == 1
    assert summary["elapsed_ms"] >= 0


def test_summarize_zero_results() -> None:
    summary = sc._summarize({}, started_at=time.monotonic())
    assert summary["total"] == 0
    assert summary["ok"] == 0
    assert summary["fail"] == 0
    assert summary["skipped"] == 0


# -----------------------------------------------------------------------------
# main() end-to-end
# -----------------------------------------------------------------------------


def _ensure_ssh_execute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point _ssh_execute_path to a writable placeholder so existence check passes."""
    fake = tmp_path / "ssh_execute.py"
    fake.write_text("# placeholder")
    monkeypatch.setattr(sc, "_ssh_execute_path", lambda: fake)
    return fake


def test_main_argparse_missing_command_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        sc.main([])
    assert exc.value.code == EXIT_ARGS


def test_main_missing_hosts_emits_target_resolution_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    code = sc.main(["uptime", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "target_resolution_failed"


def test_main_tags_only_emits_failure_and_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    code = sc.main(["uptime", "--tags", "web", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "target_resolution_failed"


def test_main_ssh_execute_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sc, "_ssh_execute_path", lambda: tmp_path / "missing.py")
    code = sc.main(["uptime", "--hosts", "pai", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "ssh_execute_missing"


def test_main_happy_single_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    _patch_run_one(monkeypatch, {})
    code = sc.main(["uptime", "--hosts", "pai", "--json"])
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["data"]["failure_class"] is None
    assert "pai" in payload["data"]["results"]
    assert payload["data"]["summary"]["ok"] == 1


def test_main_happy_multi_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    _patch_run_one(monkeypatch, {})
    code = sc.main(["uptime", "--hosts", "a,b,c", "--max-workers", "4", "--json"])
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["data"]["results"].keys()) == {"a", "b", "c"}
    assert payload["data"]["summary"]["ok"] == 3  # noqa: PLR2004


def test_main_serial_preserves_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    _patch_run_one(monkeypatch, {})
    sc.main(["uptime", "--hosts", "x,y,z", "--max-workers", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert list(payload["data"]["results"].keys()) == ["x", "y", "z"]


def test_main_fail_fast_with_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    plan = {"a": {"success": False, "failure_class": "remote_error"}}
    _patch_run_one(monkeypatch, plan)
    code = sc.main([
        "uptime", "--hosts", "a,b,c", "--max-workers", "1",
        "--fail-fast", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_FAIL
    assert payload["data"]["failure_class"] == "all_hosts_failed"  # b/c skipped count as fail
    assert payload["data"]["results"]["b"]["failure_class"] == "skipped_fail_fast"
    assert payload["data"]["results"]["c"]["failure_class"] == "skipped_fail_fast"


def test_main_health_check_mixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    plan = {"b": {"success": False, "failure_class": "network"}}
    call_log = _patch_run_one(monkeypatch, plan)
    code = sc.main([
        "ls /", "--hosts", "a,b,c", "--health-check",
        "--max-workers", "4", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    # b fails health probe -> not invoked for the business command.
    # a and c get one probe call + one business call each (4 total invocations
    # for a, b, c is: a probe, b probe, c probe, then a cmd + c cmd = 5).
    expected_b_only_calls = call_log.count("b")
    assert expected_b_only_calls == 1, call_log  # probe only, never command
    expected_a_calls = call_log.count("a")
    assert expected_a_calls == 2, call_log  # noqa: PLR2004  # probe + command
    assert code == EXIT_FAIL  # because b failed
    assert payload["data"]["results"]["b"]["failure_class"] == "health_check_failed"
    assert payload["data"]["results"]["b"]["skipped"] is True


def test_main_no_health_check_means_no_extra_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    call_log = _patch_run_one(monkeypatch, {})
    sc.main(["uptime", "--hosts", "a,b,c", "--max-workers", "4", "--json"])
    # No --health-check => only N business calls
    assert sorted(call_log) == ["a", "b", "c"]


def test_main_json_envelope_full_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    _patch_run_one(monkeypatch, {})
    sc.main(["uptime", "--hosts", "pai", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert {"success", "exit_code", "stdout", "stderr", "data"} <= payload.keys()
    data_keys = {
        "command", "hosts", "summary", "results",
        "failure_class", "fail_fast", "health_check",
    }
    assert data_keys <= payload["data"].keys()
    summary_keys = {"total", "ok", "fail", "skipped", "elapsed_ms"}
    assert summary_keys <= payload["data"]["summary"].keys()
    per_host_keys = {
        "alias", "success", "exit_code", "stdout", "stderr",
        "elapsed_ms", "failure_class", "skipped",
    }
    assert per_host_keys <= payload["data"]["results"]["pai"].keys()


def test_main_human_path_emits_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    _patch_run_one(monkeypatch, {})
    code = sc.main(["uptime", "--hosts", "pai,local"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "command:" in out
    assert "hosts:" in out
    assert "elapsed:" in out
    assert "pai" in out
    assert "local" in out


def test_main_argparse_defaults_match_constants() -> None:
    parser = sc.build_parser()
    args = parser.parse_args(["uptime", "--hosts", "x"])
    assert args.max_workers == sc.DEFAULT_MAX_WORKERS
    assert args.timeout == sc.DEFAULT_TIMEOUT
    assert args.connect_timeout == sc.DEFAULT_CONNECT_TIMEOUT
    assert args.fail_fast is False
    assert args.health_check is False


def test_main_dedup_visible_in_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    _patch_run_one(monkeypatch, {})
    sc.main(["uptime", "--hosts", "pai,pai,local,pai", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert list(payload["data"]["results"].keys()) == ["pai", "local"]


def test_main_whitespace_in_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ensure_ssh_execute(monkeypatch, tmp_path)
    _patch_run_one(monkeypatch, {})
    sc.main(["uptime", "--hosts", "  pai , local  ", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert list(payload["data"]["results"].keys()) == ["pai", "local"]
