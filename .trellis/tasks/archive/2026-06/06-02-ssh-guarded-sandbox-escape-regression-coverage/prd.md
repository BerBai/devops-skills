# Add ssh-guarded sandbox escape regression coverage

## Goal

Add focused local regression coverage for `ssh-guarded` workdir sandbox escape behavior so future changes cannot loosen documented path normalization, denylist, or executor refusal rules without failing tests.

This child task is lightweight and PRD-only: it expands deterministic pytest coverage around existing sandbox/request/executor contracts and does not introduce new architecture.

## Confirmed Facts

- `tests/README.md` lists broader `ssh-guarded` sandbox-escape attempts as missing v1.0 roadmap coverage.
- `plugins/ssh-guarded/skills/ssh-guarded/references/workdir-sandbox.md` documents these escape attempts:
  - `~/workspace/<id>/../../etc/passwd`
  - `~/workspace/<id>/./../etc/passwd`
  - `\\workspace\\<id>\\x` on Windows
  - `~/workspace/<id>/foo\x00bar`
  - `~/workspace/<id>/.env`
- Existing local unit tests already cover basic `..`, absolute path, other-project, workspace-root, denylist, drafter warning, and executor refusal behavior.
- Existing live pipeline tests cover denylist and sandbox refusal, but this task should not depend on live SSH infrastructure.

## Requirements

- Add local pytest coverage for documented sandbox escape attempts not already covered by unit tests.
- Prefer testing the shared `lib/sandbox.py` contract and one executor/request-artifact refusal path over duplicating every drafter script.
- Preserve existing behavior:
  - drafters may warn and write artifacts for outside-sandbox paths
  - executor/request validation refuses unsafe paths before transport
  - denylist hits keep the `path_in_denylist` failure class where applicable
- Keep tests deterministic and free of remote host, SSH, or external tool dependencies.
- Do not change production behavior unless tests reveal a real contract mismatch.

## Acceptance Criteria

- [x] `tests/test_ssh_guarded_lib_sandbox_unit.py` covers documented sandbox escape path shapes beyond the current smoke cases.
- [x] Executor/request validation coverage includes at least one normalized traversal escape and one denylist-in-workdir path.
- [x] `uv run pytest tests/test_ssh_guarded_lib_sandbox_unit.py tests/test_ssh_guarded_lib_request_artifact_unit.py tests/test_ssh_guarded_run_request_unit.py` passes.
- [x] `uv run ruff check` passes or any unrelated pre-existing failures are identified.
- [x] No live SSH setup is required.

## Out of Scope

- `scan_software.py` live tests.
- Runtime symlink detection inside the remote workdir; `workdir-sandbox.md` documents symlink escapes as not rejected at request time.
- New sandbox policy semantics beyond the documented v1.0 behavior.

## Notes

- Parent task: `06-02-next-priority-devops-skills-tasks`.
- Validation:
  - `uv run pytest tests/test_ssh_guarded_lib_sandbox_unit.py tests/test_ssh_guarded_lib_request_artifact_unit.py tests/test_ssh_guarded_run_request_unit.py` passed: 106 tests.
  - `uv run ruff check tests/test_ssh_guarded_lib_sandbox_unit.py tests/test_ssh_guarded_lib_request_artifact_unit.py` passed.
  - `uv run pytest tests/` passed: 1597 passed, 35 skipped.
  - `uv run ruff check .` has pre-existing unrelated failures in untracked Trellis/Codex scaffolding and historical archive spike files outside this task's touched files.
  - `uv run mypy .` has a pre-existing duplicate-module failure from plugin-local `scripts` packages.
  - Spec update check: no `.trellis/spec/` update needed because this task added tests for already documented sandbox behavior without changing CLI contracts, payload fields, exit codes, or implementation conventions.
