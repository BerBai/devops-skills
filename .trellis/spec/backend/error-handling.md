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
| `80`–`87` | `diagnose_pod` and sibling k8s-debug script failure classes (namespace `80`–`89`; this child uses `80`–`87`). `80` `host_unreachable` (ssh-core remote unreachable, or local kubectl API server refused / DNS-missing), `81` `kubectl_missing` (binary absent or rc 127), `82` `kubeconfig_missing` (KUBECONFIG unset or file missing), `83` `pod_not_found`, `84` `namespace_not_found`, `85` `forbidden` (RBAC denial, or upstream ssh auth failure — same source in this context), `86` `api_timeout` (kubectl's own `--request-timeout=30s` trip; distinct from Python-side `124`), `87` `metrics_unavailable` (soft failure — exit `0` with a `warn` finding, used for `kubectl top` against a cluster without metrics-server). Introduced by `diagnose_pod` v1.0 (see `.trellis/tasks/05-26-k8s-debug-diagnose-pod/prd.md` R6 + `design.md` D4). ADR follow-up TBD. |
| `150`–`158` | `pipeline_analyzer` cicd-debug per-script failure classes. `150` `tool_missing`, `151` `provider_unavailable`, `152` `auth`, `153` `not_found`, `154` `permission`, `155` `network`, `156` `bad_args`, `157` `api_unexpected`, `158` `log_truncated`. Reserved by `.trellis/tasks/05-26-cicd-debug-pipeline-analyzer/prd.md` D3 after 2026-05-31 namespace correction; implementation/tests must migrate from the stale `30`-`38` draft before shipping. |
| `160`–`168` | `runner_check` cicd-debug per-script failure classes. `160` `tool_missing`, `161` `provider_unavailable`, `162` `auth`, `163` `not_found`, `164` `permission`, `165` `network`, `166` `bad_args`, `167` `api_unexpected`, `168` `pagination_overflow`. Reserved by `.trellis/tasks/05-26-cicd-debug-runner-check/prd.md` D3. |
| `170`–`179` | `secret_scope_audit` cicd-debug per-script failure classes. `170` `tool_missing`, `171` `provider_unavailable`, `172` `auth`, `173` `not_found`, `174` `permission`, `175` `network`, `176` `bad_args`, `177` `api_unexpected`, `178` `workflow_yaml_parse_failed`, `179` `workflow_dir_missing` (soft class; script exits `0` when the audit completed with no workflow graph). Reserved by `.trellis/tasks/05-26-cicd-debug-secret-scope-audit/prd.md` D3. |
| `124` | Python-side timeout enforcement triggered before the underlying command could complete. Matches GNU `timeout(1)` convention. Introduced by [ADR-001](./adr-001-ssh-execute.md) for `ssh_execute`; reusable by any script that needs distinguishable timeout semantics. |

The `2` case is intentional and verifiable — see the `UNIMPLEMENTED_NOTE`
constant in `plugins/k8s-debug/skills/k8s-debug/scripts/check_namespace.py`
line 26, and the payload it returns at lines 41-47. When a scaffold script
is fleshed out, keep `0`/`1`; replace `2` only when implementing the
behavior. Do not invent new exit codes beyond the ones listed above
(`0` / `1` / `2` / `4`–`9` / `10`–`15` / `20`–`29` / `30`–`39` / `40`–`79` / `80`–`87` / `150`–`179` / `124`) — new codes
require a spec update plus a referenced ADR (or task PRD) explaining the
semantics. Per-script ranges (`10`–`15`, `20`–`29`, `30`–`39`, `40`–`79`, `80`–`87`, `150`–`179`) are namespaced to
their script and not reusable by other scripts without a fresh range
allocation here.

Scripts that emit `4`–`9` MUST also set `data.failure_class` to the
matching string (`not_found` / `already_exists` / `config_missing` /
`parse_error` / `permission` / `transfer_verify_failed`) so callers can
branch on the class without re-parsing `stderr`. The same rule applies
to per-script ranges (`10`–`15`, `20`–`29`, `30`–`39`, `40`–`79`, `80`–`87`, `150`–`179`): every numeric exit code in
those ranges has a paired `data.failure_class` string defined in the
script's source; callers MUST branch on the class, not the number.

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
