# ship cicd-debug secret_scope_audit v1.0

## Goal

Wrap `gh secret list` (across all 4 scopes) and `glab variable list` (project + group) into a single read-only Python entrypoint that turns "我有哪些 secret 跟哪个 workflow 关联，谁可能漏" into a structured audit payload: per-secret scope/visibility + workflow reference graph + risk flags. Sibling of `pipeline_analyzer` v1.0 and `runner_check` v1.0; reuses `lib_cicd.py` provider abstraction, JSON envelope, RTK escape, and integration markers.

## Confirmed facts (from code / spec / CLI research)

### v0.2 stub surface

- `plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py:4-19` declares v1.0 surface:
  - per-secret: `{name, scope, referenced_in_workflows, risk_flags[], summary}`
  - `risk_flags` set: `[exposed-on-fork-PR, used-in-pull_request_target-with-PR-checkout (CRIT), protected-secret-referenced-from-unprotected-job, long-lived-cloud-credential (suggest OIDC), wildcard-org-scope-on-public-repo]`
- v1.0 ships **3 of 5 risk_flags** as production-ready; **2 are deferred** to v1.1 (D5 below explains).
- SKILL.md `Decision tree` lines 39-42 + `Quick reference` lines 67-69 preserved.

### gh CLI scopes (v2.90.0 confirmed)

- `gh secret list -R <o/r>` — repository Actions secrets (default `-a actions`).
- `gh secret list -R <o/r> -a codespaces` — Codespaces secrets at repo scope.
- `gh secret list -R <o/r> -a dependabot` — Dependabot secrets at repo scope.
- `gh secret list -R <o/r> -e <env-name>` — Environment-scoped secrets (requires `gh api repos/{o}/{r}/environments` first to enumerate environments).
- `gh secret list -o <org>` — Organization-level secrets; field `visibility ∈ {all, private, selected}` + `numSelectedRepos`.
- `gh secret list -u` — User-level Codespaces secrets (not in cicd-debug surface; out of scope).
- `--json name,visibility,updatedAt,numSelectedRepos,selectedReposURL` — only 5 JSON fields available; **secret VALUES are never returned by design** (security boundary).
- `gh api repos/{o}/{r}/environments` — returns environment list with `protection_rules` (for the deferred risk flag); paginated.
- `gh api repos/{o}/{r}/contents/.github/workflows` — directory listing; each entry has `download_url` for raw content fetch. Walk recursively if subdirectories (rare). v1.0 fetches each `*.yml` / `*.yaml` via `gh api repos/{o}/{r}/contents/.github/workflows/<file>` (returns base64-encoded `content`).

### glab CLI scopes (NOT installed on dev host; documented)

- `glab variable list --repo <g/p> --output json` — project variables; fields include `key, value, protected, masked, environment_scope, variable_type`.
- `glab variable list --group <path> --output json` — group variables (same fields).
- **GitLab does NOT separate "secrets" and "variables"** — all are CI/CD variables with `masked: true/false` flag. v1.0 reports `masked=true` entries as "secret-like"; `masked=false` are reported but not flagged for risk audit.
- **GitLab DOES expose variable values** by default unless masked (this is opposite to GitHub's gh-secret behavior). v1.0 NEVER reads `value` even when accessible; explicitly drops it from the JSON output to avoid accidental leakage into logs.
- Workflow file fetch: `glab api projects/:id/repository/files/.gitlab-ci.yml?ref=main` returns base64 content. GitLab uses a single `.gitlab-ci.yml` (plus `include:` chain — out of scope for v1.0).

### YAML parse dependency

- Current `pyproject.toml` runtime deps: `paramiko>=3.0,<4` only. **PyYAML is NOT installed.**
- v1.0 adds `pyyaml>=6.0` to runtime deps. Rationale: workflow YAML must be parsed structurally (regex would miss `${{ secrets.X }}` inside `with:` blocks, multi-line strings, and `env:` nested blocks); we already have a precedent for adding runtime deps (paramiko/ADR-003).
- Alternative considered: ship a vendored mini-parser. Rejected — PyYAML is the standard library for this in Python; adding it is one line in `pyproject.toml`. Sibling scripts (pipeline_analyzer, runner_check) don't pull it in, so the cost is isolated to secret_scope_audit's dependency footprint.

### Lib / project facts

- `lib_cicd.py` (per pipeline_analyzer D1) provides `GhAdapter` / `GlabAdapter` / `resolve_provider`. We extend it with `list_secrets(scope, kind) -> list[SecretData]`, `list_environments(scope) -> list[EnvData]`, `fetch_workflow_files(scope) -> list[(path, str)]`.
- Per-script `failure_class` namespace; exit codes **170-179 + 124 + 1**.
- Markers `cicd_gh` / `cicd_glab` registered by pipeline_analyzer; reused.

## Cross-script dependency on pipeline_analyzer PRD

Inherits from `05-26-cicd-debug-pipeline-analyzer/prd.md`:

- **D1 (Provider abstraction)** — same module `lib_cicd.py`.
- **D2 envelope shape** — same top-level structure; success/failure_class describe AUDITOR's ability, not the secret's safety.
- **D8 RTK escape** — same `GH_BIN` / `GLAB_BIN` / `--gh-bin` / `--glab-bin` contract.

Method additions to `lib_cicd.py` adapters: `list_secrets(scope, kind) -> list[SecretData]`, `list_environments(scope) -> list[EnvData]`, `fetch_workflow_files(scope) -> list[WorkflowFile]`. These land here (this child) since pipeline_analyzer doesn't need them.

## Design decisions (locked)

### D1. Scope flag semantics

```
secret_scope_audit.py <provider>
    [--repo <owner/name>]      # gh repo scope
    [--org <name>]             # gh org scope (no workflow ref-graph at org level — env empty)
    [--project <g/p>]          # glab project scope
    [--group <path>]           # glab group scope
```

- gh requires `--repo` OR `--org` (exactly one). When `--org`, the `referenced_in_workflows` field is `null` for all org-level secrets (we don't enumerate every repo in the org).
- glab requires `--project` OR `--group`. When `--group`, the `referenced_in_workflows` field is `null` for all group-level variables (we don't enumerate every project in the group).
- `--include-codespaces` flag adds Codespaces secrets to gh audit (default off; rationale: Codespaces is dev tool, not CI; including by default would inflate findings).
- `--include-dependabot` flag adds Dependabot secrets to gh audit (default ON; rationale: Dependabot is in the CI threat model).

### D2. JSON envelope `data` payload

| Key | Type | Meaning |
|---|---|---|
| `provider` | `"gh"` \| `"glab"` | Echoed |
| `scope` | obj | `{kind: "repo"\|"org"\|"project"\|"group", value: str}` |
| `secrets` | list[obj] | per-secret (see below) |
| `environments` | list[obj] | gh repo scope only; `[{name, has_protection_rules: bool, scoped_secret_count: int}]` |
| `workflow_files` | list[obj] | `[{path, parses_ok: bool, secret_refs: [name], event_triggers: [name], pull_request_target_with_unsafe_checkout: bool}]` |
| `summary` | str | `"ok" \| "warn" \| "crit"` per D4 |
| `findings` | list[obj] | see D5; cross-cuts secrets and workflow files |
| `failure_class` | str \| null | per D3 |
| `elapsed_s` | float | auditor wall-clock |

Per-secret object:
```
{
  "name": "AWS_DEPLOY_KEY",
  "scope_kind": "repo" | "env" | "org" | "dependabot" | "project" | "group",
  "scope_value": "owner/repo" | "env:production" | "owner-org" | "group/proj",
  "kind": "actions" | "codespaces" | "dependabot" | "ci_variable",
  "visibility": "all" | "private" | "selected" | null,    // gh org only
  "num_selected_repos": int | null,                       // gh org only
  "masked": bool | null,                                  // glab only
  "protected": bool | null,                               // glab only
  "environment_scope": str | null,                        // glab only
  "updated_at": str | null,
  "referenced_in_workflows": [
    {"path": ".github/workflows/test.yml", "jobs": ["build", "deploy"]}
  ] | null,
  "risk_flags": [str, ...]
}
```

### D3. `failure_class` taxonomy (secret_scope_audit-local)

Exit codes 170-179 + 124 + 1.

| `failure_class` | Trigger | exit_code |
|---|---|---|
| `null` | Audit completed (regardless of findings severity) | 0 |
| `tool_missing` | binary not on PATH, no env override | 170 |
| `provider_unavailable` | glab requested + missing | 171 |
| `auth` | 401 / not logged in | 172 |
| `not_found` | scope target missing | 173 |
| `permission` | 403 / "admin" / "must be a member" | 174 |
| `network` | DNS / 5xx / connect timeout | 175 |
| `bad_args` | scope flag mismatch | 176 |
| `api_unexpected` | JSON shape ≠ expected | 177 |
| `workflow_yaml_parse_failed` | PyYAML raises on >50% of workflow files; audit returns partial result + finding `workflow_parse_partial` | 178 |
| `workflow_dir_missing` | repo has no `.github/workflows` (gh) or no `.gitlab-ci.yml` (glab); secrets enumerated but `referenced_in_workflows` is empty for all; emits `findings[]` info but NOT a fatal failure | 179 |
| `timeout` | subprocess timeout | 124 |
| `internal` | catch-all | 1 |

Note: `workflow_dir_missing` (179) is "soft" — exit_code 0 returned anyway because the audit IS complete (no workflows = nothing to cross-ref); the failure_class is exposed for tooling that wants to distinguish. `summary` is computed from risk_flags presence, not from this class.

### D4. `summary` computation

- `"crit"` if ANY secret has `pull_request_target_unsafe_checkout` risk flag.
- `"warn"` if any secret has `exposed_on_fork_pr` OR `long_lived_cloud_credential` risk flag.
- `"warn"` if any secret has `unused` (defined but not referenced anywhere) — opt-in via D6 flag, default ON.
- `"ok"` otherwise.

### D5. `risk_flags` (per-secret) — what ships v1.0 vs deferred

**v1.0 (3 flags):**

- `long_lived_cloud_credential` — heuristic on secret name. Triggers if name matches `^(AWS|AZURE|GCP|GOOGLE|SP|SERVICE_PRINCIPAL|DOCKER)_(.*)$` AND name does NOT contain "OIDC"/"FEDERATED"/"WIF". Hint: suggest OIDC migration with link.
  - Severity: warn.
  - Cheap (no API), deterministic.

- `exposed_on_fork_pr` — workflow analysis. Triggers if any workflow file has `on: pull_request` (or `on: pull_request: types: [...]`) AND references this secret via `${{ secrets.X }}`. GitHub's documented behavior: secrets are NOT populated for PRs from forks → workflow will fail confusingly. Hint: explain fork-PR secret model.
  - Severity: warn.
  - Requires PyYAML structural parse; not regex-derivable safely.

- `pull_request_target_unsafe_checkout` — workflow analysis. Triggers if a workflow file has `on: pull_request_target` AND uses `actions/checkout@*` with `with.ref` containing `github.event.pull_request.head.{sha,ref}` OR `github.head_ref`. This is the documented "Pwn Request" pattern (GitHub Security Lab, 2026 zombie workflows). Hint: links to securitylab.github.com guide.
  - Severity: **crit** (per stub docstring; also industry standard).
  - Requires PyYAML to walk job→steps→uses→with.

**v1.1 deferred (2 flags):**

- `protected_secret_referenced_from_unprotected_job` — needs `gh api repos/{o}/{r}/environments` enumeration AND mapping `jobs[].environment` → environment protection rules. The cross-walk is doable but heavy (extra API calls per env per workflow); we surface the raw data in `data.environments` and `data.workflow_files[].jobs[].environment` so v1.1 (or downstream tools) can derive it.
- `wildcard_org_scope_on_public_repo` — needs to know the repo's `private` field (extra `gh api repos/{o}/{r}` call) AND walk org-secret visibility. Defer because gh org scope already requires `admin:org` token — v1.1 can add the cross-call when org auth is confirmed.

`risk_flags` is a list of strings ∈ `{"long_lived_cloud_credential", "exposed_on_fork_pr", "pull_request_target_unsafe_checkout", "unused"}`. v1.1 will extend (callers should accept unknown values).

### D6. CLI surface (v1.0)

```
secret_scope_audit.py <provider>
    [--repo <owner/name> | --org <name>]            # gh scope
    [--project <g/p>   | --group <path>]            # glab scope
    [--include-codespaces]                          # gh only; default OFF
    [--no-include-dependabot]                       # gh only; default ON (so flag DISABLES it)
    [--no-workflow-scan]                            # skip workflow YAML fetch + parse; risk flags reduced to long_lived_cloud_credential only
    [--unused-flag]                                 # default ON; emit "unused" risk_flag for secrets with empty referenced_in_workflows
    [--no-unused-flag]                              # opt out
    [--workflow-fetch-cap N]                        # default 50 (cap on workflow YAML files fetched; emits finding workflow_scan_truncated if exceeded)
    [--gh-bin <path>] [--glab-bin <path>]
    [--timeout N]                                   # default 120 (workflow fetch is N+1 API calls)
    [--json]
```

- Stub `--repo / --org / --project` preserved + glab `--group` added (missing from stub).
- Operational knobs (`--workflow-fetch-cap`, `--no-workflow-scan`, `--unused-flag`) keep v1.0 honest about big repos.

### D7. Test strategy

- **Unit (`tests/test_secret_scope_audit_unit.py`)** — primary coverage. Mock `lib_cicd.GhAdapter.list_secrets`/`list_environments`/`fetch_workflow_files` + Glab equivalents. Fixture catalog:
  - canned workflow files demonstrating each risk_flag (one minimal yaml per flag);
  - argparse surface (every D6 flag);
  - `risk_flags` emission per D5 (3 v1.0 flags + `unused`);
  - `summary` per D4;
  - every `failure_class` in D3;
  - `pull_request_target_unsafe_checkout` detection robustness: multiple yaml syntax forms (inline `with:`, multi-line, `uses: actions/checkout@v4` vs `uses: actions/checkout@<sha>`);
  - PyYAML parse error gracefully degrades to `workflow_yaml_parse_failed` if >50% files unparseable;
  - `--no-workflow-scan` short-circuits (only `long_lived_cloud_credential` emitted).
  Target: ≥ 90% statement coverage on `secret_scope_audit.py`; library coverage attributed to pipeline_analyzer/runner_check tests.

- **Integration (`tests/test_secret_scope_audit_integration.py`, `cicd_gh` mark)**:
  - `--help` exits 0;
  - against `CICD_GH_REPO` (env, e.g. `cli/cli`) — public repo, no secrets accessible without auth scope; test asserts envelope shape is correct (`data.secrets` may be empty for the calling user); workflow scan succeeds (cli/cli has many `.github/workflows/*.yml`); `summary` should be `ok` or `warn` depending on what risk flags are detected in the upstream workflows.
  - `--no-workflow-scan` smoke;
  - All skip if env unset or `gh auth status` non-zero.

- **glab integration (`cicd_glab` mark)** — `--help` smoke only; full integration deferred until glab installed.

- **PyYAML import test** — added to `tests/test_manifests.py` or a new `tests/test_runtime_deps.py` to confirm `pyyaml` is importable in default env (catches missing dep in CI).

### D8. RTK escape

Inherits from pipeline_analyzer D8; no changes.

## Requirements

1. `plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py` rewritten from stub to v1.0; surface per D6; data payload per D2; failure_class per D3; risk_flags per D5; summary per D4.
2. `lib_cicd.py` extended with `list_secrets(scope, kind) -> list[SecretData]`, `list_environments(scope) -> list[EnvData]`, `fetch_workflow_files(scope, cap) -> list[WorkflowFile]`.
3. `pyproject.toml`: add `pyyaml>=6.0` to runtime dependencies; document in the file header comment (paragraph after the existing paramiko comment).
4. JSON envelope via `lib.emit`; no `value`/raw-secret fields ever serialized.
5. ruff + mypy clean; mypy on PyYAML may need `types-PyYAML` in dev deps — add if mypy strict-mode complains.
6. Unit + integration tests per D7.
7. SKILL.md Decision tree lines 39-42 updated if any surface wording changes (it shouldn't); Quick reference updated to show v1.0 flags. Output contract example updated to reflect v1.0 `data` payload.
8. `references/secret_scopes.md` anchored for risk_flag hint URLs.
9. Source file header docstring `(v1.0)` tag, surface block matching D6, `See:` block.
10. `.trellis/spec/backend/error-handling.md` updated to reserve `170-179` for `secret_scope_audit.py`; this is required before shipping any new exit-code namespace.

## Acceptance Criteria

- [ ] `secret_scope_audit.py` is no longer a stub; loc target 400-650 (similar to pipeline_analyzer).
- [ ] `lib_cicd.list_secrets` / `list_environments` / `fetch_workflow_files` present on both adapters.
- [ ] `pyproject.toml` lists `pyyaml>=6.0`; comment block explains.
- [ ] `python secret_scope_audit.py --help` shows D6 surface and exits 0.
- [ ] `tests/test_secret_scope_audit_unit.py` exists; ≥ 90% coverage on `secret_scope_audit.py`; every `failure_class` (D3) and every `risk_flags` v1.0 value (D5) covered.
- [ ] `tests/test_secret_scope_audit_integration.py` exists with `cicd_gh` mark; help + public-repo workflow scan + `--no-workflow-scan` smoke; skips gracefully.
- [ ] `pytest tests/` default suite green; new `pyyaml` import test passes.
- [ ] `pytest tests/ -m cicd_gh -k secret_scope_audit -v` green when env set (manual).
- [ ] `ruff check plugins/cicd-debug` clean; `mypy plugins/cicd-debug/skills/cicd-debug/scripts/secret_scope_audit.py` clean (with `types-PyYAML` added to dev deps if needed).
- [ ] Exit codes 170-179 + 124 + 1 reserved; no collision with pipeline_analyzer (150-159) or runner_check (160-169).
- [ ] `.trellis/spec/backend/error-handling.md` includes the `secret_scope_audit.py` 170-179 namespace and paired `failure_class` strings.
- [ ] No raw secret values ever emitted in stdout/stderr/data — explicit assertion in unit tests (`"value" not in json.dumps(payload)`).
- [ ] SKILL.md Output contract example reflects v1.0 `data` payload; risk_flags v1.0 set documented.
- [ ] `references/secret_scopes.md` anchored for risk_flag hint URLs; v1.1-deferred flags noted with TODO.

## Out of scope

- `pipeline_analyzer.py` / `runner_check.py` — sibling children.
- Secret rotation / deletion / creation — write operations.
- Reading secret VALUES (gh prevents this; glab allows it but we don't).
- v1.1 risk flags (`protected_secret_referenced_from_unprotected_job`, `wildcard_org_scope_on_public_repo`) — explicitly deferred per D5.
- `.gitlab-ci.yml` `include:` chain walking — multi-file CI config out of scope for v1.0 (single file at default path only).
- Composite actions / reusable workflows secret flow analysis — too deep for v1.0.
- User-scope Codespaces secrets (`gh secret list -u`) — not CI; not in stub surface.
- Performance: repos with >50 workflow YAML files cap at `--workflow-fetch-cap` (emits `workflow_scan_truncated` finding).

## Open questions blocking planning

None. `design.md` and `implement.md` are drafted. Awaiting user review of locked decisions and planning artifacts before `task.py start`.

## Notes

- 2026-05-26 PRD drafted alongside pipeline_analyzer + runner_check sibling PRDs.
- 2026-05-29 planning resumed; added `design.md` and `implement.md`, and clarified that GitLab group scope mirrors GitHub org scope for workflow reference scanning.
- 2026-05-31 `trellis-before-dev` prep found the backend spec requirement that new exit-code ranges must be registered in `.trellis/spec/backend/error-handling.md`; PRD/implement checklist updated accordingly.
- 2026-05-31 planning recheck: `secret_scope_audit.py` is still the v0.2 stub with `UNIMPLEMENTED_NOTE`; `lib_cicd.py` exists but only has the pipeline-analyzer-era `list_secrets(scope) -> list[dict]` placeholders, so implementation must update the protocol/adapters while preserving existing `fetch_run` / `fetch_log` / `find_last_success` behavior.
- 2026-05-31 planning recheck: `pyproject.toml` still lacks `pyyaml`; global pytest markers still lack `cicd_gh` / `cicd_glab` even though `tests/test_pipeline_analyzer_integration.py` dynamically registers them locally. Add the markers globally for the new test module.
- 2026-05-31 user approved namespace correction: `ssh_tunnel` already owns `30-39`, so cicd-debug moves to `150-179` (`pipeline_analyzer` 150-159, `runner_check` 160-169, `secret_scope_audit` 170-179). Implementation must update existing `pipeline_analyzer.py` code/tests from the old `30-39` values before shipping any cicd-debug child.
- 2026-05-31 user reviewed the planning artifacts and approved moving to implementation. Current session remains in planning until the workflow is switched to Phase 2 / `task.py start`.
- v1.0 risk_flag set is **3 of 5** from stub; 2 deferred to v1.1 (D5). Stub surface preserved by emitting empty `risk_flags` for the 2 deferred flags rather than removing the schema.
- PyYAML enters project runtime deps for the first time; sibling scripts have no use for it (kept as dep of this script only).
- `pull_request_target_unsafe_checkout` references the 2025-2026 "Zombie Workflows" research (Sonar) and GitHub Security Lab pwn-request guide; hint URL surfaces both.
