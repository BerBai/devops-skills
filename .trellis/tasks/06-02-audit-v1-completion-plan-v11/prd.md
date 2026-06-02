# Audit devops skills V1 completion and plan V1.1

## Goal

Verify whether all devops-skills plugins have reached the accepted V1 completion bar, then choose the first V1.1 deliverable to plan and implement as an independently verifiable child task.

This parent task owns the audit and V1.1 prioritization. Implementation should happen in child tasks.

## Confirmed Facts

- The archived `05-24-devops-skills-v1-rollout` parent is completed and records the accepted V1 closure criterion: shipped scripts are V1 complete with tests, while `ssh_daemon.py` is intentionally deferred to V1.1 per ADR-001 A2.
- Repository audit found 32 V1 CLI targets across 8 plugins:
  - `ssh-core`: 9 CLI targets; 8 shipped with unit/integration coverage; `ssh_daemon.py` remains a documented V1.1 defer.
  - `ssh-guarded`: 8 CLI targets; all shipped. Request drafters and `run_request.py` are covered by unit tests and full-pipeline live tests; `scan_software.py` has both unit and live_ssh integration tests.
  - `remote-debug`: 4 CLI targets; all shipped with unit/integration coverage.
  - `k8s-debug`: 4 CLI targets; all shipped with unit/integration coverage.
  - `docker-quick`: 3 CLI targets; all shipped with unit/integration coverage.
  - `log-aggregator`: 3 CLI targets; all shipped with unit/integration coverage.
  - `iac-state`: 3 CLI targets; all shipped with unit/integration coverage.
  - `cicd-debug`: 3 CLI targets; all shipped with unit/integration coverage.
- No non-deferred CLI script still calls `unimplemented(...)`.
- `tests/test_scan_software_integration.py` exists and is marked `live_ssh`; `uv run pytest tests/test_manifests.py tests/test_scan_software_unit.py tests/test_scan_software_integration.py` passed: 126 tests.
- Previous full-suite validation after the latest regression work passed: `uv run pytest tests/` -> 1597 passed, 35 skipped.
- Status docs are stale:
  - `README.md` still says `v0.2.0` scaffold stage.
  - `CONTRIBUTING.md` still shows unchecked V1 roadmap items.
  - `tests/README.md` still lists sandbox-escape coverage and `scan_software.py` live tests as missing, even though both now exist.

## V1 Audit Conclusion

V1 is complete under the accepted rollout criterion. The project should move to V1.1 planning, with a small docs/status refresh as a prerequisite cleanup so public roadmap text does not contradict the shipped state.

## V1.1 Candidate Backlog

- P0: Public V1 status refresh: update README/CONTRIBUTING/tests README to mark V1 complete, document the accepted `ssh_daemon.py` defer, and move remaining deferred items into V1.1.
- P1: `ssh-core` connection reuse / daemon revival: evaluate OpenSSH ControlMaster first, then decide whether `ssh_daemon.py` stays as a custom daemon or becomes a compatibility wrapper. This unblocks several V1.1 items.
- P1: `ssh-core` server-to-server direct/auto/hybrid transfer modes and `--use-rsync`, currently surface-preserved but deferred.
- P2: `ssh_tunnel.py --idle-timeout` and Windows behavior, likely tied to the ControlMaster decision.
- P2: long-running/follow behavior for `tail_multi.py`, `tail_log.py`, and related log workflows.
- P2: `redact_check.py` multiline private-key buffering, default catalog export, and optional regex timeout hardening.
- P3: narrower plugin-specific enhancements already documented in references, such as CI secret-scope deeper risk flags and request upload-root configurability.

## Requirements

- Do not treat stale roadmap text as authoritative when code/tests/task archives disagree.
- Keep V1.1 implementation in child tasks with independent acceptance criteria.
- Prefer foundational V1.1 work that unblocks multiple deferred behaviors before single-plugin enhancements.
- Preserve skip-safe validation for environment-dependent integration tests.
- Do not modify unrelated dirty/untracked scaffolding in the working tree.

## Acceptance Criteria

- [x] V1 completion status is audited across all plugins using repository evidence.
- [x] Remaining V1 gaps, if any, are distinguished from accepted V1.1 deferrals.
- [x] V1.1 candidates are prioritized.
- [x] At least one V1.1 child task is created for the next deliverable, or this task records why implementation is blocked.

## Recommended Next Child

Created child task `06-02-refresh-v1-status-docs-v11-roadmap` for `Refresh public V1 status docs and V1.1 roadmap`. This should run before implementation-heavy V1.1 work because it is small, local, and removes the current contradiction where public docs say scaffold/V1-incomplete while tests and archived tasks say V1 is complete.

After that, create a child task for `Plan ssh-core connection reuse V1.1` with ADR-depth design around ControlMaster versus custom `ssh_daemon.py`.

## Notes

- Current task is a parent/planning task. Do not start it for direct code implementation unless the scope changes.
