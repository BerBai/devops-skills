# Implementation - iac-state state_inspect v1.0

## Phase 0 - Pre-flight

- [x] Re-read `prd.md`.
- [x] Read iac-state `SKILL.md` and sibling stubs.
- [x] Add `design.md` with helper signatures, collection, scoring, and failure
      classes.

## Phase 1 - Terraform helper

- [x] Add `scripts/lib/terraform.py`.
- [x] Implement `TfResult` and `TfError`.
- [x] Implement `resolve_tool(host, tool_arg) -> str`.
- [x] Implement `run_tf(host, project_dir, args, tool=None, timeout=60,
      parse_json=False) -> TfResult`.
- [x] Support local subprocess and remote `ssh_execute.py --json`.
- [x] Classify 120/121/122/124.

## Phase 2 - Script foundation

- [x] Rewrite `scripts/state_inspect.py` from scaffold to v1.0.
- [x] Keep CLI surface: `<host> <project-dir> [--tool auto|tofu|terraform]
      [--json]`.
- [x] Validate project dir and initialization.
- [x] Preserve JSON output contract and human summary.

## Phase 3 - Collection and scoring

- [x] Parse cached backend metadata from `.terraform/terraform.tfstate`.
- [x] Compute local backend state size; return `null` + warning for remote
      backends.
- [x] Count resources through `state list`.
- [x] Detect lock with `plan -lock-timeout=1s -refresh=false -input=false`.
- [x] Parse lock fields and handle `lock_parse_failed`.
- [x] Parse `workspace list`.
- [x] Build `summary`, findings, and raw payload.

## Phase 4 - Tests

- [x] Add `tests/test_terraform_lib_unit.py` or include lib coverage in
      `tests/test_state_inspect_unit.py`.
- [x] Add `tests/test_state_inspect_unit.py`.
- [x] Cover 7 data fields and 120-127 failure/soft classes.
- [x] Cover local/remote command construction.
- [x] Cover lock command does not include `-lock=false`.
- [x] Add skip-safe `tests/test_state_inspect_integration.py` under
      `live_terraform`.

## Phase 5 - Quality

- [x] `uv run ruff check plugins/iac-state/ tests/test_state_inspect_unit.py tests/test_state_inspect_integration.py`
- [x] `uv run mypy plugins/iac-state/skills/iac-state/scripts/state_inspect.py plugins/iac-state/skills/iac-state/scripts/lib/terraform.py`
- [x] `uv run pytest tests/test_state_inspect_unit.py tests/test_state_inspect_integration.py -q`
- [x] `uv run pytest tests/ -q`

## Phase 6 - Finish

- [x] Update `.trellis/spec/backend/error-handling.md` with `120`-`129`.
- [ ] Commit script/lib/tests/spec.
- [ ] Update rollout parent notes.
- [ ] Archive task.

## Rollback

- If remote validation cannot safely inspect project files without a dedicated
  shell probe, keep local validation strict and make remote validation rely on
  Terraform/OpenTofu command failures, mapping those to `project_dir_invalid`
  only when stderr clearly indicates a missing directory.
