# ship cicd-debug pipeline_analyzer v1.0

## Goal

Wrap the official `gh` and `glab` CLIs into a single read-only Python entrypoint that turns "一次 CI run 跑挂了" into a structured diagnostic payload: per-job state, failed-step log excerpts, drift vs. the previous green run, cache/runner/action-usage summary. This is the **anchor script** of the `cicd-debug` plugin — it's the most-used surface and it locks the cross-script contract (provider abstraction, JSON envelope, RTK avoidance, glab graceful degrade) that `runner_check` and `secret_scope_audit` will follow.

Sibling of the already-shipped `ssh-core` v1.0 quartet (`ssh_execute` 257 loc, `ssh_config_manager` 1055 loc, `deploy_pubkey` 282 loc, `ssh_server_transfer` 600 loc). Same conventions: per-script `failure_class` namespace, JSON envelope via `lib.emit`, ruff/mypy clean, unit + integration tests.

## Confirmed facts (from code / spec / CLI research)

### v0.2 stub surface (anchor for v1.0)

- `plugins/cicd-debug/skills/cicd-debug/scripts/pipeline_analyzer.py:4-22` declares the v1.0 surface and analysis fields (`job_states / failed_steps / diff_summary / cache_summary / actions_used / runner_used / summary`). v1.0 implements all 7.
- SKILL.md `Decision tree` (line 29) + `Quick reference` (line 56-58) already document the expected invocations — we must preserve.
- Output contract example in `SKILL.md:74-92`: `{success, exit_code, stdout, stderr, data:{provider, summary, findings, raw}}` — v1.0 keeps this top-level shape; `data` payload is expanded per D6 below.

### gh CLI (v2.90.0 confirmed on dev host)

- `gh run view <id> --json` exposes 18 fixed fields: `attempt, conclusion, createdAt, databaseId, displayTitle, event, headBranch, headSha, jobs, name, number, startedAt, status, updatedAt, url, workflowDatabaseId, workflowName`. `jobs` is nested with per-job `name / status / conclusion / startedAt / completedAt / steps[]`.
- `gh run view <id> --log-failed` streams text logs for failed steps only (cheap, server-side filtered).
- `gh run view <id> --log --job <job-id>` streams full job log on demand.
- `gh run list --workflow <name> --status success --limit 1 --json databaseId,headSha,createdAt` picks the most recent green run for `--diff-against last-success`.
- `gh api repos/{owner}/{repo}/actions/runs/{run_id}/jobs` returns full REST job objects including `runner_name`, `runner_id`, `labels`, per-step `started_at/completed_at`, `conclusion`. Needed because `gh run view --json` does not expose runner identity.
- `gh api repos/{owner}/{repo}/actions/runs/{run_id}/timing` returns billing/wall-clock breakdown.
- `gh api repos/{owner}/{repo}/actions/caches?ref=refs/heads/{branch}` lists caches; combined with job log scan ("Cache hit" / "Cache miss") tells us cache outcome per run.
- `gh secret list` (used by `secret_scope_audit`) returns metadata only (`name, visibility, updatedAt, numSelectedRepos`) — **secret values are never exposed by design**. Confirms read-only audit semantics for the whole plugin.

### glab CLI (NOT installed on dev host)

- `glab ci view <pipeline-id>` is the pipeline-detail entrypoint (alias `glab pipeline view`); supports `--branch` / `--repo` selectors.
- `glab ci trace <job-id>` returns full job log (no `--log-failed` equivalent — we fetch full log per failed job).
- `glab pipeline list --repo <g/p> --status success --output json` for last-success bisect.
- `glab api projects/:id/pipelines/<id>` + `glab api projects/:id/pipelines/<id>/jobs` for REST passthrough when CLI subcommand lacks a JSON option.
- Bundled CLI integration coverage on dev box is BLOCKED by absence — covered via mocked unit tests + integration tests guarded by `pytest.importorskip`-style runtime check (D7 below).

### Lib / project facts

- `lib/__init__.py` already exports `emit, json_result, filter_ssh_noise, classify_failure, msys_safe_env, alias_state_path`. `filter_ssh_noise` is ssh-specific (we won't reuse); `emit / json_result` are the envelope helpers; `msys_safe_env` is Windows-path-safe — relevant when fetching workflow YAML via gh api.
- Established per-script `failure_class` precedents: `ssh_config_manager` (6 classes, exit codes 4-8), `deploy_pubkey` (8 classes, exit codes 10-15), `ssh_server_transfer` (12 classes, exit codes 20-29 + 124), and `ssh_tunnel` (30-39). **cicd-debug picks exit codes 150-159 + 124 for pipeline_analyzer to leave 160-169 / 170-179 for runner_check / secret_scope_audit.**
- `lib/classify_failure` is ssh-subprocess specific (NETWORK_PATTERN matches "ssh: connect", "Connection refused", etc.) — not applicable to `gh`/`glab` subprocess failures. Per-script taxonomy is the project convention; we follow it.
- `tests/README.md` integration policy: live tests run only when env is opted-in (POC_HOST=pai for ssh-core). For cicd-debug, the equivalent gating env is `CICD_GH_REPO` (target read-only repo) and `CICD_GLAB_PROJECT`; absence → `pytest.skip`.

### RTK interference (dev-host-specific)

- `rtk` (Rust Token Killer) shims `gh run / gh api / gh pr / gh issue / gh secret / gh repo` at the shell-alias layer. Direct `gh run --help` from interactive shell hits the shim; absolute-path `/opt/homebrew/bin/gh run --help` bypasses it.
- subprocess.run with argv list does NOT invoke shell aliases — but if `gh` itself is replaced by an RTK wrapper binary in $PATH (vs alias), subprocess would still hit it. **Verification is deferred to implement.md** (per cross-task decision); PRD locks the contingency: a `GH_BIN` / `GLAB_BIN` env var lets the script override which binary is exec'd, default falls back to `shutil.which("gh")` / `shutil.which("glab")`.

## Design decisions (locked)

### D1. Provider abstraction (cross-script anchor)

- A single module `plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py` (new) defines:
  - `ProviderAdapter(Protocol)` with methods `fetch_run(run_id, project) -> RunData`, `fetch_log(run_id, job_id, only_failed) -> str`, `find_last_success(...) -> str | None`, `list_workflow_files(project) -> list[(path, bytes)]`, `list_runners(scope) -> list[RunnerData]`, `list_secrets(scope) -> list[SecretData]`. **Only the methods relevant to each consumer script are called** — adapters can `raise NotImplementedError` for unused methods (e.g. pipeline_analyzer never calls `list_runners`).
  - Concrete `GhAdapter` (subprocess `gh` / `gh api`) and `GlabAdapter` (subprocess `glab` / `glab api`).
  - `resolve_provider(name: str) -> ProviderAdapter` raises `ProviderUnavailable` (mapped to `failure_class="provider_unavailable"`) if the binary is missing.
  - `run_cli(argv, *, timeout, env_overrides) -> CompletedProcess` is the shared subprocess wrapper — handles `GH_BIN`/`GLAB_BIN` overrides, MSYS-safe env, RTK escape.
- `pipeline_analyzer.py` v1.0 must import from `lib_cicd`. `runner_check.py` / `secret_scope_audit.py` v1.0 (sibling children) must import the same module — this is the rationale for landing it here first.
- Scope: `lib_cicd.py` is **plugin-local**, NOT promoted to `lib/` at the ssh-core repo root. Reason: ssh-core's `lib/` is currently transport-focused (`sftp_transport`, `classify_failure`); promoting cicd helpers there confuses the layering. ADR-001 layering keeps each plugin's helpers under its own `scripts/` tree unless 2+ plugins demand reuse. (If a future log-aggregator wants the same provider adapters, then promote — not now.)

### D2. JSON envelope `data` payload

Top-level envelope is unchanged from SKILL.md:74-92. `data` payload for pipeline_analyzer:

| Key | Type | Meaning |
|---|---|---|
| `provider` | `"gh"` \| `"glab"` | Echoed input |
| `run_ref` | str | `<run_id>` for gh, `<project>:<pipeline_id>` for glab |
| `run_meta` | obj | `{workflow_name, branch, head_sha, event, status, conclusion, created_at, url}` |
| `job_states` | list[obj] | per-job: `{name, status, conclusion, runner_name, runner_labels, duration_sec, retry_count, steps[{name, conclusion, duration_sec, failed_log_excerpt?}]}` |
| `failed_steps` | list[obj] | derived from job_states where conclusion=failure; flattened for quick display; `log_excerpt` = last 20 lines |
| `actions_used` | list[obj] | `[{action: "actions/checkout", ref: "v4", used_in: ["test", "lint"]}]` — uniquified |
| `cache_summary` | obj | `{hits: int, misses: int, saves: int, events: [{job, key_prefix, outcome}]}` derived from log scan |
| `runner_used` | list[obj] | uniquified `{name, labels, runs_on}` across jobs |
| `diff_summary` | obj \| null | null if `--diff-against` not requested; else `{reference_run_ref, action_version_drift[], env_drift[], runner_label_drift[], cache_drift[]}` |
| `summary` | str | `"ok" \| "warn" \| "crit"` — computed via D4 |
| `findings` | list[obj] | `[{severity, kind, job, step?, hint, ...}]` — see D5 |
| `failure_class` | str \| null | null on success (run was analyzed; analysis success ≠ pipeline conclusion); set if analyzer itself failed (per D3) |
| `elapsed_s` | float | analyzer wall-clock |

**Distinction**: `success / failure_class` describe the **analyzer's** ability to run, NOT the pipeline's outcome. A pipeline that crit-failed still yields `success=true, failure_class=null, summary="crit"` because we successfully analyzed it. This mirrors `ssh_cluster`'s pattern (cluster reports per-host failures via data, not envelope).

### D3. `failure_class` taxonomy (pipeline_analyzer-local)

Per-script namespace per project convention. Exit codes 150-159 + 124 + 1.

| `failure_class` | Trigger | exit_code |
|---|---|---|
| `null` | Pipeline analyzed (regardless of pipeline outcome) | 0 |
| `tool_missing` | `gh` / `glab` not on PATH AND env override empty | 150 |
| `provider_unavailable` | `--provider glab` requested but `glab` missing AND no `GLAB_BIN` (same as `tool_missing` but emitted from D1 adapter resolution path; kept separate for SKILL.md hint clarity) | 151 |
| `auth` | `gh`/`glab` returned 401 / "authentication required" / "not logged in" | 152 |
| `not_found` | Run/pipeline id doesn't exist (HTTP 404 or "no run with ID") | 153 |
| `permission` | HTTP 403 / "must have repository read permissions" | 154 |
| `network` | DNS failure / connect timeout / 5xx with no body | 155 |
| `bad_args` | `--diff-against` value invalid, `--project` missing for glab, etc. (caught pre-subprocess via argparse + post-arg validation) | 156 |
| `api_unexpected` | Successful HTTP but JSON shape ≠ expected (e.g. `gh` returns a field we didn't predict; defensive) | 157 |
| `log_truncated` | `gh run view --log-failed` exceeded size cap (default 5 MB); analyzer continues but flags `findings[]` with severity=warn and emits this class only if the analyzer cannot derive `failed_steps` at all | 158 |
| `timeout` | Any subprocess `TimeoutExpired` | 124 |
| `internal` | Bug / unhandled exception (defensive catch-all in main()) | 1 |

### D4. `summary` computation

| Condition | `summary` |
|---|---|
| `run_meta.conclusion == "success"` | `"ok"` |
| `run_meta.conclusion == "failure"` AND failed_steps non-empty | `"crit"` |
| `run_meta.conclusion == "cancelled"` | `"warn"` |
| `run_meta.conclusion in {"timed_out", "startup_failure"}` | `"crit"` |
| `run_meta.status in {"queued", "in_progress"}` AND analyzer ran successfully | `"warn"` (analyzer doesn't block on running runs) |
| `diff_summary.action_version_drift` non-empty OR `cache_summary.misses > 0 AND prior run had hits` | `"warn"` (override `"ok"` upward; never overrides `"crit"`) |

### D5. `findings[]` kinds (v1.0)

Each finding is `{severity: "info"|"warn"|"crit", kind: str, job?: str, step?: str, hint: str, ...kind_specific_fields}`. v1.0 ships these `kind` values:

- `failed_step` (crit) — emitted per failed step; fields `{exit_code?, log_excerpt}`.
- `action_version_drift` (warn) — emitted by diff path; fields `{action, from_ref, to_ref}`.
- `cache_miss_regression` (warn) — when reference run had hits but current didn't; field `{cache_key_prefix}`.
- `runner_label_drift` (info) — when current run hit different labels than reference; fields `{from_labels, to_labels}`.
- `queued_too_long` (warn) — if `run_meta.status == "queued"` AND queue dwell > 5 min; field `{dwell_sec}`.
- `analyzer_partial` (warn) — emitted when `log_truncated` or a job's `gh api` call returned 403 (covered the rest of the analysis); field `{reason, missing_keys}`.

Hint URLs reference `references/pipeline_failures.md#<anchor>` (path lives in plugin tree, anchors must exist or be added in implement step).

### D6. CLI surface (v1.0)

```
pipeline_analyzer.py <provider> <run-id|pipeline-id>
    [--project <owner/repo or group/proj>]      # required for glab
    [--diff-against last-success | <other-id>]  # optional
    [--log-size-cap-mb N]                       # default 5
    [--include-full-step-logs]                  # default off; emits full step log under data.job_states[].steps[].full_log
    [--gh-bin <path>] [--glab-bin <path>]       # override binaries; also honored from GH_BIN / GLAB_BIN env
    [--timeout N]                               # default 120; per-subprocess
    [--json]
```

- All flags from stub docstring preserved (`<provider>` positional, `<run_id>` positional, `--project`, `--diff-against`, `--json`).
- New: `--log-size-cap-mb` (governs D3 `log_truncated`), `--include-full-step-logs` (opt-in detail), `--gh-bin`/`--glab-bin`/`--timeout` (operational knobs).
- `--ssh-config` NOT present (this plugin doesn't talk SSH).
- Per-flag remediation hints go in SKILL.md update during implement (no flag deferred for now).

### D7. Test strategy

- **Unit (`tests/test_pipeline_analyzer_unit.py`)** — primary coverage. Mock `lib_cicd.run_cli`; assert:
  - argparse surface (every D6 flag);
  - GhAdapter argv construction for `fetch_run`, `fetch_log(only_failed=True)`, `find_last_success`;
  - GlabAdapter argv construction (same methods);
  - `data.job_states` shape for a canned `gh run view --json` fixture;
  - `findings` emission for each kind in D5 (one fixture per kind);
  - every `failure_class` row in D3 triggered by matching mock return;
  - `summary` computed correctly per D4 truth table;
  - `--include-full-step-logs` toggles step `.full_log` presence;
  - `--gh-bin` / `GH_BIN` env override exercised.
  Target: ≥ 90% statement coverage on `pipeline_analyzer.py`; pipeline-facing
  `lib_cicd.py` methods covered here. Full-file `lib_cicd.py` coverage is now
  shared across cicd-debug children because `secret_scope_audit.py` added
  secret/workflow provider methods to the same module.

- **Integration (`tests/test_pipeline_analyzer_integration.py`, `cicd_gh` mark)** — opt-in, hits real `gh`:
  - `--help` exits 0 (smoke);
  - against `CICD_GH_REPO` (env, e.g. `cli/cli`) with a known successful run id → `summary="ok"`, `failed_steps == []`, `actions_used` non-empty;
  - against same repo with a known failing run id → `summary="crit"`, `failed_steps` non-empty;
  - `--diff-against last-success` returns `diff_summary != null`.
  - All test cases skip if `CICD_GH_REPO` unset OR `gh auth status` non-zero.

- **glab integration (`cicd_glab` mark)** — same shape as gh integration BUT each test wrapped in `if shutil.which("glab") is None: pytest.skip("glab not installed")`. Lab parity is the v1.0 target; absence on dev host is acceptable provided unit coverage of GlabAdapter is solid.

- **Smoke marker registration** in `pyproject.toml`: register `cicd_gh` and `cicd_glab` alongside existing `live_ssh`.

- Manifest test (`tests/test_manifests.py`) auto-discovers — we get `--help` exit 0 + frontmatter checks for free.

### D8. RTK escape contract

- `lib_cicd.run_cli` accepts an explicit `binary_path: Path` argument; the caller resolves the path via:
  1. arg `--gh-bin` / `--glab-bin` (highest precedence);
  2. env `GH_BIN` / `GLAB_BIN`;
  3. `shutil.which("gh")` / `shutil.which("glab")` followed by a substring check `"rtk" not in path` — if rtk-wrapped, scan `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin` for the real binary and prefer it.
- Defensive: subprocess does not inherit shell aliases (`subprocess.run(..., shell=False)` is enforced).
- Verification deferred to implement.md: run `subprocess.run(["gh", "--version"])` from a Python REPL on the dev host and confirm it doesn't go through RTK; if it does, the fallback scan kicks in.

## Requirements

1. New file `plugins/cicd-debug/skills/cicd-debug/scripts/lib_cicd.py` implements D1 (Protocol + 2 concrete adapters + shared subprocess wrapper).
2. `plugins/cicd-debug/skills/cicd-debug/scripts/pipeline_analyzer.py` rewritten from stub to v1.0; surface per D6; data payload per D2; failure_class per D3; summary per D4; findings per D5.
3. JSON envelope emitted via `lib.emit` (project-wide convention) — NOT bypassed by ad-hoc `json.dump`.
4. ruff + mypy clean (mirror `pyproject.toml` settings; `lib_cicd.py` typed with `Protocol`).
5. Unit + integration tests per D7. Markers registered in `pyproject.toml`.
6. `references/pipeline_failures.md` gains anchors referenced by D5 hints; if anchors don't exist, add minimal sections.
7. SKILL.md `Output contract` example updated to show v1.0 expanded `data` payload (current example is from v0.2 stub).
8. Source file header docstring follows established v1.0 pattern: `(v1.0)` tag, surface block matching D6, `See:` block pointing to `prd.md`, `lib_cicd.py`, `references/pipeline_failures.md`.

## Acceptance Criteria

- [ ] `pipeline_analyzer.py` is no longer a stub; no `UNIMPLEMENTED_NOTE`; loc target 400-650 (rule of thumb: between `ssh_execute` 257 and `ssh_config_manager` 1055; this script does more orchestration than ssh_execute but less than ssh_config_manager).
- [ ] `lib_cicd.py` ships with `GhAdapter` + `GlabAdapter` + `resolve_provider` + `run_cli`; loc 200-400.
- [ ] `python pipeline_analyzer.py --help` shows the v1.0 surface from D6 and exits 0.
- [ ] `tests/test_pipeline_analyzer_unit.py` exists; ≥ 90% statement coverage on `pipeline_analyzer.py`; pipeline-facing `lib_cicd.py` methods covered; every `failure_class` row in D3 covered; every `findings` kind in D5 covered.
- [ ] `tests/test_pipeline_analyzer_integration.py` exists with `cicd_gh` mark; 4 cases (help / success-run / failure-run / diff-last-success); all skip gracefully when env unset.
- [ ] `cicd_glab` mark registered; at least the help-smoke case exists under it.
- [ ] `pytest tests/` default suite green (no regressions on existing 670+ default tests).
- [ ] `pytest tests/ -m cicd_gh -v` green when `CICD_GH_REPO` set (manual verification step).
- [ ] `ruff check plugins/cicd-debug` and `mypy plugins/cicd-debug/skills/cicd-debug/scripts/{lib_cicd,pipeline_analyzer}.py` clean.
- [ ] Exit-code table (D3) verified non-conflicting against latest `error-handling.md` or per-script registry; ranges 150-159 + 124 + 1 reserved.
- [ ] SKILL.md `Output contract` block reflects v1.0 `data` payload.
- [ ] `references/pipeline_failures.md` has anchors for every `kind` in D5 hint URLs.
- [ ] RTK escape (D8) verified in implement.md against the dev host; result documented in PRD Notes.

## Out of scope

- `runner_check.py` / `secret_scope_audit.py` are SIBLING children (`05-26-cicd-debug-runner-check`, `05-26-cicd-debug-secret-scope-audit`); they reuse `lib_cicd.py` but ship under their own PRDs.
- Triggering reruns / cancels / approvals — write operations explicitly excluded by SKILL.md.
- `act`-style local workflow execution.
- Workflow YAML linting (`gh workflow lint` is a different surface; cicd-debug is run-level diagnosis).
- Bitbucket Pipelines / CircleCI / Jenkins providers — out of plugin scope per `05-14-devops-...` PRD.
- Real-time watch (`gh run watch`) — v1.0 is one-shot snapshot, matches SKILL.md "What this skill is *not*" §3.
- Performance/scale: runs with >100 jobs or >1 GB total logs are not a v1.0 target; `log_truncated` is the safety valve.

## Open questions blocking planning

None. Awaiting user review of locked decisions; if approved, proceed to `design.md` (module-level: lib_cicd Protocol shape, GhAdapter/GlabAdapter argv tables, diff algorithm) and `implement.md` (ordered checklist with RTK-escape verification step early).

## Notes

- 2026-05-26 PRD drafted from v0.2 stub surface + gh CLI live inspection + glab docs WebSearch + ssh-core child template (`05-24-ssh-core-deploy-pubkey/prd.md`).
- This PRD is the **anchor** for cicd-debug v1.0; sibling PRDs (`runner_check`, `secret_scope_audit`) link back to D1/D2/D8 instead of re-deriving the provider abstraction / envelope / RTK escape.
