# Design - iac-state state_inspect v1.0

## Architecture

`state_inspect.py` is the iac-state foundation command. It ships the first
plugin-local Terraform/OpenTofu helper:

```python
resolve_tool(host: str, tool_arg: str) -> str
run_tf(host: str, project_dir: str, args: list[str], *, tool: str | None = None, timeout: int = 60, parse_json: bool = False) -> TfResult
```

The helper handles:

- tool autodetect: `tofu` first, then `terraform`, unless `--tool` forces one.
- local execution with `subprocess.run([...], cwd=project_dir)`.
- remote execution through `ssh-core/ssh_execute.py --json` with
  `cd <project-dir> && <tool> ...`.
- JSON parsing when requested.

No HCL parser is introduced in this task.

## CLI

Surface stays locked:

```bash
state_inspect.py <host> <project-dir> [--tool auto|tofu|terraform] [--json]
```

## Failure Classes

| Code | Class | Policy |
|------|-------|--------|
| `120` | `host_unreachable` | Hard failure for remote ssh transport/auth setup that prevents running Terraform/OpenTofu. |
| `121` | `tool_missing` | Hard failure when no selected Terraform/OpenTofu binary exists. |
| `122` | `project_dir_invalid` | Hard failure when project dir is absent, not a directory, or has no `*.tf` files. |
| `123` | `not_initialized` | Hard failure when `.terraform/terraform.tfstate` is absent. |
| `124` | `timeout` | Shared subprocess timeout convention. |
| `125` | `backend_unknown` | Soft finding when cached backend config cannot be parsed. |
| `126` | `lock_parse_failed` | Soft finding when lock is detected but stderr cannot be parsed. |
| `127` | `workspace_list_failed` | Soft finding when workspace list cannot be collected. |
| `128`-`129` | reserved | Future iac-state-local classes. |

## Collection

1. Validate project directory:
   - local: inspect filesystem directly.
   - remote: use shell test through `ssh_execute` via `run_tf`/helper probe.
2. Read cached backend metadata from `.terraform/terraform.tfstate`.
3. Count resources with `<tool> state list`.
4. Detect lock with:
   ```bash
   <tool> plan -lock-timeout=1s -refresh=false -input=false
   ```
   Never pass `-lock=false`.
5. List workspaces with `<tool> workspace list`.

## Backend and State Size

Backend data comes from cached `.terraform/terraform.tfstate`, not from `*.tf`
HCL source files.

- `backend.type` comes from `backend.type` when present.
- `backend.config` is redacted to safe fields only: `bucket`, `key`, `prefix`,
  `workspace`, `organization`, `hostname`, `path`, `container_name`, and
  `storage_account_name`.
- local backend state size uses the state path from backend config when
  available, otherwise `terraform.tfstate`.
- remote backends return `state_size_bytes=null` plus a warn finding; v1.0 does
  not call cloud provider APIs.

## Lock Parsing

Lock detection parses common Terraform/OpenTofu lock stderr fields:

- `ID:`
- `Path:`
- `Operation:`
- `Who:`
- `Version:`
- `Created:`
- `Info:`

If lock text is detected but fields cannot be parsed, return
`lock_parse_failed` as a soft finding and keep raw stderr in `data.raw`.

## Scoring

`data.summary` has one domain:

```json
{"state": "ok|warn|crit"}
```

- `crit`: state is locked.
- `warn`: backend unknown, remote backend state size unavailable, workspace
  listing failed, or lock parse failed.
- `ok`: metadata collected without findings.

`data.findings` is stable-sorted by severity, kind, name, reason.

## JSON Shape

Success data:

```json
{
  "host": "local",
  "project_dir": "/srv/infra",
  "tool": "terraform",
  "backend": {"type": "s3", "config": {"bucket": "...", "key": "..."}},
  "state_size_bytes": null,
  "resource_count": 10,
  "is_locked": false,
  "lock_info": null,
  "workspaces": [{"name": "default", "current": true}],
  "summary": {"state": "ok"},
  "findings": [],
  "raw": {"backend_state": "...", "state_list": "...", "lock_stderr": "...", "workspace_list": "..."},
  "failure_class": null
}
```

## Tests

Unit tests monkeypatch `resolve_tool`, `run_tf`, and filesystem paths. Live
tests are opt-in under `live_terraform` and skip unless Terraform/OpenTofu and
fixture provider access are available.
