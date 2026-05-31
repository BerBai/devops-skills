# Implementation - cicd-debug runner_check v1.0

> Paired PRD: `prd.md`.
> Technical design: `design.md`.

---

## Current evidence

- `runner_check.py` is still the v0.2 scaffold and contains
  `UNIMPLEMENTED_NOTE`.
- `lib_cicd.py` exists and is shared by `pipeline_analyzer.py` and
  `secret_scope_audit.py`; runner additions must preserve both siblings.
- `pyproject.toml` already registers `cicd_gh` and `cicd_glab`.
- `references/runner_debug.md` exists but needs stable anchors for emitted
  finding hints.

---

## Phase A - activate and load specs

- [x] Run `python3 ./.trellis/scripts/task.py start 05-26-cicd-debug-runner-check`.
- [x] Load `trellis-before-dev`.
- [x] Re-read backend error handling, logging, quality, and directory
      structure specs before code edits.
- [x] Baseline the shared cicd tests.

Validation:

```bash
uv run pytest tests/test_secret_scope_audit_unit.py -q
uv run pytest tests/test_pipeline_analyzer_unit.py -q
```

---

## Phase L - `lib_cicd.py` runner provider additions

- [x] Add `RunnerData` and `QueuedRun` dataclasses.
- [x] Update `ProviderAdapter` with
      `list_runners(scope, *, limit=5000)` and
      `list_queued_runs(scope, *, limit=200)`.
- [x] Implement `GhAdapter.list_runners` for repo/org/enterprise scopes.
- [x] Implement `GhAdapter.list_queued_runs` for repo scope; return empty for
      org/enterprise scopes.
- [x] Implement `GlabAdapter.list_runners` for project/group/instance scopes.
- [x] Implement `GlabAdapter.list_queued_runs` for project scope; return empty
      for group/instance scopes.
- [x] Keep existing secret-audit methods unchanged.

Validation:

```bash
python3 - <<'PY'
import sys
sys.path.insert(0, "plugins/cicd-debug/skills/cicd-debug/scripts")
from lib_cicd import RunnerData, QueuedRun
print(RunnerData("1", "r", "linux", "online", False, False, ()))
print(QueuedRun("1", "build", "2026-05-31T00:00:00Z"))
PY
```

---

## Phase R - rewrite `runner_check.py`

- [x] Replace scaffold with v1.0 implementation.
- [x] Header docstring includes `(v1.0)`, D6 CLI surface, and `See:` block.
- [x] Parser supports D6 flags: provider, all scope flags, caps, binary
      overrides, timeout, and `--json`.
- [x] Implement `Scope`, `validate_scope`, queue age parsing, aggregate
      computation, summary, findings, and human output.
- [x] Emit via local `lib.emit` / `json_result`; no direct `json.dump`.
- [x] Map every D3 failure class to the runner-check namespace.

Validation:

```bash
python3 plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py --help
python3 plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py gh --json ; test $? -ne 0
python3 plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py glab --json ; test $? -ne 0
```

---

## Phase T - unit and integration tests

- [x] Add `tests/test_runner_check_unit.py`.
- [x] Cover parser flags and scope validation.
- [x] Cover aggregation, p95/max queue ages, label coverage, summary, and every
      finding kind.
- [x] Cover every failure-class row through mocked provider exceptions.
- [x] Cover provider adapter argv construction for GitHub and GitLab runner and
      queue methods.
- [x] Add `tests/test_runner_check_integration.py`.
- [x] Integration tests must skip cleanly when `CICD_GH_REPO`, `gh auth`, or
      `glab` are unavailable.

Validation:

```bash
uv run pytest tests/test_runner_check_unit.py -q
uv run pytest tests/test_runner_check_unit.py --cov=runner_check --cov-report=term-missing -q
uv run pytest tests/test_runner_check_integration.py -q
```

---

## Phase D - docs and references

- [x] Update `SKILL.md` runner decision-tree wording for GitHub heartbeat
      deferral and GitLab heartbeat support.
- [x] Add concise runner payload example if the existing output contract becomes
      misleading.
- [x] Add anchors in `references/runner_debug.md` for:
      `all-offline`, `queue-starving`, `runner-offline-majority`,
      `queue-pressure`, `last-heartbeat-unavailable`,
      `running-job-unavailable-at-scope`, `pool-truncated`,
      `ephemeral-runner-offline`.
- [x] Ensure `.trellis/spec/backend/error-handling.md` reserves 160-168 for
      `runner_check.py`.

Validation:

```bash
rg -n "runner_check|160|pagination_overflow" .trellis/spec/backend/error-handling.md
rg -n "all-offline|queue-starving|last-heartbeat-unavailable" plugins/cicd-debug/skills/cicd-debug/references/runner_debug.md
```

---

## Phase Q - final quality gate

Run:

```bash
uv run ruff check plugins/cicd-debug tests/test_runner_check_unit.py tests/test_runner_check_integration.py
uv run mypy plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py
uv run pytest tests/test_runner_check_unit.py -q
uv run pytest tests/test_runner_check_integration.py -q
uv run pytest tests/test_secret_scope_audit_unit.py -q
uv run pytest tests/test_pipeline_analyzer_unit.py -q
uv run pytest tests/test_manifests.py -q
uv run pytest tests/ -q
```

Manual/opt-in:

```bash
uv run pytest tests/ -m cicd_gh -k runner_check -v
uv run pytest tests/ -m cicd_glab -k runner_check -v
```

Acceptable local limitation: live GH/GitLab credentials may be absent; those
tests must skip cleanly.

---

## Risky files and rollback points

- `plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py` is shared with
  two shipped sibling scripts; run their unit tests after edits.
- `plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py` is the main
  implementation target.
- `tests/test_runner_check_integration.py` must never make default test runs
  depend on real provider auth.
- `SKILL.md` already contains pipeline and secret-audit output examples; keep
  updates local to runner semantics.

---

## Ready-for-start checklist

- [x] `prd.md` exists.
- [x] `design.md` exists.
- [x] `implement.md` exists.
- [x] User asked to continue implementation by priority and approved Trellis
      task work this turn.
- [x] Run `python3 ./.trellis/scripts/task.py start 05-26-cicd-debug-runner-check`.

---

## Verification results

- [x] `uv run ruff check plugins/cicd-debug tests/test_runner_check_unit.py tests/test_runner_check_integration.py`
- [x] `uv run mypy plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py`
- [x] `uv run pytest tests/test_runner_check_unit.py --cov=runner_check --cov-report=term-missing -q` -> 98% statement coverage.
- [x] `uv run pytest tests/test_runner_check_integration.py -q` -> 1 pass, 3 skipped locally (`CICD_GH_REPO`, `CICD_GH_ORG`, `glab` unavailable).
- [x] `uv run pytest tests/test_secret_scope_audit_unit.py -q`
- [x] `uv run pytest tests/test_pipeline_analyzer_unit.py -q`
- [x] `uv run pytest tests/test_manifests.py -q`
- [x] `uv run pytest tests/ -q` -> pass; skips are local Docker/GitHub/GitLab/SSH environment gates.
