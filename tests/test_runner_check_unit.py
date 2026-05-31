"""Unit tests for cicd-debug/scripts/runner_check.py v1.0."""
# ruff: noqa: PLR2004

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "cicd-debug" / "skills" / "cicd-debug" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import runner_check  # noqa: E402
from lib_cicd import (  # noqa: E402
    ApiUnexpectedError,
    AuthError,
    BadArgsError,
    CliTimeoutError,
    GhAdapter,
    GlabAdapter,
    NetworkError,
    NotFoundError,
    PermissionError,
    ProviderUnavailable,
    QueuedRun,
    RunnerData,
)


class FakeProvider:
    def __init__(
        self,
        runners: list[RunnerData] | None = None,
        queued: list[QueuedRun] | None = None,
    ) -> None:
        self.runners = runners or []
        self.queued = queued or []

    def list_runners(self, scope: Any, *, limit: int = 5000) -> list[RunnerData]:
        return self.runners[:limit]

    def list_queued_runs(self, scope: Any, *, limit: int = 200) -> list[QueuedRun]:
        return self.queued[:limit]


def _runner(
    name: str,
    *,
    status: str = "online",
    busy: bool = False,
    labels: tuple[str, ...] = ("self-hosted", "linux"),
    ephemeral: bool = False,
    heartbeat: str | None = None,
) -> RunnerData:
    return RunnerData(
        id=name,
        name=name,
        os="linux",
        status=status,
        busy=busy,
        ephemeral=ephemeral,
        labels=tuple({"name": label, "type": "custom"} for label in labels),
        last_heartbeat=heartbeat,
        machine_info={"os": "linux"},
    )


def _run_json(
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeProvider,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(runner_check, "resolve_provider", lambda *args: provider)
    code = runner_check.main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_argparse_surface() -> None:
    args = runner_check.build_parser().parse_args(
        [
            "gh",
            "--repo",
            "owner/repo",
            "--pool-cap",
            "10",
            "--queue-cap",
            "5",
            "--gh-bin",
            "/custom/gh",
            "--timeout",
            "9",
            "--json",
        ]
    )
    assert args.provider == "gh"
    assert args.repo == "owner/repo"
    assert args.pool_cap == 10
    assert args.queue_cap == 5
    assert args.gh_bin == "/custom/gh"
    assert args.timeout == 9
    assert args.json is True


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["gh"], "exactly one"),
        (["gh", "--repo", "o/r", "--org", "org"], "exactly one"),
        (["gh", "--project", "g/p"], "gh supports"),
        (["glab", "--repo", "o/r"], "glab supports"),
        (["glab", "--project", "g/p", "--pool-cap", "0"], "pool-cap"),
        (["glab", "--project", "g/p", "--queue-cap", "-1"], "queue-cap"),
        (["glab", "--project", "g/p", "--timeout", "0"], "timeout"),
    ],
)
def test_validate_scope_bad_args(argv: list[str], message: str) -> None:
    args = runner_check.build_parser().parse_args(argv)
    with pytest.raises(BadArgsError, match=message):
        runner_check.validate_scope(args)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["gh", "--org", "myorg"], runner_check.Scope("org", "myorg")),
        (["gh", "--enterprise", "ent"], runner_check.Scope("enterprise", "ent")),
        (["gh", "--repo", "owner/repo"], runner_check.Scope("repo", "owner/repo")),
        (["glab", "--group", "group"], runner_check.Scope("group", "group")),
        (["glab", "--project", "g/p"], runner_check.Scope("project", "g/p")),
        (["glab", "--instance"], runner_check.Scope("instance", "*")),
    ],
)
def test_validate_scope_success(argv: list[str], expected: runner_check.Scope) -> None:
    assert runner_check.validate_scope(runner_check.build_parser().parse_args(argv)) == expected


def test_compute_aggregate_counts_and_queue_age() -> None:
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    aggregate = runner_check.compute_aggregate(
        [
            _runner("online-a", busy=True, labels=("linux", "gpu")),
            _runner("offline-b", status="offline", labels=("linux",)),
        ],
        [
            QueuedRun("1", "build", "2026-05-31T11:50:00Z"),
            QueuedRun("2", "test", "2026-05-31T11:58:00Z"),
        ],
        now=now,
    )
    assert aggregate == {
        "online_count": 1,
        "offline_count": 1,
        "busy_count": 1,
        "total_count": 2,
        "label_coverage": {"gpu": 1, "linux": 2},
        "queue_depth": 2,
        "queue_p95_age_sec": 120.0,
        "queue_max_age_sec": 600.0,
    }


@pytest.mark.parametrize(
    ("aggregate", "summary"),
    [
        ({"total_count": 2, "online_count": 0, "offline_count": 2, "busy_count": 0, "queue_depth": 0, "queue_p95_age_sec": None}, "crit"),
        ({"total_count": 0, "online_count": 0, "offline_count": 0, "busy_count": 0, "queue_depth": 1, "queue_p95_age_sec": 1}, "crit"),
        ({"total_count": 12, "online_count": 12, "offline_count": 0, "busy_count": 0, "queue_depth": 10, "queue_p95_age_sec": 1}, "warn"),
        ({"total_count": 12, "online_count": 12, "offline_count": 0, "busy_count": 0, "queue_depth": 1, "queue_p95_age_sec": 600}, "warn"),
        ({"total_count": 10, "online_count": 7, "offline_count": 3, "busy_count": 0, "queue_depth": 0, "queue_p95_age_sec": None}, "warn"),
        ({"total_count": 0, "online_count": 0, "offline_count": 0, "busy_count": 0, "queue_depth": 0, "queue_p95_age_sec": None}, "ok"),
    ],
)
def test_compute_summary_truth_table(aggregate: dict[str, Any], summary: str) -> None:
    assert runner_check.compute_summary(aggregate) == summary


def test_build_findings_all_kinds() -> None:
    aggregate = {
        "online_count": 0,
        "offline_count": 2,
        "busy_count": 0,
        "total_count": 2,
        "label_coverage": {"linux": 2},
        "queue_depth": 10,
        "queue_p95_age_sec": 601.0,
        "queue_max_age_sec": 700.0,
    }
    findings = runner_check.build_findings(
        "gh",
        runner_check.Scope("org", "myorg"),
        [_runner("a", status="offline", ephemeral=True), _runner("b", status="offline")],
        aggregate,
        pool_truncated=True,
    )
    assert {finding["kind"] for finding in findings} == {
        "all_offline",
        "queue_starving",
        "runner_offline_majority",
        "queue_pressure",
        "last_heartbeat_unavailable",
        "running_job_unavailable_at_scope",
        "pool_truncated",
        "ephemeral_runner_offline",
    }


def test_main_success_payload(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    code, payload = _run_json(
        monkeypatch,
        FakeProvider(
            runners=[
                _runner("a", status="online", busy=True),
                _runner("b", status="offline", labels=("linux",)),
            ],
            queued=[],
        ),
        ["gh", "--repo", "owner/repo"],
        capsys,
    )
    assert code == 0
    data = payload["data"]
    assert data["provider"] == "gh"
    assert data["scope"] == {"kind": "repo", "value": "owner/repo"}
    assert data["summary"] == "warn"
    assert data["aggregate"]["label_coverage"] == {"linux": 2, "self-hosted": 1}
    assert data["runners"][0]["machine_info"] == {"os": "linux"}
    assert data["failure_class"] is None


def test_pagination_overflow_returns_168(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    code, payload = _run_json(
        monkeypatch,
        FakeProvider(runners=[_runner("a")]),
        ["glab", "--project", "g/p", "--pool-cap", "1"],
        capsys,
    )
    assert code == 168
    assert payload["data"]["failure_class"] == "pagination_overflow"
    assert {finding["kind"] for finding in payload["data"]["findings"]} == {
        "pool_truncated"
    }


@pytest.mark.parametrize(
    ("exc", "provider", "failure_class", "exit_code"),
    [
        (ProviderUnavailable("gh binary not found"), "gh", "tool_missing", 160),
        (ProviderUnavailable("glab binary not found"), "glab", "provider_unavailable", 161),
        (AuthError("auth"), "gh", "auth", 162),
        (NotFoundError("missing"), "gh", "not_found", 163),
        (PermissionError("permission"), "gh", "permission", 164),
        (NetworkError("network"), "gh", "network", 165),
        (BadArgsError("bad"), "gh", "bad_args", 166),
        (ApiUnexpectedError("api"), "gh", "api_unexpected", 167),
        (CliTimeoutError("timeout"), "gh", "timeout", 124),
        (RuntimeError("boom"), "gh", "internal", 1),
    ],
)
def test_failure_class_mapping(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: Exception,
    provider: str,
    failure_class: str,
    exit_code: int,
) -> None:
    def raise_exc(*args: Any, **kwargs: Any) -> None:
        raise exc

    monkeypatch.setattr(runner_check, "resolve_provider", raise_exc)
    scope_args = ["--repo", "owner/repo"] if provider == "gh" else ["--project", "group/proj"]
    code = runner_check.main([provider, *scope_args, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == exit_code
    assert payload["exit_code"] == exit_code
    assert payload["data"]["failure_class"] == failure_class


def test_bad_timestamp_maps_to_api_unexpected(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    code, payload = _run_json(
        monkeypatch,
        FakeProvider(queued=[QueuedRun("1", "bad", "not-a-date")]),
        ["gh", "--repo", "owner/repo"],
        capsys,
    )
    assert code == 167
    assert payload["data"]["failure_class"] == "api_unexpected"


def test_gh_adapter_runner_and_queue_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        if "actions/runners" in argv[2]:
            stdout = json.dumps(
                {
                    "runners": [
                        {
                            "id": 1,
                            "name": "runner-a",
                            "os": "linux",
                            "status": "online",
                            "busy": False,
                            "ephemeral": False,
                            "labels": [{"name": "linux", "type": "custom"}],
                        }
                    ]
                }
            )
        else:
            stdout = json.dumps(
                {"workflow_runs": [{"id": 2, "name": "build", "created_at": "2026-05-31T00:00:00Z"}]}
            )
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GhAdapter(Path("/usr/bin/gh"))
    assert adapter.list_runners(runner_check.Scope("repo", "owner/repo"))[0].name == "runner-a"
    assert adapter.list_queued_runs(runner_check.Scope("repo", "owner/repo"))[0].name == "build"
    assert seen == [
        ["/usr/bin/gh", "api", "repos/owner/repo/actions/runners", "--paginate"],
        [
            "/usr/bin/gh",
            "api",
            "repos/owner/repo/actions/runs?status=queued&per_page=100",
            "--paginate",
        ],
    ]


def test_glab_adapter_runner_and_queue_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        if argv[1:3] == ["runner", "list"]:
            stdout = json.dumps(
                [
                    {
                        "id": 1,
                        "description": "runner-a",
                        "platform": "linux",
                        "online": True,
                        "tag_list": ["docker"],
                        "contacted_at": "2026-05-31T00:00:00Z",
                    }
                ]
            )
        else:
            stdout = json.dumps(
                [{"id": 2, "name": "build", "created_at": "2026-05-31T00:00:00Z", "tag_list": ["docker"]}]
            )
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GlabAdapter(Path("/usr/bin/glab"))
    assert adapter.list_runners(runner_check.Scope("project", "group/proj"))[0].last_heartbeat
    assert adapter.list_queued_runs(runner_check.Scope("project", "group/proj"))[0].labels == (
        "docker",
    )
    assert seen == [
        ["/usr/bin/glab", "runner", "list", "--repo", "group/proj", "--output", "json"],
        [
            "/usr/bin/glab",
            "api",
            "projects/group%2Fproj/jobs?scope[]=pending&per_page=100",
        ],
    ]

