# ship iac-state drift_check v1.0 — implementation plan

## Checklist

- [x] Activate task with `task.py start`.
- [x] Read `trellis-before-dev` and relevant backend specs before code edits.
- [x] Implement `drift_check.py`:
  - [x] parser keeps existing surface exactly
  - [x] local project validation
  - [x] init / plan / show flow
  - [x] unique temp plan path and best-effort cleanup
  - [x] action classification and severity scoring
  - [x] JSON and human rendering
  - [x] hard failure classification 130-135 and soft 136 warning
- [x] Add or adjust `lib/terraform.py` only for remote/local temp cleanup if needed.
- [x] Add unit tests:
  - [x] five action classes and fallback action ordering
  - [x] severity table for destructive, security, metadata, and unknown updates
  - [x] plan no-drift exit 0
  - [x] plan diff exit 2 with warn/info exit 0
  - [x] crit drift exit 1
  - [x] `--target` passes `-target=...`
  - [x] malformed plan JSON -> 132
  - [x] init/plan provider auth, lock, target, generic failures
  - [x] temp path cleanup called
  - [x] human output includes stderr for failures
- [x] Add skip-safe `live_terraform` integration tests:
  - [x] local no-drift fixture
  - [x] local create/update fixture if provider availability permits
  - [x] target argv smoke path
- [x] Update backend error-handling spec with 130-139.
- [x] Update iac-state SKILL.md quick reference/output wording if implementation details changed.

## Validation

Run targeted checks first:

```bash
uv run pytest tests/test_drift_check_unit.py -q
uv run pytest tests/test_drift_check_integration.py -q
uv run ruff check plugins/iac-state/ tests/test_drift_check_unit.py tests/test_drift_check_integration.py
uv run mypy plugins/iac-state/skills/iac-state/scripts/drift_check.py plugins/iac-state/skills/iac-state/scripts/lib/terraform.py
uv run python plugins/iac-state/skills/iac-state/scripts/drift_check.py --help
```

Then run the default suite:

```bash
uv run pytest tests/ -q
```

Run live Terraform only when `tofu` or `terraform` is installed:

```bash
uv run pytest tests/test_drift_check_integration.py -m live_terraform -q
```

## Review gates

- No `shell=True`.
- No writes beyond Terraform's temp plan file and Terraform's normal init cache.
- Cleanup must be best-effort and must not hide the primary plan/show result.
- `data.failure_class` is non-null only for hard command/schema failures.
- `data.raw.plan_json` is truncated at 10 MB, but parsing uses the full JSON.
- Existing `state_inspect` tests still pass after any helper change.
