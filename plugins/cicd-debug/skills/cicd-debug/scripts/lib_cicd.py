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
import shlex
import shutil
import subprocess
import sys
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
json_result = _LIB.json_result

from _ssh_core_locator import ssh_core_script  # type: ignore[import-not-found]  # noqa: E402
from _version import add_version_flag, handle_version

#: Host alias meaning "run gh/glab on this machine".
LOCAL_HOST = "local"

#: Exit code ssh-core uses when the transport itself is unavailable.
_EXIT_DEPENDENCY_MISSING = 3


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


@dataclass(frozen=True)
class RunnerData:
    """Normalized CI runner metadata."""
    id: str
    name: str
    os: str | None
    status: str
    busy: bool
    ephemeral: bool
    labels: tuple[dict[str, str | None], ...]
    last_heartbeat: str | None = None
    running_job: dict[str, Any] | None = None
    machine_info: dict[str, Any] | None = None


@dataclass(frozen=True)
class QueuedRun:
    """Normalized queued workflow run/job metadata."""
    id: str
    name: str | None
    created_at: str
    labels: tuple[str, ...] = ()


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
    def list_runners(self, scope: Any, *, limit: int = 5000) -> list[RunnerData]:
        """List runners (used by runner_check)."""
        raise NotImplementedError

    def list_queued_runs(self, scope: Any, *, limit: int = 200) -> list[QueuedRun]:
        """List queued runs/jobs (used by runner_check)."""
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
    host: str = LOCAL_HOST,
) -> CompletedProcess:
    """Shared subprocess wrapper with MSYS-safe env (PRD D1).

    ``host`` keeps the single call shape the adapters already use: ``local``
    (the default) runs the CLI on this machine, any other value forwards it to
    ssh-core. Keeping the routing *inside* this function means the adapters do
    not need a second code path, and tests that monkeypatch ``run_cli`` still
    observe the exact argv the CLI would receive.
    """
    if host != LOCAL_HOST:
        return run_remote_cli(host, argv, timeout=timeout, cwd=str(cwd) if cwd else None)

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


def run_remote_cli(
    host: str,
    argv: list[str],
    *,
    timeout: int = 120,
    cwd: str | None = None,
) -> CompletedProcess:
    """Run a gh/glab command on ``host`` through ssh-core's ssh_execute.py.

    Every cicd-debug verb takes a ``<host>`` first argument, mirroring the other
    diagnostic plugins: ``local`` runs here, any other alias is forwarded to
    ssh-core. Only the subprocess boundary changes -- the adapters still see a
    ``CompletedProcess`` with the remote stdout/stderr/returncode, so all the
    parsing and failure classification below is shared.

    Raises:
        ProviderUnavailable: ssh-core's ssh_execute.py is missing, or the
            transport reported auth/network/timeout/dependency failures.
    """
    ssh_execute = ssh_core_script("ssh_execute.py")
    if not ssh_execute.is_file():
        raise ProviderUnavailable(
            "ssh-core is required for a remote host; install the ssh-core "
            f"plugin or set DEVOPS_SSH_CORE_DIR (looked for {ssh_execute})"
        )

    command = " ".join(shlex.quote(part) for part in argv)
    if cwd:
        command = f"cd {shlex.quote(cwd)} && {command}"
    ssh_argv = [
        sys.executable,
        str(ssh_execute),
        host,
        command,
        "--json",
        "--timeout",
        str(timeout),
    ]
    try:
        proc = subprocess.run(
            ssh_argv,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            env=msys_safe_env(),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliTimeoutError(
            f"ssh_execute timed out after {timeout + 30}s for {host}: {argv[0]}"
        ) from exc

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ApiUnexpectedError(
            f"ssh_execute produced non-JSON output for {host}: {exc}; "
            f"stderr={proc.stderr[:200]!r}"
        ) from exc

    failure_class = (envelope.get("data") or {}).get("failure_class")
    if failure_class in {"auth", "host_key"}:
        raise AuthError(str(envelope.get("stderr") or failure_class))
    if failure_class == "network":
        raise NetworkError(str(envelope.get("stderr") or failure_class))
    if failure_class == "timeout":
        raise CliTimeoutError(str(envelope.get("stderr") or failure_class))
    if failure_class == "dependency_missing" or int(envelope.get("exit_code", 0)) == _EXIT_DEPENDENCY_MISSING:
        raise ProviderUnavailable(str(envelope.get("stderr") or failure_class))

    return CompletedProcess(
        returncode=int(envelope.get("exit_code", 0)),
        stdout=str(envelope.get("stdout") or ""),
        stderr=str(envelope.get("stderr") or ""),
    )


def preflight_provider(provider: str, host: str = LOCAL_HOST, *, timeout: int = 20) -> None:
    """Fail fast when the CLI is missing or not authenticated on ``host``.

    A remote host has its own ``gh``/``glab`` install and its own credentials:
    a working local login means nothing there. Without this check the first
    real API call fails with a generic 401 several seconds later, which reads
    like "the pipeline is broken" instead of "the runner box is not logged in".

    Runs ``<cli> auth status`` (the same verb both CLIs provide) and raises:

    * :class:`ProviderUnavailable` when the binary is missing
    * :class:`AuthError` when it exists but is not logged in

    A green preflight is not a promise that the token can see the requested
    repo; it only rules out the "nothing is set up here" case.
    """
    argv = [provider, "auth", "status"]
    result = run_cli(
        argv, timeout=timeout, host=host
    )
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode == 0:
        return
    if "not logged" in combined or "no oauth token" in combined or "unauthorized" in combined:
        raise AuthError(
            f"{provider} is not authenticated on host {host!r}; run "
            f"`{provider} auth login` there (a local login does not carry over)"
        )
    if "command not found" in combined or "not recognized" in combined:
        raise ProviderUnavailable(f"{provider} is not installed on host {host!r}")
    # Unknown failure: leave the real call to surface it with full context.
    return


def resolve_provider(
    name: str,
    gh_bin: str | None = None,
    glab_bin: str | None = None,
    host: str = LOCAL_HOST,
) -> ProviderAdapter:
    """Resolve an adapter by name, with binary override and host routing.

    ``host == "local"`` (the default) keeps the v1.0 behaviour: the CLI runs on
    this machine. Any other value routes every gh/glab invocation through
    ssh-core, which is what the rest of the marketplace does.
    """
    if name == "gh":
        binary_path = _resolve_binary("gh", gh_bin, "GH_BIN")
        if not binary_path and host == LOCAL_HOST:
            raise ProviderUnavailable("gh binary not found")
        return GhAdapter(binary_path or Path("gh"), host=host)
    if name == "glab":
        binary_path = _resolve_binary("glab", glab_bin, "GLAB_BIN")
        if not binary_path and host == LOCAL_HOST:
            raise ProviderUnavailable("glab binary not found")
        return GlabAdapter(binary_path or Path("glab"), host=host)
    raise ValueError(f"Unknown provider: {name}")


def resolve_remote_binary(name: str, host: str, arg_override: str | None, env_var: str) -> Path:
    """Resolve a gh/glab path for a *remote* host.

    The local ``shutil.which`` answer is meaningless on the other end, so the
    override/env contract is preserved and the bare binary name is used
    otherwise -- ssh-core runs the command through the remote shell, where PATH
    resolves it.
    """
    if arg_override:
        return Path(arg_override)
    env_val = os.getenv(env_var)
    if env_val:
        return Path(env_val)
    return Path(name)


_PROVIDERS = frozenset({"gh", "glab"})


def split_host_argv(argv: list[str]) -> tuple[str, list[str]]:
    """Split the optional leading ``<host>`` off a cicd-debug argv.

    Every other diagnostic plugin in this marketplace takes ``<host>`` as its
    first positional argument (``local`` means "this machine", anything else is
    an ssh-core alias). cicd-debug shipped without one, so it could only ever
    inspect the machine it ran on.

    The provider name starts the legacy form, which makes the split
    unambiguous and backward compatible::

        pipeline_analyzer.py gh 123                 -> ("local", ["gh", "123"])
        pipeline_analyzer.py lab-240 gh 123         -> ("lab-240", ["gh", "123"])

    Returns:
        ``(host, remaining_argv)``. ``host`` is ``"local"`` when no host token
        was supplied.
    """
    if argv and argv[0] not in _PROVIDERS and not argv[0].startswith("-"):
        return argv[0], argv[1:]
    return LOCAL_HOST, argv


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


def _runner_rows(data: Any, key: str, context: str) -> list[Any]:
    if isinstance(data, dict):
        rows = data.get(key)
        return _ensure_list(rows, context)
    return _ensure_list(data, context)


def _label_tuple(rows: Any, *, fallback_type: str | None = None) -> tuple[dict[str, str | None], ...]:
    labels: list[dict[str, str | None]] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                name = row.get("name")
                if isinstance(name, str):
                    label_type = row.get("type")
                    labels.append(
                        {"name": name, "type": label_type if isinstance(label_type, str) else fallback_type}
                    )
            elif isinstance(row, str):
                labels.append({"name": row, "type": fallback_type})
    return tuple(labels)


def _normalize_gh_runner(row: Any) -> RunnerData:
    if not isinstance(row, dict):
        raise ApiUnexpectedError("gh runners: expected object")
    runner_id = row.get("id")
    name = row.get("name")
    if runner_id is None or not isinstance(name, str):
        raise ApiUnexpectedError("gh runners: missing id/name")
    os_name = row.get("os")
    status = row.get("status")
    busy = row.get("busy")
    return RunnerData(
        id=str(runner_id),
        name=name,
        os=os_name if isinstance(os_name, str) else None,
        status=status if isinstance(status, str) else "offline",
        busy=bool(busy),
        ephemeral=bool(row.get("ephemeral")),
        labels=_label_tuple(row.get("labels"), fallback_type=None),
        last_heartbeat=None,
        running_job=None,
        machine_info={"os": os_name} if isinstance(os_name, str) else {},
    )


def _normalize_gh_queued_run(row: Any) -> QueuedRun:
    if not isinstance(row, dict):
        raise ApiUnexpectedError("gh queued runs: expected object")
    run_id = row.get("id") or row.get("database_id") or row.get("databaseId")
    created_at = row.get("created_at") or row.get("createdAt")
    if run_id is None or not isinstance(created_at, str):
        raise ApiUnexpectedError("gh queued runs: missing id/created_at")
    name = row.get("name") or row.get("display_title") or row.get("displayTitle")
    return QueuedRun(
        id=str(run_id),
        name=name if isinstance(name, str) else None,
        created_at=created_at,
        labels=tuple(),
    )


def _normalize_glab_runner(row: Any) -> RunnerData:
    if not isinstance(row, dict):
        raise ApiUnexpectedError("glab runners: expected object")
    runner_id = row.get("id")
    name = row.get("description") or row.get("name")
    if runner_id is None or not isinstance(name, str):
        raise ApiUnexpectedError("glab runners: missing id/name")
    platform = row.get("platform")
    status_value = row.get("status")
    online = row.get("online")
    status = "online" if online is True or status_value == "online" else "offline"
    machine_info = {
        key: row[key]
        for key in ("platform", "architecture", "version", "runner_type")
        if key in row and row[key] is not None
    }
    return RunnerData(
        id=str(runner_id),
        name=name,
        os=platform if isinstance(platform, str) else None,
        status=status,
        busy=bool(row.get("busy")),
        ephemeral=bool(row.get("is_ephemeral") or row.get("ephemeral")),
        labels=_label_tuple(row.get("tag_list"), fallback_type="tag"),
        last_heartbeat=row.get("contacted_at") if isinstance(row.get("contacted_at"), str) else None,
        running_job=None,
        machine_info=machine_info,
    )


def _normalize_glab_queued_run(row: Any) -> QueuedRun:
    if not isinstance(row, dict):
        raise ApiUnexpectedError("glab queued jobs: expected object")
    job_id = row.get("id")
    created_at = row.get("created_at") or row.get("createdAt")
    if job_id is None or not isinstance(created_at, str):
        raise ApiUnexpectedError("glab queued jobs: missing id/created_at")
    name = row.get("name")
    tags = row.get("tag_list") or row.get("tags")
    labels = tuple(str(tag) for tag in tags) if isinstance(tags, list) else tuple()
    return QueuedRun(
        id=str(job_id),
        name=name if isinstance(name, str) else None,
        created_at=created_at,
        labels=labels,
    )


class _HostRouted:
    """Mixin: pass the adapter's host down to :func:`run_cli`."""

    def __init__(self, binary_path: Path, host: str = LOCAL_HOST) -> None:
        self.binary = binary_path
        self.host = host

    def run_cli(
        self,
        argv: list[str],
        *,
        timeout: int = 120,
        env_overrides: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CompletedProcess:
        """Run ``argv`` locally, or on the configured host via ssh-core."""
        return run_cli(
            argv,
            timeout=timeout,
            env_overrides=env_overrides,
            cwd=cwd,
            host=self.host,
        )


class GhAdapter(_HostRouted):
    """GitHub Actions adapter via gh CLI."""

    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        """Fetch GitHub Actions run metadata."""
        argv = [str(self.binary), "run", "view", run_id, "--json"]
        result = self.run_cli(argv, timeout=30)
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
        result = self.run_cli(argv, timeout=60)
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
        result = self.run_cli(argv, timeout=30)
        if result.returncode != 0:
            return None  # Graceful degrade
        runs = json.loads(result.stdout)
        return str(runs[0]["databaseId"]) if runs else None

    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        """List GitHub Actions workflow YAML files."""
        owner, repo = project.split("/")
        argv = [str(self.binary), "api", f"repos/{owner}/{repo}/actions/workflows"]
        result = self.run_cli(argv, timeout=30)
        workflows = json.loads(result.stdout)
        files = []
        for wf in workflows.get("workflows", []):
            path = wf["path"]
            api_argv = [
                str(self.binary),
                "api",
                f"repos/{owner}/{repo}/contents/{path}",
            ]
            api_result = self.run_cli(api_argv, timeout=30)
            content = json.loads(api_result.stdout)
            decoded = base64.b64decode(content["content"])
            files.append((path, decoded))
        return files

    def list_runners(self, scope: Any, *, limit: int = 5000) -> list[RunnerData]:
        """List GitHub runners (used by runner_check)."""
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind == "repo":
            owner, repo = scope_value.split("/", 1)
            endpoint = f"repos/{owner}/{repo}/actions/runners"
        elif scope_kind == "org":
            endpoint = f"orgs/{scope_value}/actions/runners"
        elif scope_kind == "enterprise":
            endpoint = f"enterprises/{scope_value}/actions/runners"
        else:
            raise BadArgsError("gh supports repo, org, or enterprise runner scope")
        data = self._run_json([str(self.binary), "api", endpoint, "--paginate"], timeout=60)
        rows = _runner_rows(data, "runners", "gh runners")
        return [_normalize_gh_runner(row) for row in rows[:limit]]

    def list_queued_runs(self, scope: Any, *, limit: int = 200) -> list[QueuedRun]:
        """List queued GitHub Actions runs for repo scope."""
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind != "repo":
            return []
        owner, repo = scope_value.split("/", 1)
        endpoint = f"repos/{owner}/{repo}/actions/runs?status=queued&per_page=100"
        data = self._run_json([str(self.binary), "api", endpoint, "--paginate"], timeout=60)
        rows = _runner_rows(data, "workflow_runs", "gh queued runs")
        return [_normalize_gh_queued_run(row) for row in rows[:limit]]

    def _run_json(self, argv: list[str], *, timeout: int = 120) -> Any:
        result = self.run_cli(argv, timeout=timeout)
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
        list_result = self.run_cli(
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


class GlabAdapter(_HostRouted):
    """GitLab CI adapter via glab CLI."""

    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        """Fetch GitLab CI pipeline metadata."""
        argv = [str(self.binary), "ci", "view", run_id, "--output", "json"]
        if project:
            argv.extend(["--repo", project])
        result = self.run_cli(argv, timeout=30)
        if result.returncode != 0:
            _map_glab_error(result.stderr)
        return json.loads(result.stdout)

    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        """Fetch GitLab CI job log."""
        # glab has no --log-failed equivalent; fetch full log
        argv = [str(self.binary), "ci", "trace", job_id]
        result = self.run_cli(argv, timeout=60)
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
        result = self.run_cli(argv, timeout=30)
        if result.returncode != 0:
            return None
        pipelines = json.loads(result.stdout)
        return str(pipelines[0]["id"]) if pipelines else None

    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        """List GitLab CI workflow YAML files (.gitlab-ci.yml)."""
        # GitLab uses a single .gitlab-ci.yml at repo root
        # For v1.0, return empty list (GitLab workflow structure differs)
        return []

    def list_runners(self, scope: Any, *, limit: int = 5000) -> list[RunnerData]:
        """List GitLab runners (used by runner_check)."""
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind == "project":
            argv = [str(self.binary), "runner", "list", "--repo", scope_value, "--output", "json"]
        elif scope_kind == "group":
            argv = [str(self.binary), "runner", "list", "--group", scope_value, "--output", "json"]
        elif scope_kind == "instance":
            argv = [str(self.binary), "runner", "list", "--instance", "--output", "json"]
        else:
            raise BadArgsError("glab supports project, group, or instance runner scope")
        rows = _ensure_list(self._run_json(argv, timeout=60), "glab runners")
        return [_normalize_glab_runner(row) for row in rows[:limit]]

    def list_queued_runs(self, scope: Any, *, limit: int = 200) -> list[QueuedRun]:
        """List queued GitLab jobs for project scope."""
        scope_kind, scope_value = _scope_parts(scope)
        if scope_kind != "project":
            return []
        encoded = quote(scope_value, safe="")
        endpoint = f"projects/{encoded}/jobs?scope[]=pending&per_page=100"
        rows = _ensure_list(
            self._run_json([str(self.binary), "api", endpoint], timeout=60),
            "glab queued jobs",
        )
        return [_normalize_glab_queued_run(row) for row in rows[:limit]]

    def _run_json(self, argv: list[str], *, timeout: int = 120) -> Any:
        result = self.run_cli(argv, timeout=timeout)
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
        file_result = self.run_cli(
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
    _early = handle_version("lib_cicd.py")
    if _early is not None:
        raise SystemExit(_early)
    _build_help_parser().parse_args()
