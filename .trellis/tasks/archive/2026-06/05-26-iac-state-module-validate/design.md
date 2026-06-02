# ship iac-state module_validate v1.0 — design

## Scope and boundaries

`module_validate.py` is the iac-state static module audit entrypoint. It is
read-only: it never runs plan/apply/import/destroy and never rewrites HCL. It
combines Terraform/OpenTofu CLI checks (`fmt -check -recursive`,
`validate -json`) with static HCL inspection.

The script reuses `scripts/lib/terraform.py` for tool resolution and local/ssh
dispatch. Static HCL parsing uses `python-hcl2>=8.1,<9`; PyPI lists 8.1.2 as
the latest stable release as of 2026-06-02.

Registry calls use stdlib `urllib.request` instead of adding `requests` as a
second runtime dependency. Tests mock the registry fetch function directly, so
unit tests never reach the network.

## Data flow

1. Validate module path:
   - local: directory exists and contains at least one `*.tf`
   - remote: command path is trusted to Terraform; hard validation happens via
     `fmt` / `validate`
2. Resolve `tofu` or `terraform`.
3. Run `fmt -check -recursive`.
4. Parse all local `*.tf` files with `python-hcl2` when `host == "local"`.
   Remote v1.0 runs only CLI checks and returns a warning that static HCL audit
   is local-only.
5. Run `validate -json` unless parsed HCL contains nested `module` blocks; in
   that case v1.0 skips validate because module source initialization is out of
   scope.
6. Execute seven static check categories when HCL parse succeeds:
   `structure`, `inputs`, `outputs`, `naming`, `pinning`, `antipatterns`,
   `security_hints`.
7. If `--target-version` is present, locate a registry source in `module`
   blocks, fetch the target version metadata, parse its variables when provided,
   and emit `upgrade_diff` findings.

## Static check rules

- `structure`
  - warn when `main.tf`, `variables.tf`, `outputs.tf`, `README.md`, or
    `versions.tf` is missing.
- `inputs`
  - warn when a variable lacks `type` or `description`.
  - info when a required variable lacks validation.
- `outputs`
  - warn when an output lacks `description`.
  - warn when an output name contains `password`, `secret`, `token`, or `key`
    but `sensitive=true` is absent.
- `naming`
  - warn when no variables named `name_prefix`, `name`, or `tags` exist.
  - warn when resource bodies contain hardcoded `name = "..."` and no variable
    interpolation.
- `pinning`
  - warn when `terraform.required_version` is missing.
  - warn when provider requirements lack fully-qualified `source`.
  - warn when provider version lacks an upper bound (`<` or `~>`).
- `antipatterns`
  - crit for `terraform.backend` blocks inside a module.
  - warn for `provider` blocks inside a module.
  - warn for `count = 0`, resource-level `depends_on`, and
    `local-exec` / `remote-exec` provisioners.
- `security_hints`
  - warn for literal `Resource="*"`, `Action="*"`, `0.0.0.0/0`,
    `public-read`, `public_acl`, and `publicly_accessible=true`.

Findings have stable fields:
`severity`, `kind`, `name`, `reason`, `message`, and `hint`.

## Output contract

`data` contains:

- `host`, `module_path`, `target_version`, `tool`
- `summary`: `{modules: ok|warn|crit, findings_by_kind, severity_counts}`
- `findings`: sorted by severity, kind, name, reason
- `warnings`: soft degraded-mode notes
- `raw`: `fmt_stdout`, `fmt_stderr`, `fmt_exit_code`, `validate_stdout`,
  `validate_stderr`, `validate_json`
- `failure_class`: hard failure class or `null`

`fmt_failed`, `hcl_parse_failed`, and registry network failures are soft
findings/warnings. `validate_failed` is a hard module-health failure with exit
143 because Terraform validation diagnostics mean the module is invalid.

## Failure classes

- `140 module_path_invalid`
- `141 hcl_parse_failed` (soft warning; static checks skipped)
- `142 fmt_failed` (soft finding; exit follows summary severity)
- `143 validate_failed`
- `144 registry_unreachable` (soft warning; upgrade_diff skipped)
- `145 registry_version_not_found`
- `146 unsupported_source`
- `147`-`149` reserved

Shared iac-state failures `120`-`124` can pass through from `lib/terraform.py`.

## Registry source support

`--target-version` v1.0 supports only public registry source strings:

```text
namespace/name/provider
registry.terraform.io/namespace/name/provider
```

Git, local, and private registry sources return `unsupported_source` (146).
The fetch URL is:

```text
https://registry.terraform.io/v1/modules/<namespace>/<name>/<provider>/<version>
```

If the response includes a `root.inputs[]` array, compare variable names
against current local variables. Removed current variables are `crit`; new
required target variables are `warn`.

## Rollback

Risky files:

- `plugins/iac-state/skills/iac-state/scripts/module_validate.py`
- `pyproject.toml` / `uv.lock`
- `tests/test_module_validate_unit.py`
- `tests/test_module_validate_integration.py`
- `.trellis/spec/backend/error-handling.md`
- `plugins/iac-state/skills/iac-state/SKILL.md`

Rollback is a direct revert of this child commit. No persistent state is
written by the script.
