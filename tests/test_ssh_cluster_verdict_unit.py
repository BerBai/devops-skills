"""DEVOPS-BUG-010 / DEVOPS-OPT-003: the cluster verdict and its observability.

Offline: the workers are stubbed, so a skip, a real failure and a success can
be put in the same batch without a host. The point of these cases is the
*contract*, which used to contradict itself:

* ``_summarize`` already excluded ``skipped`` from ``fail``, while
  ``_classify_top`` counted a skip as "not ok" — so ``fail: 0`` came with
  ``failure_class: partial_failure`` and exit 1;
* the skip reason lived in ``failure_class``, next to genuine failures;
* the alias buckets (ok / failed / skipped) did not exist at the top level;
* human output showed counts but not who failed for what.
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import load_script  # noqa: E402

cluster = load_script("ssh_cluster")


def _env(
    success: bool,
    *,
    failure_class: str | None = None,
    skipped: bool = False,
    skipped_reason: str | None = None,
    stdout: str = "",
) -> dict[str, Any]:
    """A per-host envelope with the documented shape."""
    env: dict[str, Any] = {
        "alias": "h",
        "success": success,
        "exit_code": 0 if success else 1,
        "stdout": stdout,
        "stderr": "",
        "elapsed_ms": 1,
        "failure_class": failure_class,
        "skipped": skipped,
    }
    if skipped_reason is not None:
        env["skipped_reason"] = skipped_reason
    return env


def _skipped() -> dict[str, Any]:
    return _env(
        False,
        failure_class="health_check_failed",
        skipped=True,
        skipped_reason="health_check_failed",
    )


# --- BUG-010: a skip must not fail the batch ---------------------------------


def test_every_host_skipped_is_a_success() -> None:
    """All hosts skipped: nothing failed, so the batch succeeded."""
    results = {"web-1": _skipped(), "web-2": _skipped()}
    success, failure_class = cluster._classify_top(results)
    assert success is True
    assert failure_class is None

    summary = cluster._summarize(results, 0.0)
    assert summary["total"] == 2
    assert summary["ok"] == 0
    assert summary["fail"] == 0
    assert summary["skipped"] == 2


def test_mixed_ok_and_skipped_is_a_success() -> None:
    results = {"web-1": _env(True), "web-2": _skipped()}
    success, failure_class = cluster._classify_top(results)
    assert success is True
    assert failure_class is None

    summary = cluster._summarize(results, 0.0)
    assert (summary["ok"], summary["fail"], summary["skipped"]) == (1, 0, 1)


def test_a_real_failure_is_still_partial() -> None:
    results = {"web-1": _env(True), "web-2": _env(False, failure_class="auth")}
    assert cluster._classify_top(results) == (False, "partial_failure")


def test_nothing_ok_and_nothing_skipped_is_all_hosts_failed() -> None:
    results = {
        "web-1": _env(False, failure_class="network"),
        "web-2": _env(False, failure_class="auth"),
    }
    assert cluster._classify_top(results) == (False, "all_hosts_failed")


def test_skips_do_not_mask_a_total_outage() -> None:
    """A skipped host is not evidence that the batch worked."""
    results = {"web-1": _env(False, failure_class="network"), "web-2": _skipped()}
    assert cluster._classify_top(results) == (False, "partial_failure")


def test_summary_and_verdict_agree_on_fail() -> None:
    """The self-contradiction from the issue: fail=0 must mean success.

    ``_summarize`` and ``_classify_top`` now share one bucketing function, so
    for every mix of ok/failed/skipped the two cannot disagree.
    """
    mixes = [
        {"a": _env(True)},
        {"a": _skipped()},
        {"a": _env(True), "b": _skipped()},
        {"a": _env(True), "b": _env(False, failure_class="auth")},
        {"a": _env(False, failure_class="auth")},
        {"a": _env(False, failure_class="auth"), "b": _skipped()},
    ]
    for results in mixes:
        summary = cluster._summarize(results, 0.0)
        ok_hosts, failed_hosts, skipped_hosts = cluster._bucket_hosts(results)
        assert summary["ok"] == len(ok_hosts)
        assert summary["fail"] == len(failed_hosts)
        assert summary["skipped"] == len(skipped_hosts)
        assert summary["total"] == (
            summary["ok"] + summary["fail"] + summary["skipped"]
        )
        success, failure_class = cluster._classify_top(results)
        assert (summary["fail"] == 0) is success, results
        assert (summary["fail"] == 0) is (failure_class is None), results


# --- OPT-003: alias buckets, skipped_reason, grouped human output ------------


def test_alias_buckets_are_disjoint_and_ordered() -> None:
    results = {
        "web-1": _env(True),
        "web-2": _skipped(),
        "web-3": _env(False, failure_class="auth"),
    }
    assert cluster._bucket_hosts(results) == (["web-1"], ["web-3"], ["web-2"])


def test_health_check_skip_sets_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skip reason no longer *only* lives in ``failure_class``."""

    def fake_run_one(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _env(False, failure_class="remote_error")

    monkeypatch.setattr(cluster, "_run_one", fake_run_one)
    alive, dead, _extra = cluster._health_check(
        Path("ssh_execute.py"), ["web-9"], 5, 1,
    )
    assert alive == []
    env = dead["web-9"]
    assert env["skipped"] is True
    assert env["skipped_reason"] == "health_check_failed"
    # Kept for consumers written before skipped_reason existed.
    assert env["failure_class"] == "health_check_failed"


def test_worker_declared_skip_propagates_and_does_not_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skip decided *inside* the worker (ssh_execute) is also not a failure.

    ssh_execute's local route can declare "this source is not servable on this
    platform" (``skipped`` + ``skipped_reason``, no ``failure_class``). Before
    the propagation the cluster dropped those fields and counted the host as
    failed, so the batch went red for a deliberate skip.
    """
    worker_envelope = {
        "success": False,
        "exit_code": 0,
        "stdout": "",
        "stderr": "local: /etc/hostname is not readable on Windows\n",
        "data": {
            "route": "local",
            "skipped": True,
            "skipped_reason": "posix_path_on_windows",
            "failure_class": None,
        },
    }

    class _Proc:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = json.dumps(worker_envelope)
            self.stderr = ""

    monkeypatch.setattr(cluster.subprocess, "run", lambda *_a, **_kw: _Proc())
    env = cluster._run_one_attempt(Path("ssh_execute.py"), "local", "cat /etc/hostname", 5, 2)
    assert env["skipped"] is True
    assert env["skipped_reason"] == "posix_path_on_windows"
    assert env["failure_class"] is None
    # ... and therefore the batch is a success.
    assert cluster._classify_top({"local": env}) == (True, None)


def test_worker_skip_cannot_mask_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A contradictory worker envelope (`skipped` + `failure_class`) fails.

    Otherwise any worker bug that set both flags would hide a real failure
    behind "it was only skipped".
    """

    class _Proc:
        returncode = 1

        def __init__(self) -> None:
            self.stdout = json.dumps(
                {
                    "success": False,
                    "exit_code": 104,
                    "stdout": "",
                    "stderr": "boom\n",
                    "data": {
                        "skipped": True,
                        "skipped_reason": "posix_path_on_windows",
                        "failure_class": "remote_error",
                    },
                }
            )
            self.stderr = ""

    monkeypatch.setattr(cluster.subprocess, "run", lambda *_a, **_kw: _Proc())
    env = cluster._run_one_attempt(Path("ssh_execute.py"), "local", "cat /etc/hostname", 5, 2)
    assert env["skipped"] is False
    assert "skipped_reason" not in env
    assert env["failure_class"] == "remote_error"
    assert cluster._classify_top({"local": env}) == (False, "all_hosts_failed")


def test_fail_fast_skips_are_reported_as_skips_but_keep_the_batch_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--fail-fast: the host that failed decides the verdict, not the skips.

    The unstarted hosts are real skips (they were deliberately not run), but
    the batch still failed because one host did. Before the fix these three
    cases were indistinguishable from "every host failed".
    """

    def fake_run_one(_ssh_exec, alias, *_args, **_kwargs):
        if alias == "web-1":
            return _env(False, failure_class="auth")
        raise AssertionError(f"worker for {alias} should never run")

    monkeypatch.setattr(cluster, "_run_one", fake_run_one)
    results = cluster._broadcast_serial(
        Path("ssh_execute.py"), ["web-1", "web-2", "web-3"],
        "true", 5, 2, fail_fast=True,
    )
    assert results["web-2"]["skipped"] is True
    assert results["web-2"]["skipped_reason"] == "skipped_fail_fast"
    assert results["web-3"]["skipped_reason"] == "skipped_fail_fast"
    assert cluster._bucket_hosts(results) == ([], ["web-1"], ["web-2", "web-3"])
    assert cluster._classify_top(results) == (False, "partial_failure")
    summary = cluster._summarize(results, 0.0)
    assert (summary["ok"], summary["fail"], summary["skipped"]) == (0, 1, 2)


def test_plain_broadcast_keeps_the_eight_field_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No skip, no ``skipped_reason`` key: old consumers see the old shape."""

    class _Proc:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = json.dumps(
                {"success": True, "exit_code": 0, "stdout": "ok\n",
                 "stderr": "", "data": {"failure_class": None}}
            )
            self.stderr = ""

    monkeypatch.setattr(cluster.subprocess, "run", lambda *_a, **_kw: _Proc())
    env = cluster._run_one_attempt(Path("ssh_execute.py"), "web-1", "true", 5, 2)
    assert "skipped_reason" not in env
    assert set(env) == {
        "alias", "success", "exit_code", "stdout", "stderr",
        "elapsed_ms", "failure_class", "skipped",
    }


def test_main_reports_every_host_skipped_as_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through ``main``: --health-check fails for both hosts."""

    def fake_run_one(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _env(False, failure_class="remote_error")

    monkeypatch.setattr(cluster, "_run_one", fake_run_one)
    rc = cluster.main(
        ["uptime", "--hosts", "web-1,web-2", "--health-check", "--json"]
    )
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["success"] is True
    assert env["exit_code"] == 0
    data = env["data"]
    assert data["failure_class"] is None
    assert data["summary"] == {
        "total": 2, "ok": 0, "fail": 0, "skipped": 2,
        "elapsed_ms": data["summary"]["elapsed_ms"],
    }
    assert data["ok_hosts"] == []
    assert data["failed_hosts"] == []
    assert data["skipped_hosts"] == ["web-1", "web-2"]
    for alias in ("web-1", "web-2"):
        assert data["results"][alias]["skipped_reason"] == "health_check_failed"


def test_main_mixed_ok_and_skipped_is_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_health_check(*_args: Any, **_kwargs: Any):
        return (
            ["web-1"],
            {"web-2": _skipped()},
            {},
        )

    monkeypatch.setattr(cluster, "_health_check", fake_health_check)
    monkeypatch.setattr(
        cluster, "_run_one", lambda *_a, **_kw: _env(True, stdout="up 3 days\n")
    )
    rc = cluster.main(
        ["uptime", "--hosts", "web-1,web-2", "--health-check", "--json"]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["ok_hosts"] == ["web-1"]
    assert data["failed_hosts"] == []
    assert data["skipped_hosts"] == ["web-2"]
    assert data["summary"]["fail"] == 0
    assert data["failure_class"] is None


def test_main_real_failure_still_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run_one(_ssh_exec, alias, *_args, **_kwargs):
        if alias == "web-1":
            return _env(True, stdout="ok\n")
        return _env(False, failure_class="auth")

    monkeypatch.setattr(cluster, "_run_one", fake_run_one)
    rc = cluster.main(["uptime", "--hosts", "web-1,web-2", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    data = env["data"]
    assert env["success"] is False
    assert data["failure_class"] == "partial_failure"
    assert data["ok_hosts"] == ["web-1"]
    assert data["failed_hosts"] == ["web-2"]
    assert data["skipped_hosts"] == []
    # A genuine failure keeps failure_class and has no skip reason.
    assert data["results"]["web-2"]["failure_class"] == "auth"
    assert "skipped_reason" not in data["results"]["web-2"]


def _group_lines(text: str) -> list[str]:
    """The indented group block, whitespace-normalised.

    The label column is padded so counts line up; the exact number of spaces
    is a rendering detail, which alias is in which group is the contract.
    Per-host detail lines (``probe:`` / ``stderr:``) are excluded.
    """
    detail_prefixes = ("probe:", "stderr:", "[")
    return [
        " ".join(line.split())
        for line in text.splitlines()
        if line.startswith("  ")
        and not line.strip().startswith(detail_prefixes)
    ]


def test_human_output_groups_failures_by_class() -> None:
    results = {
        "web-1": _env(True, stdout="up\n"),
        "web-3": _env(False, failure_class="auth"),
        "web-4": _env(False, failure_class="auth"),
        "db-2": _skipped(),
    }
    data = {
        "command": "uptime",
        "summary": cluster._summarize(results, 0.0),
        "results": results,
        "failure_class": "partial_failure",
    }
    text = cluster._format_human(data)
    assert "hosts:    4 (ok=1 fail=2 skipped=1)" in text
    groups = _group_lines(text)
    assert "auth 2 web-3, web-4" in groups
    assert "health_check_failed 1 db-2" in groups
    # The skipped host is not filed under a failure label.
    assert all("db-2" not in line or "health_check_failed" in line for line in groups)
    assert "failure_class: partial_failure" in text


def test_human_output_shows_a_closed_probe_port_on_a_skipped_host() -> None:
    skipped = _skipped()
    skipped["probe_records"] = [
        {"kind": "tcp", "port": 3306, "ok": False, "error": "ECONNREFUSED (111)"},
    ]
    skipped["probe"] = {
        "ok": False, "probes": 1, "failed": 1, "failed_targets": ["db-2:3306"],
    }
    data = {
        "command": "uptime",
        "summary": cluster._summarize({"db-2": skipped}, 0.0),
        "results": {"db-2": skipped},
        "failure_class": None,
    }
    text = cluster._format_human(data)
    assert "health_check_failed 1 db-2 (port 3306 closed)" in _group_lines(text)


def test_human_output_shows_probe_results() -> None:
    two_ports = _env(True, stdout="up\n")
    two_ports["probe_records"] = [
        {"kind": "tcp", "port": 22, "ok": True, "error": None},
        {"kind": "tcp", "port": 3306, "ok": False, "error": "ECONNREFUSED (111)"},
    ]
    two_ports["probe"] = {
        "ok": False, "probes": 2, "failed": 1, "failed_targets": ["db:3306"],
    }
    data = {
        "command": "uptime",
        "summary": cluster._summarize({"db-1": two_ports}, 0.0),
        "results": {"db-1": two_ports},
        "failure_class": None,
    }
    text = cluster._format_human(data)
    assert "probe: tcp:22 ok, tcp:3306 refused" in text


def test_human_output_does_not_invent_a_probe_line() -> None:
    results = {"web-1": _env(True, stdout="up\n")}
    data = {
        "command": "uptime",
        "summary": cluster._summarize(results, 0.0),
        "results": results,
        "failure_class": None,
    }
    assert "probe:" not in cluster._format_human(data)
