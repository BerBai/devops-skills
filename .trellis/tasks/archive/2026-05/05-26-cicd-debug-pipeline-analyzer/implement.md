# Implementation - cicd-debug pipeline_analyzer v1.0

> Paired PRD: `prd.md`.
> Technical design: `design.md`.

---

## Current evidence

- `pipeline_analyzer.py` is already rewritten locally from the v0.2 scaffold to
  a v1.0 implementation, but remains uncommitted.
- `lib_cicd.py` now exists because the shipped `secret_scope_audit` child
  required the shared provider abstraction. This task must preserve the
  secret-audit additions while hardening the pipeline-analyzer contract.
- `tests/test_pipeline_analyzer_unit.py` and
  `tests/test_pipeline_analyzer_integration.py` exist locally but are still
  untracked.
- `tests/test_pipeline_analyzer_integration.py` currently references `json`
  without importing it. The default suite skips most live cases when
  `CICD_GH_REPO` is unset, so unit/default green alone is not enough.
- `pipeline_analyzer.py` carries file-level ruff suppressions from the local
  draft. The implementation phase should either remove them by refactoring
  narrow functions or leave only justified suppressions.

---

## Phase A - activate and preserve shared contracts

- [ ] Run `task.py start 05-26-cicd-debug-pipeline-analyzer`.
- [ ] Re-read backend specs via `trellis-before-dev`.
- [ ] Confirm `secret_scope_audit.py` tests still pass after any shared
      `lib_cicd.py` changes.
- [ ] Preserve `SecretData`, `EnvData`, `WorkflowFile`,
      `list_secrets`, `list_environments`, and `fetch_workflow_files`.

Validation:

```bash
uv run pytest tests/test_secret_scope_audit_unit.py -q
```

---

## Phase B - fix integration and quality defects

- [ ] Add missing imports and `check=False` to pipeline integration tests.
- [ ] Confirm `cicd_gh` and `cicd_glab` markers are globally registered in
      `pyproject.toml`.
- [ ] Keep the local-lib import collision fix from the secret-audit work:
      load cicd-debug `scripts/lib` by file path rather than bare `from lib`.
- [ ] Re-check RTK escape behavior with `gh --version` and binary override
      tests.

Validation:

```bash
uv run pytest tests/test_pipeline_analyzer_integration.py -q
uv run pytest tests/ -m cicd_gh -k pipeline_analyzer -v
uv run pytest tests/ -m cicd_glab -k pipeline_analyzer -v
```

---

## Phase C - harden pipeline_analyzer behavior

- [ ] Verify CLI surface exactly matches PRD D6.
- [ ] Verify exit codes are `150`-`158`, `124`, and `1`.
- [ ] Verify `success/failure_class` describe analyzer success, not pipeline
      conclusion.
- [ ] Ensure `--log-size-cap-mb` is enforced or explicitly documented if the
      current implementation only exposes the flag.
- [ ] Ensure `--include-full-step-logs` is either implemented or downgraded
      before commit; do not leave a silent no-op without tests.
- [ ] Cover all D5 finding kinds or update PRD/design if a finding is deferred.

Validation:

```bash
uv run pytest tests/test_pipeline_analyzer_unit.py -q
uv run pytest tests/test_pipeline_analyzer_unit.py --cov=pipeline_analyzer --cov-report=term-missing -q
```

---

## Phase D - docs and references

- [ ] Update `SKILL.md` output contract for the actual v1.0 payload.
- [ ] Ensure `references/pipeline_failures.md` has anchors for all emitted
      `findings[].kind` hints.
- [ ] Ensure `.trellis/spec/backend/error-handling.md` reserves
      `150`-`158` for `pipeline_analyzer.py`.

Validation:

```bash
rg -n "150|pipeline_analyzer|log_truncated" .trellis/spec/backend/error-handling.md
rg -n "failed-steps|action-version-drift|cache-miss|self-hosted-runner" plugins/cicd-debug/skills/cicd-debug/references/pipeline_failures.md
```

---

## Phase Q - final quality gate

Run:

```bash
uv run ruff check plugins/cicd-debug tests/test_pipeline_analyzer_unit.py tests/test_pipeline_analyzer_integration.py
uv run mypy plugins/cicd-debug/skills/cicd-debug/scripts/pipeline_analyzer.py plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py
uv run pytest tests/test_pipeline_analyzer_unit.py -q
uv run pytest tests/test_pipeline_analyzer_integration.py -q
uv run pytest tests/test_secret_scope_audit_unit.py -q
uv run pytest tests/test_manifests.py -q
uv run pytest tests/ -q
```

Manual/opt-in:

```bash
uv run pytest tests/ -m cicd_gh -k pipeline_analyzer -v
uv run pytest tests/ -m cicd_glab -k pipeline_analyzer -v
```

Acceptable local limitation: `CICD_GH_REPO` and `glab` may be absent locally;
the corresponding integration cases must skip cleanly.

---

## Risky files and rollback points

- `plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py` is now shared
  with `secret_scope_audit.py`; run secret audit unit tests after edits.
- `plugins/cicd-debug/skills/cicd-debug/scripts/pipeline_analyzer.py` is the
  main implementation target.
- `tests/test_pipeline_analyzer_integration.py` has live-test gates; avoid
  making default `pytest tests/` require real gh/glab auth.
- `SKILL.md` already contains secret-audit updates from the previous task; do
  not regress that output contract while updating pipeline docs.

---

## Ready-for-start checklist

- [x] `prd.md` exists.
- [x] `design.md` exists.
- [x] `implement.md` exists.
- [x] User approved continuing with the next priority task.
- [ ] Run `python3 ./.trellis/scripts/task.py start 05-26-cicd-debug-pipeline-analyzer`.
