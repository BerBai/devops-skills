"""Unit tests for cicd-debug/scripts/pipeline_analyzer.py v1.0.

Subprocess is mocked via monkeypatch so these run without any real gh/glab CLI.
The live counterpart lives in tests/test_pipeline_analyzer_integration.py (marker: cicd_gh/cicd_glab).

PRD: .trellis/tasks/05-26-cicd-debug-pipeline-analyzer/prd.md
Design: .trellis/tasks/05-26-cicd-debug-pipeline-analyzer/design.md
"""
# ruff: noqa: PLR2004, PLC0415

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "cicd-debug" / "skills" / "cicd-debug" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import pipeline_analyzer  # noqa: E402, I001
from lib_cicd import (  # noqa: E402, I001
    ApiUnexpectedError,
    AuthError,
    BadArgsError,
    GhAdapter,
    GlabAdapter,
    NetworkError,
    NotFoundError,
    PermissionError,
    ProviderUnavailable,
    _map_gh_error,
    resolve_provider,
)

# Exit codes per PRD D3
EXIT_OK = 0
EXIT_TOOL_MISSING = 150
EXIT_PROVIDER_UNAVAILABLE = 151
EXIT_AUTH = 152
EXIT_NOT_FOUND = 153
EXIT_PERMISSION = 154
EXIT_NETWORK = 155
EXIT_BAD_ARGS = 156
EXIT_API_UNEXPECTED = 157
EXIT_LOG_TRUNCATED = 158
EXIT_TIMEOUT = 124
EXIT_INTERNAL = 1

# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """Stand-in for subprocess.CompletedProcess."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# gh run view --json fixture
GH_RUN_VIEW_FIXTURE = {
    "databaseId": 12345678,
    "name": "test workflow",
    "displayTitle": "test workflow #12345",
    "status": "completed",
    "conclusion": "failure",
    "createdAt": "2026-05-26T10:00:00Z",
    "startedAt": "2026-05-26T10:01:00Z",
    "completedAt": "2026-05-26T10:05:00Z",
    "headBranch": "main",
    "headSha": "abc123",
    "event": "push",
    "url": "https://github.com/owner/repo/actions/runs/12345678",
    "workflowName": "test.yml",
    "jobs": [
        {
            "id": 456,
            "name": "test",
            "status": "completed",
            "conclusion": "failure",
            "startedAt": "2026-05-26T10:01:00Z",
            "completedAt": "2026-05-26T10:05:00Z",
            "runnerName": "my-runner",
            "runnerLabels": ["self-hosted", "linux"],
            "steps": [
                {"name": "checkout", "conclusion": "success", "number": 1},
                {"name": "run tests", "conclusion": "failure", "number": 2},
            ],
        }
    ],
}

# -----------------------------------------------------------------------------
# argparse surface tests (PRD D6)
# -----------------------------------------------------------------------------


def test_argparse_provider_choices() -> None:
    """Provider must be gh or glab."""
    with pytest.raises(SystemExit):
        pipeline_analyzer.build_parser().parse_args(["invalid", "123"])


def test_argparse_glab_requires_project() -> None:
    """glab provider requires --project."""
    with pytest.raises(SystemExit):
        pipeline_analyzer.build_parser().parse_args(["glab", "123"])


def test_argparse_glab_with_project_ok() -> None:
    """glab with --project parses correctly."""
    args = pipeline_analyzer.build_parser().parse_args(
        ["glab", "123", "--project", "group/repo"]
    )
    assert args.provider == "glab"
    assert args.run_id == "123"
    assert args.project == "group/repo"


def test_argparse_diff_against_last_success() -> None:
    """--diff-against last-success parses correctly."""
    args = pipeline_analyzer.build_parser().parse_args(
        ["gh", "123", "--diff-against", "last-success"]
    )
    assert args.diff_against == "last-success"


def test_argparse_log_size_cap_mb() -> None:
    """--log-size-cap-mb parses correctly."""
    args = pipeline_analyzer.build_parser().parse_args(["gh", "123", "--log-size-cap-mb", "10"])
    assert args.log_size_cap_mb == 10


def test_argparse_include_full_step_logs() -> None:
    """--include-full-step-logs flag works."""
    args = pipeline_analyzer.build_parser().parse_args(
        ["gh", "123", "--include-full-step-logs"]
    )
    assert args.include_full_step_logs is True


def test_argparse_gh_bin() -> None:
    """--gh-bin parses correctly."""
    args = pipeline_analyzer.build_parser().parse_args(["gh", "123", "--gh-bin", "/custom/gh"])
    assert args.gh_bin == "/custom/gh"


def test_argparse_glab_bin() -> None:
    """--glab-bin parses correctly."""
    args = pipeline_analyzer.build_parser().parse_args(
        ["glab", "123", "--project", "g/r", "--glab-bin", "/custom/glab"]
    )
    assert args.glab_bin == "/custom/glab"


def test_argparse_timeout() -> None:
    """--timeout parses correctly."""
    args = pipeline_analyzer.build_parser().parse_args(["gh", "123", "--timeout", "60"])
    assert args.timeout == 60


# -----------------------------------------------------------------------------
# lib_cicd tests
# -----------------------------------------------------------------------------


def test_resolve_provider_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_provider returns GhAdapter for gh."""
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    adapter = resolve_provider("gh")
    assert isinstance(adapter, GhAdapter)


def test_resolve_provider_glab(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_provider returns GlabAdapter for glab."""
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    adapter = resolve_provider("glab")
    assert isinstance(adapter, GlabAdapter)


def test_resolve_provider_unknown() -> None:
    """resolve_provider raises ValueError for unknown provider."""
    with pytest.raises(ValueError, match="Unknown provider"):
        resolve_provider("unknown")


def test_resolve_provider_tool_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_provider raises ProviderUnavailable when binary not found."""
    monkeypatch.setattr("shutil.which", lambda x: None)
    with pytest.raises(ProviderUnavailable):
        resolve_provider("gh")


def test_resolve_provider_gh_bin_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """--gh-bin override takes precedence."""
    adapter = resolve_provider("gh", gh_bin="/custom/gh")
    assert isinstance(adapter, GhAdapter)
    assert str(adapter.binary) == "/custom/gh"


def test_resolve_provider_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """GH_BIN env var override works."""
    monkeypatch.setenv("GH_BIN", "/env/gh")
    monkeypatch.setattr("shutil.which", lambda x: None)
    adapter = resolve_provider("gh")
    assert isinstance(adapter, GhAdapter)
    assert str(adapter.binary) == "/env/gh"


def test_gh_adapter_fetch_run_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """GhAdapter.fetch_run constructs correct argv."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(0, stdout=json.dumps(GH_RUN_VIEW_FIXTURE))

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GhAdapter(Path("/usr/bin/gh"))
    adapter.fetch_run("12345678", "owner/repo")
    assert seen["argv"] == ["/usr/bin/gh", "run", "view", "12345678", "--json"]


def test_gh_adapter_fetch_log_only_failed_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """GhAdapter.fetch_log with only_failed=True uses --log-failed."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(0, stdout="log output")

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GhAdapter(Path("/usr/bin/gh"))
    adapter.fetch_log("12345678", "456", only_failed=True)
    assert seen["argv"] == ["/usr/bin/gh", "run", "view", "12345678", "--log-failed"]


def test_gh_adapter_fetch_log_full_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """GhAdapter.fetch_log with only_failed=False uses --log --job."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(0, stdout="log output")

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GhAdapter(Path("/usr/bin/gh"))
    adapter.fetch_log("12345678", "456", only_failed=False)
    assert seen["argv"] == ["/usr/bin/gh", "run", "view", "12345678", "--log", "--job", "456"]


def test_gh_adapter_find_last_success_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """GhAdapter.find_last_success constructs correct argv."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(0, stdout=json.dumps([{"databaseId": 999}]))

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GhAdapter(Path("/usr/bin/gh"))
    adapter.find_last_success("owner/repo", "test.yml", "main")
    expected = [
        "/usr/bin/gh",
        "run",
        "list",
        "--workflow",
        "test.yml",
        "--branch",
        "main",
        "--status",
        "success",
        "--limit",
        "1",
        "--json",
        "databaseId",
    ]
    assert seen["argv"] == expected


def test_glab_adapter_fetch_run_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """GlabAdapter.fetch_run constructs correct argv."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(0, stdout=json.dumps({"id": 123, "status": "failed"}))

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GlabAdapter(Path("/usr/bin/glab"))
    adapter.fetch_run("123", "group/repo")
    assert seen["argv"] == ["/usr/bin/glab", "ci", "view", "123", "--output", "json", "--repo", "group/repo"]


def test_glab_adapter_fetch_log_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """GlabAdapter.fetch_log constructs correct argv (no --log-failed)."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(0, stdout="log output")

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GlabAdapter(Path("/usr/bin/glab"))
    adapter.fetch_log("123", "456", only_failed=True)
    assert seen["argv"] == ["/usr/bin/glab", "ci", "trace", "456"]


def test_map_gh_error_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """_map_gh_error raises AuthError for auth messages."""
    with pytest.raises(AuthError):
        _map_gh_error("authentication required")


def test_map_gh_error_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """_map_gh_error raises NotFoundError for not found messages."""
    with pytest.raises(NotFoundError):
        _map_gh_error("not found")


def test_map_gh_error_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """_map_gh_error raises PermissionError for permission messages."""
    with pytest.raises(PermissionError):
        _map_gh_error("must have repository read permissions")


def test_map_gh_error_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """_map_gh_error raises NetworkError for network errors."""
    with pytest.raises(NetworkError):
        _map_gh_error("could not resolve")


# -----------------------------------------------------------------------------
# pipeline_analyzer data extraction tests
# -----------------------------------------------------------------------------


def test_extract_cache_summary_hits() -> None:
    """extract_cache_summary detects cache hits."""
    logs = {"test-job": "Cache restored from key: node-cache-123\n"}
    result = pipeline_analyzer.extract_cache_summary(logs)
    assert result["hits"] == 1
    assert result["misses"] == 0
    assert result["events"][0]["outcome"] == "hit"


def test_extract_cache_summary_misses() -> None:
    """extract_cache_summary detects cache misses."""
    logs = {"test-job": "Cache not found\n"}
    result = pipeline_analyzer.extract_cache_summary(logs)
    assert result["hits"] == 0
    assert result["misses"] == 1
    assert result["events"][0]["outcome"] == "miss"


def test_extract_cache_summary_saves() -> None:
    """extract_cache_summary detects cache saves."""
    logs = {"test-job": "Cache saved\n"}
    result = pipeline_analyzer.extract_cache_summary(logs)
    assert result["saves"] == 1
    assert result["events"][0]["outcome"] == "save"


def test_extract_actions_used() -> None:
    """extract_actions_used extracts action@ref patterns."""
    logs = {
        "test-job": "Run actions/checkout@v3\nRun actions/setup-python@v4\n"
    }
    result = pipeline_analyzer.extract_actions_used(logs)
    assert len(result) == 2
    actions = {a["action"]: a["ref"] for a in result}
    assert actions["actions/checkout"] == "v3"
    assert actions["actions/setup-python"] == "v4"


def test_extract_runner_used() -> None:
    """extract_runner_used extracts runner info."""
    run_data = {
        "jobs": [
            {
                "runner_name": "my-runner",
                "runner_labels": ["self-hosted", "linux"],
                "runs_on": ["ubuntu-latest"],
            }
        ]
    }
    result = pipeline_analyzer.extract_runner_used(run_data)
    assert len(result) == 1
    assert result[0]["name"] == "my-runner"
    assert result[0]["labels"] == ["self-hosted", "linux"]


# -----------------------------------------------------------------------------
# summary computation tests (PRD D4)
# -----------------------------------------------------------------------------


def test_compute_summary_success() -> None:
    """compute_summary returns 'ok' for successful conclusion."""
    run_meta = {"conclusion": "success", "status": "completed"}
    result = pipeline_analyzer.compute_summary(run_meta, [], None)
    assert result == "ok"


def test_compute_summary_failure_with_steps() -> None:
    """compute_summary returns 'crit' for failure with failed steps."""
    run_meta = {"conclusion": "failure", "status": "completed"}
    failed_steps = [{"name": "test"}]
    result = pipeline_analyzer.compute_summary(run_meta, failed_steps, None)
    assert result == "crit"


def test_compute_summary_cancelled() -> None:
    """compute_summary returns 'warn' for cancelled."""
    run_meta = {"conclusion": "cancelled", "status": "completed"}
    result = pipeline_analyzer.compute_summary(run_meta, [], None)
    assert result == "warn"


def test_compute_summary_timed_out() -> None:
    """compute_summary returns 'crit' for timed_out."""
    run_meta = {"conclusion": "timed_out", "status": "completed"}
    result = pipeline_analyzer.compute_summary(run_meta, [], None)
    assert result == "crit"


def test_compute_summary_queued() -> None:
    """compute_summary returns 'warn' for queued status."""
    run_meta = {"status": "queued", "conclusion": None}
    result = pipeline_analyzer.compute_summary(run_meta, [], None)
    assert result == "warn"


def test_compute_summary_drift_override() -> None:
    """compute_summary overrides 'ok' to 'warn' for action drift."""
    run_meta = {"conclusion": "success", "status": "completed"}
    diff_summary = {"action_version_drift": [{"action": "checkout", "from_ref": "v3", "to_ref": "v4"}]}
    result = pipeline_analyzer.compute_summary(run_meta, [], diff_summary)
    assert result == "warn"


# -----------------------------------------------------------------------------
# diff computation tests (PRD D2)
# -----------------------------------------------------------------------------


def test_compute_diff_summary_action_drift() -> None:
    """compute_diff_summary detects action version drift."""
    current = {
        "actions_used": [{"action": "checkout", "ref": "v4"}],
        "runner_used": [],
        "cache_summary": {"hits": 0, "misses": 0},
    }
    reference = {
        "actions_used": [{"action": "checkout", "ref": "v3"}],
        "runner_used": [],
        "cache_summary": {"hits": 0, "misses": 0},
    }
    result = pipeline_analyzer.compute_diff_summary(current, reference)
    assert len(result["action_version_drift"]) == 1
    assert result["action_version_drift"][0]["from_ref"] == "v3"
    assert result["action_version_drift"][0]["to_ref"] == "v4"


def test_compute_diff_summary_cache_regression() -> None:
    """compute_diff_summary detects cache regression."""
    current = {
        "actions_used": [],
        "runner_used": [],
        "cache_summary": {"hits": 0, "misses": 5},
    }
    reference = {
        "actions_used": [],
        "runner_used": [],
        "cache_summary": {"hits": 10, "misses": 0},
    }
    result = pipeline_analyzer.compute_diff_summary(current, reference)
    assert len(result["cache_drift"]) == 1
    assert result["cache_drift"][0]["from"] == "hits"


# -----------------------------------------------------------------------------
# findings generation tests (PRD D5)
# -----------------------------------------------------------------------------


def test_generate_findings_failed_step() -> None:
    """generate_findings emits failed_step finding."""
    failed_steps = [
        {"job": "test", "name": "run tests", "exit_code": 1, "log_excerpt": "error"}
    ]
    result = pipeline_analyzer.generate_findings([], failed_steps, None, {}, {})
    assert len(result) == 1
    assert result[0]["kind"] == "failed_step"
    assert result[0]["severity"] == "crit"


def test_generate_findings_action_version_drift() -> None:
    """generate_findings emits action_version_drift finding."""
    diff_summary = {
        "action_version_drift": [{"action": "checkout", "from_ref": "v3", "to_ref": "v4"}]
    }
    result = pipeline_analyzer.generate_findings([], [], diff_summary, {}, {})
    assert len(result) == 1
    assert result[0]["kind"] == "action_version_drift"
    assert result[0]["severity"] == "warn"


def test_generate_findings_cache_miss_regression() -> None:
    """generate_findings emits cache_miss_regression finding."""
    diff_summary = {"cache_drift": [{"from": "hits", "cache_key_prefix": "regression"}]}
    result = pipeline_analyzer.generate_findings([], [], diff_summary, {}, {})
    assert len(result) == 1
    assert result[0]["kind"] == "cache_miss_regression"
    assert result[0]["severity"] == "warn"


def test_generate_findings_runner_label_drift() -> None:
    """generate_findings emits runner_label_drift finding."""
    diff_summary = {
        "runner_label_drift": [
            {"runner_name": "r1", "from_labels": ["linux"], "to_labels": ["linux", "gpu"]}
        ]
    }
    result = pipeline_analyzer.generate_findings([], [], diff_summary, {}, {})
    assert len(result) == 1
    assert result[0]["kind"] == "runner_label_drift"
    assert result[0]["severity"] == "info"


def test_generate_findings_queued_too_long() -> None:
    """generate_findings emits queued_too_long for old queued runs."""
    run_meta = {"status": "queued", "created_at": "2026-05-26T10:00:00Z"}
    result = pipeline_analyzer.generate_findings([], [], None, run_meta, {})
    assert any(f["kind"] == "queued_too_long" for f in result)


# -----------------------------------------------------------------------------
# main success-path behavior tests
# -----------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, log: str = "Run actions/checkout@v4\n") -> None:
        self.log = log
        self.fetch_run_ids: list[str] = []

    def fetch_run(self, run_id: str, project: str | None) -> dict:
        self.fetch_run_ids.append(run_id)
        data = dict(GH_RUN_VIEW_FIXTURE)
        if run_id == "999":
            data = {
                **data,
                "databaseId": 999,
                "conclusion": "success",
                "jobs": [
                    {
                        **data["jobs"][0],
                        "conclusion": "success",
                        "steps": [{"name": "checkout", "conclusion": "success", "number": 1}],
                    }
                ],
            }
        return data

    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        if run_id == "999":
            return "Run actions/checkout@v3\n"
        return self.log

    def find_last_success(
        self, project: str, workflow_name: str | None, branch: str
    ) -> str | None:
        return "999"


def _capture_emit(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen = {}

    def fake_emit(data, code, stderr, json_mode):
        seen.update({"data": data, "code": code, "stderr": stderr, "json_mode": json_mode})
        return code

    monkeypatch.setattr("pipeline_analyzer.emit", fake_emit)
    return seen


def test_main_include_full_step_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """--include-full-step-logs adds full_log to step payloads."""
    monkeypatch.setattr("pipeline_analyzer.resolve_provider", lambda *args: FakeProvider("full log"))
    seen = _capture_emit(monkeypatch)
    result = pipeline_analyzer.main(["gh", "123", "--include-full-step-logs"])
    assert result == EXIT_OK
    step = seen["data"]["job_states"][0]["steps"][0]
    assert step["full_log"] == "full log"


def test_main_without_full_step_logs_omits_full_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """full_log is opt-in."""
    monkeypatch.setattr("pipeline_analyzer.resolve_provider", lambda *args: FakeProvider("full log"))
    seen = _capture_emit(monkeypatch)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_OK
    step = seen["data"]["job_states"][0]["steps"][0]
    assert "full_log" not in step


def test_main_log_size_cap_emits_partial_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized logs are truncated and surfaced as analyzer_partial."""
    huge_log = "x" * (pipeline_analyzer.BYTES_PER_MB + 1)
    monkeypatch.setattr("pipeline_analyzer.resolve_provider", lambda *args: FakeProvider(huge_log))
    seen = _capture_emit(monkeypatch)
    result = pipeline_analyzer.main(["gh", "123", "--log-size-cap-mb", "1"])
    assert result == EXIT_OK
    assert any(f["kind"] == "analyzer_partial" for f in seen["data"]["findings"])
    assert seen["data"]["failed_steps"][0]["log_excerpt"].startswith("[log truncated")


def test_main_diff_against_specific_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """--diff-against <id> compares against the requested reference run."""
    provider = FakeProvider("Run actions/checkout@v4\n")
    monkeypatch.setattr("pipeline_analyzer.resolve_provider", lambda *args: provider)
    seen = _capture_emit(monkeypatch)
    result = pipeline_analyzer.main(["gh", "123", "--diff-against", "999"])
    assert result == EXIT_OK
    assert provider.fetch_run_ids == ["123", "999"]
    assert seen["data"]["diff_summary"]["action_version_drift"] == [
        {"action": "actions/checkout", "from_ref": "v3", "to_ref": "v4"}
    ]


# -----------------------------------------------------------------------------
# failure_class tests (PRD D3)
# -----------------------------------------------------------------------------


def test_main_tool_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 150 when gh binary not found."""
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_TOOL_MISSING


def test_main_provider_unavailable_for_glab(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 151 when glab provider is unavailable."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(ProviderUnavailable("glab binary not found")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["glab", "123", "--project", "group/repo"])
    assert result == EXIT_PROVIDER_UNAVAILABLE


def test_main_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 152 on auth error."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(AuthError("auth failed")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_AUTH


def test_main_not_found_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 153 on not found error."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(NotFoundError("not found")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_NOT_FOUND


def test_main_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 154 on permission error."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(PermissionError("permission denied")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_PERMISSION


def test_main_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 155 on network error."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(NetworkError("network error")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_NETWORK


def test_main_bad_args_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 156 on bad args."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(BadArgsError("bad args")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_BAD_ARGS


def test_main_api_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 157 on API unexpected error."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(ApiUnexpectedError("api unexpected")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_API_UNEXPECTED


def test_main_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """main returns exit code 1 on internal error."""
    monkeypatch.setattr(
        "pipeline_analyzer.resolve_provider",
        lambda *args: (_ for _ in ()).throw(RuntimeError("internal error")),
    )
    monkeypatch.setattr("pipeline_analyzer.emit", lambda data, code, stderr, json_mode: code)
    result = pipeline_analyzer.main(["gh", "123"])
    assert result == EXIT_INTERNAL


# -----------------------------------------------------------------------------
# duration parsing tests
# -----------------------------------------------------------------------------


def test_parse_duration_valid() -> None:
    """_parse_duration correctly parses ISO timestamps."""
    result = pipeline_analyzer._parse_duration(
        "2026-05-26T10:00:00Z", "2026-05-26T10:05:00Z"
    )
    assert result == 300.0


def test_parse_duration_missing() -> None:
    """_parse_duration returns 0.0 when timestamps missing."""
    result = pipeline_analyzer._parse_duration(None, None)
    assert result == 0.0


def test_parse_duration_invalid() -> None:
    """_parse_duration returns 0.0 on invalid timestamps."""
    result = pipeline_analyzer._parse_duration("invalid", "invalid")
    assert result == 0.0
