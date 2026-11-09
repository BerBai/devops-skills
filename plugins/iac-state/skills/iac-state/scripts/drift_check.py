#!/usr/bin/env python3
"""drift_check.py - detect Terraform/OpenTofu drift via plan JSON (v1.0).

v1.0 surface:
    drift_check.py <host> <project-dir> [--target ADDR] [--tool auto|tofu|terraform]
                                          [--json]

Workflow:
    1. terraform init -input=false -lock=false -upgrade=false
    2. terraform plan -detailed-exitcode -out=/tmp/plan.bin [-target=<addr>]
    3. terraform show -json /tmp/plan.bin
    4. Parse resource_changes[]; classify each:
         no-op | create | update-in-place | replace | destroy
    5. For each non-no-op:
         severity = priority_table(resource_type)
         hint = link into references/drift_patterns.md
    6. Summary: count by action + crit/warn/info histogram
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import emit, json_result  # type: ignore[import-not-found]  # noqa: E402, I001
from lib.terraform import (  # type: ignore[import-not-found]  # noqa: E402
    TfError,
    remove_path,
    resolve_tool,
    run_tf,
)
from _version import add_version_flag, handle_version

_INIT_FAILED = 130
_PLAN_FAILED = 131
_SHOW_PARSE_FAILED = 132
_TARGET_NOT_FOUND = 133
_LOCK_HELD = 134
_PROVIDER_AUTH_FAILED = 135
_TEN_MB = 10 * 1024 * 1024
_PLAN_DIFF_EXIT = 2
_TRUNCATE_MARKER = "\n... <truncated 10MB cap>\n"

_SEVERITY_ORDER = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
_ACTION_ORDER = {"destroy": 0, "replace": 1, "create": 2, "update-in-place": 3}
_SECURITY_TYPE_MARKERS = (
    "iam",
    "security_group",
    "security_group_rule",
    "acl",
    "policy",
    "firewall",
    "route",
    "key",
    "secret",
    "db_",
    "database",
    "bucket_policy",
    "public_access",
    "network",
    "subnet",
    "vpc",
)
_METADATA_MARKERS = ("tag", "tags", "description", "label", "labels", "annotation", "metadata")
_PROVIDER_AUTH_MARKERS = (
    "credential",
    "credentials",
    "expired token",
    "invalid token",
    "access key",
    "access denied",
    "unauthorized",
    "forbidden",
    "auth",
)
_LOCK_MARKERS = ("error acquiring the state lock", "state lock", "lock timeout", "locked")
_TARGET_MARKERS = ("target", "resource address", "does not exist", "no matching")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drift_check.py")
    parser.add_argument("host", help="ssh-core alias, or 'local'")
    parser.add_argument("project_dir", help="Terraform project root")
    parser.add_argument("--target", default=None, help="Limit to one resource address, e.g. aws_instance.web")
    parser.add_argument("--tool", choices=["auto", "tofu", "terraform"], default="auto")
    parser.add_argument("--json", action="store_true")
    add_version_flag(parser)
    return parser


def _validate_local_project(project_dir: str) -> None:
    project = Path(project_dir).expanduser()
    if not project.is_dir():
        raise TfError("project_dir_invalid", 122, f"project dir is not a directory: {project_dir}")
    if not any(project.glob("*.tf")):
        raise TfError("project_dir_invalid", 122, f"project dir has no *.tf files: {project_dir}")


def _truncate_10mb(value: str) -> tuple[str, bool]:
    if len(value) <= _TEN_MB:
        return value, False
    return value[:_TEN_MB] + _TRUNCATE_MARKER, True


def _new_plan_path(host: str) -> str:
    if host == "local":
        fd, path = tempfile.mkstemp(prefix="drift-check-", suffix=".tfplan")
        os.close(fd)
        return path
    return f"/tmp/drift-check-{uuid.uuid4().hex}.tfplan"


def _classify_action(actions: Any) -> str:
    if not isinstance(actions, list):
        return "no-op"
    parts = [str(action) for action in actions]
    action_set = set(parts)
    if not parts or action_set == {"no-op"}:
        classified = "no-op"
    elif "delete" in action_set and "create" in action_set:
        classified = "replace"
    elif "delete" in action_set:
        classified = "destroy"
    elif "create" in action_set:
        classified = "create"
    else:
        classified = "update-in-place"
    return classified


def _is_metadata_change(resource_type: str, address: str) -> bool:
    haystack = f"{resource_type} {address}".lower()
    return any(marker in haystack for marker in _METADATA_MARKERS)


def _is_security_sensitive(resource_type: str, address: str) -> bool:
    haystack = f"{resource_type} {address}".lower()
    return any(marker in haystack for marker in _SECURITY_TYPE_MARKERS)


def _severity(action: str, resource_type: str, address: str) -> str:
    if action in {"destroy", "replace"}:
        return "crit"
    if action == "create":
        return "warn"
    if action == "update-in-place":
        if _is_metadata_change(resource_type, address):
            return "info"
        if _is_security_sensitive(resource_type, address):
            return "warn"
        return "warn"
    return "info"


def _hint(action: str, resource_type: str, address: str) -> str:
    if action == "destroy":
        return "references/drift_patterns.md#resource-deleted-in-cloud"
    if action == "replace":
        return "references/drift_patterns.md#interpreting-plan-diffs"
    if action == "create":
        return "references/drift_patterns.md#name-namespace-conflicts"
    if "iam" in resource_type.lower() or "security_group" in resource_type.lower():
        return "references/drift_patterns.md#manual-console-changes"
    if _is_metadata_change(resource_type, address):
        return "references/drift_patterns.md#provider-managed-fields"
    return "references/drift_patterns.md#common-drift-patterns"


def _diff_summary(address: str, action: str, resource_type: str) -> str:
    if action == "destroy":
        return f"{address} ({resource_type}) is planned for destroy"
    if action == "replace":
        return f"{address} ({resource_type}) is planned for replacement"
    if action == "create":
        return f"{address} ({resource_type}) is planned for create"
    if action == "update-in-place":
        return f"{address} ({resource_type}) is planned for in-place update"
    return f"{address} ({resource_type}) has no actionable drift"


def _finding(change: dict[str, Any]) -> dict[str, Any] | None:
    address = str(change.get("address") or "")
    resource_type = str(change.get("type") or "")
    name = str(change.get("name") or "")
    raw_change = change.get("change")
    change_body: dict[str, Any] = raw_change if isinstance(raw_change, dict) else {}
    action = _classify_action(change_body.get("actions"))
    if action == "no-op":
        return None
    severity = _severity(action, resource_type, address)
    return {
        "severity": severity,
        "kind": "drift",
        "resource": address,
        "resource_type": resource_type,
        "name": name,
        "action": action,
        "diff_summary": _diff_summary(address, action, resource_type),
        "hint": _hint(action, resource_type, address),
    }


def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item.get("severity")), 9),
            _ACTION_ORDER.get(str(item.get("action")), 9),
            str(item.get("resource") or ""),
        ),
    )


def _summarize(findings: list[dict[str, Any]], plan_exit_code: int) -> dict[str, Any]:
    counts_by_action = {name: 0 for name in ("create", "update-in-place", "replace", "destroy")}
    severity_counts = {name: 0 for name in ("crit", "warn", "info")}
    for finding in findings:
        action = str(finding.get("action") or "")
        severity = str(finding.get("severity") or "")
        if action in counts_by_action:
            counts_by_action[action] += 1
        if severity in severity_counts:
            severity_counts[severity] += 1
    if severity_counts["crit"]:
        drift = "crit"
    elif severity_counts["warn"]:
        drift = "warn"
    elif severity_counts["info"]:
        drift = "info"
    elif plan_exit_code == _PLAN_DIFF_EXIT:
        drift = "ok"
    else:
        drift = "ok"
    return {
        "drift": drift,
        "total_changes": len(findings),
        "counts_by_action": counts_by_action,
        "severity_counts": severity_counts,
    }


def _parse_show_json(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise TfError("show_parse_failed", _SHOW_PARSE_FAILED, f"terraform show -json output is invalid JSON: {exc}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("resource_changes"), list):
        raise TfError("show_parse_failed", _SHOW_PARSE_FAILED, "terraform show -json output has no resource_changes[]")
    changes = payload["resource_changes"]
    return [change for change in changes if isinstance(change, dict)]


def _contains_marker(stderr: str, markers: tuple[str, ...]) -> bool:
    lower = stderr.lower()
    return any(marker in lower for marker in markers)


def _stage_error(stage: str, stderr: str, *, target: str | None = None) -> TfError:
    if _contains_marker(stderr, _LOCK_MARKERS):
        return TfError("lock_held", _LOCK_HELD, stderr or f"terraform {stage} failed because state is locked")
    if target and _contains_marker(stderr, _TARGET_MARKERS):
        return TfError("target_not_found", _TARGET_NOT_FOUND, stderr or f"target not found: {target}")
    if _contains_marker(stderr, _PROVIDER_AUTH_MARKERS):
        return TfError("provider_auth_failed", _PROVIDER_AUTH_FAILED, stderr or f"terraform {stage} provider auth failed")
    if stage == "init":
        return TfError("init_failed", _INIT_FAILED, stderr or "terraform init failed")
    return TfError("plan_failed", _PLAN_FAILED, stderr or "terraform plan failed")


def _run_drift(args: argparse.Namespace) -> dict[str, Any]:
    if args.host == "local":
        _validate_local_project(args.project_dir)
    tool = resolve_tool(args.host, args.tool)
    raw: dict[str, Any] = {}
    warnings: list[dict[str, Any]] = []
    plan_path = _new_plan_path(args.host)

    try:
        init = run_tf(
            args.host,
            args.project_dir,
            ["init", "-input=false", "-lock=false", "-upgrade=false"],
            tool=tool,
            timeout=120,
        )
        raw["init_stdout"] = init.stdout
        raw["init_stderr"] = init.stderr
        if init.returncode != 0:
            raise _stage_error("init", init.stderr)

        plan_args = [
            "plan",
            "-detailed-exitcode",
            "-input=false",
            "-lock-timeout=1s",
            f"-out={plan_path}",
        ]
        if args.target:
            plan_args.append(f"-target={args.target}")
        plan = run_tf(args.host, args.project_dir, plan_args, tool=tool, timeout=300)
        raw["plan_stdout"] = plan.stdout
        raw["plan_stderr"] = plan.stderr
        raw["plan_exit_code"] = plan.returncode
        if plan.returncode not in {0, _PLAN_DIFF_EXIT}:
            raise _stage_error("plan", plan.stderr, target=args.target)

        findings: list[dict[str, Any]] = []
        if plan.returncode == _PLAN_DIFF_EXIT:
            show = run_tf(args.host, args.project_dir, ["show", "-json", plan_path], tool=tool, timeout=120)
            raw["show_stdout"] = show.stdout
            raw["show_stderr"] = show.stderr
            if show.returncode != 0:
                raise TfError("show_parse_failed", _SHOW_PARSE_FAILED, show.stderr or "terraform show -json failed")
            raw_plan_json, truncated = _truncate_10mb(show.stdout)
            raw["plan_json"] = raw_plan_json
            if truncated:
                warnings.append(
                    {
                        "failure_class": "plan_too_large",
                        "exit_code": 136,
                        "message": "terraform show -json exceeded 10 MB; raw plan JSON was truncated in output",
                    }
                )
            for change in _parse_show_json(show.stdout):
                finding = _finding(change)
                if finding:
                    findings.append(finding)
        else:
            raw["plan_json"] = ""

        sorted_findings = _sort_findings(findings)
        summary = _summarize(sorted_findings, plan.returncode)
        exit_code = 1 if summary["drift"] == "crit" else 0
        return json_result(
            success=exit_code == 0,
            exit_code=exit_code,
            data={
                "host": args.host,
                "project_dir": args.project_dir,
                "target": args.target,
                "tool": tool,
                "summary": summary,
                "findings": sorted_findings,
                "warnings": warnings,
                "raw": raw,
                "failure_class": None,
            },
        )
    finally:
        try:
            remove_path(args.host, plan_path)
        except TfError as exc:
            warnings.append({"failure_class": exc.failure_class, "exit_code": exc.exit_code, "message": exc.stderr})


def _format_human(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    lines = [
        f"Drift check {data.get('project_dir')} on {data.get('host')}",
        f"tool             {data.get('tool')}",
        f"target           {data.get('target') or '-'}",
        f"summary          {summary.get('drift')}",
        f"changes          {summary.get('total_changes', 0)}",
    ]
    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    if findings:
        lines.append("Findings:")
        for finding in findings[:10]:
            lines.append(
                f"- {finding.get('severity')} {finding.get('action')} {finding.get('resource')}: "
                f"{finding.get('diff_summary')}"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    early = handle_version("drift_check.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    try:
        result = _run_drift(args)
    except TfError as exc:
        result = json_result(
            success=False,
            exit_code=exc.exit_code,
            stderr=exc.stderr,
            data={
                "host": args.host,
                "project_dir": args.project_dir,
                "target": args.target,
                "tool": args.tool,
                "failure_class": exc.failure_class,
            },
        )
    except Exception as exc:  # pragma: no cover - last-ditch safety
        result = json_result(
            success=False,
            exit_code=1,
            stderr=f"drift_check unexpected error: {type(exc).__name__}: {exc}",
            data={"host": args.host, "project_dir": args.project_dir, "failure_class": None},
        )
    if not args.json and result.get("success"):
        result = dict(result)
        result["stdout"] = _format_human(result)
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
