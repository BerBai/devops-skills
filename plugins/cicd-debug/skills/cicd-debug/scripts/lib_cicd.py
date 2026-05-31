#!/usr/bin/env python3
"""lib_cicd.py — CI/CD provider abstraction layer (v1.0).

Provides ProviderAdapter protocol for GitHub Actions (gh CLI) and GitLab CI (glab CLI).
Shared subprocess wrapper with RTK escape and MSYS-safe environment.

See: .trellis/tasks/05-26-cicd-debug-pipeline-analyzer/prd.md D1-D8
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

SCRIPTS_DIR = Path(__file__).resolve().parent
_LIB_SPEC = importlib.util.spec_from_file_location(
    "cicd_debug_lib", SCRIPTS_DIR / "lib" / "__init__.py"
)
if _LIB_SPEC is None or _LIB_SPEC.loader is None:
    raise RuntimeError("could not load cicd-debug local lib")
_LIB = importlib.util.module_from_spec(_LIB_SPEC)
_LIB_SPEC.loader.exec_module(_LIB)
msys_safe_env = _LIB.msys_safe_env


# Exception hierarchy for failure_class mapping
class ProviderUnavailable(Exception):
    """Raised when provider binary not found."""
    pass


class AuthError(Exception):
    """Raised when authentication fails (401 / not logged in)."""
    pass


class NotFoundError(Exception):
    """Raised when resource not found (404)."""
    pass


class PermissionError(Exception):
    """Raised when permission denied (403)."""
    pass


class NetworkError(Exception):
    """Raised when network/DNS fails."""
    pass


class BadArgsError(Exception):
    """Raised when CLI arguments are invalid."""
    pass


class ApiUnexpectedError(Exception):
    """Raised when API returns unexpected JSON shape."""
    pass


class CliTimeoutError(Exception):
    """Raised when a provider CLI command times out."""
    pass


class LogTruncatedError(Exception):
    """Raised when log exceeds size cap."""
    pass


@dataclass
class CompletedProcess:
    """Minimal subprocess result wrapper."""
    returncode: int
    stdout: str
    stderr: str


@dataclass
class RunData:
    """Normalized run/pipeline metadata."""
    workflow_name: str
    branch: str
    head_sha: str
    event: str
    status: str
    conclusion: str | None
    created_at: str
    url: str
    raw: dict[str, Any]


@dataclass
class JobData:
    """Normalized job metadata."""
    name: str
    status: str
    conclusion: str | None
    runner_name: str | None
    runner_labels: list[str]
    duration_sec: float
    retry_count: int
    steps: list[dict[str, Any]]


@dataclass(frozen=True)
class SecretData:
    """Normalized secret/variable metadata. Never stores raw values."""
    name: str
    scope_kind: str
    scope_value: str
    kind: str
    visibility: str | None = None
    num_selected_repos: int | None = None
    masked: bool | None = None
    protected: bool | None = None
    environment_scope: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class EnvData:
    """Normalized GitHub environment metadata."""
    name: str
    has_protection_rules: bool
    scoped_secret_count: int


@dataclass(frozen=True)
class WorkflowFile:
    """Workflow config content fetched from a provider."""
    path: str
    content: str
    source: str


@runtime_checkable
class ProviderAdapter(Protocol):
    """Protocol for CI/CD provider CLI adapters (gh/glab)."""

    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        """Fetch run/pipeline metadata as JSON dict."""
        ...

    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        """Fetch job log as string. If only_failed=True, filter server-side."""
        ...

    def find_last_success(
        self, project: str, workflow_name: str | None, branch: str
    ) -> str | None:
        """Return run_id/pipeline_id of most recent successful run, or None."""
        ...

    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        """Return list of (path, content) for workflow YAML files."""
        ...

    # Methods used by siblings (raise NotImplementedError in pipeline_analyzer's usage)
    def list_runners(self, scope: str) -> list[dict[str, Any]]:
        """List runners (used by runner_check)."""
        raise NotImplementedError

    def list_secrets(
        self,
        scope: Any,
        *,
        include_codespaces: bool = False,
        include_dependabot: bool = True,
    ) -> list[SecretData]:
        """List secrets (used by secret_scope_audit)."""
        raise NotImplementedError

    def list_environments(self, scope: Any) -> list[EnvData]:
        """List environments (used by secret_scope_audit)."""
        raise NotImplementedError

    def fetch_workflow_files(self, scope: Any, cap: int = 50) -> list[WorkflowFile]:
        """Fetch workflow YAML files (used by secret_scope_audit)."""
        raise NotImplementedError


def _resolve_binary(name: str, arg_override: str | None, env_var: str) -> Path | None:
    """Resolve binary path with RTK escape (PRD D8)."""
    # 1. CLI arg override (highest precedence)
    if arg_override:
        return Path(arg_override)

    # 2. Env var override
    env_val = os.getenv(env_var)
    if env_val:
        return Path(env_val)

    # 3. shutil.which with RTK check
    which_path = shutil.which(name)
    if which_path:
        path = Path(which_path)
        # RTK escape: if path contains "rtk", scan for real binary
        if "rtk" in str(path).lower():
            for search_dir in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]:
                candidate = Path(search_dir) / name
                if candidate.exists():
                    return candidate
        return path

    return None


def run_cli(
    argv: list[str],
    *,
    timeout: int = 120,
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> CompletedProcess:
    """Shared subprocess wrapper with MSYS-safe env (PRD D1)."""
    env = msys_safe_env()
    if env_overrides:
        env.update(env_overrides)

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
            shell=False,  # Defensive: no shell alias invocation
        )
    except subprocess.TimeoutExpired as exc:
        raise CliTimeoutError(f"command timed out after {timeout}s: {argv[0]}") from exc

    return CompletedProcess(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def resolve_provider(
    name: str, gh_bin: str | None = None, glab_bin: str | None = None
) -> ProviderAdapter:
    """Resolve provider adapter by name, with binary override support."""
    if name == "gh":
        binary_path = _resolve_binary("gh", gh_bin, "GH_BIN")
        if not binary_path:
            raise ProviderUnavailable("gh binary not found")
        return GhAdapter(binary_path)
    elif name == "glab":
        binary_path = _resolve_binary("glab", glab_bin, "GLAB_BIN")
        if not binary_path:
            raise ProviderUnavailable("glab binary not found")
        return GlabAdapter(binary_path)
    else:
        raise ValueError(f"Unknown provider: {name}")


def _map_gh_error(stderr: str) -> None:
    """Map gh CLI stderr to failure_class exceptions."""
    stderr_lower = stderr.lower()
    if "authentication required" in stderr_lower or "not logged in" in stderr_lower or "401" in stderr:
        raise AuthError(stderr)
    elif "not found" in stderr_lower or "no run with id" in stderr_lower or "404" in stderr:
        raise NotFoundError(stderr)
    elif (
        "must have repository read permissions" in stderr_lower
        or "403" in stderr
        or "must be a member" in stderr_lower
        or "admin" in stderr_lower
        or "permission" in stderr_lower
    ):
        raise PermissionError(stderr)
    elif (
        "could not resolve" in stderr_lower
        or "connection" in stderr_lower
        or "timeout" in stderr_lower
        or "502" in stderr
        or "503" in stderr
        or "504" in stderr
    ):
        raise NetworkError(stderr)
    else:
        raise ApiUnexpectedError(stderr)


def _map_glab_error(stderr: str) -> None:
    """Map glab CLI stderr to failure_class exceptions."""
    stderr_lower = stderr.lower()
    if "authentication" in stderr_lower or "401" in stderr:
        raise AuthError(stderr)
    elif "404" in stderr:
        raise NotFoundError(stderr)
    elif "403" in stderr or "permission" in stderr_lower:
        raise PermissionError(stderr)
    elif (
        "could not resolve" in stderr_lower
        or "connection" in stderr_lower
        or "timeout" in stderr_lower
        or "502" in stderr
        or "503" in stderr
        or "504" in stderr
    ):
        raise NetworkError(stderr)
    else:
        raise ApiUnexpectedError(stderr)


def _json_loads(stdout: str, context: str) -> Any:
    try:
        return json.loads(stdout or "null")
    except json.JSONDecodeError as exc:
        raise ApiUnexpectedError(f"{context}: invalid JSON") from exc


def _scope_parts(scope: Any) -> tuple[str, str]:
    if hasattr(scope, "kind") and hasattr(scope, "value"):
        return str(scope.kind), str(scope.value)
    if isinstance(scope, str) and ":" in scope:
        kind, value = scope.split(":", 1)
        return kind, value
    if isinstance(scope, str):
        return "repo", scope
    raise BadArgsError(f"unsupported scope: {scope!r}")


def _decode_content(value: str) -> str:
    cleaned = "".join(value.split())
    try:
        return base64.b64decode(cleaned).decode("utf-8", errors="replace")
    except (ValueError, TypeError) as exc:
        raise ApiUnexpectedError("content was not valid base64") from exc


def _ensure_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ApiUnexpectedError(f"{context}: expected list")
    return value


class GhAdapter:
    """GitHub Actions adapter via gh CLI."""

    def __init__(self, binary_path: Path):
        self.binary = binary_path

    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        """Fetch GitHub Actions run metadata."""
        argv = [str(self.binary), "run", "view", run_id, "--json"]
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            _map_gh_error(result.stderr)
        return json.loads(result.stdout)

    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        """Fetch GitHub Actions job log."""
        if only_failed:
            argv = [str(self.binary), "run", "view", run_id, "--log-failed"]
        else:
            argv = [
                str(self.binary),
                "run",
                "view",
                run_id,
                "--log",
                "--job",
                job_id,
            ]
        result = run_cli(argv, timeout=60)
        if result.returncode != 0:
            _map_gh_error(result.stderr)
        return result.stdout

    def find_last_success(
        self, project: str, workflow_name: str | None, branch: str
    ) -> str | None:
        """Find most recent successful GitHub Actions run."""
        argv = [str(self.binary), "run", "list"]
        if workflow_name:
            argv.extend(["--workflow", workflow_name])
        argv.extend(
            [
                "--branch",
                branch,
                "--status",
                "success",
                "--limit",
                "1",
                "--json",
                "databaseId",
            ]
        )
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            return None  # Graceful degrade
        runs = json.loads(result.stdout)
        return str(runs[0]["databaseId"]) if runs else None

    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        """List GitHub Actions workflow YAML files."""
        owner, repo = project.split("/")
        argv = [str(self.binary), "api", f"repos/{owner}/{repo}/actions/workflows"]
        result = run_cli(argv, timeout=30)
        workflows = json.loads(result.stdout)
        files = []
        for wf in workflows.get("workflows", []):
            path = wf["path"]
            api_argv = [
                str(self.binary),
                "api",
                f"repos/{owner}/{repo}/contents/{path}",
            ]
            api_result = run_cli(api_argv, timeout=30)
            content = json.loads(api_result.stdout)
            decoded = base64.b64decode(content["content"])
            files.append((path, decoded))
        return files

    def list_runners(self, scope: str) -> list[dict[str, Any]]:
        """List GitHub runners (used by runner_check)."""
        raise NotImplementedError

    def _run_json(self, argv: list[str], *, timeout: int = 120) -> Any:
        result = run_cli(argv, timeout=timeout)
        if result.returncode != 0:
            _map_gh_error(result.stderr)
        return _json_loads(result.stdout, argv[1] if len(argv) > 1 else "gh")

    def list_secrets(
        self,
        scope: Any,
        *,
        include_codespaces: bool = False,
        include_dependabot: bool = True,
    ) -> list[SecretData]:
        """List GitHub secrets for repo/org scopes."""
        scope_kind, scope_value = _scope_parts(scope)
        secrets: list[SecretData] = []

        if scope_kind == "org":
            rows = _ensure_list(
                self._run_json(
                    [
                        str(self.binary),
                        "secret",
                        "list",
                        "-o",
                        scope_value,
                        "--json",
                        "name,visibility,updatedAt,numSelectedRepos,selectedReposURL",
                    ],
                    timeout=60,
                ),
                "gh org secrets",
            )
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise ApiUnexpectedError("gh org secrets: missing name")
                secrets.append(
                    SecretData(
                        name=row["name"],
                        scope_kind="org",
                        scope_value=scope_value,
                        kind="actions",
                        visibility=row.get("visibility"),
                        num_selected_repos=row.get("numSelectedRepos"),
                        updated_at=row.get("updatedAt"),
                    )
                )
            return secrets

        if scope_kind != "repo":
            raise BadArgsError("gh supports repo or org scope")

        kinds = [("actions", ["-a", "actions"], "repo")]
        if include_dependabot:
            kinds.append(("dependabot", ["-a", "dependabot"], "dependabot"))
        if include_codespaces:
            kinds.append(("codespaces", ["-a", "codespaces"], "codespaces"))

        for kind, kind_args, secret_scope_kind in kinds:
            rows = _ensure_list(
                self._run_json(
                    [
                        str(self.binary),
                        "secret",
                        "list",
                        "-R",
                        scope_value,
                        *kind_args,
                        "--json",
                        "name,updatedAt",
                    ],
                    timeout=60,
                ),
                f"gh {kind} secrets",
            )
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise ApiUnexpectedError(f"gh {kind} secrets: missing name")
                secrets.append(
                    SecretData(
                        name=row["name"],
                        scope_kind=secret_scope_kind,
                        scope_value=scope_value,
                        kind=kind,
                        updated_at=row.get("updatedAt"),
                    )
                )

        for env in self.list_environments(scope):
            rows = _ensure_list(
                self._run_json(
                    [
                        str(self.binary),
                        "secret",
                        "list",
                        "-R",
                        scope_value,
                        "-e",
                        env.name,
                        "--json",
                        "name,updatedAt",
                    ],
                    timeout=60,
                ),
                f"gh env {env.name} secrets",
            )
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise ApiUnexpectedError(f"gh env {env.name} secrets: missing name")
                secrets.append(
                    SecretData(
                        name=row["name"],
                        scope_kind="env",
                        scope_value=f"env:{env.name}",
                        kind="actions",
                        updated_at=row.get("updatedAt"),
                    )
                )

        return secrets

    def list_environments(self, scope: Any) -> list[EnvData]:
        """List GitHub environments for repo scope."""
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind != "repo":
            return []
        owner, repo = scope_value.split("/", 1)
        data = self._run_json(
            [str(self.binary), "api", f"repos/{owner}/{repo}/environments", "--paginate"],
            timeout=60,
        )
        rows = data.get("environments", []) if isinstance(data, dict) else data
        envs = []
        for row in _ensure_list(rows, "gh environments"):
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise ApiUnexpectedError("gh environments: missing name")
            rules = row.get("protection_rules") or []
            envs.append(
                EnvData(
                    name=row["name"],
                    has_protection_rules=bool(rules),
                    scoped_secret_count=int(row.get("secrets_count") or 0),
                )
            )
        return envs

    def fetch_workflow_files(self, scope: Any, cap: int = 50) -> list[WorkflowFile]:
        """Fetch GitHub workflow YAML content for repo scope."""
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind != "repo":
            return []
        owner, repo = scope_value.split("/", 1)
        list_result = run_cli(
            [str(self.binary), "api", f"repos/{owner}/{repo}/contents/.github/workflows"],
            timeout=60,
        )
        if list_result.returncode != 0:
            if "not found" in list_result.stderr.lower() or "404" in list_result.stderr:
                return []
            _map_gh_error(list_result.stderr)
        entries = _ensure_list(
            _json_loads(list_result.stdout, "gh workflow directory"),
            "gh workflow directory",
        )
        yaml_entries = sorted(
            [
                e
                for e in entries
                if isinstance(e, dict)
                and e.get("type") == "file"
                and isinstance(e.get("path"), str)
                and e["path"].endswith((".yml", ".yaml"))
            ],
            key=lambda e: e["path"],
        )[:cap]
        files: list[WorkflowFile] = []
        for entry in yaml_entries:
            data = self._run_json(
                [str(self.binary), "api", f"repos/{owner}/{repo}/contents/{entry['path']}"],
                timeout=60,
            )
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                raise ApiUnexpectedError(f"gh workflow content: {entry['path']}")
            files.append(
                WorkflowFile(
                    path=entry["path"],
                    content=_decode_content(data["content"]),
                    source="gh_contents",
                )
            )
        return files


class GlabAdapter:
    """GitLab CI adapter via glab CLI."""

    def __init__(self, binary_path: Path):
        self.binary = binary_path

    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        """Fetch GitLab CI pipeline metadata."""
        argv = [str(self.binary), "ci", "view", run_id, "--output", "json"]
        if project:
            argv.extend(["--repo", project])
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            _map_glab_error(result.stderr)
        return json.loads(result.stdout)

    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        """Fetch GitLab CI job log."""
        # glab has no --log-failed equivalent; fetch full log
        argv = [str(self.binary), "ci", "trace", job_id]
        result = run_cli(argv, timeout=60)
        if result.returncode != 0:
            _map_glab_error(result.stderr)
        return result.stdout

    def find_last_success(
        self, project: str, workflow_name: str | None, branch: str
    ) -> str | None:
        """Find most recent successful GitLab CI pipeline."""
        argv = [
            str(self.binary),
            "pipeline",
            "list",
            "--repo",
            project,
            "--status",
            "success",
            "--output",
            "json",
        ]
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            return None
        pipelines = json.loads(result.stdout)
        return str(pipelines[0]["id"]) if pipelines else None

    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        """List GitLab CI workflow YAML files (.gitlab-ci.yml)."""
        # GitLab uses a single .gitlab-ci.yml at repo root
        # For v1.0, return empty list (GitLab workflow structure differs)
        return []

    def list_runners(self, scope: str) -> list[dict[str, Any]]:
        """List GitLab runners (used by runner_check)."""
        raise NotImplementedError

    def _run_json(self, argv: list[str], *, timeout: int = 120) -> Any:
        result = run_cli(argv, timeout=timeout)
        if result.returncode != 0:
            _map_glab_error(result.stderr)
        return _json_loads(result.stdout, argv[1] if len(argv) > 1 else "glab")

    def list_secrets(
        self,
        scope: Any,
        *,
        include_codespaces: bool = False,
        include_dependabot: bool = True,
    ) -> list[SecretData]:
        """List GitLab CI/CD variables for project/group scopes."""
        del include_codespaces, include_dependabot
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind == "project":
            argv = [str(self.binary), "variable", "list", "--repo", scope_value, "--output", "json"]
        elif scope_kind == "group":
            argv = [str(self.binary), "variable", "list", "--group", scope_value, "--output", "json"]
        else:
            raise BadArgsError("glab supports project or group scope")
        rows = _ensure_list(self._run_json(argv, timeout=60), "glab variables")
        secrets: list[SecretData] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ApiUnexpectedError("glab variables: expected object")
            name = row.get("key") or row.get("name")
            if not isinstance(name, str):
                raise ApiUnexpectedError("glab variables: missing key")
            secrets.append(
                SecretData(
                    name=name,
                    scope_kind=scope_kind,
                    scope_value=scope_value,
                    kind="ci_variable",
                    masked=row.get("masked"),
                    protected=row.get("protected"),
                    environment_scope=row.get("environment_scope"),
                    updated_at=row.get("updated_at"),
                )
            )
        return secrets

    def list_environments(self, scope: Any) -> list[EnvData]:
        """GitLab environments are out of scope for secret_scope_audit v1.0."""
        return []

    def fetch_workflow_files(self, scope: Any, cap: int = 50) -> list[WorkflowFile]:
        """Fetch `.gitlab-ci.yml` content for project scope."""
        del cap
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind != "project":
            return []
        encoded = quote(scope_value, safe="")
        meta = self._run_json([str(self.binary), "api", f"projects/{encoded}"], timeout=60)
        default_branch = "main"
        if isinstance(meta, dict) and isinstance(meta.get("default_branch"), str):
            default_branch = meta["default_branch"]
        file_result = run_cli(
            [
                str(self.binary),
                "api",
                f"projects/{encoded}/repository/files/.gitlab-ci.yml?ref={quote(default_branch, safe='')}",
            ],
            timeout=60,
        )
        if file_result.returncode != 0:
            if "404" in file_result.stderr or "not found" in file_result.stderr.lower():
                return []
            _map_glab_error(file_result.stderr)
        data = _json_loads(file_result.stdout, "glab repository file")
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            raise ApiUnexpectedError("glab repository file: missing content")
        return [
            WorkflowFile(
                path=".gitlab-ci.yml",
                content=_decode_content(data["content"]),
                source="glab_repository_file",
            )
        ]


def _build_help_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lib_cicd.py",
        description="Shared provider adapter module for cicd-debug scripts.",
    )
    return parser


if __name__ == "__main__":
    _build_help_parser().parse_args()
