"""Unit tests for cicd-debug/scripts/secret_scope_audit.py v1.0."""
# ruff: noqa: PLR2004

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "cicd-debug" / "skills" / "cicd-debug" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import secret_scope_audit  # noqa: E402
from lib_cicd import (  # noqa: E402
    ApiUnexpectedError,
    AuthError,
    BadArgsError,
    CliTimeoutError,
    EnvData,
    GlabAdapter,
    NetworkError,
    NotFoundError,
    PermissionError,
    ProviderUnavailable,
    SecretData,
    WorkflowFile,
)


class FakeProvider:
    def __init__(
        self,
        secrets: list[SecretData] | None = None,
        envs: list[EnvData] | None = None,
        files: list[WorkflowFile] | None = None,
    ) -> None:
        self.secrets = secrets or []
        self.envs = envs or []
        self.files = files or []

    def list_secrets(self, scope: Any, **kwargs: Any) -> list[SecretData]:
        return self.secrets

    def list_environments(self, scope: Any) -> list[EnvData]:
        return self.envs

    def fetch_workflow_files(self, scope: Any, cap: int = 50) -> list[WorkflowFile]:
        return self.files[:cap]


def _run_json(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider, argv: list[str], capsys):
    monkeypatch.setattr(secret_scope_audit, "resolve_provider", lambda *args: provider)
    code = secret_scope_audit.main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_argparse_surface() -> None:
    args = secret_scope_audit.build_parser().parse_args(
        [
            "gh",
            "--repo",
            "owner/repo",
            "--include-codespaces",
            "--no-include-dependabot",
            "--no-workflow-scan",
            "--no-unused-flag",
            "--workflow-fetch-cap",
            "9",
            "--gh-bin",
            "/custom/gh",
            "--timeout",
            "10",
        ]
    )
    assert args.provider == "gh"
    assert args.include_codespaces is True
    assert args.include_dependabot is False
    assert args.workflow_scan is False
    assert args.unused_flag is False
    assert args.workflow_fetch_cap == 9
    assert args.gh_bin == "/custom/gh"


@pytest.mark.parametrize(
    "argv,error",
    [
        (["gh"], "exactly one"),
        (["gh", "--repo", "o/r", "--org", "org"], "exactly one"),
        (["glab"], "exactly one"),
        (["glab", "--project", "g/p", "--include-codespaces"], "GitHub-only"),
        (["gh", "--repo", "o/r", "--workflow-fetch-cap", "0"], "workflow-fetch-cap"),
    ],
)
def test_validate_scope_bad_args(argv: list[str], error: str) -> None:
    args = secret_scope_audit.build_parser().parse_args(argv)
    with pytest.raises(BadArgsError, match=error):
        secret_scope_audit.validate_scope(args)


def test_gh_risk_flags_and_summary(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    provider = FakeProvider(
        secrets=[
            SecretData("AWS_DEPLOY_KEY", "repo", "owner/repo", "actions"),
            SecretData("API_TOKEN", "repo", "owner/repo", "actions"),
            SecretData("UNUSED_SECRET", "repo", "owner/repo", "actions"),
        ],
        files=[
            WorkflowFile(
                ".github/workflows/pr.yml",
                """
on: pull_request
jobs:
  test:
    steps:
      - run: echo "${{ secrets.AWS_DEPLOY_KEY }}"
""",
                "gh_contents",
            ),
            WorkflowFile(
                ".github/workflows/target.yml",
                """
on: pull_request_target
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: echo "${{ secrets.API_TOKEN }}"
""",
                "gh_contents",
            ),
        ],
    )
    code, payload = _run_json(monkeypatch, provider, ["gh", "--repo", "owner/repo"], capsys)
    assert code == 0
    data = payload["data"]
    assert data["summary"] == "crit"
    by_name = {secret["name"]: secret for secret in data["secrets"]}
    assert by_name["AWS_DEPLOY_KEY"]["risk_flags"] == [
        "exposed_on_fork_pr",
        "long_lived_cloud_credential",
    ]
    assert by_name["API_TOKEN"]["risk_flags"] == ["pull_request_target_unsafe_checkout"]
    assert by_name["UNUSED_SECRET"]["risk_flags"] == ["unused"]
    assert by_name["AWS_DEPLOY_KEY"]["referenced_in_workflows"][0]["jobs"] == ["test"]
    assert {finding["kind"] for finding in data["findings"]} >= {
        "long_lived_cloud_credential",
        "exposed_on_fork_pr",
        "pull_request_target_unsafe_checkout",
        "unused",
    }


def test_no_workflow_scan_emits_only_name_based_flags(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = FakeProvider(
        secrets=[
            SecretData("AWS_KEY", "repo", "owner/repo", "actions"),
            SecretData("PLAIN_TOKEN", "repo", "owner/repo", "actions"),
        ],
        files=[],
    )
    code, payload = _run_json(
        monkeypatch, provider, ["gh", "--repo", "owner/repo", "--no-workflow-scan"], capsys
    )
    assert code == 0
    by_name = {secret["name"]: secret for secret in payload["data"]["secrets"]}
    assert by_name["AWS_KEY"]["risk_flags"] == ["long_lived_cloud_credential"]
    assert by_name["PLAIN_TOKEN"]["risk_flags"] == []


def test_org_scope_has_null_references_and_no_unused(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = FakeProvider(secrets=[SecretData("ORG_SECRET", "org", "myorg", "actions")])
    code, payload = _run_json(monkeypatch, provider, ["gh", "--org", "myorg"], capsys)
    assert code == 0
    secret = payload["data"]["secrets"][0]
    assert secret["referenced_in_workflows"] is None
    assert "unused" not in secret["risk_flags"]


def test_glab_variable_references_and_no_value_leak(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = FakeProvider(
        secrets=[
            SecretData(
                "DEPLOY_TOKEN",
                "project",
                "group/project",
                "ci_variable",
                masked=True,
                protected=True,
                environment_scope="production",
            )
        ],
        files=[
            WorkflowFile(
                ".gitlab-ci.yml",
                """
deploy:
  script:
    - curl -H "Authorization: ${DEPLOY_TOKEN}" example.invalid
""",
                "glab_repository_file",
            )
        ],
    )
    code, payload = _run_json(
        monkeypatch, provider, ["glab", "--project", "group/project"], capsys
    )
    assert code == 0
    dumped = json.dumps(payload)
    assert "raw-secret-value" not in dumped
    assert all("value" not in secret for secret in payload["data"]["secrets"])
    secret = payload["data"]["secrets"][0]
    assert secret["masked"] is True
    assert secret["protected"] is True
    assert secret["referenced_in_workflows"] == [{"path": ".gitlab-ci.yml", "jobs": ["deploy"]}]


def test_parse_failure_threshold_returns_178(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = FakeProvider(
        secrets=[SecretData("TOKEN", "repo", "owner/repo", "actions")],
        files=[
            WorkflowFile(".github/workflows/bad.yml", "jobs: [", "gh_contents"),
            WorkflowFile(".github/workflows/good.yml", "on: push\njobs: {}\n", "gh_contents"),
        ],
    )
    code, payload = _run_json(monkeypatch, provider, ["gh", "--repo", "owner/repo"], capsys)
    assert code == 0
    assert payload["data"]["failure_class"] is None

    provider.files.append(WorkflowFile(".github/workflows/bad2.yml", "jobs: [", "gh_contents"))
    code, payload = _run_json(monkeypatch, provider, ["gh", "--repo", "owner/repo"], capsys)
    assert code == 178
    assert payload["data"]["failure_class"] == "workflow_yaml_parse_failed"


def test_workflow_dir_missing_is_soft_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = FakeProvider(secrets=[SecretData("TOKEN", "repo", "owner/repo", "actions")])
    code, payload = _run_json(monkeypatch, provider, ["gh", "--repo", "owner/repo"], capsys)
    assert code == 0
    assert payload["data"]["failure_class"] == "workflow_dir_missing"


@pytest.mark.parametrize(
    ("exc", "failure_class", "exit_code"),
    [
        (ProviderUnavailable("gh binary not found"), "tool_missing", 170),
        (ProviderUnavailable("glab binary not found"), "provider_unavailable", 171),
        (AuthError("auth"), "auth", 172),
        (NotFoundError("not found"), "not_found", 173),
        (PermissionError("permission"), "permission", 174),
        (NetworkError("network"), "network", 175),
        (BadArgsError("bad args"), "bad_args", 176),
        (ApiUnexpectedError("api"), "api_unexpected", 177),
        (CliTimeoutError("timeout"), "timeout", 124),
        (RuntimeError("boom"), "internal", 1),
    ],
)
def test_failure_class_mapping(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    exc: Exception,
    failure_class: str,
    exit_code: int,
) -> None:
    def raise_exc(*args: Any, **kwargs: Any) -> None:
        raise exc

    monkeypatch.setattr(secret_scope_audit, "resolve_provider", raise_exc)
    code = secret_scope_audit.main(["gh", "--repo", "owner/repo", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == exit_code
    assert payload["exit_code"] == exit_code
    assert payload["data"]["failure_class"] == failure_class


def test_glab_adapter_drops_value(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    [
                        {
                            "key": "TOKEN",
                            "value": "secret",
                            "masked": True,
                            "protected": False,
                            "environment_scope": "*",
                        }
                    ]
                ),
                "stderr": "",
            },
        )()

    monkeypatch.setattr("lib_cicd.run_cli", fake_run)
    adapter = GlabAdapter(Path("/usr/bin/glab"))
    secrets = adapter.list_secrets(secret_scope_audit.Scope("project", "group/project"))
    assert seen["argv"] == [
        "/usr/bin/glab",
        "variable",
        "list",
        "--repo",
        "group/project",
        "--output",
        "json",
    ]
    assert secrets == [
        SecretData(
            "TOKEN",
            "project",
            "group/project",
            "ci_variable",
            masked=True,
            protected=False,
            environment_scope="*",
        )
    ]
    assert not hasattr(secrets[0], "value")
