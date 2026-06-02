# Refresh public V1 status docs and V1.1 roadmap

## Goal

Update public-facing repository docs so they accurately state the current V1 completion status and move accepted deferrals into a V1.1 roadmap.

This child task is lightweight and PRD-only: it changes documentation/status text, not runtime code.

## Confirmed Facts

- The archived `05-24-devops-skills-v1-rollout` task is completed and records V1 closure.
- `ssh_daemon.py` is intentionally deferred to V1.1 per ADR-001 A2; it is not a missing V1 implementation.
- `tests/README.md` still lists sandbox-escape coverage and `scan_software.py` live tests as missing, but both now exist.
- `README.md` still describes the repo as `v0.2.0` scaffold-stage.
- `CONTRIBUTING.md` still shows the broad V1 checklist as unchecked even though code/tests/archives show V1 is closed.

## Requirements

- Update `README.md` and `README.zh-CN.md` status from scaffold-stage to V1-complete wording.
- Update `CONTRIBUTING.md` and `CONTRIBUTING.zh-CN.md` roadmap text so V1 is not presented as unfinished.
- Update `tests/README.md` to remove stale missing V1 items and summarize current V1 coverage accurately.
- Add or revise a V1.1 roadmap section that includes accepted deferrals and next candidates:
  - ssh-core connection reuse / ControlMaster-first daemon revival
  - server-to-server direct/auto/hybrid transfer and `--use-rsync`
  - tunnel idle-timeout / Windows follow-up
  - long-running log follow behavior
  - redaction multiline/default-catalog/regex-hardening follow-ups
  - plugin-specific enhancements such as CI secret-scope deeper flags and upload-root configurability
- Preserve existing install and contribution guidance.
- Do not change code, tests, manifests, or unrelated Trellis/Codex scaffolding.

## Acceptance Criteria

- [x] `README.md` no longer says the project is `v0.2.0` scaffold-stage.
- [x] `CONTRIBUTING.md` no longer presents completed V1 scripts as unchecked pending work.
- [x] `tests/README.md` no longer lists completed sandbox/scan coverage as missing.
- [x] V1.1 roadmap candidates are documented with `ssh_daemon.py`/ControlMaster as the foundational next implementation area.
- [x] Markdown remains concise and consistent with existing repo tone.
- [x] `uv run pytest tests/test_manifests.py` passes.

## Out of Scope

- Implementing V1.1 runtime behavior.
- Running live SSH tests beyond existing skip-safe coverage.
- Editing `.trellis/spec/` unless a new implementation convention is introduced.

## Notes

- Parent task: `06-02-audit-v1-completion-plan-v11`.
- Validation:
  - `uv run pytest tests/test_manifests.py` passed: 64 tests.
  - Stale status search passed: no `v0.2.0`, scaffold-stage, V1-missing, or stale scan/sandbox missing text remains in README/CONTRIBUTING/tests README.
  - `uv run pytest --collect-only -q tests/test_ssh_guarded_lib_sandbox_unit.py tests/test_ssh_guarded_lib_redact_unit.py tests/test_ssh_guarded_lib_lockfile_unit.py tests/test_ssh_guarded_lib_request_artifact_unit.py` confirmed 87 ssh-guarded lib tests.
  - Spec update check: no `.trellis/spec/` update needed because this task only refreshed public roadmap/status docs and introduced no new implementation contract.
