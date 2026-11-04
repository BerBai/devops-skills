# Implementation Plan

## 0. Preparation

- [x] Confirm user approval to create the task.
- [x] Identify the standalone source version as `1.4.0`.
- [x] Identify diagnostic plugins that depend on `ssh_execute.py`.

## 1. Tree sync

- [x] Re-read backend coding guidelines before editing.
- [x] Remove obsolete files under `plugins/ssh-core/skills/ssh-core/`.
- [x] Copy `ssh-core/skills/ssh-core/` to
      `plugins/ssh-core/skills/ssh-core/`, excluding caches and bytecode.
- [x] Verify copied files match source and no cache files are added.
- [x] Update plugin metadata to version `1.4.0` and the new capability text.

## 2. Project integration

- [x] Scan for diagnostic callers of `ssh_execute.py` and preserved CLI flags.
- [x] Update root documentation where ssh-core capabilities changed.
- [x] Add or adapt focused tests for the synced JSON/timeout/auth/transfer
      contracts without importing across plugin boundaries.

## 3. Validation

- [x] Run the synced 1.4.0 runtime suites plus manifest and diagnostic
      unit tests; all passed.
- [x] Run non-live integration suites; live-SSH/docker/terraform cases
      skipped by their environment guards.
- [x] Run compile/structure checks and confirm the source/target tree matches.
- [x] Run the full project test suite: green (environment-dependent tests
      skipped).

## 4. Documentation and quality gate

- [x] Add a backend ADR for the 1.4.0 sync and update its index.
- [x] Update obsolete contract references in backend specs.
- [x] Run the Trellis quality-check workflow.

## 5. Commit

- [x] Review the final diff for cache/build files and unrelated changes.
- [ ] Commit with a focused message such as
      `feat: sync ssh-core 1.4.0 plugin`.

## Validation Notes

- Full suite: `PYTHONDONTWRITEBYTECODE=1 uv run python -m pytest -q` passed;
  live SSH, Docker, Kubernetes, and Terraform tests skipped by their guards.
- Structure: source/target `diff -qr` clean for 36 skill files; 30 Python files
  compile; no caches; AST scan found no cross-plugin imports.
- Static analysis: repository-wide Ruff is pre-existing red (585 issues); a
  scoped check of the 1.4.0 source and copied suites reports 271 mostly
  source-style issues. Mypy reports 18 issues in the 1.4.0 source. These are
  not treated as regressions from the standalone source; cleaning them belongs
  to a separate lint-hardening task.
- Full-suite collection exposed the existing bare `lib` name collision between
  plugins. `tests/conftest.py` now pins ssh-core's plugin-private `lib` for the
  runtime suite, and this fixture contract is documented in the directory spec.
