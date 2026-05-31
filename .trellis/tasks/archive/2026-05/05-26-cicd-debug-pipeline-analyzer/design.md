# Design: ship cicd-debug pipeline_analyzer v1.0

## Overview

This document expands the PRD design decisions (D1-D8) into implementation-level detail for `pipeline_analyzer.py` v1.0 and the new `lib_cicd.py` provider abstraction layer.

## Module structure

```
plugins/cicd-debug/skills/cicd-debug/scripts/
├── lib_cicd.py              # NEW: ProviderAdapter protocol + adapters
├── pipeline_analyzer.py      # REWRITE: v0.2 stub → v1.0
├── runner_check.py           # existing stub (future sibling)
└── secret_scope_audit.py     # existing stub (future sibling)
```

## lib_cicd.py design

### D1: Provider abstraction

#### Protocol definition

```python
from typing import Protocol, runtime_checkable, Any
from dataclasses import dataclass
from pathlib import Path
import subprocess

@runtime_checkable
class ProviderAdapter(Protocol):
    """Protocol for CI/CD provider CLI adapters (gh/glab)."""
    
    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        """Fetch run/pipeline metadata as JSON dict."""
        ...
    
    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        """Fetch job log as string. If only_failed=True, filter server-side."""
        ...
    
    def find_last_success(self, project: str, workflow_name: str | None, branch: str) -> str | None:
        """Return run_id/pipeline_id of most recent successful run, or None."""
        ...
    
    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        """Return list of (path, content) for workflow YAML files."""
        ...
    
    # Methods used by siblings (raise NotImplementedError in pipeline_analyzer's usage)
    def list_runners(self, scope: str) -> list[dict[str, Any]]:
        """List runners (used by runner_check)."""
        raise NotImplementedError
    
    def list_secrets(self, scope: str) -> list[dict[str, Any]]:
        """List secrets (used by secret_scope_audit)."""
        raise NotImplementedError


@dataclass
class CompletedProcess:
    """Minimal subprocess result wrapper."""
    returncode: int
    stdout: str
    stderr: str


class ProviderUnavailable(Exception):
    """Raised when provider binary not found."""
    pass
```

#### resolve_provider function

```python
def resolve_provider(name: str, gh_bin: str | None = None, glab_bin: str | None = None) -> ProviderAdapter:
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
        else:
        raise ValueError(f"Unknown provider: {name}")
```

#### _resolve_binary (RTK escape)

```python
def _resolve_binary(name: str, arg_override: str | None, env_var: str) -> Path | None:
    """Resolve binary path with RTK escape (D8)."""
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
```

#### run_cli wrapper

```python
def run_cli(
    argv: list[str],
    *,
    timeout: int = 120,
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> CompletedProcess:
    """Shared subprocess wrapper with MSYS-safe env (D1)."""
    env = msys_safe_env()
    if env_overrides:
        env.update(env_overrides)
    
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
        shell=False,  # Defensive: no shell alias invocation
    )
    
    return CompletedProcess(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
```

#### GhAdapter implementation

```python
class GhAdapter:
    """GitHub Actions adapter via gh CLI."""
    
    def __init__(self, binary_path: Path):
        self.binary = binary_path
    
    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        # gh run view <id> --json
        argv = [str(self.binary), "run", "view", run_id, "--json"]
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            _map_gh_error(result.stderr)
        return json.loads(result.stdout)
    
    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        if only_failed:
            argv = [str(self.binary), "run", "view", run_id, "--log-failed"]
        else:
            argv = [str(self.binary), "run", "view", run_id, "--log", "--job", job_id]
        result = run_cli(argv, timeout=60)
        if result.returncode != 0:
            _map_gh_error(result.stderr)
        return result.stdout
    
    def find_last_success(self, project: str, workflow_name: str | None, branch: str) -> str | None:
        argv = [str(self.binary), "run", "list"]
        if workflow_name:
            argv.extend(["--workflow", workflow_name])
        argv.extend(["--branch", branch, "--status", "success", "--limit", "1", "--json", "databaseId"])
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            return None  # Graceful degrade
        runs = json.loads(result.stdout)
        return str(runs[0]["databaseId"]) if runs else None
    
    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        # gh api repos/{owner}/{repo}/actions/workflows
        owner, repo = project.split("/")
        argv = [str(self.binary), "api", f"repos/{owner}/{repo}/actions/workflows"]
        result = run_cli(argv, timeout=30)
        workflows = json.loads(result.stdout)
        files = []
        for wf in workflows.get("workflows", []):
            # Fetch each workflow YAML
            path = wf["path"]
            api_argv = [str(self.binary), "api", f"repos/{owner}/{repo}/contents/{path}"]
            api_result = run_cli(api_argv, timeout=30)
            content = json.loads(api_result.stdout)
            # content is base64-encoded
            import base64
            decoded = base64.b64decode(content["content"])
            files.append((path, decoded))
        return files
```

#### GlabAdapter implementation

```python
class GlabAdapter:
    """GitLab CI adapter via glab CLI."""
    
    def __init__(self, binary_path: Path):
        self.binary = binary_path
    
    def fetch_run(self, run_id: str, project: str | None) -> dict[str, Any]:
        # glab ci view <pipeline-id> --output json
        argv = [str(self.binary), "ci", "view", run_id, "--output", "json"]
        if project:
            argv.extend(["--repo", project])
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            _map_glab_error(result.stderr)
        return json.loads(result.stdout)
    
    def fetch_log(self, run_id: str, job_id: str, only_failed: bool) -> str:
        # glab has no --log-failed equivalent; fetch full log
        argv = [str(self.binary), "ci", "trace", job_id]
        result = run_cli(argv, timeout=60)
        if result.returncode != 0:
            _map_glab_error(result.stderr)
        return result.stdout
    
    def find_last_success(self, project: str, workflow_name: str | None, branch: str) -> str | None:
        argv = [str(self.binary), "pipeline", "list", "--repo", project, "--status", "success", "--output", "json"]
        result = run_cli(argv, timeout=30)
        if result.returncode != 0:
            return None
        pipelines = json.loads(result.stdout)
        return str(pipelines[0]["id"]) if pipelines else None
    
    def list_workflow_files(self, project: str) -> list[tuple[str, bytes]]:
        # glab api projects/:id/pipelines not directly helpful for workflows
        # Use REST API via glab api
        # For v1.0, return empty list (GitLab workflow structure differs)
        return []
```

#### Error mapping helpers

```python
def _map_gh_error(stderr: str) -> None:
    """Map gh CLI stderr to failure_class exceptions."""
    if "authentication required" in stderr.lower() or "not logged in" in stderr.lower():
        raise AuthError(stderr)
    elif "not found" in stderr.lower() or "no run with id" in stderr.lower():
        raise NotFoundError(stderr)
    elif "must have repository read permissions" in stderr.lower():
        raise PermissionError(stderr)
    elif "could not resolve" in stderr.lower() or "connection" in stderr.lower():
        raise NetworkError(stderr)
    else:
        raise ApiUnexpectedError(stderr)


def _map_glab_error(stderr: str) -> None:
    """Map glab CLI stderr to failure_class exceptions."""
    # Similar mapping for glab error patterns
    if "authentication" in stderr.lower():
        raise AuthError(stderr)
    elif "404" in stderr:
        raise NotFoundError(stderr)
    elif "403" in stderr:
        raise PermissionError(stderr)
    else:
        raise ApiUnexpectedError(stderr)
```

### Type definitions

```python
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
    steps: list[StepData]


@dataclass
class StepData:
    """Normalized step metadata."""
    name: str
    conclusion: str | None
    duration_sec: float
    failed_log_excerpt: str | None = None
    full_log: str | None = None
```

## pipeline_analyzer.py design

### Main flow

```
main()
├── Parse args (D6)
├── Resolve provider (D1)
├── Fetch run metadata
├── Fetch job states (with logs)
├── Compute summary (D4)
├── Generate findings (D5)
├── If --diff-against:
│   ├── Fetch reference run
│   ├── Compute diff_summary (D2)
│   └── Augment findings
├── Emit JSON envelope (lib.emit)
└── Return exit code (D3)
```

### Data structures

```python
@dataclass
class AnalysisResult:
    """Top-level analysis result."""
    provider: str
    run_ref: str
    run_meta: dict[str, Any]
    job_states: list[dict[str, Any]]
    failed_steps: list[dict[str, Any]]
    actions_used: list[dict[str, Any]]
    cache_summary: dict[str, Any]
    runner_used: list[dict[str, Any]]
    diff_summary: dict[str, Any] | None
    summary: str  # "ok" | "warn" | "crit"
    findings: list[dict[str, Any]]
    failure_class: str | None
    elapsed_s: float
```

### Diff algorithm (D2)

```python
def compute_diff_summary(
    current: AnalysisResult,
    reference: AnalysisResult,
) -> dict[str, Any]:
    """Compute drift between current and reference run."""
    action_drift = []
    env_drift = []
    runner_drift = []
    cache_drift = []
    
    # Action version drift
    current_actions = {a["action"]: a["ref"] for a in current.actions_used}
    ref_actions = {a["action"]: a["ref"] for a in reference.actions_used}
    for action, ref_ver in ref_actions.items():
        if action in current_actions and current_actions[action] != ref_ver:
            action_drift.append({
                "action": action,
                "from_ref": ref_ver,
                "to_ref": current_actions[action],
            })
    
    # Runner label drift
    current_runners = {r["name"]: set(r["labels"]) for r in current.runner_used}
    ref_runners = {r["name"]: set(r["labels"]) for r in reference.runner_used}
    for name, labels in ref_runners.items():
        if name in current_runners and current_runners[name] != labels:
            runner_drift.append({
                "runner_name": name,
                "from_labels": sorted(labels),
                "to_labels": sorted(current_runners[name]),
            })
    
    # Cache regression
    if reference.cache_summary["hits"] > 0 and current.cache_summary["misses"] > 0:
        cache_drift.append({
            "cache_key_prefix": "regression",
            "from": "hits",
            "to": "misses",
        })
    
    return {
        "reference_run_ref": reference.run_ref,
        "action_version_drift": action_drift,
        "env_drift": env_drift,
        "runner_label_drift": runner_drift,
        "cache_drift": cache_drift,
    }
```

### Cache summary extraction

```python
def extract_cache_summary(logs: dict[str, str]) -> dict[str, Any]:
    """Parse job logs for cache events."""
    events = []
    hits = 0
    misses = 0
    saves = 0
    
    for job_name, log in logs.items():
        # Scan for "Cache restored from key", "Cache not found", "Cache saved"
        for line in log.split("\n"):
            if "Cache restored from key" in line:
                hits += 1
                events.append({"job": job_name, "key_prefix": _extract_cache_key(line), "outcome": "hit"})
            elif "Cache not found" in line or "Cache miss" in line:
                misses += 1
                events.append({"job": job_name, "key_prefix": _extract_cache_key(line), "outcome": "miss"})
            elif "Cache saved" in line:
                saves += 1
                events.append({"job": job_name, "key_prefix": _extract_cache_key(line), "outcome": "save"})
    
    return {"hits": hits, "misses": misses, "saves": saves, "events": events}
```

### Summary computation (D4)

```python
def compute_summary(run_meta: dict[str, Any], failed_steps: list, diff_summary: dict[str, Any] | None) -> str:
    """Compute summary string per D4 truth table."""
    conclusion = run_meta.get("conclusion")
    status = run_meta.get("status")
    
    if conclusion == "success":
        base = "ok"
    elif conclusion == "failure" and failed_steps:
        base = "crit"
    elif conclusion == "cancelled":
        base = "warn"
    elif conclusion in ("timed_out", "startup_failure"):
        base = "crit"
    elif status in ("queued", "in_progress"):
        base = "warn"
    else:
        base = "warn"  # Default
    
    # Override upward for drift
    if base == "ok":
        if diff_summary:
            if diff_summary.get("action_version_drift") or (
                diff_summary.get("cache_drift") and
                any(d["from"] == "hits" for d in diff_summary["cache_drift"])
            ):
                base = "warn"
    
    return base
```

### Findings generation (D5)

```python
def generate_findings(
    job_states: list[dict],
    failed_steps: list[dict],
    diff_summary: dict[str, Any] | None,
    run_meta: dict[str, Any],
    cache_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Generate findings list per D5 kinds."""
    findings = []
    
    # failed_step (crit)
    for step in failed_steps:
        findings.append({
            "severity": "crit",
            "kind": "failed_step",
            "job": step.get("job"),
            "step": step.get("name"),
            "exit_code": step.get("exit_code"),
            "log_excerpt": step.get("log_excerpt"),
            "hint": "see references/pipeline_failures.md#failed-steps",
        })
    
    # action_version_drift (warn)
    if diff_summary:
        for drift in diff_summary.get("action_version_drift", []):
            findings.append({
                "severity": "warn",
                "kind": "action_version_drift",
                "action": drift["action"],
                "from_ref": drift["from_ref"],
                "to_ref": drift["to_ref"],
                "hint": "see references/pipeline_failures.md#action-version-drift",
            })
    
    # cache_miss_regression (warn)
    if diff_summary and diff_summary.get("cache_drift"):
        for drift in diff_summary["cache_drift"]:
            if drift["from"] == "hits":
                findings.append({
                    "severity": "warn",
                    "kind": "cache_miss_regression",
                    "cache_key_prefix": drift["cache_key_prefix"],
                    "hint": "see references/pipeline_failures.md#cache-miss",
                })
    
    # runner_label_drift (info)
    if diff_summary:
        for drift in diff_summary.get("runner_label_drift", []):
            findings.append({
                "severity": "info",
                "kind": "runner_label_drift",
                "from_labels": drift["from_labels"],
                "to_labels": drift["to_labels"],
                "hint": "see references/pipeline_failures.md#self-hosted-runner",
            })
    
    # queued_too_long (warn)
    if run_meta.get("status") == "queued":
        created_at = run_meta.get("created_at")
        if created_at:
            dwell = (datetime.now() - datetime.fromisoformat(created_at)).total_seconds()
            if dwell > 300:  # 5 minutes
                findings.append({
                    "severity": "warn",
                    "kind": "queued_too_long",
                    "dwell_sec": dwell,
                    "hint": "see references/pipeline_failures.md#self-hosted-runner",
                })
    
    return findings
```

### Exit code mapping (D3)

```python
FAILURE_CLASS_TO_EXIT = {
    None: 0,
    "tool_missing": 150,
    "provider_unavailable": 151,
    "auth": 152,
    "not_found": 153,
    "permission": 154,
    "network": 155,
    "bad_args": 156,
    "api_unexpected": 157,
    "log_truncated": 158,
    "timeout": 124,
    "internal": 1,
}
```

## Test fixtures

### Unit test fixtures

```python
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
            "name": "test",
            "status": "completed",
            "conclusion": "failure",
            "startedAt": "2026-05-26T10:01:00Z",
            "completedAt": "2026-05-26T10:05:00Z",
            "steps": [
                {"name": "checkout", "conclusion": "success", "number": 1},
                {"name": "run tests", "conclusion": "failure", "number": 2},
            ],
        }
    ],
}
```

### Integration test markers

```python
# pyproject.toml markers
[tool.pytest.ini_options]
markers = [
    "live_ssh: tests requiring live SSH access to POC_HOST",
    "cicd_gh: tests requiring GitHub CLI access to CICD_GH_REPO",
    "cicd_glab: tests requiring GitLab CLI access to CICD_GLAB_PROJECT",
]
```

## Implementation checklist

1. Create `lib_cicd.py` with:
   - ProviderAdapter protocol
   - GhAdapter and GlabAdapter
   - resolve_provider function
   - run_cli wrapper with RTK escape
   - Error mapping helpers
   - Type definitions (RunData, JobData, StepData)

2. Rewrite `pipeline_analyzer.py`:
   - Update argparse surface per D6
   - Implement main flow
   - Implement diff algorithm
   - Implement cache summary extraction
   - Implement summary computation
   - Implement findings generation
   - Wire up lib.emit for JSON envelope
   - Add failure_class mapping

3. Create `tests/test_pipeline_analyzer_unit.py`:
   - Mock lib_cicd.run_cli
   - Test argparse surface
   - Test GhAdapter argv construction
   - Test GlabAdapter argv construction
   - Test data.job_states shape
   - Test every findings kind (D5)
   - Test every failure_class (D3)
   - Test summary computation (D4)
   - Test --include-full-step-logs toggle
   - Test --gh-bin / GH_BIN override

4. Create `tests/test_pipeline_analyzer_integration.py`:
   - cicd_gh marker tests (help, success-run, failure-run, diff-last-success)
   - cicd_glab marker tests (help smoke)
   - Skip when env unset or CLI missing

5. Update `references/pipeline_failures.md`:
   - Add anchor `#failed-steps` (new section)
   - Verify existing anchors: `#action-version-drift`, `#cache-miss`, `#self-hosted-runner`

6. Update SKILL.md:
   - Replace Output contract example with v1.0 expanded data payload

7. Register markers in `pyproject.toml`

8. Run full test suite and verify acceptance criteria

## RTK verification step (D8)

During implementation.md, run:

```python
import subprocess
result = subprocess.run(["gh", "--version"], capture_output=True, text=True)
print(result.stdout)
# Check if output contains "rtk" or unexpected wrapper messages
```

Document result in PRD Notes section.
