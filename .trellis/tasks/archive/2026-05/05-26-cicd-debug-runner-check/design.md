# Design - cicd-debug runner_check v1.0

> Paired PRD: `prd.md`.
> Implementation checklist: `implement.md`.
> This task reuses the cicd-debug provider layer introduced by
> `pipeline_analyzer.py` and extended by `secret_scope_audit.py`.

---

## 1. Architecture overview

```
runner_check.py
├── argparse surface from PRD D6
├── validate_scope(args) -> Scope
├── resolve_provider(args.provider, gh_bin, glab_bin) -> GhAdapter | GlabAdapter
├── provider.list_runners(scope, limit=pool_cap)
├── provider.list_queued_runs(scope, limit=queue_cap)
├── normalize_runner(...) / normalize_queue(...)
├── compute_aggregate(...)
├── compute_summary(...)
├── build_findings(...)
└── lib.emit(json_result(...))

lib_cicd.py additions
├── dataclass RunnerData
├── dataclass QueuedRun
├── GhAdapter.list_runners/list_queued_runs
└── GlabAdapter.list_runners/list_queued_runs
```

Data flow:

```
argv -> Scope -> provider IO -> normalized runners + queue
                          -> aggregate + summary + findings
                          -> JSON envelope
```

The envelope `success` reports whether the runner pool was inspected. An
unhealthy pool still exits 0 when provider IO succeeded and exposes the health
state under `data.summary` and `data.findings`.

---

## 2. Module boundaries

### `runner_check.py`

Owns user-facing behavior:

- CLI parsing and provider/scope validation.
- Aggregation over normalized runner and queue data.
- Pool-health summary and finding construction.
- Human-readable output.
- Mapping library exceptions to the 160-169 + 124 + 1 namespace.

### `lib_cicd.py`

Owns provider IO only:

- subprocess wrapper and RTK binary escape remain shared.
- provider-specific argv/API construction.
- JSON shape validation at the provider boundary.
- dataclass normalization into `RunnerData` and `QueuedRun`.

`lib_cicd.py` must not compute pool health; it returns provider facts.

---

## 3. Shared dataclasses

Add to `lib_cicd.py`:

```python
@dataclass(frozen=True)
class RunnerData:
    id: str
    name: str
    os: str | None
    status: str
    busy: bool
    ephemeral: bool
    labels: tuple[dict[str, str | None], ...]
    last_heartbeat: str | None = None
    running_job: dict[str, Any] | None = None
    machine_info: dict[str, Any] | None = None


@dataclass(frozen=True)
class QueuedRun:
    id: str
    name: str | None
    created_at: str
    labels: tuple[str, ...] = ()
```

`RunnerData.labels` preserves GitHub label `type` and GitLab tag origin when
available. `running_job` is filled only for GitHub repo scope or GitLab project
scope when the provider can cheaply cross-reference active jobs.

Script-local dataclass:

```python
@dataclass(frozen=True)
class Scope:
    kind: str
    value: str
```

---

## 4. Provider command contracts

### GitHub runners

Scope routing:

| Scope | Command |
|---|---|
| `repo:owner/name` | `gh api repos/{owner}/{repo}/actions/runners --paginate` |
| `org:name` | `gh api orgs/{org}/actions/runners --paginate` |
| `enterprise:slug` | `gh api enterprises/{slug}/actions/runners --paginate` |

Expected shape is either a page object with `runners` or a bare list after
pagination. Each runner is normalized from:

- `id`, `name`, `os`, `status`, `busy`, `ephemeral`
- `labels[]` with `name` and optional `type`

GitHub does not expose runner heartbeat. `last_heartbeat` is always `None` and
`runner_check.py` emits `last_heartbeat_unavailable`.

Queue routing:

| Scope | Command |
|---|---|
| `repo:owner/name` | `gh api repos/{owner}/{repo}/actions/runs?status=queued&per_page=100 --paginate` |
| other GH scopes | return `[]` and let script emit a scope-limitation finding |

Queued workflow runs expose `id`, `name` / `display_title`, and `created_at`.
Labels are not exposed on the queued-run list in v1.0, so missing-label
calculation degrades to an empty set.

### GitLab runners

Scope routing:

| Scope | Command |
|---|---|
| `project:g/p` | `glab runner list --repo g/p --output json` |
| `group:path` | `glab runner list --group path --output json` |
| `instance:*` | `glab runner list --instance --output json` |

Normalize from common GitLab Runner fields:

- `id`, `description` or `name`, `platform`, `status`, `online`, `active`
- `tag_list`, `contacted_at`, `runner_type`, `version`, `architecture`

`status="online"` if `online is true` or `status == "online"`. `busy` is not
reliably exposed by `glab runner list` in v1.0, so it defaults false unless a
field named `busy` is present.

Queue routing:

| Scope | Command |
|---|---|
| `project:g/p` | `glab api projects/:encoded/jobs?scope[]=pending&per_page=100` |
| other GitLab scopes | return `[]` and let script emit a scope-limitation finding |

Queued jobs expose `id`, `name`, `created_at`, and optional `tag_list`.

---

## 5. Scope validation

Exactly one scope flag is required.

GitHub allows `--org`, `--repo`, and `--enterprise`.
GitLab allows `--group`, `--project`, and `--instance`.

Invalid combinations raise `BadArgsError` before provider resolution:

- no scope flag
- multiple scope flags
- provider/scope mismatch
- `--pool-cap < 1`, `--queue-cap < 0`, or `--timeout < 1`

---

## 6. Aggregation and findings

Aggregate fields:

- `total_count`, `online_count`, `offline_count`, `busy_count`
- `label_coverage`: runner label/tag name to count
- `queue_depth`
- `queue_p95_age_sec`, `queue_max_age_sec`

Queue ages are computed against an injectable `now` for unit tests. Use
ISO-8601 parsing with `Z` handled as UTC. If a queue timestamp is malformed,
treat it as provider-shape failure (`api_unexpected`) instead of silently
dropping it.

Summary:

- `crit`: non-empty pool with zero online runners.
- `crit`: queued work exists and zero online runners.
- `warn`: queue depth >= 10 or p95 queue age >= 600 seconds.
- `warn`: offline ratio >= 30 percent for a non-empty pool.
- `ok`: otherwise.

Finding emission:

- `all_offline`
- `queue_starving`
- `runner_offline_majority`
- `queue_pressure`
- `last_heartbeat_unavailable`
- `running_job_unavailable_at_scope`
- `pool_truncated`
- `ephemeral_runner_offline`

Each finding includes a stable `hint` into `references/runner_debug.md`.

---

## 7. Failure mapping

`runner_check.py` owns the exit-code table:

| Exception / condition | failure_class | exit |
|---|---|---|
| `ProviderUnavailable` and provider `gh` | `tool_missing` | 160 |
| `ProviderUnavailable` and provider `glab` | `provider_unavailable` | 161 |
| `AuthError` | `auth` | 162 |
| `NotFoundError` | `not_found` | 163 |
| `PermissionError` | `permission` | 164 |
| `NetworkError` | `network` | 165 |
| `BadArgsError` | `bad_args` | 166 |
| `ApiUnexpectedError` | `api_unexpected` | 167 |
| cap hit | `pagination_overflow` | 168 |
| `CliTimeoutError` | `timeout` | 124 |
| unexpected exception | `internal` | 1 |

For `pagination_overflow`, return the capped pool data with
`success=true`, `exit_code=168`, and a `pool_truncated` finding. This mirrors
the PRD: analysis partially succeeded but did not cover the full pool.

---

## 8. Compatibility notes

- Preserve existing `pipeline_analyzer.py` and `secret_scope_audit.py` imports
  from `lib_cicd.py`.
- Adding `RunnerData` / `QueuedRun` must not alter `SecretData`, `EnvData`, or
  `WorkflowFile` behavior.
- Default `pytest tests/` must not require real `gh` / `glab` auth.
- `cicd_gh` and `cicd_glab` marker behavior stays opt-in.

---

## 9. Documentation updates

- `SKILL.md` runner section should state that GitHub heartbeat is unavailable
  in v1.0, while GitLab `contacted_at` is surfaced.
- `SKILL.md` quick reference should include the richer scope choices only if
  that keeps the block concise.
- `references/runner_debug.md` needs anchors for every D5 finding kind used by
  script hints.
- `.trellis/spec/backend/error-handling.md` should reserve `160-168` if not
  already present in this workspace.

