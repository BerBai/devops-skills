#!/usr/bin/env python3
# ruff: noqa: E402, I001, PLR0911, PLR2004
"""secret_scope_audit.py — CI secret visibility audit (v1.0).

v1.0 surface:
    secret_scope_audit.py gh
        [--repo <owner/name> | --org <name>]
        [--include-codespaces] [--no-include-dependabot]
        [--no-workflow-scan] [--unused-flag | --no-unused-flag]
        [--workflow-fetch-cap N]
        [--gh-bin <path>] [--timeout N] [--json]

    secret_scope_audit.py glab
        [--project <group/project> | --group <path>]
        [--no-workflow-scan] [--unused-flag | --no-unused-flag]
        [--workflow-fetch-cap N]
        [--glab-bin <path>] [--timeout N] [--json]

See:
    references/secret_scopes.md
    .trellis/tasks/05-26-cicd-debug-secret-scope-audit/prd.md
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_LIB_SPEC = importlib.util.spec_from_file_location(
    "cicd_debug_lib", SCRIPTS_DIR / "lib" / "__init__.py"
)
if _LIB_SPEC is None or _LIB_SPEC.loader is None:
    raise RuntimeError("could not load cicd-debug local lib")
_LIB = importlib.util.module_from_spec(_LIB_SPEC)
_LIB_SPEC.loader.exec_module(_LIB)
emit = _LIB.emit
json_result = _LIB.json_result
from lib_cicd import (  # type: ignore[import-not-found]
    ApiUnexpectedError,
    AuthError,
    BadArgsError,
    CliTimeoutError,
    EnvData,
    NetworkError,
    NotFoundError,
    PermissionError,
    ProviderUnavailable,
    SecretData,
    WorkflowFile,
    resolve_provider,
)

FAILURE_CLASS_TO_EXIT = {
    None: 0,
    "tool_missing": 170,
    "provider_unavailable": 171,
    "auth": 172,
    "not_found": 173,
    "permission": 174,
    "network": 175,
    "bad_args": 176,
    "api_unexpected": 177,
    "workflow_yaml_parse_failed": 178,
    "workflow_dir_missing": 179,
    "timeout": 124,
    "internal": 1,
}

DEFAULT_TIMEOUT_SEC = 120
DEFAULT_WORKFLOW_FETCH_CAP = 50
GH_SECRET_RE = re.compile(
    r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
    r"|\$\{\{\s*secrets\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]\s*\}\}"
)
CLOUD_CREDENTIAL_RE = re.compile(
    r"^(AWS|AZURE|GCP|GOOGLE|SP|SERVICE_PRINCIPAL|DOCKER)_(.*)$", re.IGNORECASE
)
OIDC_HINT_RE = re.compile(r"(OIDC|FEDERATED|WIF)", re.IGNORECASE)
GLAB_RESERVED_TOP_LEVEL = {
    "stages",
    "variables",
    "workflow",
    "include",
    "default",
    "image",
    "services",
    "before_script",
    "after_script",
    "cache",
}


@dataclass(frozen=True)
class Scope:
    kind: str
    value: str


@dataclass(frozen=True)
class WorkflowScan:
    path: str
    parses_ok: bool
    secret_refs: tuple[str, ...]
    event_triggers: tuple[str, ...]
    pull_request_target_with_unsafe_checkout: bool
    jobs_by_secret: dict[str, tuple[str, ...]]
    jobs_with_environment: dict[str, str]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="secret_scope_audit.py")
    p.add_argument("provider", choices=["gh", "glab"])
    p.add_argument("--repo", default=None, help="GitHub owner/repo scope")
    p.add_argument("--org", default=None, help="GitHub organization scope")
    p.add_argument("--project", default=None, help="GitLab group/project scope")
    p.add_argument("--group", default=None, help="GitLab group scope")
    p.add_argument("--include-codespaces", action="store_true")
    p.add_argument(
        "--include-dependabot",
        dest="include_dependabot",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    p.add_argument("--no-include-dependabot", dest="include_dependabot", action="store_false")
    p.add_argument("--no-workflow-scan", dest="workflow_scan", action="store_false", default=True)
    p.add_argument("--unused-flag", dest="unused_flag", action="store_true", default=True)
    p.add_argument("--no-unused-flag", dest="unused_flag", action="store_false")
    p.add_argument("--workflow-fetch-cap", type=int, default=DEFAULT_WORKFLOW_FETCH_CAP)
    p.add_argument("--gh-bin", default=None, help="Path to gh binary (overrides GH_BIN env)")
    p.add_argument("--glab-bin", default=None, help="Path to glab binary (overrides GLAB_BIN env)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    p.add_argument("--json", action="store_true")
    return p


def validate_scope(args: argparse.Namespace) -> Scope:
    if args.workflow_fetch_cap < 1:
        raise BadArgsError("--workflow-fetch-cap must be >= 1")
    if args.timeout < 1:
        raise BadArgsError("--timeout must be >= 1")
    if args.provider == "gh":
        if bool(args.repo) == bool(args.org):
            raise BadArgsError("gh requires exactly one of --repo or --org")
        if args.project or args.group:
            raise BadArgsError("glab scope flags are not valid with gh")
        return Scope("repo", args.repo) if args.repo else Scope("org", args.org)
    if bool(args.project) == bool(args.group):
        raise BadArgsError("glab requires exactly one of --project or --group")
    if args.repo or args.org:
        raise BadArgsError("gh scope flags are not valid with glab")
    if args.include_codespaces or not args.include_dependabot:
        raise BadArgsError("GitHub-only include flags are not valid with glab")
    return Scope("project", args.project) if args.project else Scope("group", args.group)


def _events_from_on(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, dict):
        return tuple(str(key) for key in value)
    return ()


def event_triggers(doc: dict[Any, Any]) -> tuple[str, ...]:
    raw = doc.get("on")
    if raw is None:
        raw = doc.get(True)
    return tuple(sorted(set(_events_from_on(raw))))


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for child in value.values():
            out.extend(_walk_strings(child))
        return out
    if isinstance(value, list):
        out = []
        for child in value:
            out.extend(_walk_strings(child))
        return out
    return []


def _gh_secret_refs(value: Any) -> tuple[set[str], bool]:
    refs: set[str] = set()
    dynamic = False
    for text in _walk_strings(value):
        for match in GH_SECRET_RE.finditer(text):
            refs.add(next(group for group in match.groups() if group))
        if "secrets[" in text and not GH_SECRET_RE.search(text):
            dynamic = True
    return refs, dynamic


def _glab_secret_refs(value: Any, names: set[str]) -> set[str]:
    refs: set[str] = set()
    for text in _walk_strings(value):
        for name in names:
            if re.search(rf"(?<![A-Za-z0-9_])\${re.escape(name)}(?![A-Za-z0-9_])", text):
                refs.add(name)
            if f"${{{name}}}" in text or f"%{name}%" in text:
                refs.add(name)
    return refs


def _has_unsafe_checkout(doc: dict[str, Any]) -> bool:
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    unsafe_refs = (
        "github.event.pull_request.head.sha",
        "github.event.pull_request.head.ref",
        "github.head_ref",
    )
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses", ""))
            with_block = step.get("with")
            ref = ""
            if isinstance(with_block, dict):
                ref = str(with_block.get("ref", ""))
            if uses.startswith("actions/checkout@") and any(token in ref for token in unsafe_refs):
                return True
    return False


def _jobs_with_environment(doc: dict[str, Any]) -> dict[str, str]:
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    out = {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        env = job.get("environment")
        if isinstance(env, str):
            out[str(job_id)] = env
        elif isinstance(env, dict) and isinstance(env.get("name"), str):
            out[str(job_id)] = env["name"]
    return out


def _analyze_gh_workflow(path: str, doc: dict[str, Any]) -> tuple[WorkflowScan, bool]:
    all_refs, dynamic = _gh_secret_refs(doc)
    jobs_by_secret: dict[str, set[str]] = {name: set() for name in all_refs}
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job_id, job in jobs.items():
            refs, job_dynamic = _gh_secret_refs(job)
            dynamic = dynamic or job_dynamic
            for ref in refs:
                jobs_by_secret.setdefault(ref, set()).add(str(job_id))
    scan = WorkflowScan(
        path=path,
        parses_ok=True,
        secret_refs=tuple(sorted(all_refs)),
        event_triggers=event_triggers(doc),
        pull_request_target_with_unsafe_checkout=_has_unsafe_checkout(doc),
        jobs_by_secret={k: tuple(sorted(v)) for k, v in jobs_by_secret.items()},
        jobs_with_environment=_jobs_with_environment(doc),
    )
    return scan, dynamic


def _analyze_glab_workflow(path: str, doc: dict[str, Any], names: set[str]) -> WorkflowScan:
    all_refs = _glab_secret_refs(doc, names)
    jobs_by_secret: dict[str, set[str]] = {name: set() for name in all_refs}
    for key, value in doc.items():
        if key in GLAB_RESERVED_TOP_LEVEL or not isinstance(value, dict):
            continue
        for ref in _glab_secret_refs(value, names):
            jobs_by_secret.setdefault(ref, set()).add(str(key))
    return WorkflowScan(
        path=path,
        parses_ok=True,
        secret_refs=tuple(sorted(all_refs)),
        event_triggers=tuple(),
        pull_request_target_with_unsafe_checkout=False,
        jobs_by_secret={k: tuple(sorted(v)) for k, v in jobs_by_secret.items()},
        jobs_with_environment={},
    )


def analyze_workflows(
    provider: str, files: list[WorkflowFile], secret_names: set[str]
) -> tuple[list[WorkflowScan], list[dict[str, Any]], str | None]:
    scans: list[WorkflowScan] = []
    findings: list[dict[str, Any]] = []
    parse_failures = 0
    for workflow in files:
        try:
            loaded = yaml.safe_load(workflow.content)
            if not isinstance(loaded, dict):
                raise yaml.YAMLError("workflow root is not a mapping")
        except yaml.YAMLError as exc:
            parse_failures += 1
            scans.append(
                WorkflowScan(
                    path=workflow.path,
                    parses_ok=False,
                    secret_refs=tuple(),
                    event_triggers=tuple(),
                    pull_request_target_with_unsafe_checkout=False,
                    jobs_by_secret={},
                    jobs_with_environment={},
                )
            )
            findings.append(
                {
                    "severity": "warn",
                    "kind": "workflow_parse_failed",
                    "path": workflow.path,
                    "error": str(exc),
                    "hint": "see references/secret_scopes.md#which-workflows-use-which-secrets",
                }
            )
            continue
        if provider == "gh":
            scan, dynamic = _analyze_gh_workflow(workflow.path, loaded)
            if dynamic:
                findings.append(
                    {
                        "severity": "info",
                        "kind": "dynamic_secret_reference",
                        "path": workflow.path,
                        "hint": "see references/secret_scopes.md#which-workflows-use-which-secrets",
                    }
                )
        else:
            scan = _analyze_glab_workflow(workflow.path, loaded, secret_names)
        scans.append(scan)
    failure_class = None
    if files and parse_failures / len(files) > 0.5:
        failure_class = "workflow_yaml_parse_failed"
    return scans, findings, failure_class


def _refs_by_secret(scans: list[WorkflowScan]) -> dict[str, list[dict[str, Any]]]:
    refs: dict[str, list[dict[str, Any]]] = {}
    for scan in scans:
        if not scan.parses_ok:
            continue
        for name in scan.secret_refs:
            refs.setdefault(name, []).append(
                {"path": scan.path, "jobs": list(scan.jobs_by_secret.get(name, tuple()))}
            )
    return refs


def _is_long_lived_cloud_credential(secret: SecretData) -> bool:
    return bool(CLOUD_CREDENTIAL_RE.match(secret.name)) and not OIDC_HINT_RE.search(secret.name)


def _risk_flags_for_secret(
    provider: str,
    scope: Scope,
    secret: SecretData,
    scans: list[WorkflowScan],
    references: list[dict[str, Any]] | None,
    unused_enabled: bool,
) -> list[str]:
    flags: list[str] = []
    if _is_long_lived_cloud_credential(secret):
        flags.append("long_lived_cloud_credential")
    if provider == "gh" and references:
        ref_paths = {ref["path"] for ref in references}
        for scan in scans:
            if scan.path not in ref_paths:
                continue
            triggers = set(scan.event_triggers)
            if "pull_request" in triggers:
                flags.append("exposed_on_fork_pr")
            if (
                "pull_request_target" in triggers
                and scan.pull_request_target_with_unsafe_checkout
            ):
                flags.append("pull_request_target_unsafe_checkout")
    if unused_enabled and scope.kind not in {"org", "group"} and references == []:
        flags.append("unused")
    return sorted(set(flags))


def _finding_for_flag(secret: SecretData, flag: str) -> dict[str, Any]:
    severity = "crit" if flag == "pull_request_target_unsafe_checkout" else "warn"
    hints = {
        "long_lived_cloud_credential": "see references/secret_scopes.md#oidc-use-fewer-long-lived-tokens",
        "exposed_on_fork_pr": "see references/secret_scopes.md#pull_request-from-fork",
        "pull_request_target_unsafe_checkout": "see references/secret_scopes.md#pull_request_target-trap",
        "unused": "see references/secret_scopes.md#which-workflows-use-which-secrets",
    }
    return {
        "severity": severity,
        "kind": flag,
        "secret": secret.name,
        "scope_kind": secret.scope_kind,
        "scope_value": secret.scope_value,
        "hint": hints[flag],
    }


def compute_summary(secrets: list[dict[str, Any]]) -> str:
    flags = {flag for secret in secrets for flag in secret["risk_flags"]}
    if "pull_request_target_unsafe_checkout" in flags:
        return "crit"
    if flags.intersection({"exposed_on_fork_pr", "long_lived_cloud_credential", "unused"}):
        return "warn"
    return "ok"


def _secret_payload(secret: SecretData, references: list[dict[str, Any]] | None, flags: list[str]) -> dict[str, Any]:
    return {
        "name": secret.name,
        "scope_kind": secret.scope_kind,
        "scope_value": secret.scope_value,
        "kind": secret.kind,
        "visibility": secret.visibility,
        "num_selected_repos": secret.num_selected_repos,
        "masked": secret.masked,
        "protected": secret.protected,
        "environment_scope": secret.environment_scope,
        "updated_at": secret.updated_at,
        "referenced_in_workflows": references,
        "risk_flags": flags,
    }


def _format_human(data: dict[str, Any]) -> str:
    return (
        f"secret_scope_audit {data['provider']} {data['scope']['kind']}:{data['scope']['value']} "
        f"summary={data['summary']} secrets={len(data['secrets'])} "
        f"findings={len(data['findings'])}\n"
    )


def _emit_data(
    data: dict[str, Any],
    exit_code: int,
    stderr: str,
    json_mode: bool,
    stdout: str = "",
) -> int:
    return emit(
        json_result(
            success=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            data=data,
        ),
        json_mode,
    )


def build_success_data(
    args: argparse.Namespace,
    scope: Scope,
    secrets: list[SecretData],
    envs: list[EnvData],
    scans: list[WorkflowScan],
    findings: list[dict[str, Any]],
    failure_class: str | None,
    started_at: float,
) -> dict[str, Any]:
    refs = _refs_by_secret(scans)
    secret_payloads = []
    for secret in secrets:
        references = None if scope.kind in {"org", "group"} else refs.get(secret.name, [])
        flags = _risk_flags_for_secret(
            args.provider,
            scope,
            secret,
            scans,
            references,
            args.unused_flag and args.workflow_scan,
        )
        secret_payloads.append(_secret_payload(secret, references, flags))
        findings.extend(_finding_for_flag(secret, flag) for flag in flags)
    summary = compute_summary(secret_payloads)
    data = {
        "provider": args.provider,
        "scope": asdict(scope),
        "secrets": secret_payloads,
        "environments": [asdict(env) for env in envs],
        "workflow_files": [asdict(scan) for scan in scans],
        "summary": summary,
        "findings": findings,
        "failure_class": failure_class,
        "elapsed_s": time.time() - started_at,
    }
    return data


def _failure_data(args: argparse.Namespace, started_at: float, failure_class: str) -> dict[str, Any]:
    return {
        "provider": getattr(args, "provider", None),
        "scope": None,
        "secrets": [],
        "environments": [],
        "workflow_files": [],
        "summary": "crit",
        "findings": [],
        "failure_class": failure_class,
        "elapsed_s": time.time() - started_at,
    }


def _map_exception(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ProviderUnavailable):
        message = str(exc)
        if "glab" in message.lower():
            return "provider_unavailable", message
        return "tool_missing", message
    if isinstance(exc, AuthError):
        return "auth", str(exc)
    if isinstance(exc, NotFoundError):
        return "not_found", str(exc)
    if isinstance(exc, PermissionError):
        return "permission", str(exc)
    if isinstance(exc, NetworkError):
        return "network", str(exc)
    if isinstance(exc, BadArgsError):
        return "bad_args", str(exc)
    if isinstance(exc, ApiUnexpectedError):
        return "api_unexpected", str(exc)
    if isinstance(exc, (CliTimeoutError, TimeoutError)):
        return "timeout", str(exc)
    return "internal", f"Internal error: {exc}"


def main(argv: list[str] | None = None) -> int:
    started_at = time.time()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        scope = validate_scope(args)
        provider = resolve_provider(args.provider, args.gh_bin, args.glab_bin)
        secrets = provider.list_secrets(
            scope,
            include_codespaces=args.include_codespaces,
            include_dependabot=args.include_dependabot,
        )
        envs = provider.list_environments(scope)
        files: list[WorkflowFile] = []
        findings: list[dict[str, Any]] = []
        workflow_failure_class = None
        if args.workflow_scan and scope.kind in {"repo", "project"}:
            files = provider.fetch_workflow_files(scope, cap=args.workflow_fetch_cap)
            if not files:
                workflow_failure_class = "workflow_dir_missing"
                findings.append(
                    {
                        "severity": "info",
                        "kind": "workflow_dir_missing",
                        "hint": "see references/secret_scopes.md#which-workflows-use-which-secrets",
                    }
                )
        scans, workflow_findings, parse_failure_class = analyze_workflows(
            args.provider, files, {secret.name for secret in secrets}
        )
        findings.extend(workflow_findings)
        failure_class = parse_failure_class or workflow_failure_class
        data = build_success_data(args, scope, secrets, envs, scans, findings, failure_class, started_at)
        stdout = _format_human(data)
        exit_code = 0 if failure_class == "workflow_dir_missing" else FAILURE_CLASS_TO_EXIT[failure_class]
        if exit_code == 0:
            return _emit_data(data, 0, "", args.json, stdout)
        return _emit_data(data, exit_code, f"{failure_class}\n", args.json)
    except Exception as exc:
        failure_class, stderr = _map_exception(exc)
        data = _failure_data(args, started_at, failure_class)
        return _emit_data(data, FAILURE_CLASS_TO_EXIT[failure_class], stderr, args.json)


if __name__ == "__main__":
    sys.exit(main())
