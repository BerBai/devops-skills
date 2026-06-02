"""Unit tests for iac-state/scripts/module_validate.py v1.0."""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "iac-state" / "skills" / "iac-state" / "scripts"

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import module_validate  # noqa: E402, I001
from lib.terraform import TfResult  # noqa: E402

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _tf(stdout: str = "", stderr: str = "", returncode: int = 0, args: list[str] | None = None) -> TfResult:
    return TfResult(stdout, stderr, returncode, 0.01, args or ["terraform"], "local")


def _good_module(path: Path) -> Path:
    (path / "main.tf").write_text(
        """
resource "terraform_data" "example" {
  input = var.name
}
""".strip()
    )
    (path / "variables.tf").write_text(
        """
variable "name" {
  type        = string
  description = "Name."
  validation {
    condition     = length(var.name) > 0
    error_message = "name required"
  }
}

variable "tags" {
  type        = map(string)
  description = "Tags."
  default     = {}
}
""".strip()
    )
    (path / "outputs.tf").write_text(
        """
output "id" {
  value       = terraform_data.example.id
  description = "ID."
}
""".strip()
    )
    (path / "versions.tf").write_text(
        """
terraform {
  required_version = ">= 1.5, < 2.0"
  required_providers {
    terraform = {
      source  = "terraform.io/builtin/terraform"
      version = "~> 1.0"
    }
  }
}
""".strip()
    )
    (path / "README.md").write_text("# module\n")
    return path


def test_clean_removes_hcl_quotes() -> None:
    assert module_validate._clean('"name"') == "name"
    assert module_validate._clean("${var.name}") == "${var.name}"


def test_validate_local_module_rejects_missing_tf(tmp_path: Path) -> None:
    with pytest.raises(Exception) as exc:
        module_validate._validate_local_module(str(tmp_path))
    assert exc.value.failure_class == "module_path_invalid"


def test_load_hcl_files_and_block_helpers(tmp_path: Path) -> None:
    _good_module(tmp_path)
    parsed, raw = module_validate._load_hcl_files(tmp_path)
    assert "terraform_data" in raw
    assert set(module_validate._named_blocks(parsed, "variable")) == {"name", "tags"}
    resources = module_validate._resources(parsed)
    assert resources[0]["type"] == "terraform_data"


@pytest.mark.parametrize("missing", ["main.tf", "variables.tf", "outputs.tf", "README.md", "versions.tf"])
def test_structure_missing_files(tmp_path: Path, missing: str) -> None:
    _good_module(tmp_path)
    (tmp_path / missing).unlink()
    findings = module_validate._check_structure(tmp_path)
    assert any(f["name"] == missing for f in findings)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "missing_type"),
        ({"type": "string"}, "missing_description"),
        ({"type": "string", "description": '"x"'}, "required_without_validation"),
    ],
)
def test_input_findings(body: dict[str, Any], reason: str) -> None:
    findings = module_validate._check_inputs({"name": body})
    assert any(f["reason"] == reason for f in findings)


@pytest.mark.parametrize(
    ("name", "body", "reason"),
    [
        ("id", {}, "missing_description"),
        ("admin_password", {"description": '"x"'}, "sensitive_not_flagged"),
    ],
)
def test_output_findings(name: str, body: dict[str, Any], reason: str) -> None:
    findings = module_validate._check_outputs({name: body})
    assert any(f["reason"] == reason for f in findings)


def test_naming_requires_name_inputs() -> None:
    findings = module_validate._check_naming({}, [])
    assert findings[0]["reason"] == "missing_name_inputs"


def test_naming_detects_hardcoded_resource_name() -> None:
    resources = [{"type": "aws_instance", "name": "web", "body": {"name": '"web-prod"'}}]
    findings = module_validate._check_naming({"name": {}}, resources)
    assert findings[0]["reason"] == "hardcoded_name"


@pytest.mark.parametrize(
    ("terraform", "reason"),
    [
        ([], "required_version_missing"),
        ([{"required_version": '">= 1.5"', "required_providers": []}], "providers_missing"),
        ([{"required_version": '">= 1.5"', "required_providers": [{"aws": {"source": '"aws"', "version": '">= 5.0"'}}]}], "provider_source_not_fqn"),
        ([{"required_version": '">= 1.5"', "required_providers": [{"aws": {"source": '"hashicorp/aws"', "version": '">= 5.0"'}}]}], "provider_version_unbounded"),
    ],
)
def test_pinning_findings(terraform: list[dict[str, Any]], reason: str) -> None:
    findings = module_validate._check_pinning(terraform)
    assert any(f["reason"] == reason for f in findings)


def test_provider_requirements_extracts_blocks() -> None:
    requirements = module_validate._provider_requirements(
        [{"required_providers": [{"aws": {"source": '"hashicorp/aws"', "version": '"~> 5.0"'}}]}]
    )
    assert requirements["aws"]["source"] == '"hashicorp/aws"'


@pytest.mark.parametrize(
    ("payload", "resources", "reason"),
    [
        ({"terraform": [{"backend": [{"s3": {}}]}]}, [], "backend_in_module"),
        ({"provider": [{"aws": {}}]}, [], "provider_block_in_module"),
        ({}, [{"type": "aws_instance", "name": "x", "body": {"count": 0}}], "count_zero_disable"),
        ({}, [{"type": "aws_instance", "name": "x", "body": {"depends_on": []}}], "resource_depends_on"),
        ({}, [{"type": "aws_instance", "name": "x", "body": {"provisioner": [{'"local-exec"': {}}]}}], "exec_provisioner"),
    ],
)
def test_antipattern_findings(payload: dict[str, Any], resources: list[dict[str, Any]], reason: str) -> None:
    findings = module_validate._check_antipatterns(payload, resources)
    assert any(f["reason"] == reason for f in findings)


@pytest.mark.parametrize("marker", ['Resource = "*"', 'Action = "*"', "0.0.0.0/0", "public-read", "public_acl", "publicly_accessible = true"])
def test_security_hints(marker: str) -> None:
    findings = module_validate._check_security(marker)
    assert findings[0]["reason"] == "risky_literal"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("terraform-aws-modules/eks/aws", ("terraform-aws-modules", "eks", "aws")),
        ("registry.terraform.io/hashicorp/consul/aws", ("hashicorp", "consul", "aws")),
        ("git::https://example.com/mod.git", None),
    ],
)
def test_parse_registry_source(source: str, expected: tuple[str, str, str] | None) -> None:
    assert module_validate._parse_registry_source(source) == expected


def test_registry_inputs_extracts_root_inputs() -> None:
    payload = {"root": {"inputs": [{"name": "a", "required": True}, {"name": "b"}]}}
    assert set(module_validate._registry_inputs(payload)) == {"a", "b"}


def test_upgrade_findings_removed_and_new_required() -> None:
    findings = module_validate._upgrade_findings(
        {"old": {}, "kept": {}},
        {"kept": {}, "new": {"required": True}},
    )
    assert [(f["reason"], f["name"]) for f in findings] == [
        ("removed_variable", "old"),
        ("new_required_variable", "new"),
    ]


def test_summary_scores_crit_warn_ok() -> None:
    assert module_validate._summarize([])["modules"] == "ok"
    assert module_validate._summarize([{"severity": "warn", "kind": "x"}])["modules"] == "warn"
    assert module_validate._summarize([{"severity": "crit", "kind": "x"}])["modules"] == "crit"


def test_success_envelope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _good_module(tmp_path)
    monkeypatch.setattr(module_validate, "resolve_tool", lambda *_a: "terraform")

    def fake_run_tf(_host: str, _module_path: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "fmt":
            return _tf(args=args)
        if args[0] == "validate":
            return _tf(json.dumps({"valid": True}), args=args)
        raise AssertionError(args)

    monkeypatch.setattr(module_validate, "run_tf", fake_run_tf)
    rc = module_validate.main(["local", str(tmp_path), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["summary"]["modules"] == "ok"


def test_fmt_failure_is_soft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _good_module(tmp_path)
    monkeypatch.setattr(module_validate, "resolve_tool", lambda *_a: "terraform")

    def fake_run_tf(_host: str, _module_path: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "fmt":
            return _tf(stderr="needs format", returncode=3, args=args)
        if args[0] == "validate":
            return _tf(json.dumps({"valid": True}), args=args)
        raise AssertionError(args)

    monkeypatch.setattr(module_validate, "run_tf", fake_run_tf)
    rc = module_validate.main(["local", str(tmp_path), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(f["reason"] == "fmt_failed" for f in env["data"]["findings"])


def test_validate_failure_is_hard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _good_module(tmp_path)
    monkeypatch.setattr(module_validate, "resolve_tool", lambda *_a: "terraform")

    def fake_run_tf(_host: str, _module_path: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "fmt":
            return _tf(args=args)
        if args[0] == "validate":
            return _tf(stderr="invalid", returncode=1, args=args)
        raise AssertionError(args)

    monkeypatch.setattr(module_validate, "run_tf", fake_run_tf)
    rc = module_validate.main(["local", str(tmp_path), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 143
    assert env["data"]["failure_class"] == "validate_failed"


def test_hcl_parse_failure_degrades(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "main.tf").write_text("not valid {")
    monkeypatch.setattr(module_validate, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(module_validate, "run_tf", lambda *_a, **_kw: _tf(json.dumps({"valid": True})))
    rc = module_validate.main(["local", str(tmp_path), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(w["failure_class"] == "hcl_parse_failed" for w in env["data"]["warnings"])


def test_unsupported_source_for_target_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _good_module(tmp_path)
    (tmp_path / "module.tf").write_text('module "x" { source = "./local" }\n')
    monkeypatch.setattr(module_validate, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(module_validate, "run_tf", lambda *_a, **_kw: _tf(json.dumps({"valid": True})))
    rc = module_validate.main(["local", str(tmp_path), "--target-version", "1.0.0", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 146
    assert env["data"]["failure_class"] == "unsupported_source"


def test_registry_version_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_validate.urllib.request, "urlopen", lambda *_a, **_kw: (_ for _ in ()).throw(module_validate.urllib.error.HTTPError("u", 404, "missing", {}, None)))
    with pytest.raises(module_validate.TfError) as exc:
        module_validate._fetch_registry_version("hashicorp/consul/aws", "0.0.0")
    assert exc.value.failure_class == "registry_version_not_found"


def test_registry_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_validate.urllib.request, "urlopen", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(module_validate.TfError) as exc:
        module_validate._fetch_registry_version("hashicorp/consul/aws", "1.0.0")
    assert exc.value.failure_class == "registry_unreachable"
