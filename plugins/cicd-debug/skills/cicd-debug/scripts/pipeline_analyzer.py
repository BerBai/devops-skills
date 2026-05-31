#!/usr/bin/env python3
# ruff: noqa: E402, I001, SIM102, PLR0912, PLR0915, PLR2004
"""pipeline_analyzer.py — single CI run analysis (v1.0).

v1.0 surface:
    pipeline_analyzer.py <provider> <run-id|pipeline-id>
        [--project <owner/repo or group/proj>]
        [--diff-against last-success | <other-run-id>]
        [--log-size-cap-mb N]
        [--include-full-step-logs]
        [--gh-bin <path>] [--glab-bin <path>]
        [--timeout N]
        [--json]

provider:
    gh    → uses `gh` CLI (requires gh auth login)
    glab  → uses `glab` CLI (requires glab auth login)

Analysis:
    job_states         per-job status + duration + retry count
    failed_steps       step-level failure with last 20 log lines
    diff_summary       vs reference run: action versions, env, runners, cache
    cache_summary      hit/miss/save events
    actions_used       full list of actions/<repo>@<ref> seen
    runner_used        runner_id / labels / runs_on
    summary            ok | warn | crit + top findings + hint links

See: .trellis/tasks/05-26-cicd-debug-pipeline-analyzer/prd.md
See: lib_cicd.py (provider abstraction)
See: references/pipeline_failures.md (failure patterns)
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Add repo root to sys.path for lib imports
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
# Add scripts directory for lib_cicd import
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

_LIB_SPEC = importlib.util.spec_from_file_location(
    "cicd_debug_lib", SCRIPTS_DIR / "lib" / "__init__.py"
)
if _LIB_SPEC is None or _LIB_SPEC.loader is None:
    raise RuntimeError("could not load cicd-debug local lib")
_LIB = importlib.util.module_from_spec(_LIB_SPEC)
_LIB_SPEC.loader.exec_module(_LIB)
emit = _LIB.emit
from lib_cicd import (  # type: ignore[import-not-found]
    ApiUnexpectedError,
    AuthError,
    BadArgsError,
    LogTruncatedError,
    NetworkError,
    NotFoundError,
    PermissionError,
    ProviderUnavailable,
    resolve_provider,
)

# Failure class to exit code mapping (PRD D3)
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

DEFAULT_LOG_SIZE_CAP_MB = 5
DEFAULT_TIMEOUT_SEC = 120
BYTES_PER_MB = 1024 * 1024


class _PipelineArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.provider == "glab" and not parsed.project:
            self.error("--project is required for glab")
        return parsed


def build_parser() -> argparse.ArgumentParser:
    p = _PipelineArgumentParser(prog="pipeline_analyzer.py")
    p.add_argument("provider", choices=["gh", "glab"], help="CI provider: gh or glab")
    p.add_argument("run_id", help="GitHub run-id or GitLab pipeline-id")
    p.add_argument(
        "--project",
        default=None,
        help="owner/repo (gh) or group/project (glab) — required for glab",
    )
    p.add_argument(
        "--diff-against",
        default=None,
        help='"last-success" or another run/pipeline id',
    )
    p.add_argument(
        "--log-size-cap-mb",
        type=int,
        default=DEFAULT_LOG_SIZE_CAP_MB,
        help=f"Log size cap in MB (default: {DEFAULT_LOG_SIZE_CAP_MB})",
    )
    p.add_argument(
        "--include-full-step-logs",
        action="store_true",
        help="Include full step logs in output (default: off)",
    )
    p.add_argument("--gh-bin", default=None, help="Path to gh binary (overrides GH_BIN env)")
    p.add_argument(
        "--glab-bin", default=None, help="Path to glab binary (overrides GLAB_BIN env)"
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Subprocess timeout in seconds (default: {DEFAULT_TIMEOUT_SEC})",
    )
    p.add_argument("--json", action="store_true")
    return p


def _extract_cache_key(line: str) -> str:
    """Extract cache key prefix from log line."""
    # Heuristic: extract key after "Cache restored from key" or similar
    if "Cache restored from key" in line:
        parts = line.split("key:")
        if len(parts) > 1:
            return parts[1].split()[0].strip()
    return "unknown"


def extract_cache_summary(logs: dict[str, str]) -> dict[str, Any]:
    """Parse job logs for cache events (PRD D2)."""
    events = []
    hits = 0
    misses = 0
    saves = 0

    for job_name, log in logs.items():
        # Scan for cache events
        for line in log.split("\n"):
            if "Cache restored from key" in line or "Cache Size:" in line:
                hits += 1
                events.append(
                    {"job": job_name, "key_prefix": _extract_cache_key(line), "outcome": "hit"}
                )
            elif "Cache not found" in line or "Cache miss" in line:
                misses += 1
                events.append(
                    {"job": job_name, "key_prefix": _extract_cache_key(line), "outcome": "miss"}
                )
            elif "Cache saved" in line:
                saves += 1
                events.append(
                    {"job": job_name, "key_prefix": _extract_cache_key(line), "outcome": "save"}
                )

    return {"hits": hits, "misses": misses, "saves": saves, "events": events}


def extract_actions_used(logs: dict[str, str]) -> list[dict[str, Any]]:
    """Extract actions/<repo>@<ref> from job logs."""
    actions: dict[str, dict[str, Any]] = {}
    for job_name, log in logs.items():
        for line in log.split("\n"):
            # Match patterns like "actions/checkout@v3" or "actions/setup-python@v4"
            if "actions/" in line and "@" in line:
                # Extract action@ref pattern
                parts = line.split()
                for part in parts:
                    if part.startswith("actions/") and "@" in part:
                        action, ref = part.split("@", 1)
                        # Clean up trailing punctuation
                        ref = ref.rstrip("',\"")
                        if action not in actions:
                            actions[action] = {"action": action, "ref": ref, "used_in": []}
                        if job_name not in actions[action]["used_in"]:
                            actions[action]["used_in"].append(job_name)

    return list(actions.values())


def extract_runner_used(run_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract runner info from run data."""
    runners = {}
    jobs = run_data.get("jobs", [])

    for job in jobs:
        runner_name = job.get("runner_name") or job.get("runner", {}).get("name")
        labels = job.get("runner_labels", []) or job.get("runner", {}).get("labels", [])
        runs_on = job.get("runs_on", [])

        if runner_name:
            key = runner_name
            if key not in runners:
                runners[key] = {
                    "name": runner_name,
                    "labels": labels if isinstance(labels, list) else [labels],
                    "runs_on": runs_on if isinstance(runs_on, list) else [runs_on],
                }

    return list(runners.values())


def compute_summary(
    run_meta: dict[str, Any], failed_steps: list, diff_summary: dict[str, Any] | None
) -> str:
    """Compute summary string per PRD D4 truth table."""
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
    if base == "ok" and diff_summary:
        if diff_summary.get("action_version_drift") or (
            diff_summary.get("cache_drift")
            and any(d["from"] == "hits" for d in diff_summary["cache_drift"])
        ):
            base = "warn"

    return base


def compute_diff_summary(
    current: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    """Compute drift between current and reference run (PRD D2)."""
    action_drift = []
    env_drift: list[dict[str, Any]] = []
    runner_drift = []
    cache_drift = []

    # Action version drift
    current_actions = {a["action"]: a["ref"] for a in current.get("actions_used", [])}
    ref_actions = {a["action"]: a["ref"] for a in reference.get("actions_used", [])}
    for action, ref_ver in ref_actions.items():
        if action in current_actions and current_actions[action] != ref_ver:
            action_drift.append(
                {"action": action, "from_ref": ref_ver, "to_ref": current_actions[action]}
            )

    # Runner label drift
    current_runners = {
        r["name"]: set(r["labels"]) for r in current.get("runner_used", [])
    }
    ref_runners = {r["name"]: set(r["labels"]) for r in reference.get("runner_used", [])}
    for name, labels in ref_runners.items():
        if name in current_runners and current_runners[name] != labels:
            runner_drift.append(
                {
                    "runner_name": name,
                    "from_labels": sorted(labels),
                    "to_labels": sorted(current_runners[name]),
                }
            )

    # Cache regression
    ref_cache = reference.get("cache_summary", {})
    current_cache = current.get("cache_summary", {})
    if ref_cache.get("hits", 0) > 0 and current_cache.get("misses", 0) > 0:
        cache_drift.append({"cache_key_prefix": "regression", "from": "hits", "to": "misses"})

    return {
        "reference_run_ref": reference.get("run_ref"),
        "action_version_drift": action_drift,
        "env_drift": env_drift,
        "runner_label_drift": runner_drift,
        "cache_drift": cache_drift,
    }


def generate_findings(
    job_states: list[dict],
    failed_steps: list[dict],
    diff_summary: dict[str, Any] | None,
    run_meta: dict[str, Any],
    cache_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Generate findings list per PRD D5 kinds."""
    findings = []

    # failed_step (crit)
    for step in failed_steps:
        findings.append(
            {
                "severity": "crit",
                "kind": "failed_step",
                "job": step.get("job"),
                "step": step.get("name"),
                "exit_code": step.get("exit_code"),
                "log_excerpt": step.get("log_excerpt"),
                "hint": "see references/pipeline_failures.md#failed-steps",
            }
        )

    # action_version_drift (warn)
    if diff_summary:
        for drift in diff_summary.get("action_version_drift", []):
            findings.append(
                {
                    "severity": "warn",
                    "kind": "action_version_drift",
                    "action": drift["action"],
                    "from_ref": drift["from_ref"],
                    "to_ref": drift["to_ref"],
                    "hint": "see references/pipeline_failures.md#action-version-drift",
                }
            )

    # cache_miss_regression (warn)
    if diff_summary and diff_summary.get("cache_drift"):
        for drift in diff_summary["cache_drift"]:
            if drift["from"] == "hits":
                findings.append(
                    {
                        "severity": "warn",
                        "kind": "cache_miss_regression",
                        "cache_key_prefix": drift["cache_key_prefix"],
                        "hint": "see references/pipeline_failures.md#cache-miss",
                    }
                )

    # runner_label_drift (info)
    if diff_summary:
        for drift in diff_summary.get("runner_label_drift", []):
            findings.append(
                {
                    "severity": "info",
                    "kind": "runner_label_drift",
                    "from_labels": drift["from_labels"],
                    "to_labels": drift["to_labels"],
                    "hint": "see references/pipeline_failures.md#self-hosted-runner",
                }
            )

    # queued_too_long (warn)
    if run_meta.get("status") == "queued":
        created_at = run_meta.get("created_at")
        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                dwell = (datetime.now(created_dt.tzinfo) - created_dt).total_seconds()
                if dwell > 300:  # 5 minutes
                    findings.append(
                        {
                            "severity": "warn",
                            "kind": "queued_too_long",
                            "dwell_sec": dwell,
                            "hint": "see references/pipeline_failures.md#self-hosted-runner",
                        }
                    )
            except (ValueError, TypeError):
                pass  # Skip if date parsing fails

    return findings


def _apply_log_cap(log: str, cap_mb: int) -> tuple[str, bool]:
    cap_bytes = max(cap_mb, 1) * BYTES_PER_MB
    encoded = log.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return log, False
    truncated = encoded[-cap_bytes:].decode("utf-8", errors="replace")
    return f"[log truncated to last {cap_mb} MiB]\n{truncated}", True


def _steps_payload(steps: list[dict[str, Any]], log: str, include_full_logs: bool) -> list[dict[str, Any]]:
    payload = []
    for step in steps:
        step_payload = dict(step)
        if include_full_logs:
            step_payload["full_log"] = log
        payload.append(step_payload)
    return payload


def _reference_summary(
    provider: Any,
    run_id: str,
    project: str | None,
) -> dict[str, Any]:
    ref_raw = provider.fetch_run(run_id, project)
    ref_logs = {}
    for ref_job in ref_raw.get("jobs", []):
        ref_job_name = ref_job.get("name", "")
        ref_job_id = ref_job.get("id") or ref_job.get("databaseId")
        try:
            ref_logs[ref_job_name] = provider.fetch_log(run_id, ref_job_id, False)
        except Exception:
            ref_logs[ref_job_name] = ""
    return {
        "run_ref": run_id,
        "actions_used": extract_actions_used(ref_logs),
        "cache_summary": extract_cache_summary(ref_logs),
        "runner_used": extract_runner_used(ref_raw),
    }


def main(argv: list[str] | None = None) -> int:
    start_time = time.time()
    args = build_parser().parse_args(argv)

    failure_class: str | None = None
    stderr = ""
    data: dict[str, Any] = {}

    try:
        # Validate args
        if args.provider == "glab" and not args.project:
            raise BadArgsError("--project is required for glab")

        # Resolve provider
        provider = resolve_provider(args.provider, args.gh_bin, args.glab_bin)

        # Fetch run metadata
        run_raw = provider.fetch_run(args.run_id, args.project)

        # Normalize run metadata (provider-specific to common format)
        if args.provider == "gh":
            run_meta = {
                "workflow_name": run_raw.get("workflowName", ""),
                "branch": run_raw.get("headBranch", ""),
                "head_sha": run_raw.get("headSha", ""),
                "event": run_raw.get("event", ""),
                "status": run_raw.get("status", ""),
                "conclusion": run_raw.get("conclusion"),
                "created_at": run_raw.get("createdAt", ""),
                "url": run_raw.get("url", ""),
            }
        else:  # glab
            run_meta = {
                "workflow_name": "",  # GitLab doesn't have workflow name in same way
                "branch": run_raw.get("ref", ""),
                "head_sha": run_raw.get("sha", ""),
                "event": run_raw.get("source", ""),
                "status": run_raw.get("status", ""),
                "conclusion": run_raw.get("status"),  # GitLab uses status for conclusion
                "created_at": run_raw.get("created_at", ""),
                "url": run_raw.get("web_url", ""),
            }

        # Fetch job states and logs
        jobs = run_raw.get("jobs", [])
        job_states = []
        failed_steps = []
        job_logs = {}
        partial_findings = []

        for job in jobs:
            job_name = job.get("name", "")
            job_id = job.get("id") or job.get("databaseId")

            # Fetch job log (only failed jobs for efficiency)
            only_failed = job.get("conclusion") == "failure"
            try:
                log = provider.fetch_log(args.run_id, job_id, only_failed)
                log, was_truncated = _apply_log_cap(log, args.log_size_cap_mb)
                if was_truncated:
                    partial_findings.append(
                        {
                            "severity": "warn",
                            "kind": "analyzer_partial",
                            "job": job_name,
                            "reason": "log_truncated",
                            "hint": "see references/pipeline_failures.md#failed-steps",
                        }
                    )
                job_logs[job_name] = log
            except Exception:
                log = ""

            # Extract failed steps
            steps = job.get("steps", [])
            for step in steps:
                if step.get("conclusion") == "failure":
                    # Get last 20 lines of log for this step
                    log_lines = log.split("\n")
                    log_excerpt = "\n".join(log_lines[-20:]) if log_lines else ""
                    failed_steps.append(
                        {
                            "job": job_name,
                            "name": step.get("name", ""),
                            "exit_code": step.get("exit_code"),
                            "log_excerpt": log_excerpt,
                        }
                    )

            job_states.append(
                {
                    "name": job_name,
                    "status": job.get("status", ""),
                    "conclusion": job.get("conclusion"),
                    "runner_name": job.get("runner_name"),
                    "runner_labels": job.get("runner_labels", []),
                    "duration_sec": _parse_duration(job.get("startedAt"), job.get("completedAt")),
                    "retry_count": 0,  # GitHub doesn't expose retry count in basic view
                    "steps": _steps_payload(steps, log, args.include_full_step_logs),
                }
            )

        # Extract cache summary and actions used
        cache_summary = extract_cache_summary(job_logs)
        actions_used = extract_actions_used(job_logs)
        runner_used = extract_runner_used(run_raw)

        # Build current result
        current_result = {
            "run_ref": args.run_id,
            "run_meta": run_meta,
            "job_states": job_states,
            "failed_steps": failed_steps,
            "actions_used": actions_used,
            "cache_summary": cache_summary,
            "runner_used": runner_used,
        }

        # Diff against reference if requested
        diff_summary = None
        if args.diff_against:
            if args.diff_against == "last-success":
                ref_run_id = provider.find_last_success(
                    args.project or "", run_meta.get("workflow_name"), run_meta.get("branch")
                )
                if ref_run_id:
                    ref_result = _reference_summary(provider, ref_run_id, args.project)
                    diff_summary = compute_diff_summary(current_result, ref_result)
            else:
                ref_result = _reference_summary(provider, args.diff_against, args.project)
                diff_summary = compute_diff_summary(current_result, ref_result)

        # Compute summary and findings
        summary = compute_summary(run_meta, failed_steps, diff_summary)
        findings = generate_findings(
            job_states, failed_steps, diff_summary, run_meta, cache_summary
        )
        findings.extend(partial_findings)

        # Build data payload
        data = {
            "provider": args.provider,
            "run_ref": args.run_id,
            "run_meta": run_meta,
            "job_states": job_states,
            "failed_steps": failed_steps,
            "actions_used": actions_used,
            "cache_summary": cache_summary,
            "runner_used": runner_used,
            "diff_summary": diff_summary,
            "summary": summary,
            "findings": findings,
            "failure_class": None,
            "elapsed_s": time.time() - start_time,
        }

    except ProviderUnavailable as e:
        failure_class = "provider_unavailable" if "glab" in str(e).lower() else "tool_missing"
        stderr = str(e)
    except AuthError as e:
        failure_class = "auth"
        stderr = str(e)
    except NotFoundError as e:
        failure_class = "not_found"
        stderr = str(e)
    except PermissionError as e:
        failure_class = "permission"
        stderr = str(e)
    except NetworkError as e:
        failure_class = "network"
        stderr = str(e)
    except BadArgsError as e:
        failure_class = "bad_args"
        stderr = str(e)
    except ApiUnexpectedError as e:
        failure_class = "api_unexpected"
        stderr = str(e)
    except LogTruncatedError as e:
        failure_class = "log_truncated"
        stderr = str(e)
    except TimeoutError as e:
        failure_class = "timeout"
        stderr = str(e)
    except Exception as e:
        failure_class = "internal"
        stderr = f"Internal error: {e}"

    # Update data with failure_class if set
    if failure_class:
        data["failure_class"] = failure_class
        data["elapsed_s"] = time.time() - start_time

    # Emit JSON envelope
    exit_code = FAILURE_CLASS_TO_EXIT.get(failure_class, 1)
    return emit(data, exit_code, stderr, args.json)


def _parse_duration(started_at: str | None, completed_at: str | None) -> float:
    """Parse duration from ISO timestamps."""
    if not started_at or not completed_at:
        return 0.0
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        return (end - start).total_seconds()
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    sys.exit(main())
