# ship iac-state module_validate v1.0 — implementation plan

## Checklist

- [x] Activate task with `task.py start`.
- [x] Add `python-hcl2>=8.1,<9` runtime dependency and sync `uv.lock`.
- [x] Implement `module_validate.py`:
  - [x] keep existing CLI surface
  - [x] JSON/human output via iac-state `json_result` / `emit`
  - [x] local module path validation
  - [x] fmt and validate CLI runners
  - [x] HCL file loading and parsing
  - [x] seven static check categories
  - [x] registry target-version fetch and upgrade diff
  - [x] hard/soft failure classes 140-146
- [x] Add unit tests:
  - [x] parser/help and envelope shape
  - [x] each static category ok/violation
  - [x] fmt soft failure
  - [x] validate hard failure
  - [x] HCL parse soft degradation
  - [x] registry timeout/404/unsupported source
  - [x] upgrade_diff removed/new required variables
- [x] Add skip-safe `live_terraform` integration tests:
  - [x] well-formed module
  - [x] broken module
- [x] Update `.trellis/spec/backend/error-handling.md` with 140-149 scenario.
- [x] Update iac-state SKILL.md and references if output wording changes.

## Validation

```bash
uv run pytest tests/test_module_validate_unit.py -q
uv run pytest tests/test_module_validate_integration.py -q
uv run ruff check plugins/iac-state/ tests/test_module_validate_unit.py tests/test_module_validate_integration.py
uv run mypy plugins/iac-state/skills/iac-state/scripts/module_validate.py
uv run python plugins/iac-state/skills/iac-state/scripts/module_validate.py --help
uv run pytest tests/ -q
```

`live_terraform` integration tests must skip cleanly when no `tofu` or
`terraform` binary is installed.

## Review gates

- No `shell=True`.
- No Terraform write commands.
- No real registry calls in unit tests.
- HCL parse failure must not prevent fmt / validate.
- `validate_failed` is hard exit 143; fmt and parse failures are findings.
- `uv.lock` changes must include only dependency sync for this task.
