# Add redact catalog regression coverage

## Goal

Add focused regression coverage for `ssh-guarded` redact catalog behavior so `redact_check.py` catches catalog/schema/coverage regressions without requiring remote infrastructure.

This child task is lightweight and PRD-only: it changes tests around an existing CLI and redaction library contract rather than introducing a new architecture.

## Confirmed Facts

- `tests/README.md` explicitly lists "Redact-catalog regression suite for `redact_check.py`" as missing.
- `redact_check.py` already validates catalog schema, compiles catalog regexes, reports per-line hit details, and supports JSON/human output.
- `tests/test_redact_check_unit.py` already covers basic built-in patterns, custom catalog override, schema errors, JSON output, report-only mode, multiline line numbers, empty stdin, and binary stdin.
- `plugins/ssh-guarded/skills/ssh-guarded/references/redact.md` documents a broader secret catalog than the current built-in implementation. This task should add regression tests for shipped behavior only unless the implementation is intentionally updated.
- The project quality bar requires `pytest tests/`, `ruff check .`, and `mypy` where practical.

## Requirements

- Add local pytest coverage that exercises catalog behavior more thoroughly than the existing smoke cases.
- Cover edge cases that are likely to regress:
  - multiple catalog patterns firing in one input
  - replacement text used by catalog entries
  - ordered redaction when one replacement could affect a later pattern
  - JSON error envelope shape for catalog failures
  - built-in catalog behavior for currently shipped default patterns
- Keep tests deterministic and free of remote host or external tool dependencies.
- Do not add tests for unshipped catalog entries unless implementation is intentionally updated in the same task.
- Preserve the existing `redact_check.py` JSON envelope and exit-code contract.

## Acceptance Criteria

- [x] `tests/test_redact_check_unit.py` includes focused catalog regression tests for shipped behavior.
- [x] The redact regression tests pass locally.
- [x] `pytest tests/test_redact_check_unit.py` passes.
- [x] `ruff check .` passes or any pre-existing unrelated failures are identified.
- [x] No unrelated production behavior changes are made unless required by failing tests.

## Notes

- Parent task: `06-02-next-priority-devops-skills-tasks`.
- Validation:
  - `uv run pytest tests/test_redact_check_unit.py` passed: 23 tests.
  - `uv run ruff check tests/test_redact_check_unit.py` passed.
  - `uv run pytest tests/` passed: 1519 passed, 108 skipped.
  - `uv run ruff check .` has pre-existing unrelated failures outside this task's touched files.
  - `uv run mypy .` has a pre-existing duplicate-module failure from plugin-local `scripts` packages.
