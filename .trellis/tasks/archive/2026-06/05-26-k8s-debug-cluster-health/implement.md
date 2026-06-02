# Implementation - k8s-debug cluster_health v1.0

## Phase 0 - Pre-flight

- [x] Re-read `prd.md`.
- [x] Verify `scripts/lib/kubectl.py` exists from `diagnose_pod`.
- [x] Add `design.md` with scoring rules, managed control-plane fallback, and
      failure-class policy.

## Phase 1 - Script foundation

- [x] Rewrite `scripts/cluster_health.py` from scaffold to v1.0.
- [x] Keep CLI surface: `<host> [--json] [--since 30]`.
- [x] Import `json_result`, `emit`, `run_kubectl`, `KubectlError`, and
      `KubectlResult`.
- [x] Add local exit code `100 cluster_unreachable`; pass through 80-86 unless
      the mandatory node probe proves a Kubernetes API-server reachability
      failure.

## Phase 2 - Domain collection

- [x] Collect mandatory nodes JSON first with `run_kubectl(host, None,
      ["get", "nodes"], use_json=True)`.
- [x] Collect kube-system pods/events, raw healthz/readyz, and version text.
- [x] Use kube-system pods for both control-plane self-hosted scoring and
      kube-system addon scoring.
- [x] Store raw stdout per domain, truncating at 10 MB.
- [x] Treat healthz/readyz and version failures as soft findings.

## Phase 3 - Scoring

- [x] Implement pure scoring helpers:
      `_score_nodes`, `_score_control_plane`, `_score_kube_system`,
      `_score_events`, `_score_versions`.
- [x] Return `(score, findings)` for each helper.
- [x] Use stable top-5 sorting from `design.md`.

## Phase 4 - Output

- [x] JSON success envelope with `summary`, `findings`, `raw`, `warnings`, and
      `failure_class`.
- [x] JSON/human error handling for pass-through 80-86 and local 100.
- [x] Human summary table for non-JSON.

## Phase 5 - Tests

- [x] Add `tests/test_cluster_health_unit.py`.
- [x] Cover node Ready/pressure/unschedulable rules.
- [x] Cover self-hosted and managed control-plane scoring.
- [x] Cover kube-system pod, event, and version scoring.
- [x] Cover local 100 conversion, pass-through 80-86, and soft 101/102 findings.
- [x] Cover stable top-5 sorting and JSON envelope shape.
- [x] Add `tests/test_cluster_health_integration.py` with `live_k8s` skip-safe
      kind cases.

## Phase 6 - Quality

- [x] `uv run ruff check plugins/k8s-debug/ tests/test_cluster_health_unit.py tests/test_cluster_health_integration.py`
- [x] `uv run mypy plugins/k8s-debug/skills/k8s-debug/scripts/cluster_health.py`
- [x] `uv run pytest tests/test_cluster_health_unit.py tests/test_cluster_health_integration.py -q`
- [x] `uv run pytest tests/ -q`

## Phase 7 - Finish

- [x] Update `.trellis/spec/backend/error-handling.md` with `100`-`109`.
- [ ] Commit script/tests/spec.
- [ ] Update rollout parent notes.
- [ ] Archive task.

## Rollback

- If managed control-plane raw endpoints are too inconsistent across hosted
  Kubernetes providers, keep visible control-plane pod scoring and downgrade
  raw endpoint failures to an informational warning while leaving the rest of
  the cluster snapshot usable.
