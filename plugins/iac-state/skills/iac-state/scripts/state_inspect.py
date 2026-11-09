#!/usr/bin/env python3
"""state_inspect.py - Terraform/OpenTofu state overview (v1.0)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import emit, json_result  # type: ignore[import-not-found]  # noqa: E402, I001
from lib.terraform import TfError, read_text, resolve_tool, run_tf  # type: ignore[import-not-found]  # noqa: E402
from _version import add_version_flag, handle_version

_BACKEND_UNKNOWN = 125
_LOCK_PARSE_FAILED = 126
_WORKSPACE_LIST_FAILED = 127
_REMOTE_BACKENDS = {"s3", "gcs", "azurerm", "remote", "http", "oss", "cos"}
_SAFE_BACKEND_KEYS = {
    "bucket",
    "key",
    "prefix",
    "workspace",
    "organization",
    "hostname",
    "path",
    "container_name",
    "storage_account_name",
}
_SEVERITY_ORDER = {"crit": 0, "warn": 1, "ok": 2}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="state_inspect.py")
    parser.add_argument("host", help="ssh-core alias, or 'local'")
    parser.add_argument("project_dir", help="Terraform project root containing .terraform/")
    parser.add_argument("--tool", choices=["auto", "tofu", "terraform"], default="auto")
    parser.add_argument("--json", action="store_true")
    add_version_flag(parser)
    return parser


def _finding(severity: str, kind: str, name: str, reason: str, message: str) -> dict[str, Any]:
    return {"severity": severity, "kind": kind, "name": name, "reason": reason, "message": message}


def _top_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(str(f.get("severity")), 9),
            str(f.get("kind") or ""),
            str(f.get("name") or ""),
            str(f.get("reason") or ""),
        ),
    )


def _validate_local_project(project_dir: str) -> None:
    project = Path(project_dir).expanduser()
    if not project.is_dir():
        raise TfError("project_dir_invalid", 122, f"project dir is not a directory: {project_dir}")
    if not any(project.glob("*.tf")):
        raise TfError("project_dir_invalid", 122, f"project dir has no *.tf files: {project_dir}")
    if not (project / ".terraform" / "terraform.tfstate").exists():
        raise TfError("not_initialized", 123, "missing .terraform/terraform.tfstate; run terraform init first")


def _parse_backend(raw: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "unknown", "config": {}}, _finding(
            "warn", "backend", "backend", "backend_unknown", "cached backend state is not valid JSON"
        )
    backend = payload.get("backend") if isinstance(payload, dict) else None
    if not isinstance(backend, dict):
        return {"type": "unknown", "config": {}}, _finding(
            "warn", "backend", "backend", "backend_unknown", "cached backend state has no backend object"
        )
    backend_type = str(backend.get("type") or "unknown")
    config = backend.get("config")
    safe_config = {
        str(key): value
        for key, value in (config.items() if isinstance(config, dict) else [])
        if str(key) in _SAFE_BACKEND_KEYS
    }
    if backend_type == "unknown":
        return {"type": backend_type, "config": safe_config}, _finding(
            "warn", "backend", "backend", "backend_unknown", "backend type is missing"
        )
    return {"type": backend_type, "config": safe_config}, None


def _state_size(project_dir: str, backend: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    backend_type = str(backend.get("type") or "unknown")
    if backend_type in _REMOTE_BACKENDS:
        return None, _finding(
            "warn",
            "state_size",
            "state_size_bytes",
            "remote_size_unavailable",
            "remote backend object size is not collected in v1.0",
        )
    config = backend.get("config")
    state_path = "terraform.tfstate"
    if isinstance(config, dict) and isinstance(config.get("path"), str):
        state_path = str(config["path"])
    path = Path(project_dir).expanduser() / state_path
    if not path.exists():
        path = Path(project_dir).expanduser() / ".terraform" / "terraform.tfstate"
    return path.stat().st_size if path.exists() else None, None


def _resource_count(stdout: str) -> int:
    return len([line for line in stdout.splitlines() if line.strip()])


def _parse_workspaces(stdout: str) -> list[dict[str, Any]]:
    workspaces = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        current = stripped.startswith("*")
        name = stripped[1:].strip() if current else stripped
        workspaces.append({"name": name, "current": current})
    return workspaces


def _parse_lock(stderr: str) -> tuple[bool, dict[str, str] | None, dict[str, Any] | None]:
    if "Error acquiring the state lock" not in stderr and "state lock" not in stderr.lower():
        return False, None, None
    fields: dict[str, str] = {}
    for line in stderr.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        normalized = key.strip().lower()
        if normalized in {"id", "path", "operation", "who", "version", "created", "info"}:
            fields[normalized] = value.strip()
    if not fields:
        return True, None, _finding("warn", "lock", "state-lock", "lock_parse_failed", "lock detected but fields could not be parsed")
    return True, fields, None


def _score(findings: list[dict[str, Any]], is_locked: bool) -> str:
    if is_locked:
        return "crit"
    if findings:
        return "warn"
    return "ok"


def _inspect(args: argparse.Namespace) -> dict[str, Any]:
    if args.host == "local":
        _validate_local_project(args.project_dir)
    tool = resolve_tool(args.host, args.tool)
    raw: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []

    backend_raw = read_text(args.host, args.project_dir, ".terraform/terraform.tfstate")
    raw["backend_state"] = backend_raw
    backend, backend_finding = _parse_backend(backend_raw)
    if backend_finding:
        findings.append(backend_finding)

    state_size_bytes, size_finding = _state_size(args.project_dir, backend) if args.host == "local" else (None, None)
    if args.host != "local":
        size_finding = _finding("warn", "state_size", "state_size_bytes", "remote_size_unavailable", "remote backend object size is not collected in v1.0")
    if size_finding:
        findings.append(size_finding)

    state_list = run_tf(args.host, args.project_dir, ["state", "list"], tool=tool, timeout=60)
    raw["state_list"] = state_list.stdout
    resource_count = _resource_count(state_list.stdout)

    lock_probe = run_tf(
        args.host,
        args.project_dir,
        ["plan", "-lock-timeout=1s", "-refresh=false", "-input=false"],
        tool=tool,
        timeout=60,
    )
    raw["lock_stdout"] = lock_probe.stdout
    raw["lock_stderr"] = lock_probe.stderr
    is_locked, lock_info, lock_parse_finding = _parse_lock(lock_probe.stderr)
    if is_locked:
        findings.append(_finding("crit", "lock", "state-lock", "locked", "state is locked"))
    if lock_parse_finding:
        findings.append(lock_parse_finding)

    workspaces: list[dict[str, Any]] = []
    try:
        workspace_result = run_tf(args.host, args.project_dir, ["workspace", "list"], tool=tool, timeout=30)
        raw["workspace_list"] = workspace_result.stdout
        workspaces = _parse_workspaces(workspace_result.stdout)
    except TfError as exc:
        raw["workspace_error"] = exc.stderr
        findings.append(_finding("warn", "workspace", "workspaces", "workspace_list_failed", exc.stderr))

    return json_result(
        success=True,
        exit_code=0,
        data={
            "host": args.host,
            "project_dir": args.project_dir,
            "tool": tool,
            "backend": backend,
            "state_size_bytes": state_size_bytes,
            "resource_count": resource_count,
            "is_locked": is_locked,
            "lock_info": lock_info,
            "workspaces": workspaces,
            "summary": {"state": _score(findings, is_locked)},
            "findings": _top_findings(findings),
            "raw": raw,
            "failure_class": None,
        },
    )


def _format_human(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    backend = data.get("backend") or {}
    lines = [
        f"State inspect {data.get('project_dir')} on {data.get('host')}",
        f"tool             {data.get('tool')}",
        f"backend          {backend.get('type')}",
        f"resources        {data.get('resource_count')}",
        f"locked           {data.get('is_locked')}",
        f"summary          {(data.get('summary') or {}).get('state')}",
    ]
    findings = data.get("findings") or []
    if findings:
        lines.append("Findings:")
        for finding in findings:
            lines.append(f"- {finding.get('severity')} {finding.get('kind')}: {finding.get('reason')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    early = handle_version("state_inspect.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    try:
        result = _inspect(args)
    except TfError as exc:
        result = json_result(
            success=False,
            exit_code=exc.exit_code,
            stderr=exc.stderr,
            data={"host": args.host, "project_dir": args.project_dir, "tool": args.tool, "failure_class": exc.failure_class},
        )
    except Exception as exc:  # pragma: no cover - last-ditch safety
        result = json_result(
            success=False,
            exit_code=1,
            stderr=f"state_inspect unexpected error: {type(exc).__name__}: {exc}",
            data={"host": args.host, "failure_class": None},
        )
    if not args.json and result.get("success"):
        result = dict(result)
        result["stdout"] = _format_human(result)
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
