# ship cicd-debug runner_check v1.0

## Goal

Wrap `gh api .../actions/runners` and `glab runner list --output json` into a single read-only Python entrypoint that answers "我的 runner 池子健康吗？" — per-runner state + pool aggregate + queue pressure. Sibling of `pipeline_analyzer` v1.0 (`05-26-cicd-debug-pipeline-analyzer`); reuses the same `lib_cicd.py` provider abstraction, JSON envelope, RTK escape contract, and integration markers.

## Confirmed facts (from code / spec / CLI research)

### v0.2 stub surface

- `plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py:4-19` declares v1.0 surface: per-runner `{name, status, busy, labels, last_heartbeat, running_job, machine_info}` + aggregate `{online_count, offline_count, busy_count, label_coverage, queue_depth, queue_p95_age_sec, summary}`.
- **Surface correction**: `last_heartbeat` is NOT exposed by the public GitHub Actions API (`/orgs/{o}/actions/runners` returns id/name/os/status/busy/ephemeral/labels only — no heartbeat timestamp). Verified via 2026-03-10 GitHub REST API docs. v1.0 marks `last_heartbeat` deferred (returned as `null`) with a `findings[]` info entry pointing at the API limitation; `machine_info` is reduced to `{os}` only (the one field actually exposed).
- SKILL.md `Decision tree` line 36-37 + `Quick reference` line 63-64 must be preserved (and the deferred-fields note added).

### GitHub REST API (verified)

- `GET /orgs/{org}/actions/runners` — needs `admin:org` token scope; returns `{total_count, runners: [{id, name, os, status, busy, ephemeral, labels: [{id, name, type}]}]}`. `labels[].type` ∈ `{"read-only", "custom"}`.
- `GET /repos/{owner}/{repo}/actions/runners` — needs `repo` token scope (or fine-grained "Administration: read"); same runner schema.
- `GET /enterprises/{enterprise}/actions/runners` — enterprise-cloud only; same schema.
- `GET /repos/{owner}/{repo}/actions/runs?status=queued&per_page=100` — paginated; each run has `created_at` for queue-dwell calculation.
- `GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs` — per-job `runner_name` reveals which runner is executing what; cross-join with runner list to populate `running_job`.

### GitLab CLI (NOT installed on dev host; documented behavior)

- `glab runner list --repo <g/p> --output json` — project-scoped runners; supports `--group` / `--instance` flags.
- GitLab Runner API exposes `description, ip_address, active, paused, is_shared, runner_type, name, online, status, contacted_at, tag_list, version, architecture, platform, projects`. **`contacted_at` IS exposed** (unlike GitHub) — v1.0 surfaces it as `last_heartbeat` when provider=glab; provider=gh leaves the field null.
- `glab api projects/:id/jobs?scope[]=pending` for queue depth — paginated; each job has `created_at` for dwell.

### Lib / project facts

- `lib_cicd.py` (introduced by `05-26-cicd-debug-pipeline-analyzer` D1) provides `ProviderAdapter` Protocol, `GhAdapter`/`GlabAdapter`, `resolve_provider`, `run_cli`. **runner_check.py must reuse it, not re-derive subprocess wrappers.**
- The Protocol needs methods `list_runners(scope) -> list[RunnerData]` and `list_queued_runs(scope) -> list[QueuedRun]`. These were sketched in pipeline_analyzer's D1 (as `list_runners` / `list_secrets` / etc.) and become concrete here.
- Per-script `failure_class` namespace pattern; exit codes **160-169 + 124 + 1** for runner_check (pipeline_analyzer reserved 150-159).
- Test markers `cicd_gh` / `cicd_glab` were registered by pipeline_analyzer; we reuse them.

## Cross-script dependency on pipeline_analyzer PRD

This PRD inherits from `05-26-cicd-debug-pipeline-analyzer/prd.md`:

- **D1 (Provider abstraction)** — `lib_cicd.py` is the single source of truth for `GhAdapter` / `GlabAdapter` / `resolve_provider` / `run_cli`. runner_check.py imports it.
- **D2 envelope shape** — `{success, exit_code, stdout, stderr, data}` structure, `data.failure_class`, `success / failure_class` describe ANALYZER's ability (NOT pool health).
- **D8 RTK escape** — `--gh-bin` / `--glab-bin` / `GH_BIN` / `GLAB_BIN`; binary resolution scans `/opt/homebrew/bin` etc. when which-resolved path contains "rtk".

The implementation-order dependency: `lib_cicd.list_runners` + `list_queued_runs` methods must land **as part of pipeline_analyzer's implement.md** (foundational module) OR be added by this child's implement.md if pipeline_analyzer hasn't merged yet. PRD locks the contract; sequencing handled in implement.

## Design decisions (locked)

### D1. Scope flag semantics

```
runner_check.py <provider>
    [--org <name>]      # gh org-level runners; requires admin:org token
    [--enterprise <slug>]  # gh enterprise-cloud runners
    [--repo <owner/name>]   # gh repo-level runners
    [--group <path>]    # glab group-scoped runners
    [--project <g/p>]   # glab project-scoped runners
    [--instance]        # glab instance-wide runners (sudo / admin token only)
```

- **Exactly one** scope flag must be set; argparse enforces via custom `--require-one-of` validator (no built-in argparse support; we'll implement a 5-line check in `main()`).
- gh + `--group/--project/--instance` → `failure_class=bad_args` (mismatch).
- glab + `--org/--enterprise/--repo` → `failure_class=bad_args` (mismatch). 

### D2. JSON envelope `data` payload

Inherits envelope shape from pipeline_analyzer D2.

| Key | Type | Meaning |
|---|---|---|
| `provider` | `"gh"` \| `"glab"` | Echoed |
| `scope` | obj | `{kind: "org"\|"repo"\|"enterprise"\|"group"\|"project"\|"instance", value: str}` |
| `runners` | list[obj] | per-runner: `{id, name, os, status: "online"\|"offline", busy: bool, ephemeral: bool, labels: [{name, type}], last_heartbeat: str\|null, running_job: obj\|null, machine_info: {os: str}}` |
| `aggregate` | obj | `{online_count, offline_count, busy_count, total_count, label_coverage: {<label> -> count}, queue_depth: int\|null, queue_p95_age_sec: float\|null, queue_max_age_sec: float\|null}` |
| `summary` | str | `"ok" \| "warn" \| "crit"` per D4 |
| `findings` | list[obj] | see D5 |
| `failure_class` | str \| null | per D3 |
| `elapsed_s` | float | analyzer wall-clock |

Note: `running_job` is populated only when scope is `repo` (gh) or `project` (glab) — cross-join with active jobs API requires that level; for org/enterprise/group/instance scope, `running_job` is `null` and a `findings[]` info entry explains.

### D3. `failure_class` taxonomy (runner_check-local)

Exit codes 160-169 + 124 + 1.

| `failure_class` | Trigger | exit_code |
|---|---|---|
| `null` | Pool listed (regardless of pool health) | 0 |
| `tool_missing` | binary not on PATH AND no env override | 160 |
| `provider_unavailable` | glab requested but missing | 161 |
| `auth` | 401 / "not logged in" | 162 |
| `not_found` | scope target doesn't exist (HTTP 404 / "no such org") | 163 |
| `permission` | 403 / "admin:org required" / "must have admin permissions" | 164 |
| `network` | DNS / timeout / 5xx | 165 |
| `bad_args` | wrong scope flag for provider, multiple scope flags, missing scope flag | 166 |
| `api_unexpected` | JSON shape ≠ expected | 167 |
| `pagination_overflow` | runner pool > 5000 (cap); analysis still returns first 5000 + finding `pool_truncated` | 168 |
| `timeout` | subprocess timeout | 124 |
| `internal` | catch-all | 1 |

### D4. `summary` computation

- `"crit"` if `aggregate.online_count == 0` AND total_count > 0 (all offline).
- `"crit"` if `aggregate.queue_depth >= 1` AND `aggregate.online_count == 0` (work queued, nothing online).
- `"warn"` if `aggregate.queue_depth >= 10` OR `aggregate.queue_p95_age_sec >= 600` (10-min wait p95).
- `"warn"` if `aggregate.offline_count / total_count >= 0.30` (>30% offline pool).
- `"warn"` if any label in `aggregate.label_coverage` has count == 0 BUT is referenced by recent queued jobs (cross-join required, only for repo/project scope; deferred to v1.1 if cross-join unavailable).
- `"ok"` otherwise.

### D5. `findings[]` kinds (v1.0)

- `all_offline` (crit) — every runner is offline; fields `{total_count}`.
- `queue_starving` (crit) — queue has jobs AND no online runner can take them; fields `{queue_depth, missing_labels}`.
- `runner_offline_majority` (warn) — >30% offline; field `{offline_pct}`.
- `queue_pressure` (warn) — queue_depth ≥ 10 OR queue_p95_age_sec ≥ 600; fields `{queue_depth, p95_sec}`.
- `last_heartbeat_unavailable` (info) — gh provider; field `{api_limitation_url}`.
- `running_job_unavailable_at_scope` (info) — scope not repo/project; field `{scope_kind}`.
- `pool_truncated` (warn) — `failure_class=pagination_overflow`; field `{returned: 5000, hint}`.
- `ephemeral_runner_offline` (info) — ephemeral runners are expected to come and go; field `{runner_name}` (not crit even when status=offline).

### D6. CLI surface (v1.0)

```
runner_check.py <provider>
    [--org <name> | --enterprise <slug> | --repo <owner/name>]    # gh scope choice
    [--group <path> | --project <g/p> | --instance]               # glab scope choice
    [--pool-cap N]            # default 5000 (D3 pagination_overflow trigger)
    [--queue-cap N]           # default 200 queued runs scanned for p95
    [--gh-bin <path>] [--glab-bin <path>]
    [--timeout N]             # default 60
    [--json]
```

- Stub docstring's `--org / --group / --repo / --project` preserved; `--enterprise` and `--instance` added because the underlying API supports them.
- `--no-running-job` flag NOT added (cross-join is cheap when scope allows; off-scope auto-degrades to null).
- `--include-machine-info` NOT added (only `os` is available; included by default).

### D7. Test strategy

- **Unit (`tests/test_runner_check_unit.py`)** — primary coverage. Mock `lib_cicd.GhAdapter.list_runners` / `list_queued_runs` and same for Glab:
  - argparse surface (every D6 flag) — including the scope-flag exclusivity check;
  - aggregate computation (label_coverage builder, p95 calc with various fixtures, queue_depth);
  - `summary` per D4 truth table (one fixture per row);
  - `findings` emission per D5 kind (one fixture per kind);
  - every `failure_class` row in D3 covered;
  - `--include-full-step-logs` absent (only pipeline_analyzer has that flag).
  Target: ≥ 90% statement coverage on `runner_check.py`; library coverage attributed to pipeline_analyzer tests.

- **Integration (`tests/test_runner_check_integration.py`, `cicd_gh` mark)**:
  - `--help` exits 0;
  - against `CICD_GH_REPO` (env) — `gh --repo $CICD_GH_REPO actions runners` may be empty (most public repos have no self-hosted runners); test asserts `data.runners == []`, `summary="ok"`, `total_count=0`. Skip if env unset or `gh auth status` non-zero.
  - against `CICD_GH_ORG` (env, opt-in second env) — if set, exercises org-scope; otherwise marked xfail-on-unset, never crit.
  - No queue_depth integration test in v1.0 (depends on a known queued state — too flaky for CI). Unit fixtures cover the math.

- **glab integration (`cicd_glab` mark)** — `--help` smoke; skipped on dev host until glab installed.

### D8. RTK escape

Inherits from pipeline_analyzer D8; no changes.

## Requirements

1. `plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py` rewritten from stub to v1.0; surface per D6; data payload per D2; failure_class per D3; summary per D4; findings per D5.
2. `lib_cicd.py` extended (if not already by pipeline_analyzer's implement.md) with `list_runners(scope) -> list[RunnerData]` and `list_queued_runs(scope, limit) -> list[QueuedRun]` on both GhAdapter and GlabAdapter.
3. JSON envelope via `lib.emit`; no ad-hoc `json.dump`.
4. ruff + mypy clean.
5. Unit + integration tests per D7.
6. SKILL.md `Quick reference` line 63-64 updated if scope-flag spelling changes (it shouldn't — we kept stub spellings).
7. SKILL.md Decision tree line 36-37 line about `last_heartbeat` re-worded to note v1.0 `gh` deferred + `glab` supported.
8. `references/runner_debug.md` gains anchors for D5 hint URLs.
9. Source file header docstring `(v1.0)` tag + surface block matching D6 + `See:` block to prd.md / lib_cicd.py / references/runner_debug.md.

## Acceptance Criteria

- [ ] `runner_check.py` is no longer a stub; loc target 250-450.
- [ ] `python runner_check.py --help` shows D6 surface and exits 0.
- [ ] `tests/test_runner_check_unit.py` exists; ≥ 90% statement coverage on `runner_check.py`; every `failure_class` in D3 covered; every `findings` kind in D5 covered.
- [ ] `tests/test_runner_check_integration.py` exists with `cicd_gh` mark; `--help` smoke + `CICD_GH_REPO` empty-pool + (optional) `CICD_GH_ORG`; all skip gracefully when env unset.
- [ ] `pytest tests/` default suite green.
- [ ] `pytest tests/ -m cicd_gh -k runner_check -v` green when env set (manual).
- [ ] `ruff check plugins/cicd-debug` and `mypy plugins/cicd-debug/skills/cicd-debug/scripts/runner_check.py` clean.
- [ ] Exit codes 160-169 + 124 + 1 reserved and non-conflicting with pipeline_analyzer 150-159.
- [ ] SKILL.md updated re: `last_heartbeat` deferral + `running_job` scope limitation.
- [ ] `references/runner_debug.md` anchors for D5 kinds exist.
- [ ] `lib_cicd.list_runners` / `list_queued_runs` methods present on both adapters (added by this child or inherited from pipeline_analyzer's implement).

## Out of scope

- `pipeline_analyzer.py` / `secret_scope_audit.py` — sibling children.
- Runner registration / deregistration / label editing — write operations.
- Runner installation guidance (covered by `references/runner_debug.md` content, not script logic).
- GH `last_heartbeat` workaround (e.g., scraping the Actions UI) — out of scope; v1.0 honestly reports `null` for gh.
- Cross-scope rollup (e.g., "give me a view of all my orgs' runners") — v1.0 is one scope per invocation.
- Performance: pools > 5000 runners caps and warns (`pagination_overflow`); larger pools are v1.1.

## Open questions blocking planning

None. Awaiting user review of locked decisions, then `design.md` (adapter method signatures, aggregate computation, fixture catalog) + `implement.md` (sequence: extend lib_cicd → write runner_check → unit → integration → SKILL/references sync).

## Notes

- 2026-05-26 PRD drafted alongside pipeline_analyzer + secret_scope_audit sibling PRDs.
- Surface correction over stub: `last_heartbeat` was unconditional in stub; v1.0 splits provider-specific (gh=null, glab=populated). `machine_info` reduced to `{os}` to match available API.
