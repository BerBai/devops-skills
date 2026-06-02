# Implementation — k8s-debug check_namespace v1.0

## Phase 0 — Pre-flight

- [x] Re-read `prd.md`.
- [x] Verify `scripts/lib/kubectl.py` exists from `diagnose_pod`.
- [x] Add `design.md` with scoring rules and partial collection policy.

## Phase 1 — Script foundation

- [x] Rewrite `scripts/check_namespace.py` from scaffold to v1.0.
- [x] Keep CLI surface: `<host> <namespace> [--since 60] [--json]`.
- [x] Import `json_result`, `emit`, `run_kubectl`, `KubectlError`, `KubectlResult`.
- [x] Add local exit code `88 partial_collection`; pass through 80-86 from lib.

## Phase 2 — Domain collectors

- [x] Probe namespace with `run_kubectl(host, None, ["get", "ns", namespace])`.
- [x] Collect pods/events/deployments/services/endpoints/pvcs concurrently.
- [x] Store raw stdout per domain, truncating at 10 MB.
- [x] Domain collection failures become partial failures unless 4+ domains fail.

## Phase 3 — Scoring

- [x] Implement pure scoring helpers:
      `_score_pods`, `_score_events`, `_score_deployments`,
      `_score_services`, `_score_pvcs`.
- [x] Return `(score, findings)` for each helper.
- [x] Use stable top-3 sort from `design.md`.

## Phase 4 — Output

- [x] JSON success envelope with `summary`, `findings`, `raw`,
      `partial_failures`, `failure_class`.
- [x] JSON/human error handling for pass-through 80-86 and local 88.
- [x] Human summary table for non-JSON.

## Phase 5 — Tests

- [x] Add `tests/test_check_namespace_unit.py`.
- [x] Cover each domain's ok/warn/crit rules.
- [x] Cover namespace_not_found pass-through 84.
- [x] Cover partial collection success (<=3 failures) and hard 88 (>=4 failures).
- [x] Cover stable top-3 sorting and JSON envelope shape.
- [x] Add `tests/test_check_namespace_integration.py` with `live_k8s` skip-safe
      local cluster cases.

## Phase 6 — Quality

- [x] `uv run ruff check plugins/k8s-debug/ tests/test_check_namespace_unit.py tests/test_check_namespace_integration.py`
- [x] `uv run mypy plugins/k8s-debug/skills/k8s-debug/scripts/check_namespace.py`
- [x] `uv run pytest tests/test_check_namespace_unit.py -q`
- [x] `uv run pytest tests/test_check_namespace_integration.py -m live_k8s -q`
- [x] `uv run pytest tests/ -q`

## Phase 7 — Finish

- [ ] Commit script/tests.
- [ ] Update rollout parent notes.
- [ ] Archive task.

## Rollback

- If `services` endpoint joining is unreliable across Kubernetes versions,
  keep endpoints raw in `data.raw` and downgrade unknown service endpoint
  shapes to warn findings rather than hard failure.
