#!/usr/bin/env python3
"""module_validate.py - Terraform/OpenTofu module audit (v1.0)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import hcl2  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import emit, json_result  # type: ignore[import-not-found]  # noqa: E402, I001
from lib.terraform import TfError, resolve_tool, run_tf  # type: ignore[import-not-found]  # noqa: E402

_MODULE_PATH_INVALID = 140
_HCL_PARSE_FAILED = 141
_FMT_FAILED = 142
_VALIDATE_FAILED = 143
_REGISTRY_UNREACHABLE = 144
_REGISTRY_VERSION_NOT_FOUND = 145
_UNSUPPORTED_SOURCE = 146
_MIN_QUOTED_LEN = 2
_REGISTRY_SOURCE_PARTS = 3
_HTTP_NOT_FOUND = 404

_STRUCTURE_FILES = ("main.tf", "variables.tf", "outputs.tf", "README.md", "versions.tf")
_SEVERITY_ORDER = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
_SECRET_OUTPUT_MARKERS = ("password", "secret", "token", "key")
_SECURITY_TEXT_MARKERS = (
    'Resource = "*"',
    '"Resource": "*"',
    'Action = "*"',
    '"Action": "*"',
    "0.0.0.0/0",
    "public-read",
    "public_acl",
    "publicly_accessible = true",
)
_REGISTRY_BASE = "https://registry.terraform.io/v1/modules"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="module_validate.py")
    parser.add_argument("host", help="ssh-core alias, or 'local'")
    parser.add_argument("module_path", help="Path to the module directory")
    parser.add_argument("--target-version", default=None, help="Compare against this registry version")
    parser.add_argument("--tool", choices=["auto", "tofu", "terraform"], default="auto")
    parser.add_argument("--json", action="store_true")
    return parser


def _clean(value: Any) -> str:
    text = str(value or "")
    if len(text) >= _MIN_QUOTED_LEN and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _finding(severity: str, kind: str, name: str, reason: str, message: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "kind": kind,
        "name": name,
        "reason": reason,
        "message": message,
        "hint": f"references/module_audit.md#{kind}",
    }


def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item.get("severity")), 9),
            str(item.get("kind") or ""),
            str(item.get("name") or ""),
            str(item.get("reason") or ""),
        ),
    )


def _validate_local_module(module_path: str) -> Path:
    path = Path(module_path).expanduser()
    if not path.is_dir():
        raise TfError("module_path_invalid", _MODULE_PATH_INVALID, f"module path is not a directory: {module_path}")
    if not any(path.glob("*.tf")):
        raise TfError("module_path_invalid", _MODULE_PATH_INVALID, f"module path has no *.tf files: {module_path}")
    return path


def _load_hcl_files(module_dir: Path) -> tuple[dict[str, Any], str]:
    merged: dict[str, Any] = {}
    raw_parts: list[str] = []
    for path in sorted(module_dir.glob("*.tf")):
        raw = path.read_text()
        raw_parts.append(raw)
        try:
            parsed = hcl2.loads(raw)
        except Exception as exc:
            raise TfError("hcl_parse_failed", _HCL_PARSE_FAILED, f"{path.name}: {exc}") from None
        for key, value in parsed.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            else:
                merged[key] = value
    return merged, "\n".join(raw_parts)


def _named_blocks(payload: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for block in payload.get(key, []):
        if not isinstance(block, dict):
            continue
        for name, body in block.items():
            if name == "__is_block__" or not isinstance(body, dict):
                continue
            out[_clean(name)] = body
    return out


def _resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for block in payload.get("resource", []):
        if not isinstance(block, dict):
            continue
        for raw_type, by_name in block.items():
            if raw_type == "__is_block__" or not isinstance(by_name, dict):
                continue
            for raw_name, body in by_name.items():
                if raw_name == "__is_block__" or not isinstance(body, dict):
                    continue
                resources.append({"type": _clean(raw_type), "name": _clean(raw_name), "body": body})
    return resources


def _terraform_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in payload.get("terraform", []) if isinstance(item, dict)]


def _check_structure(module_dir: Path) -> list[dict[str, Any]]:
    findings = []
    for filename in _STRUCTURE_FILES:
        if not (module_dir / filename).exists():
            findings.append(_finding("warn", "structure", filename, "missing_file", f"{filename} is missing"))
    return findings


def _check_inputs(variables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for name, body in variables.items():
        if "type" not in body:
            findings.append(_finding("warn", "inputs", name, "missing_type", "variable has no type"))
        if "description" not in body:
            findings.append(_finding("warn", "inputs", name, "missing_description", "variable has no description"))
        if "default" not in body and "validation" not in body:
            findings.append(_finding("info", "inputs", name, "required_without_validation", "required variable has no validation block"))
    return findings


def _check_outputs(outputs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for name, body in outputs.items():
        if "description" not in body:
            findings.append(_finding("warn", "outputs", name, "missing_description", "output has no description"))
        if any(marker in name.lower() for marker in _SECRET_OUTPUT_MARKERS) and body.get("sensitive") is not True:
            findings.append(_finding("warn", "outputs", name, "sensitive_not_flagged", "secret-like output is not sensitive"))
    return findings


def _value_uses_var(value: Any) -> bool:
    return "var." in str(value)


def _check_naming(variables: dict[str, dict[str, Any]], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    if not ({"name_prefix", "name", "tags"} & set(variables)):
        findings.append(_finding("warn", "naming", "variables", "missing_name_inputs", "module has no name/name_prefix/tags input"))
    for resource in resources:
        body = resource["body"]
        if "name" in body and isinstance(body["name"], str) and not _value_uses_var(body["name"]):
            findings.append(
                _finding(
                    "warn",
                    "naming",
                    f"{resource['type']}.{resource['name']}",
                    "hardcoded_name",
                    "resource name is hardcoded instead of variable-derived",
                )
            )
    return findings


def _provider_requirements(terraform: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    requirements: dict[str, dict[str, Any]] = {}
    for block in terraform:
        for req in block.get("required_providers", []):
            if not isinstance(req, dict):
                continue
            for name, body in req.items():
                if name != "__is_block__" and isinstance(body, dict):
                    requirements[_clean(name)] = body
    return requirements


def _has_upper_bound(version: str) -> bool:
    return "<" in version or "~>" in version


def _check_pinning(terraform: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    if not any("required_version" in block for block in terraform):
        findings.append(_finding("warn", "pinning", "terraform", "required_version_missing", "required_version is missing"))
    providers = _provider_requirements(terraform)
    if not providers:
        findings.append(_finding("warn", "pinning", "required_providers", "providers_missing", "required_providers is missing"))
    for name, body in providers.items():
        source = _clean(body.get("source"))
        version = _clean(body.get("version"))
        if "/" not in source:
            findings.append(_finding("warn", "pinning", name, "provider_source_not_fqn", "provider source is not fully qualified"))
        if not version or not _has_upper_bound(version):
            findings.append(_finding("warn", "pinning", name, "provider_version_unbounded", "provider version lacks upper bound"))
    return findings


def _check_antipatterns(payload: dict[str, Any], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for block in _terraform_blocks(payload):
        if "backend" in block:
            findings.append(_finding("crit", "antipatterns", "terraform.backend", "backend_in_module", "backend block must not live inside a module"))
    for block in payload.get("provider", []):
        if isinstance(block, dict):
            findings.append(_finding("warn", "antipatterns", "provider", "provider_block_in_module", "provider block should be injected by root"))
    for resource in resources:
        body = resource["body"]
        name = f"{resource['type']}.{resource['name']}"
        if body.get("count") == 0:
            findings.append(_finding("warn", "antipatterns", name, "count_zero_disable", "count = 0 is a brittle disable pattern"))
        if "depends_on" in body:
            findings.append(_finding("warn", "antipatterns", name, "resource_depends_on", "resource-level depends_on usually hides data-flow issues"))
        for prov in body.get("provisioner", []):
            if isinstance(prov, dict):
                for prov_name in prov:
                    clean_name = _clean(prov_name)
                    if clean_name in {"local-exec", "remote-exec"}:
                        findings.append(_finding("warn", "antipatterns", name, "exec_provisioner", f"{clean_name} provisioner couples module to runtime environment"))
    return findings


def _check_security(raw_hcl: str) -> list[dict[str, Any]]:
    findings = []
    for marker in _SECURITY_TEXT_MARKERS:
        if marker in raw_hcl:
            findings.append(_finding("warn", "security_hints", marker, "risky_literal", f"found risky literal {marker}"))
    return findings


def _registry_source(modules: dict[str, dict[str, Any]]) -> str | None:
    for body in modules.values():
        source = _clean(body.get("source"))
        if source:
            return source
    return None


def _parse_registry_source(source: str) -> tuple[str, str, str] | None:
    clean = source.removeprefix("registry.terraform.io/")
    parts = clean.split("/")
    if len(parts) == _REGISTRY_SOURCE_PARTS and all(parts):
        return parts[0], parts[1], parts[2]
    return None


def _fetch_registry_version(source: str, version: str, timeout: int = 10) -> dict[str, Any]:
    parsed = _parse_registry_source(source)
    if not parsed:
        raise TfError("unsupported_source", _UNSUPPORTED_SOURCE, f"unsupported module source for registry diff: {source}")
    namespace, name, provider = parsed
    url = f"{_REGISTRY_BASE}/{namespace}/{name}/{provider}/{version}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == _HTTP_NOT_FOUND:
            raise TfError("registry_version_not_found", _REGISTRY_VERSION_NOT_FOUND, f"registry version not found: {source}@{version}") from None
        raise TfError("registry_unreachable", _REGISTRY_UNREACHABLE, str(exc)) from None
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise TfError("registry_unreachable", _REGISTRY_UNREACHABLE, str(exc)) from None


def _registry_inputs(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_root = payload.get("root")
    root: dict[str, Any] = raw_root if isinstance(raw_root, dict) else {}
    raw_inputs = root.get("inputs")
    inputs: list[Any] = raw_inputs if isinstance(raw_inputs, list) else []
    out: dict[str, dict[str, Any]] = {}
    for item in inputs:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = item
    return out


def _upgrade_findings(
    current_variables: dict[str, dict[str, Any]],
    target_inputs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings = []
    current_names = set(current_variables)
    target_names = set(target_inputs)
    for name in sorted(current_names - target_names):
        findings.append(_finding("crit", "upgrade_diff", name, "removed_variable", "target version removes this variable"))
    for name in sorted(target_names - current_names):
        target = target_inputs[name]
        if target.get("required") is True or target.get("default") in {None, ""}:
            findings.append(_finding("warn", "upgrade_diff", name, "new_required_variable", "target version adds a required variable"))
    return findings


def _summarize(findings: list[dict[str, Any]]) -> dict[str, Any]:
    severity_counts = {"crit": 0, "warn": 0, "info": 0}
    by_kind: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "")
        kind = str(finding.get("kind") or "")
        if severity in severity_counts:
            severity_counts[severity] += 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    if severity_counts["crit"]:
        modules = "crit"
    elif severity_counts["warn"]:
        modules = "warn"
    else:
        modules = "ok"
    return {"modules": modules, "findings_by_kind": by_kind, "severity_counts": severity_counts}


def _run_fmt(args: argparse.Namespace, tool: str, raw: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    fmt = run_tf(args.host, args.module_path, ["fmt", "-check", "-recursive"], tool=tool, timeout=60)
    raw.update({"fmt_stdout": fmt.stdout, "fmt_stderr": fmt.stderr, "fmt_exit_code": fmt.returncode})
    if fmt.returncode != 0:
        findings.append(_finding("warn", "fmt", "terraform fmt", "fmt_failed", "terraform fmt -check reported files needing format"))


def _parse_local_hcl(
    module_dir: Path | None,
    warnings: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    if module_dir is not None:
        try:
            return _load_hcl_files(module_dir)
        except TfError as exc:
            warnings.append({"failure_class": exc.failure_class, "exit_code": exc.exit_code, "message": exc.stderr})
            findings.append(_finding("warn", "hcl_parse", "hcl", "hcl_parse_failed", exc.stderr))
    else:
        warnings.append({"failure_class": "hcl_parse_failed", "exit_code": 141, "message": "remote static HCL parsing is deferred in v1.0"})
    return None, ""


def _run_tf_validate(
    args: argparse.Namespace,
    tool: str,
    raw: dict[str, Any],
    modules: dict[str, dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    if modules:
        warnings.append({"failure_class": None, "exit_code": 0, "message": "terraform validate skipped because nested module initialization is out of scope"})
        return
    validate = run_tf(args.host, args.module_path, ["validate", "-json"], tool=tool, timeout=60)
    raw.update(
        {
            "validate_stdout": validate.stdout,
            "validate_stderr": validate.stderr,
            "validate_json": validate.stdout,
        }
    )
    if validate.returncode != 0:
        raise TfError("validate_failed", _VALIDATE_FAILED, validate.stderr or validate.stdout or "terraform validate failed")


def _run_static_checks(
    parsed: dict[str, Any] | None,
    module_dir: Path | None,
    raw_hcl: str,
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    variables: dict[str, dict[str, Any]] = {}
    if parsed is not None and module_dir is not None:
        variables = _named_blocks(parsed, "variable")
        outputs = _named_blocks(parsed, "output")
        resources = _resources(parsed)
        findings.extend(_check_structure(module_dir))
        findings.extend(_check_inputs(variables))
        findings.extend(_check_outputs(outputs))
        findings.extend(_check_naming(variables, resources))
        findings.extend(_check_pinning(_terraform_blocks(parsed)))
        findings.extend(_check_antipatterns(parsed, resources))
        findings.extend(_check_security(raw_hcl))
    return variables


def _run_upgrade_diff(
    args: argparse.Namespace,
    modules: dict[str, dict[str, Any]],
    variables: dict[str, dict[str, Any]],
    findings: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    if args.target_version:
        source = _registry_source(modules)
        if not source:
            raise TfError("unsupported_source", _UNSUPPORTED_SOURCE, "target-version requires a registry module source")
        try:
            target_payload = _fetch_registry_version(source, args.target_version)
            findings.extend(_upgrade_findings(variables, _registry_inputs(target_payload)))
        except TfError as exc:
            if exc.failure_class == "registry_unreachable":
                warnings.append({"failure_class": exc.failure_class, "exit_code": exc.exit_code, "message": exc.stderr})
            else:
                raise


def _run_validate(args: argparse.Namespace) -> dict[str, Any]:
    module_dir = _validate_local_module(args.module_path) if args.host == "local" else None
    tool = resolve_tool(args.host, args.tool)
    raw: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    _run_fmt(args, tool, raw, findings)
    parsed, raw_hcl = _parse_local_hcl(module_dir, warnings, findings)
    modules = _named_blocks(parsed or {}, "module")
    _run_tf_validate(args, tool, raw, modules, warnings)
    variables = _run_static_checks(parsed, module_dir, raw_hcl, findings)
    _run_upgrade_diff(args, modules, variables, findings, warnings)

    sorted_findings = _sort_findings(findings)
    summary = _summarize(sorted_findings)
    exit_code = 1 if summary["modules"] == "crit" else 0
    return json_result(
        success=exit_code == 0,
        exit_code=exit_code,
        data={
            "host": args.host,
            "module_path": args.module_path,
            "target_version": args.target_version,
            "tool": tool,
            "summary": summary,
            "findings": sorted_findings,
            "warnings": warnings,
            "raw": raw,
            "failure_class": None,
        },
    )


def _format_human(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    lines = [
        f"Module validate {data.get('module_path')} on {data.get('host')}",
        f"tool             {data.get('tool')}",
        f"summary          {summary.get('modules')}",
    ]
    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    if findings:
        lines.append("Findings:")
        for finding in findings[:12]:
            lines.append(f"- {finding.get('severity')} {finding.get('kind')}: {finding.get('reason')} ({finding.get('name')})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _run_validate(args)
    except TfError as exc:
        result = json_result(
            success=False,
            exit_code=exc.exit_code,
            stderr=exc.stderr,
            data={
                "host": args.host,
                "module_path": args.module_path,
                "target_version": args.target_version,
                "tool": args.tool,
                "failure_class": exc.failure_class,
            },
        )
    except Exception as exc:  # pragma: no cover - last-ditch safety
        result = json_result(
            success=False,
            exit_code=1,
            stderr=f"module_validate unexpected error: {type(exc).__name__}: {exc}",
            data={"host": args.host, "module_path": args.module_path, "failure_class": None},
        )
    if not args.json and result.get("success"):
        result = dict(result)
        result["stdout"] = _format_human(result)
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
