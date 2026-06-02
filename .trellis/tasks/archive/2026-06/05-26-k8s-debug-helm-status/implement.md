# Implementation - k8s-debug helm_status v1.0

## Phase 0 - Pre-flight

- [x] Re-read `prd.md`.
- [x] Read `references/common_issues.md` Helm stuck section.
- [x] Add `design.md` with Helm runner, parsing, scoring, and failure classes.

## Phase 1 - Script foundation

- [x] Rewrite `scripts/helm_status.py` from scaffold to v1.0.
- [x] Keep CLI surface:
      `<host> <namespace> <release> [--json]` and
      `<host> --list-pending [--json]`.
- [x] Add local Helm runner for `host=local`.
- [x] Add remote Helm runner through `ssh-core/ssh_execute.py --json`.
- [x] Add local exit codes `110`-`114`; pass through kubectl `80`-`86` for hook
      job collection.

## Phase 2 - Collection

- [x] Probe `helm version --short` and reject Helm major < 3.
- [x] Per-release: collect status, history, and associated jobs.
- [x] Pending scan: collect `helm list --all-namespaces --pending -o json`.
- [x] Store raw stdout per domain, truncating at 10 MB.
- [x] Treat history failure as soft `history_truncated`.

## Phase 3 - Parsing and scoring

- [x] Normalize Helm status JSON across Helm 3 variants.
- [x] Normalize history to last 5 entries.
- [x] Normalize pending scan rows.
- [x] Implement hook job stuck/failed scoring.
- [x] Implement release summary and `recommended_next_step`.
- [x] Use stable top-5 sorting from `design.md`.

## Phase 4 - Output

- [x] JSON success envelope for per-release and list-pending modes.
- [x] JSON/human error handling for 110-112, 124, and pass-through 80-86.
- [x] Human summary for non-JSON.

## Phase 5 - Tests

- [x] Add `tests/test_helm_status_unit.py`.
- [x] Cover all 8 Helm status values.
- [x] Cover per-release and list-pending modes.
- [x] Cover Helm missing, release missing, unsupported version, and history
      truncated.
- [x] Cover associated job stuck/failed rules.
- [x] Add `tests/test_helm_status_integration.py` with `live_k8s` skip-safe
      kind/helm cases.

## Phase 6 - Quality

- [x] `uv run ruff check plugins/k8s-debug/ tests/test_helm_status_unit.py tests/test_helm_status_integration.py`
- [x] `uv run mypy plugins/k8s-debug/skills/k8s-debug/scripts/helm_status.py`
- [x] `uv run pytest tests/test_helm_status_unit.py tests/test_helm_status_integration.py -q`
- [x] `uv run pytest tests/ -q`

## Phase 7 - Finish

- [x] Update `.trellis/spec/backend/error-handling.md` with `110`-`119`.
- [ ] Commit script/tests/spec.
- [ ] Update rollout parent notes.
- [ ] Archive task.

## Rollback

- If Helm JSON shape varies more than expected, keep `helm status/history -o
  json` for primary data but add parser fallbacks rather than shelling to text
  output. Text parsing should stay a last resort because Helm localizes less in
  JSON and more in human output.
