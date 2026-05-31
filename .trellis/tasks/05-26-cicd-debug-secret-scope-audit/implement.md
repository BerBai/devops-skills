# Implementation - cicd-debug secret_scope_audit v1.0

> Paired PRD: `prd.md`.
> Technical design: `design.md`.
> Do not run `task.py start` until the user reviews these planning artifacts.

---

## Pre-flight checks before `task.py start`

- [ ] `plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py`
      still contains `UNIMPLEMENTED_NOTE`.
- [ ] Check whether
      `plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py` exists.
      If absent, this task creates it with the shared provider contract from the
      pipeline_analyzer PRD.
- [ ] If `lib_cicd.py` exists with the current pipeline-analyzer-era
      `list_secrets(scope) -> list[dict]` placeholders, update the protocol and
      both adapters to the `SecretData` / `EnvData` / `WorkflowFile` contract
      while preserving existing `fetch_run`, `fetch_log`, `find_last_success`,
      and `list_workflow_files` callers.
- [ ] `python3 plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py --help`
      exits 0 or exposes the current stub help.
- [ ] `gh --version` exits 0 on the dev host, or document that only mocked unit
      coverage is available locally.
- [ ] `python3 -c "import yaml"` currently fails until `pyyaml` is added, or
      succeeds if the dependency is already present through the environment.
- [ ] Baseline: `pytest tests/test_manifests.py -q`.

If any pre-flight check reveals unrelated dirty user changes, preserve them and
work around them.

---

## Phase A - dependency and markers

- [ ] Update `pyproject.toml` runtime dependencies to include `pyyaml>=6.0`.
- [ ] Add a short dependency comment explaining that `secret_scope_audit.py`
      parses workflow YAML structurally.
- [ ] If mypy needs it, add `types-PyYAML` to `[project.optional-dependencies].dev`.
- [ ] Register `cicd_gh` and `cicd_glab` pytest markers if not already present.

Validation:

```bash
python3 -c "import yaml; print(yaml.__version__)"
pytest --collect-only -m cicd_gh tests/ -q
pytest --collect-only -m cicd_glab tests/ -q
```

---

## Phase L - `lib_cicd.py` provider additions

### L1. Shared types and errors

- [ ] Create or update
      `plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py`.
- [ ] Add `SecretData`, `EnvData`, and `WorkflowFile` dataclasses from
      `design.md`.
- [ ] Add provider exceptions if absent:
      `ProviderUnavailable`, `AuthError`, `NotFoundError`, `PermissionError`,
      `NetworkError`, `ApiUnexpectedError`, `CliTimeoutError`.
- [ ] Ensure existing pipeline_analyzer/runner_check contracts remain intact.

Validation:

```bash
python3 - <<'PY'
import sys
sys.path.insert(0, "plugins/cicd-debug/skills/cicd-debug/scripts")
from lib_cicd import SecretData, EnvData, WorkflowFile
print(SecretData("X", "repo", "o/r", "actions"))
print(EnvData("prod", True, 1))
print(WorkflowFile(".github/workflows/a.yml", "name: a", "gh_contents"))
PY
```

### L2. GitHub adapter

- [ ] Implement `GhAdapter.list_secrets(...)` for repo/org scopes.
- [ ] Implement environment enumeration and env-secret listing for repo scope.
- [ ] Implement `GhAdapter.list_environments(...)`.
- [ ] Implement `GhAdapter.fetch_workflow_files(scope, cap)`.
- [ ] Normalize all gh JSON into dataclasses and validate required fields.
- [ ] Map gh subprocess/HTTP failures into provider exceptions.

Unit coverage:

- [ ] repo actions/dependabot/codespaces argv construction.
- [ ] org argv construction.
- [ ] env list + env secrets argv/API construction.
- [ ] workflow directory missing.
- [ ] workflow fetch cap truncation support.
- [ ] malformed JSON -> `ApiUnexpectedError`.

### L3. GitLab adapter

- [ ] Implement `GlabAdapter.list_secrets(...)` for project/group scopes.
- [ ] Drop any `value` field before creating `SecretData`.
- [ ] Implement `GlabAdapter.fetch_workflow_files(...)` for project scope.
- [ ] Project workflow fetch must use project `default_branch` when available.
- [ ] Group scope returns no workflow files in v1.0.
- [ ] Implement `list_environments(...)` as empty for GitLab v1.0.

Unit coverage:

- [ ] project variable argv construction.
- [ ] group variable argv construction.
- [ ] value dropping assertion.
- [ ] default-branch `.gitlab-ci.yml` fetch.
- [ ] missing `.gitlab-ci.yml` -> soft workflow-dir-missing path.

---

## Phase S - script surface and failure envelope

- [ ] Rewrite `secret_scope_audit.py` from stub to v1.0.
- [ ] Header docstring includes `(v1.0)`, D6 CLI surface, and `See:` block.
- [ ] Build parser with all PRD D6 flags.
- [ ] Implement `validate_scope(args) -> Scope`.
- [ ] Implement failure-class mapping table for 170-179 + 124 + 1.
- [ ] Emit via `lib.emit` / `json_result`; no ad-hoc `json.dump`.
- [ ] Preserve `--json` behavior and human-readable summary.

Validation:

```bash
python3 plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py --help
python3 plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py gh --json ; test $? -ne 0
python3 plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py glab --json ; test $? -ne 0
```

---

## Phase W - workflow parser and reference graph

- [ ] Implement `_load_workflow_yaml(path, content)`.
- [ ] Handle PyYAML boolean `on` key by checking both `"on"` and `True`.
- [ ] Implement `_event_triggers(doc)`.
- [ ] Implement recursive secret/variable reference scanning.
- [ ] Implement GitHub job attribution under `jobs.<job_id>`.
- [ ] Implement GitLab job attribution with reserved top-level keys.
- [ ] Implement dynamic-secret-reference info finding.
- [ ] Implement parse-failure threshold logic.

Fixtures to add under `tests/fixtures/cicd/secret_scope_audit/`:

- [ ] `gh_pull_request_secret.yml`
- [ ] `gh_pull_request_target_unsafe_checkout.yml`
- [ ] `gh_multiline_env_secret.yml`
- [ ] `gh_dynamic_secret_reference.yml`
- [ ] `glab_project_variables.yml`
- [ ] `invalid_workflow.yml`

Validation:

```bash
pytest tests/test_secret_scope_audit_unit.py -k "workflow or reference" -q
```

---

## Phase R - risk flags and summary

- [ ] Implement `long_lived_cloud_credential` name heuristic.
- [ ] Implement `exposed_on_fork_pr`.
- [ ] Implement `pull_request_target_unsafe_checkout`.
- [ ] Implement `unused` with org/group exception.
- [ ] Implement findings with stable `kind`, `severity`, `hint`, and source file.
- [ ] Implement summary computation from PRD D4.

Unit matrix:

- [ ] every v1.0 `risk_flags` value.
- [ ] every summary state: `ok`, `warn`, `crit`.
- [ ] `--no-workflow-scan` emits only name-based flags.
- [ ] `--no-unused-flag` suppresses `unused`.
- [ ] org/group scope does not emit `unused`.

Validation:

```bash
pytest tests/test_secret_scope_audit_unit.py -k "risk or summary" -q
```

---

## Phase T - tests

### Unit tests

- [ ] Create `tests/test_secret_scope_audit_unit.py`.
- [ ] Mock adapters rather than requiring real `gh`/`glab`.
- [ ] Cover argparse surface for every D6 flag.
- [ ] Cover every failure class in PRD D3.
- [ ] Assert no serialized payload contains `"value"`.
- [ ] Target at least 90 percent statement coverage for
      `secret_scope_audit.py`.

Command:

```bash
pytest tests/test_secret_scope_audit_unit.py --cov=plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py --cov-report=term-missing -q
```

### Integration tests

- [ ] Create `tests/test_secret_scope_audit_integration.py`.
- [ ] Add `cicd_gh` help smoke.
- [ ] Add `cicd_gh` public repo workflow scan smoke gated by `CICD_GH_REPO`
      and `gh auth status`.
- [ ] Add `cicd_gh` `--no-workflow-scan` smoke.
- [ ] Add `cicd_glab` help smoke; skip full glab integration if `glab` absent.

Commands:

```bash
pytest tests/test_secret_scope_audit_integration.py -q
pytest tests/ -m cicd_gh -k secret_scope_audit -v
pytest tests/ -m cicd_glab -k secret_scope_audit -v
```

---

## Phase D - docs and references

- [ ] Update `.trellis/spec/backend/error-handling.md` to reserve
      `170`-`179` for `secret_scope_audit.py` and list each paired
      `failure_class` string from PRD D3.
- [ ] Update `plugins/cicd-debug/skills/cicd-debug/SKILL.md` quick reference
      if CLI wording changed.
- [ ] Update the output-contract example for the v1.0 `data` payload.
- [ ] Update `plugins/cicd-debug/skills/cicd-debug/references/secret_scopes.md`
      with anchors for:
      - fork PR secret model
      - pull_request_target unsafe checkout
      - OIDC migration for long-lived cloud credentials
      - deferred v1.1 flags
- [ ] Ensure hint strings used by code point to existing anchors.

Validation:

```bash
rg -n "170|secret_scope_audit|workflow_yaml_parse_failed" .trellis/spec/backend/error-handling.md
rg -n "fork|pull_request_target|OIDC|deferred|v1.1" plugins/cicd-debug/skills/cicd-debug/references/secret_scopes.md
pytest tests/test_manifests.py -q
```

---

## Phase Q - final quality gate

Run:

```bash
ruff check plugins/cicd-debug tests/test_secret_scope_audit_unit.py tests/test_secret_scope_audit_integration.py
mypy plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py
pytest tests/test_secret_scope_audit_unit.py -q
pytest tests/test_secret_scope_audit_integration.py -q
pytest tests/test_manifests.py -q
pytest tests/ -q
```

Manual/opt-in:

```bash
pytest tests/ -m cicd_gh -k secret_scope_audit -v
pytest tests/ -m cicd_glab -k secret_scope_audit -v
```

Acceptable local limitation: `glab` is not installed on the current dev host, so
full GitLab live integration may be skipped. Unit coverage must still cover
GlabAdapter behavior.

---

## Risky files and rollback points

Risky files:

- `plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py`
- `plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py`
- `pyproject.toml`
- `uv.lock`
- `.trellis/spec/backend/error-handling.md`
- `plugins/cicd-debug/skills/cicd-debug/SKILL.md`
- `plugins/cicd-debug/skills/cicd-debug/references/secret_scopes.md`

Rollback approach:

- Keep provider IO commits separate from parser/risk commits if committing.
- If PyYAML dependency update causes broad lockfile churn, inspect and keep only
  resolver-required changes.
- If shared `lib_cicd.py` changes break sibling tests, preserve the public
  methods and add compatibility wrappers rather than editing sibling scripts.

---

## Ready-for-start checklist

- [x] `prd.md` reviewed.
- [x] `design.md` reviewed.
- [x] `implement.md` reviewed.
- [x] User explicitly approves moving from planning to implementation.
- [ ] Then run `python3 ./.trellis/scripts/task.py start 05-26-cicd-debug-secret-scope-audit`
      or the platform-equivalent Trellis start command.
