# ship iac-state drift_check v1.0 — design

## Scope and boundaries

`drift_check.py` is the iac-state drift entrypoint. It stays read-only:
no `apply`, `destroy`, `import`, `state rm`, or HCL edits. It shells out through
the plugin-local Terraform helper and preserves the existing host model:

- `local` runs Terraform/OpenTofu in `project_dir`.
- any other host runs through `ssh-core/ssh_execute.py` via `lib/terraform.py`.

The script owns drift interpretation only. Tool discovery, local/remote command
dispatch, and shared 120-124 iac-state errors remain in `scripts/lib/terraform.py`.

## Data flow

1. Validate the local project has a directory with at least one `*.tf`.
2. Resolve `tofu` or `terraform` with `resolve_tool(host, --tool)`.
3. Run `init -input=false -lock=false -upgrade=false`.
4. Generate a unique plan path:
   - local: `tempfile.mkstemp(suffix=".tfplan")`, immediately closed
   - remote: `/tmp/drift-check-<uuid>.tfplan`
5. Run `plan -detailed-exitcode -input=false -lock-timeout=1s -out=<path>`
   plus `-target=<addr>` when `--target` is present.
6. If plan exits `0`, skip `show` and return no drift.
7. If plan exits `2`, run `show -json <path>`, parse `resource_changes[]`,
   classify actions, assign severity and hint, and summarize.
8. Unlink the temp plan file in a best-effort `finally` block.

`-lock-timeout=1s` is intentional. Drift detection should reveal a held state
lock instead of silently bypassing the lock with `-lock=false`. This mirrors the
lock probe style already used by `state_inspect.py`.

## Contracts

JSON output follows the repo-wide envelope:

```json
{"success": true, "exit_code": 0, "stdout": "", "stderr": "", "data": {...}}
```

`data` contains:

- `host`, `project_dir`, `target`, `tool`
- `summary`: `{drift, counts_by_action, severity_counts, total_changes}`
- `findings`: non-`no-op` resource changes sorted by severity, action, address
- `raw`: `init_stdout`, `init_stderr`, `plan_stdout`, `plan_stderr`,
  `plan_exit_code`, `plan_json`
- `warnings`: soft issues such as raw plan JSON truncation or cleanup failure
- `failure_class`: string for hard command/schema failures, otherwise `null`

Raw plan JSON is capped at 10 MB in the envelope. Parsing uses the full stdout
that Terraform returned; only the retained raw string is truncated.

## Drift classification

Terraform actions map to script actions:

| Terraform JSON actions | Script action |
|---|---|
| `["no-op"]` or empty | `no-op` |
| `["create"]` | `create` |
| `["update"]` | `update-in-place` |
| `["delete", "create"]` or `["create", "delete"]` | `replace` |
| `["delete"]` | `destroy` |
| anything else containing `delete` and `create` | `replace` |
| anything else containing `delete` | `destroy` |
| anything else containing `create` | `create` |
| anything else containing `update` | `update-in-place` |

Severity:

- `destroy` and `replace` are `crit`.
- `create` is `warn`.
- `update-in-place` is `warn` for IAM, security group, ACL, policy, route,
  firewall, key, secret, DB, bucket policy, public access, and network resources.
- `update-in-place` is `info` for tag, description, label, annotation, and
  metadata-only resource types or addresses.
- unknown `update-in-place` defaults to `warn`.
- `no-op` is omitted from findings.

Hints point to stable anchors in `references/drift_patterns.md`:
manual console edits, mixed IaC ownership, ignore_changes, provider-managed
fields, import mismatch, deleted cloud resources, and name conflicts.

## Exit behavior

This script distinguishes "the analyzer ran" from "the drift is safe":

- no changes, warn drift, or info drift: `success=true`, `exit_code=0`
- any crit drift finding: `success=false`, `exit_code=1`, `failure_class=null`
- hard command/schema failures: `success=false`, per failure-class exit code

This matches existing diagnostic scripts where a `crit` summary raises process
exit `1` while command-specific failures use namespaced codes.

## Failure classes

Shared `lib/terraform.py` classes can pass through unchanged:

- `120 host_unreachable`
- `121 tool_missing`
- `122 project_dir_invalid`
- `124 timeout`

`drift_check.py` owns:

- `130 init_failed`
- `131 plan_failed`
- `132 show_parse_failed`
- `133 target_not_found`
- `134 lock_held`
- `135 provider_auth_failed`
- `136 plan_too_large` (soft warning, normally exit 0)

`137`-`139` remain reserved.

Classification is stderr-based and conservative. Lock and provider auth markers
win over generic init/plan failure; target-not-found wins for `--target` plan
errors that mention an absent target resource.

## Compatibility

The parser accepts Terraform/OpenTofu JSON objects with or without
`resource_changes`. Missing or malformed `resource_changes` is treated as
`show_parse_failed` because the script cannot produce trustworthy findings.

The implementation must not import across plugins. Tests import the script with
the same sys.path isolation used by `state_inspect` to avoid local `lib`
module-cache collisions.

## Rollback

The risky files are:

- `plugins/iac-state/skills/iac-state/scripts/drift_check.py`
- `plugins/iac-state/skills/iac-state/scripts/lib/terraform.py`
- `tests/test_drift_check_unit.py`
- `tests/test_drift_check_integration.py`
- `.trellis/spec/backend/error-handling.md`
- `plugins/iac-state/skills/iac-state/SKILL.md`

Rollback is direct file revert for this child. The only shared helper change
should be a narrow temp-file cleanup function; if it breaks existing scripts,
remove that helper and inline cleanup in `drift_check.py`.
