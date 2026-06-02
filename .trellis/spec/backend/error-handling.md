# Error Handling

> Two pillars: a uniform JSON output contract, and `subprocess` safety.

---

## Pillar 1: JSON output contract

Every script under `plugins/<plugin>/skills/<plugin>/scripts/*.py` MUST
accept `--json` and, when given, emit exactly this shape on stdout:

```json
{"success": false, "exit_code": 2, "stdout": "", "stderr": "...", "data": {...}}
```

Source: CONTRIBUTING.md line 36 ("JSON output contract"). This is what
lets Claude Code chain calls without parsing prose. Reference
implementation: `plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py`
lines 47-58 (payload + JSON branch inside `main`).

Rules:
- `success` is `True` only when the operation completed as requested.
- `exit_code` must equal the process exit code (so callers can read either).
- `stdout` / `stderr` carry the raw text the underlying command emitted
  (or empty strings if there was no underlying command).
- `data` carries structured fields for the caller; never put error text
  here, put it in `stderr`.
- Without `--json`, write errors and diagnostics to `stderr` and a
  human-readable summary to `stdout`.

---

## Exit codes

The scaffold-era convention, used by every v0.2 stub:

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Generic failure (the operation ran and failed). |
| `2` | Unimplemented in this scaffold. |
| `4` | Lookup miss in a CRUD-shaped script (alias/resource not found). Introduced by [ADR-002](./adr-002-ssh-config-manager.md) for `ssh_config_manager` (`update` / `delete` / `find` on a missing alias). Reusable by any CRUD script with the same `not_found` semantics. |
| `5` | Already-exists conflict in a CRUD-shaped script. Introduced by [ADR-002](./adr-002-ssh-config-manager.md) for `ssh_config_manager create` on an existing alias. Reusable for analogous "create on a name already taken" cases. |
| `6` | Backing config / state file does not exist. Introduced by [ADR-002](./adr-002-ssh-config-manager.md) for `ssh_config_manager` when `~/.ssh/config` (or `--config-path` target) is missing. Reusable by any script whose primary input file is absent. |
| `7` | Backing config / state file could not be parsed. Introduced by [ADR-002](./adr-002-ssh-config-manager.md) for `ssh_config_manager` on malformed `Host` lines. Reusable for any "file present but unreadable as the expected format" case. |
| `8` | File permission error on read or write. Introduced by [ADR-002](./adr-002-ssh-config-manager.md) for `ssh_config_manager` when `~/.ssh` modes deny access. Reusable for any script that owns its own file I/O. |
| `9` | sha256 / content verification of a transferred payload failed. Introduced by [ADR-003](./adr-003-sftp-transport.md) for `ssh_upload` / `ssh_download` on the `--resume` path. Reusable for any transfer script that performs end-to-end content verification. |
| `10`–`15` | `deploy_pubkey` first-touch auth deploy failure classes. `10` `tool_missing` (ssh-copy-id absent), `11` `key_file_missing`, `12` `no_tty`, `13` `auth`, `14` `host_key_unknown`, `15` `network`. Per-script namespace; introduced by `plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py:78-87`. Spec backfill: 2026-05-25; ADR follow-up TBD. |
| `20`–`29` | `ssh_server_transfer` per-script failure classes. `20` `mode_deferred` (capability not yet shipped), `21` `flag_deferred` (flag requires deferred capability), `22` `network_src` / `23` `network_dst` (per-side connection failure), `24` `auth_src` / `25` `auth_dst` (per-side authentication failure), `26` `src_path_missing`, `27` `src_is_directory` (caller forgot `--recursive`), `28` `dst_path_invalid` (parent missing / unwritable), `29` `transfer_io` (mid-flight IOError). Introduced by `ssh_server_transfer` v1.0 (see `.trellis/tasks/05-24-ssh-core-ssh-server-transfer/prd.md` D5). ADR follow-up TBD. |
| `30`–`39` | `ssh_tunnel` per-script failure classes. `30` `posix_only` (Windows / no `os.setsid`), `31` `port_busy` (explicit `--local-port` already bound), `32` `port_pool_exhausted` (pool retries all failed), `33` `ssh_connect_failed` (DNS / unreachable / refused), `34` `auth` (key auth failed), `35` `forward_setup_failed` (remote-side `ExitOnForwardFailure` trip), `36` `tunnel_not_found` (`stop` / `status` saw no matching state), `37` `ambiguous_stop` (`stop` with multiple forwards and no `--port`), `38` `stale_state_only` (`stop` found state but pid dead), `39` `kill_failed` (`SIGTERM` + `SIGKILL` both refused). Introduced by `ssh_tunnel` v1.0 (see `.trellis/tasks/05-24-ssh-core-ssh-tunnel/prd.md` D7). ADR follow-up TBD. |
| `40`–`44` | `request_mkdir` ssh-guarded per-script failure classes. `40` `mode_invalid`, `41` `remote_path_outside_sandbox`, `42` `parents_required_for_nested`. Reserved by archived request-pipeline design §1 C5. |
| `45`–`49` | `request_delete` ssh-guarded per-script failure classes. `45` `remote_path_outside_sandbox`, `46` `recursive_required_for_directory`, `47` `path_in_denylist`, `48` `pre_check_stat_failed`. Reserved by archived request-pipeline design §1 C5. |
| `50`–`59` | `run_request` ssh-guarded per-script failure classes. `50` `workdir_outside_sandbox`, `51` `path_in_denylist`, `53` `artifact_invalid` / `artifact_expired`, `54` `lock_held`, `56` `sha256_mismatch`, `57` `transport_failed`, `59` `delete_confirm_required`. Reserved by archived request-pipeline design §1 C5. |
| `60`–`69` | `exec_detached` ssh-guarded per-script failure classes. `60` `request_not_executable`, `61` `nohup_dispatch_failed`, `62` `pid_capture_failed`, `63` `remote_log_unreachable`, `64` `job_vanished`, `65` `job_state_corrupt`. Reserved by `.trellis/tasks/05-25-ssh-guarded-exec-detached/prd.md` R8. |
| `70`–`74` | `redact_check` ssh-guarded per-script failure classes. `70` `catalog_load_failed`, `71` `catalog_schema_invalid`, `72` `regex_compile_failed`. Reserved by archived request-pipeline design §1 C5. |
| `75`–`79` | `scan_software` ssh-guarded per-script failure classes. `75` `catalog_load_failed`, `76` `cache_write_failed`, `77` `cache_corrupt`, `78` `ssh_batch_failed`, `79` `tool_probe_corrupted`. Reserved by archived scan_software task PRD D8. |
| `80`–`88` | k8s-debug script failure classes (namespace `80`–`89`). `80` `host_unreachable` (ssh-core remote unreachable, or local kubectl API server refused / DNS-missing), `81` `kubectl_missing` (binary absent or rc 127), `82` `kubeconfig_missing` (KUBECONFIG unset or file missing), `83` `pod_not_found`, `84` `namespace_not_found`, `85` `forbidden` (RBAC denial, or upstream ssh auth failure — same source in this context), `86` `api_timeout` (kubectl's own `--request-timeout=30s` trip; distinct from Python-side `124`), `87` `metrics_unavailable` (soft failure — exit `0` with a `warn` finding, used for `kubectl top` against a cluster without metrics-server), `88` `partial_collection` (check_namespace collected too few namespace domains to produce a reliable health snapshot). `80`–`87` introduced by `diagnose_pod` v1.0 (see `.trellis/tasks/05-26-k8s-debug-diagnose-pod/prd.md` R6 + `design.md` D4); `88` introduced by `check_namespace` v1.0 after avoiding the stale `90`–`99` planning conflict with log-aggregator `correlate`. ADR follow-up TBD. |
| `90`–`99` | `correlate` log-aggregator per-script failure classes. `90` `anchor_not_found`, `91` `window_invalid`, `92` `pair_unmatched`; `93`–`99` reserved. Introduced by `correlate` v1.0 after resolving the stale `50`–`59` planning conflict (see `.trellis/tasks/05-25-log-aggregator-correlate/prd.md` R8). |
| `100`–`109` | `cluster_health` k8s-debug per-script failure classes. `100` `cluster_unreachable` (mandatory `kubectl get nodes` probe reached kubectl but could not reach the Kubernetes API server); `101` `healthz_unavailable` (soft finding when managed-cluster control-plane pods and raw health endpoints are unavailable); `102` `versions_partial` (soft finding when `kubectl version --short` cannot be collected while nodes are available); `103`–`109` reserved. Introduced by `cluster_health` v1.0 (see `.trellis/tasks/05-26-k8s-debug-cluster-health/design.md`). |
| `110`–`119` | `helm_status` k8s-debug per-script failure classes. `110` `helm_missing`, `111` `release_not_found`, `112` `helm_version_unsupported`, `113` `history_truncated` (soft warning; exit `0`), `114` `pending_scan_partial` (reserved soft class for future namespace-partial scans), `115`–`119` reserved. Introduced by `helm_status` v1.0 (see `.trellis/tasks/05-26-k8s-debug-helm-status/design.md`). |
| `120`–`129` | `state_inspect` iac-state per-script failure classes. `120` `host_unreachable`, `121` `tool_missing`, `122` `project_dir_invalid`, `123` `not_initialized`, `125` `backend_unknown` (soft warning; exit `0`), `126` `lock_parse_failed` (soft warning; exit `0`), `127` `workspace_list_failed` (soft warning; exit `0`), `128`–`129` reserved. `124` remains the shared timeout convention. Introduced by `state_inspect` v1.0 (see `.trellis/tasks/05-26-iac-state-state-inspect/design.md`). |
| `130`–`139` | `drift_check` iac-state per-script failure classes. `130` `init_failed`, `131` `plan_failed`, `132` `show_parse_failed`, `133` `target_not_found`, `134` `lock_held`, `135` `provider_auth_failed`, `136` `plan_too_large` (soft warning; exit normally follows drift severity), `137`–`139` reserved. Introduced by `drift_check` v1.0 (see `.trellis/tasks/05-26-iac-state-drift-check/design.md`). |
| `150`–`158` | `pipeline_analyzer` cicd-debug per-script failure classes. `150` `tool_missing`, `151` `provider_unavailable`, `152` `auth`, `153` `not_found`, `154` `permission`, `155` `network`, `156` `bad_args`, `157` `api_unexpected`, `158` `log_truncated`. Reserved by `.trellis/tasks/05-26-cicd-debug-pipeline-analyzer/prd.md` D3 after 2026-05-31 namespace correction; implementation/tests must migrate from the stale `30`-`38` draft before shipping. |
| `160`–`168` | `runner_check` cicd-debug per-script failure classes. `160` `tool_missing`, `161` `provider_unavailable`, `162` `auth`, `163` `not_found`, `164` `permission`, `165` `network`, `166` `bad_args`, `167` `api_unexpected`, `168` `pagination_overflow`. Reserved by `.trellis/tasks/05-26-cicd-debug-runner-check/prd.md` D3. |
| `170`–`179` | `secret_scope_audit` cicd-debug per-script failure classes. `170` `tool_missing`, `171` `provider_unavailable`, `172` `auth`, `173` `not_found`, `174` `permission`, `175` `network`, `176` `bad_args`, `177` `api_unexpected`, `178` `workflow_yaml_parse_failed`, `179` `workflow_dir_missing` (soft class; script exits `0` when the audit completed with no workflow graph). Reserved by `.trellis/tasks/05-26-cicd-debug-secret-scope-audit/prd.md` D3. |
| `180`–`189` | `grep_across_sources` log-aggregator per-script failure classes. `180` `pattern_invalid`; `181`–`189` reserved. Introduced by `grep_across_sources` v1.0 after resolving the stale `60`–`69` planning conflict with `ssh-guarded exec_detached` (see `.trellis/tasks/05-25-log-aggregator-grep-across-sources/prd.md` R8). |
| `124` | Python-side timeout enforcement triggered before the underlying command could complete. Matches GNU `timeout(1)` convention. Introduced by [ADR-001](./adr-001-ssh-execute.md) for `ssh_execute`; reusable by any script that needs distinguishable timeout semantics. |

The `2` case is intentional and verifiable — see the `UNIMPLEMENTED_NOTE`
constant in `plugins/k8s-debug/skills/k8s-debug/scripts/check_namespace.py`
line 26, and the payload it returns at lines 41-47. When a scaffold script
is fleshed out, keep `0`/`1`; replace `2` only when implementing the
behavior. Do not invent new exit codes beyond the ones listed above
(`0` / `1` / `2` / `4`–`9` / `10`–`15` / `20`–`29` / `30`–`39` / `40`–`79` / `80`–`88` / `90`–`139` / `150`–`179` / `180`–`189` / `124`) — new codes
require a spec update plus a referenced ADR (or task PRD) explaining the
semantics. Per-script ranges (`10`–`15`, `20`–`29`, `30`–`39`, `40`–`79`, `80`–`88`, `90`–`139`, `150`–`179`, `180`–`189`) are namespaced to
their script and not reusable by other scripts without a fresh range
allocation here.

Scripts that emit `4`–`9` MUST also set `data.failure_class` to the
matching string (`not_found` / `already_exists` / `config_missing` /
`parse_error` / `permission` / `transfer_verify_failed`) so callers can
branch on the class without re-parsing `stderr`. The same rule applies
to per-script ranges (`10`–`15`, `20`–`29`, `30`–`39`, `40`–`79`, `80`–`88`, `90`–`139`, `150`–`179`, `180`–`189`): every numeric exit code in
those ranges has a paired `data.failure_class` string defined in the
script's source; callers MUST branch on the class, not the number.

## Scenario: iac-state `drift_check` v1.0

### 1. Scope / Trigger

- Trigger: `drift_check.py` is the iac-state drift entrypoint. It runs
  Terraform/OpenTofu `init`, `plan -detailed-exitcode`, and `show -json`, then
  converts `resource_changes[]` into drift findings.
- Scope: CLI signature, JSON `data` payload, drift severity exit behavior,
  temp plan cleanup, and drift-check-local failure classes `130`-`136`.

### 2. Signatures

```bash
drift_check.py <host> <project-dir> [--target ADDR] [--tool auto|tofu|terraform] [--json]
```

- `host` is `local` or an ssh-core alias accepted by
  `plugins/iac-state/skills/iac-state/scripts/lib/terraform.py`.
- `project-dir` must be a Terraform/OpenTofu project with at least one `*.tf`
  file for local runs.
- `--target` becomes `-target=<ADDR>` on the plan command.
- `--tool auto` resolves `tofu` first, then `terraform`.

### 3. Contracts

- Success envelope for no/warn/info drift: `success=true`, `exit_code=0`, and
  `data.failure_class=null`.
- Critical drift is a completed analysis but not an OK state:
  `success=false`, `exit_code=1`, and `data.failure_class=null`.
- `data.summary` contains `drift`, `total_changes`, `counts_by_action`, and
  `severity_counts`.
- `data.findings[]` contains `severity`, `kind="drift"`, `resource`,
  `resource_type`, `name`, `action`, `diff_summary`, and `hint`.
- `data.raw` contains init/plan stdout and stderr, `plan_exit_code`, and
  `plan_json` capped at 10 MB for output retention.
- `data.warnings[]` carries soft issues such as `plan_too_large` or temp-file
  cleanup failure; soft warnings do not set top-level `failure_class`.

### 4. Validation & Error Matrix

| Condition | `failure_class` | Exit |
|-----------|-----------------|------|
| Shared local/remote transport failure | `host_unreachable` | `120` |
| Terraform/OpenTofu missing | `tool_missing` | `121` |
| Local project path invalid or has no `*.tf` files | `project_dir_invalid` | `122` |
| Python-side timeout | `timeout` | `124` |
| `terraform init` fails without a more specific marker | `init_failed` | `130` |
| `terraform plan` exits non-0/non-2 without a more specific marker | `plan_failed` | `131` |
| `terraform show -json` fails, emits invalid JSON, or lacks `resource_changes[]` | `show_parse_failed` | `132` |
| `--target` points at no matching resource address | `target_not_found` | `133` |
| init/plan reports a held state lock | `lock_held` | `134` |
| provider credentials are missing, expired, or unauthorized | `provider_auth_failed` | `135` |
| retained raw plan JSON exceeds 10 MB | `plan_too_large` | `0` or `1` following drift severity |
| Unexpected script bug | `null` | `1` |

### 5. Good/Base/Bad Cases

- Good: plan exit `0` -> no findings, `summary.drift="ok"`, exit `0`.
- Base: plan exit `2` with create/update findings only -> sorted warn/info
  findings, exit `0`.
- Bad: plan exit `2` with destroy/replace findings -> `summary.drift="crit"`,
  exit `1`.
- Failure: plan stderr says state lock is held -> `failure_class="lock_held"`,
  exit `134`.

### 6. Tests Required

- Unit tests cover action classification, severity table, stable sorting,
  `--target` argv propagation, hard failure-class mapping, invalid show JSON,
  temp cleanup, JSON envelope shape, and human stderr behavior.
- Live Terraform tests are opt-in under `live_terraform` and must be skip-safe
  when neither `tofu` nor `terraform` is installed. They should cover a no-drift
  local project and a create-drift fixture when the built-in provider is
  available.
- Default `pytest tests/` must remain green with environment-dependent live
  cases skipped rather than failed.

### 7. Wrong vs Correct

#### Wrong

```json
{"success": true, "exit_code": 0, "data": {"summary": {"drift": "crit"}, "failure_class": null}}
```

Critical drift must raise process exit `1`, matching existing diagnostic
scripts where `crit` summaries are not OK states.

#### Correct

```json
{"success": false, "exit_code": 1, "data": {"summary": {"drift": "crit"}, "failure_class": null}}
```

`failure_class` stays `null` because Terraform ran successfully; the nonzero
exit is caused by the analyzed drift severity, not by a command failure.

## Scenario: k8s-debug `check_namespace` v1.0

### 1. Scope / Trigger

- Trigger: `check_namespace.py` is the namespace-level k8s-debug entrypoint.
  It probes a namespace, collects five health domains, scores each domain, and
  returns top findings without mutating the cluster.
- Scope: CLI signature, JSON `data` payload, partial collection behavior, and
  k8s-debug-local failure code `88`.

### 2. Signatures

```bash
check_namespace.py <host> <namespace> [--since 60] [--json]
```

- `host` is `local` or an ssh-core alias accepted by `lib.kubectl.run_kubectl`.
- `namespace` is passed to `kubectl -n <namespace>` after an existence probe
  with `kubectl get ns <namespace>`.
- `--since` is minutes for Event recency intent. Values below `1` normalize to
  `1`; values above `1440` are accepted but add a warning in `data.warnings`.

### 3. Contracts

- Success envelope: `success=true`, `exit_code=0`, and
  `data.failure_class=null`.
- `data.summary` must contain exactly the user-facing domains
  `pods`, `events`, `deployments`, `services`, and `pvcs`, each scored
  `ok | warn | crit`.
- `data.findings` contains the stable top 3 findings sorted by severity, domain
  order, kind, name, then reason. `data.all_findings_count` records the full
  untruncated finding count.
- `data.raw` keeps raw kubectl JSON stdout per collected raw domain, including
  `endpoints` even though endpoint scoring rolls up under `services`.
- 1-3 failed collection domains are a completed degraded snapshot:
  `success=true`, `exit_code=0`, failed user-facing domains score `warn`, and
  `data.partial_failures[]` records domain, `failure_class`, exit code, and
  stderr.

### 4. Validation & Error Matrix

| Condition | `failure_class` | Exit |
|-----------|-----------------|------|
| Host cannot run kubectl locally or remotely | `host_unreachable` | `80` |
| Missing kubectl binary | `kubectl_missing` | `81` |
| Missing kubeconfig / kube context access | `kubeconfig_missing` | `82` |
| Namespace does not exist | `namespace_not_found` | `84` |
| RBAC denial | `forbidden` | `85` |
| kubectl API timeout | `api_timeout` | `86` |
| 4+ collection domains fail after namespace probe succeeds | `partial_collection` | `88` |
| Unexpected script bug | `null` | `1` |

`80`-`86` are pass-through `KubectlError` classes from `lib.kubectl`;
`check_namespace.py` owns only `88`.

### 5. Good/Base/Bad Cases

- Good: healthy namespace with no warnings -> all summary domains `ok`, no
  findings, exit `0`.
- Base: one or more unhealthy objects but collection succeeds -> exit `0`,
  `success=true`, summary domains mark `warn` or `crit`, and findings explain
  the health issue.
- Bad: namespace probe succeeds but 4+ collection domains fail -> exit `88`,
  `success=false`, `data.failure_class="partial_collection"`.

### 6. Tests Required

- Unit tests cover each scorer's ok/warn/crit behavior, stable top-3 sorting,
  namespace-not-found pass-through, soft partial collection, hard `88`, and
  JSON/human envelope shape.
- Live k8s tests are opt-in under `live_k8s` and must be skip-safe when kind,
  kubectl, or Docker are unavailable. They should cover both a healthy
  namespace and a degraded fixture namespace when the local kind fixture can
  run.
- Default `pytest tests/` must remain green with environment-dependent live
  cases skipped rather than failed.

### 7. Wrong vs Correct

#### Wrong

```json
{"success": false, "exit_code": 91, "data": {"failure_class": "partial_collection"}}
```

This uses the stale `90`-`99` planning namespace, which is reserved for
log-aggregator `correlate`.

#### Correct

```json
{"success": false, "exit_code": 88, "data": {"failure_class": "partial_collection"}}
```

`88` stays inside the k8s-debug `80`-`89` namespace and is the only
`check_namespace.py`-local failure code.

## Scenario: k8s-debug `cluster_health` v1.0

### 1. Scope / Trigger

- Trigger: `cluster_health.py` is the cluster-level k8s-debug entrypoint. It
  probes nodes, control-plane visibility, kube-system pods/events, and version
  skew before a user drills into namespace or Pod-level symptoms.
- Scope: CLI signature, JSON `data` payload, managed control-plane fallback,
  and cluster-health-local failure classes `100`-`102`.

### 2. Signatures

```bash
cluster_health.py <host> [--json] [--since 30]
```

- `host` is `local` or an ssh-core alias accepted by `lib.kubectl.run_kubectl`.
- `--since` is minutes for kube-system Event lookback intent. Values below
  `1` normalize to `1`; values above `1440` are accepted but add a warning in
  `data.warnings`.

### 3. Contracts

- Success envelope: `success=true`, `exit_code=0`, and
  `data.failure_class=null`.
- `data.summary` contains `nodes`, `control_plane`, `kube_system`, `events`,
  and `versions`, each scored `ok | warn | crit`.
- `data.findings` contains the stable top 5 findings sorted by severity,
  domain order, kind, name, then reason. `data.all_findings_count` records the
  full untruncated finding count.
- `data.raw` keeps raw kubectl output per collected domain: `nodes`,
  `kube_system_pods`, `kube_system_events`, `healthz`, `readyz`, and `version`
  when available.
- Optional domain failures do not fail the command. They produce warn findings
  and `data.partial_failures[]`; only the mandatory nodes probe can produce a
  hard cluster-health-local failure.

### 4. Validation & Error Matrix

| Condition | `failure_class` | Exit |
|-----------|-----------------|------|
| SSH host cannot run kubectl or remote transport fails | `host_unreachable` | `80` |
| Missing kubectl binary | `kubectl_missing` | `81` |
| Missing kubeconfig / kube context access | `kubeconfig_missing` | `82` |
| RBAC denial | `forbidden` | `85` |
| kubectl API timeout | `api_timeout` | `86` |
| Mandatory `get nodes` reaches kubectl but cannot reach the API server | `cluster_unreachable` | `100` |
| Managed-cluster control-plane pods and raw health endpoints unavailable | `healthz_unavailable` | `0` soft finding |
| Version command unavailable while nodes are available | `versions_partial` | `0` soft finding |
| Unexpected script bug | `null` | `1` |

### 5. Good/Base/Bad Cases

- Good: healthy kind or production cluster -> all summary domains `ok`, no
  critical findings, exit `0`.
- Base: kube-system has a failed addon pod or node pressure -> exit `0`,
  `success=true`, relevant summary domain `warn` or `crit`, and top findings
  explain the unhealthy object.
- Bad: `kubectl get nodes` cannot reach the Kubernetes API server -> exit
  `100`, `success=false`, `data.failure_class="cluster_unreachable"`.

### 6. Tests Required

- Unit tests cover node Ready/pressure/cordon rules, self-hosted and managed
  control-plane paths, kube-system pod scoring, event scoring, version skew,
  `100` conversion, `80` pass-through, soft `101`/`102`, sorting, and envelope
  rendering.
- Live k8s tests are opt-in under `live_k8s` and must be skip-safe when kind,
  kubectl, or Docker are unavailable. They should cover a healthy kind cluster
  and a degraded kube-system pod when local kind can run.
- Default `pytest tests/` must remain green with environment-dependent live
  cases skipped rather than failed.

### 7. Wrong vs Correct

#### Wrong

```json
{"success": false, "exit_code": 80, "data": {"failure_class": "cluster_unreachable"}}
```

This mixes a cluster-health-local API-server failure class with the shared
`80 host_unreachable` transport/kubectl wrapper code.

#### Correct

```json
{"success": false, "exit_code": 100, "data": {"failure_class": "cluster_unreachable"}}
```

`100` distinguishes "kubectl was invoked but the Kubernetes API server is not
reachable" from SSH transport failures that remain `80 host_unreachable`.

## Scenario: k8s-debug `helm_status` v1.0

### 1. Scope / Trigger

- Trigger: `helm_status.py` is the Helm release diagnostic entrypoint for
  per-release health and cluster-wide pending release scans.
- Scope: CLI signature, JSON `data` payload, Helm 3 requirement, associated
  hook job scoring, and helm-status-local failure classes `110`-`114`.

### 2. Signatures

```bash
helm_status.py <host> <namespace> <release> [--json]
helm_status.py <host> --list-pending [--json]
```

- `host` is `local` or an ssh-core alias. Local mode runs `helm` directly;
  remote mode shells out through `ssh-core/ssh_execute.py --json`.
- `--list-pending` is mutually exclusive with `<namespace> <release>`.

### 3. Contracts

- Success envelope: `success=true`, `exit_code=0`, and
  `data.failure_class=null`.
- Per-release data contains `status`, `history`, `associated_jobs`,
  `stuck_resources`, `stuck_resources_deferred`, `summary`,
  `recommended_next_step`, `findings`, `partial_failures`, and `raw`.
- Pending scan data contains `pending_releases`, `summary`, `findings`, and
  `raw`.
- `associated_jobs.stuck` means an active hook job is older than five minutes
  and has not succeeded. `associated_jobs.failed` means `status.failed > 0` or
  Job condition `Failed=True`.
- v1.0 does not diff Helm manifests against live resources. It returns
  `stuck_resources=[]` and `stuck_resources_deferred=true`.

### 4. Validation & Error Matrix

| Condition | `failure_class` | Exit |
|-----------|-----------------|------|
| Missing local or remote `helm` binary | `helm_missing` | `110` |
| Requested release is absent | `release_not_found` | `111` |
| Helm major version is below 3 | `helm_version_unsupported` | `112` |
| Release history cannot be collected or parsed | `history_truncated` | `0` soft warning |
| Future namespace-partial pending scan | `pending_scan_partial` | `0` soft warning |
| Hook Job collection RBAC/API failure | kubectl pass-through class | `0` soft partial failure |
| SSH transport failure in remote Helm route | `host_unreachable` | `80` |
| Remote SSH auth failure in Helm route | `forbidden` | `85` |
| Helm subprocess timeout | `timeout` | `124` |
| Unexpected script bug | `null` | `1` |

### 5. Good/Base/Bad Cases

- Good: deployed release with no stuck jobs -> `summary="ok"`,
  `recommended_next_step="no action"`, exit `0`.
- Base: pending-upgrade release or stuck hook job -> `summary="crit"`, exit
  `0`, findings point to the Helm stuck pending section.
- Bad: requested release is absent -> exit `111`, `success=false`,
  `data.failure_class="release_not_found"`.

### 6. Tests Required

- Unit tests cover all eight Helm statuses, status/history/pending JSON schema
  variants, per-release mode, list-pending mode, hook job stuck/failed rules,
  110/111/112, soft 113, kubectl soft partial failure, sorting, and envelope
  rendering.
- Live k8s tests are opt-in under `live_k8s` and must be skip-safe when helm,
  kind, kubectl, Docker, or chart registry access are unavailable.
- Default `pytest tests/` must remain green with environment-dependent live
  cases skipped rather than failed.

### 7. Wrong vs Correct

#### Wrong

```json
{"success": false, "exit_code": 1, "data": {"failure_class": "helm_failed"}}
```

This loses the distinction between an absent release and an implementation or
provider failure.

#### Correct

```json
{"success": false, "exit_code": 111, "data": {"failure_class": "release_not_found"}}
```

Callers can branch on `release_not_found` without parsing Helm's human stderr.

## Scenario: iac-state `state_inspect` v1.0

### 1. Scope / Trigger

- Trigger: `state_inspect.py` is the iac-state foundation command and ships the
  shared Terraform/OpenTofu runner used by sibling iac-state scripts.
- Scope: CLI signature, JSON `data` payload, local/remote Terraform execution,
  state lock detection, and state-inspect-local failure classes `120`-`127`.

### 2. Signatures

```bash
state_inspect.py <host> <project-dir> [--tool auto|tofu|terraform] [--json]
```

Shared helper signatures:

```python
resolve_tool(host: str, tool_arg: str) -> str
run_tf(host: str, project_dir: str, args: list[str], *, tool: str | None = None, timeout: int = 60, parse_json: bool = False) -> TfResult
```

### 3. Contracts

- `--tool auto` tries `tofu`, then `terraform`; explicit `--tool` requires that
  selected binary.
- Local execution runs argv-list subprocesses with `cwd=<project-dir>`.
- Remote execution shells out through `ssh-core/ssh_execute.py --json` with
  `cd <project-dir> && <tool> ...`.
- Success data contains `backend`, `state_size_bytes`, `resource_count`,
  `is_locked`, `lock_info`, `workspaces`, `summary`, `findings`, `raw`, and
  `failure_class=null`.
- Lock detection must use `plan -lock-timeout=1s -refresh=false -input=false`
  and must not use `-lock=false`.

### 4. Validation & Error Matrix

| Condition | `failure_class` | Exit |
|-----------|-----------------|------|
| Remote host or ssh transport unavailable | `host_unreachable` | `120` |
| Neither Terraform nor OpenTofu binary is available | `tool_missing` | `121` |
| Project directory is absent, not a directory, or has no `*.tf` files | `project_dir_invalid` | `122` |
| Cached `.terraform/terraform.tfstate` is absent | `not_initialized` | `123` |
| Terraform/OpenTofu subprocess timeout | `timeout` | `124` |
| Cached backend metadata cannot be parsed | `backend_unknown` | `0` soft warning |
| State lock detected but fields cannot be parsed | `lock_parse_failed` | `0` soft warning |
| Workspace listing fails after core state metadata is collected | `workspace_list_failed` | `0` soft warning |
| Unexpected script bug | `null` | `1` |

### 5. Good/Base/Bad Cases

- Good: initialized local backend, no lock, workspace list succeeds ->
  `summary.state="ok"`, exit `0`.
- Base: remote backend or workspace listing failure -> exit `0`,
  `summary.state="warn"`, with findings explaining degraded metadata.
- Bad: uninitialized project -> exit `123`, `success=false`,
  `data.failure_class="not_initialized"`.

### 6. Tests Required

- Unit tests cover backend parsing, safe config redaction, local/remote backend
  state size behavior, resource count, workspace parsing, lock parsing,
  non-mutating lock command argv, hard 120-123 classes, and soft 125-127
  findings.
- Live Terraform tests are opt-in under `live_terraform` and must be skip-safe
  when Terraform/OpenTofu or fixture provider setup is unavailable.
- Default `pytest tests/` must remain green with environment-dependent live
  cases skipped rather than failed.

### 7. Wrong vs Correct

#### Wrong

```bash
terraform plan -lock=false -refresh=false
```

This bypasses lock acquisition and cannot detect the locked-state failure the
script is meant to surface.

#### Correct

```bash
terraform plan -lock-timeout=1s -refresh=false -input=false
```

This keeps the operation read-only while still exercising Terraform's normal
state lock acquisition path.

## Scenario: cicd-debug `pipeline_analyzer` v1.0

### 1. Scope / Trigger

- Trigger: `pipeline_analyzer.py` is the first shipped `cicd-debug`
  provider-backed command and defines the read-only GitHub/GitLab analysis
  contract that sibling scripts reuse.
- Scope: analyzer result semantics, CLI signature, JSON `data` payload, and
  provider failure mapping. This spec does not define mutating CI operations.

### 2. Signatures

```bash
pipeline_analyzer.py <provider> <run-id|pipeline-id> \
  [--project <owner/repo|group/project>] \
  [--diff-against last-success|<other-id>] \
  [--log-size-cap-mb N] \
  [--include-full-step-logs] \
  [--gh-bin <path>] [--glab-bin <path>] \
  [--timeout N] [--json]
```

- `provider` is exactly `gh` or `glab`.
- `--project` is required for `glab`; it is optional for `gh`.
- `--gh-bin` / `--glab-bin` override `GH_BIN` / `GLAB_BIN`, which override
  normal binary discovery.

### 3. Contracts

- Analyzer success is not pipeline success. If the CI run was fetched and
  analyzed, emit `success=true`, `exit_code=0`, and
  `data.failure_class=null` even when `data.summary="crit"`.
- `data` must contain `provider`, `run_ref`, `run_meta`, `job_states`,
  `failed_steps`, `actions_used`, `cache_summary`, `runner_used`,
  `diff_summary`, `summary`, `findings`, `failure_class`, and `elapsed_s`.
- `job_states[].steps[].full_log` is opt-in only when
  `--include-full-step-logs` is present.
- Oversized logs are capped by `--log-size-cap-mb` and must add an
  `analyzer_partial` finding when analysis continues with truncated input.
- `findings[].hint` values that point into
  `references/pipeline_failures.md#...` must use anchors that exist.

### 4. Validation & Error Matrix

| Condition | `failure_class` | Exit |
|-----------|-----------------|------|
| Missing `gh` / `glab` from direct binary discovery | `tool_missing` | `150` |
| Requested provider cannot be resolved through adapter setup | `provider_unavailable` | `151` |
| CLI reports unauthenticated / not logged in | `auth` | `152` |
| Run or pipeline id is absent | `not_found` | `153` |
| Provider denies repository/project read access | `permission` | `154` |
| DNS, connect timeout, or 5xx transport failure | `network` | `155` |
| Invalid arguments, including missing `--project` for `glab` | `bad_args` | `156` |
| Provider returns successful status with unexpected JSON shape | `api_unexpected` | `157` |
| Logs are too large to derive required failed-step data | `log_truncated` | `158` |
| Subprocess timeout | `timeout` | `124` |
| Unhandled analyzer bug | `internal` | `1` |

### 5. Good/Base/Bad Cases

- Good: completed successful run -> `success=true`, `summary="ok"`,
  `failed_steps=[]`, exit `0`.
- Base: completed failed run -> `success=true`, `summary="crit"`, at least
  one `failed_step` finding when failed step data is available, exit `0`.
- Bad: provider auth failure -> `success=false`,
  `data.failure_class="auth"`, exit `152`.

### 6. Tests Required

- Unit tests assert the CLI surface, every failure class above, summary
  computation, finding kinds, log-cap behavior, full-log opt-in, and
  `--diff-against` with both `last-success` and a concrete reference id.
- Integration tests are opt-in: `cicd_gh` cases skip unless `CICD_GH_REPO`
  and `gh` auth are available; `cicd_glab` cases skip unless `glab` is
  installed.
- Coverage target for `pipeline_analyzer.py` is at least 90% statements.

### 7. Wrong vs Correct

#### Wrong

```json
{"success": false, "exit_code": 1, "data": {"summary": "crit"}}
```

This treats a failed CI run as an analyzer failure and forces callers to
parse generic failure output.

#### Correct

```json
{"success": true, "exit_code": 0, "data": {"summary": "crit", "failure_class": null}}
```

The analyzer completed successfully; callers read `summary` / `findings` for
CI health and `failure_class` only for analyzer/provider failures.

---

## Scenario: log-aggregator `correlate` v1.0

### 1. Scope / Trigger

- Trigger: `correlate.py` is the first non-streaming log-aggregator command.
  It resolves multiple sources, fetches them to exhaustion, anchors on a regex
  match, returns a bounded time window, and emits one JSON envelope.
- Scope: CLI surface, JSON `data` payload, human section layout, and
  correlate-local failure classes. This spec does not define source parsing or
  fetcher transport; those remain in the log-aggregator local `lib/` modules.

### 2. Signatures

```bash
correlate.py --sources <spec>... --anchor <regex>
    [--window 30s] [--pair-mode] [--since 1h] [--json]
```

- `--sources` and `--anchor` are required.
- `--window` accepts positive `s/m/h/d` durations such as `30s`, `2m`, `1h`.
- `--pair-mode` treats the earliest and latest anchor matches as span start/end.
- `--json` emits a single envelope object, not NDJSON.

### 3. Contracts

- Success envelope:
  - `success=true`, `exit_code=0`, `data.failure_class=null`.
  - `data.anchor` is an object for single-anchor mode, and `{first, last}` for
    pair mode.
  - `data.window_lines[]` entries contain `source`, `time`, `level`, `body`,
    `is_anchor`, and `fields`.
  - `data.summary` contains `sources_with_hits`, `events_in_window`,
    `anchor_severity`, `likely_root_cause_hint`, and `partial_fetch`.
  - Pair mode also includes `data.summary.duration_ms`.
- Partial fetch is not a command failure when an anchor is still found:
  `success=true`, `exit_code=0`, `data.failed_fetchers[]` explains failed
  sources, and `data.summary.partial_fetch=true`.
- Human output uses exactly these section markers:
  `=== Anchor ===`, `=== Within window (<duration>) ===`, `=== Summary ===`.

### 4. Validation & Error Matrix

| Condition | `failure_class` | Exit |
|-----------|-----------------|------|
| No source line matches `--anchor` | `anchor_not_found` | `90` |
| Invalid or non-positive `--window` | `window_invalid` | `91` |
| `--pair-mode` sees only one match | `pair_unmatched` | `92` |
| Source spec parse failure | `source_spec_invalid` | `40` |
| Source expansion resolves no sources | `no_sources_resolved` | `44` |
| Subprocess timeout from underlying transport | `timeout` | `124` |

### 5. Good/Base/Bad Cases

- Good: two file sources, anchor found, window includes critical event ->
  `success=true`, sorted `window_lines`, non-null `likely_root_cause_hint`.
- Base: one source fetcher fails but another contains anchor -> `success=true`,
  `partial_fetch=true`, `failed_fetchers` populated.
- Bad: no anchor match in fetched lines -> `success=false`, `exit_code=90`,
  `data.failure_class="anchor_not_found"`.

### 6. Tests Required

- Unit tests cover window parsing, anchor search, pair mode, window filtering,
  hint computation, human layout, JSON envelope shape, and every correlate-local
  failure class.
- Integration tests cover a local file fixture with `--pair-mode` and a
  skip-safe live SSH journal query.
- Full default `pytest tests/` must remain green, including existing
  `tail_multi` tests.

### 7. Wrong vs Correct

#### Wrong

```json
{"success": false, "exit_code": 1, "data": {"summary": "no match"}}
```

This hides the actionable branch condition behind a generic failure.

#### Correct

```json
{"success": false, "exit_code": 90, "data": {"failure_class": "anchor_not_found"}}
```

Callers can branch on the class without parsing human text.

---

## Pillar 2: subprocess safety

Use `subprocess.run(argv_list, ...)` — **always a list**, never a shell
string, never `shell=True`. CONTRIBUTING.md line 37 spells this out:

> No exceptions, even for "trusted" input. Test commands include `;`,
> `&&`, and `` ` `` deliberately.

Why this rule exists: command arguments in this repo include user-supplied
hosts, aliases, paths, and shell-meaningful characters. A single
`shell=True` call ruins the whole safety story.

Other subprocess rules already in effect (`plugins/ssh-core/skills/ssh-core/SKILL.md`
"Safety baseline", lines 113-117):
- Stdout/stderr streamed with a 10 MB cap per stream; truncate with a marker.
- Credentials never appear in argv. Passwords go through stdin to
  `sshpass`-equivalent or paramiko.
- On Windows, file-transfer commands prefix `MSYS_NO_PATHCONV=1`.

---

## Hard checkpoints for irreversible operations

Irreversible side effects (delete, overwrite, restart, force-push) require
a written checkpoint sequence in `SKILL.md` and the script MUST refuse to
deviate. Source: CONTRIBUTING.md line 35 ("Hard checkpoints"). The
`ssh-guarded` plugin is the canonical implementation — it splits every
mutating action into a `request_*.py` (draft) and `run_request.py
--execute` (commit) pair, refusing to run the destructive step without an
explicit `--execute` flag (see roadmap entries in CONTRIBUTING.md lines
52-53).

When you add a destructive operation:
1. Write the checkpoint in the relevant `SKILL.md` "Hard checkpoints"
   section (canonical model: `plugins/ssh-guarded/skills/ssh-guarded/SKILL.md`
   "Hard checkpoints" lines 28-43, which spells out the 12-step
   discover → draft → consent → execute sequence).
2. Gate the destructive call behind an explicit flag, not a default.
3. Emit the JSON output contract whether the gate trips or the action runs.

---

## What NOT to do

- Do NOT raise exceptions out to the user — catch them and emit the JSON
  payload with `success=False` and a populated `stderr`.
- Do NOT use `os.system`, `os.popen`, or `subprocess.call(shell=True)`.
- Do NOT silently swallow non-zero exit codes from `subprocess.run`.
- Do NOT add a new exit code without documenting it here in a referenced
  ADR (see the `124`, `4`, `5`, `6`, `7`, `8` precedents above).
