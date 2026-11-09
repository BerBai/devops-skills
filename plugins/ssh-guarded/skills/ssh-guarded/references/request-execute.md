# request → execute lifecycle

The full mechanic behind the "two-phase write" you see in `SKILL.md`.

## Why this exists

Agent tools that execute writes directly conflate **intent** with **action**. The transcript shows "I ran X" but not "I considered Y first". When something breaks, the human reading the transcript can't reconstruct what the agent was trying to do, only what it ended up doing.

A request artifact splits the two. The agent declares intent (request), the human (or the agent on a second pass) reviews it, then execution happens against the artifact. Auditable, reproducible, replayable.

## Artifact location

Runtime state lives **outside** the skill directory (GAP-006). Writing it into
the installed plugin used to break on read-only installs and lost pending
approvals on upgrade.

```
$SSH_GUARDED_HOME/reports/<project_id>/      # default ~/.devops/ssh-guarded
├── requests/
│   ├── command-a1b2c3d4.json
│   ├── command-a1b2c3d4.result.json         # written by --execute
│   ├── command-a1b2c3d4.lock                # held during --execute
│   ├── upload-e5f6g7h8.json
│   └── delete-i9j0k1l2.json
└── jobs/
    └── <job_id>.json                        # only when exec_detached is used
```

- `$SSH_GUARDED_HOME` defaults to `~/.devops/ssh-guarded`
  (`%USERPROFILE%\.devops\ssh-guarded` on Windows).
- `--reports-dir PATH` on each `request_*.py`, plus `exec_detached.py` and
  `gc_artifacts.py`, replaces the `.../reports` level — use it in CI and
  containers. `run_request.py` needs no such flag: it writes `result.json`
  next to the artifact it was handed.
- `<project_id>` is `--project-id`, else `SSH_GUARDED_PROJECT_ID`, else the git
  repo basename. Two projects never see each other's artifacts.
- The plugin tree keeps only `reports/README.md`. If artifacts from the old
  in-tree layout are found, the first run prints a one-time note naming both
  paths; nothing is moved or deleted automatically.

Paths contain user intent and partial command output. Keep them out of shared
transcripts; do not commit them.

## Request shape (v1)

```json
{
  "version": 1,
  "request_id": "<operation>-<8 hex>",
  "operation": "command" | "upload" | "mkdir" | "delete",
  "server": "<alias>",
  "reason": "<one line user-supplied or agent-restated intent>",
  "created_at": "<ISO 8601 UTC>",
  "expires_at": "<ISO 8601 UTC, default created_at + 1h>",
  "payload": { ... operation-specific ... },
  "risk_summary": ["<short bullet>", "<short bullet>"],
  "project": {                    // optional, set when project.local.json present
    "project_id": "...",
    "workdir": "~/workspace/..."
  }
}
```

### Operation payloads

**command**:
```json
{ "command": "logrotate -f /etc/logrotate.d/nginx",
  "workdir": "~/workspace/log-rotation",
  "timeout": 60,
  "env": { "FOO": "bar" } }
```

**upload**:
```json
{ "local_path": "/abs/path/in/upload_roots",
  "remote_path": "~/workspace/<id>/release.tar.gz",
  "checksum": "sha256:...",
  "size_bytes": 12345678,
  "overwrite": false }
```

**mkdir**:
```json
{ "remote_path": "~/workspace/<id>/staging/2026-05-14",
  "mode": "0755" }
```

**delete**:
```json
{ "remote_path": "~/workspace/<id>/stale.log",
  "recursive": false,
  "is_directory": false }
```

## Risk summary

Generated from the payload. The script computes; the agent does not freestyle it. Examples:

| Operation | Auto-summary points |
|---|---|
| `command` matching `rm`/`mv`/`>/` | "command may delete or truncate files" |
| `command` matching `systemctl`/`service` | "command affects service state" |
| `command` matching `sudo` | "command runs with elevated privileges" |
| `upload` with overwrite=true | "remote file will be replaced" |
| `delete` recursive=true | "recursive delete — irreversible" |
| `delete` of a path the pre-check found to be a directory | scope is `tree`, even without `--recursive` (and the drafter refuses unless `--recursive` is passed) |
| `delete` of any path | "modifies remote workdir content" |

The agent **may** add reason-specific lines, never remove auto-generated ones.

## Validation (what `run_request.py` checks without `--execute`)

1. `expires_at` not in the past
2. `server` resolves in `~/.ssh/config`
3. `payload.remote_path` (when present) inside `~/workspace/<project_id>/`
4. `payload.local_path` (when present) inside `upload_roots`
5. `checksum` (when present) matches local file
6. Risk summary is non-empty for any write
7. Disk space at destination is plausible (skips on `command`)

Validation prints the result and exits 0/1. With `--execute`, validation runs first, then the action.

## Execute (`run_request.py --execute`)

1. Re-run validation.
2. Acquire a lockfile next to the artifact: `<request_id>.lock`. Prevents two concurrent `--execute`s on the same artifact.
3. Hand off to `ssh-core`:
   - `command` → `ssh_execute.py`
   - `upload` → `ssh_upload.py` (`--overwrite` is passed through when the
     artifact says `overwrite: true`; without it `ssh_upload.py` refuses to
     clobber an existing destination with exit 168 `remote_path_exists`)
   - `mkdir`/`delete` → `ssh_execute.py` with the obvious one-liner
4. Capture stdout / stderr / exit code into `<request_id>.result.json`.
5. Print the redacted summary. Raw output is in the result file.

On non-zero exit, the artifact stays. Re-execution requires a new request (same payload is fine, new id, new reason).

### Failure semantics

`success` means **the action completed**, never "the request was dispatched".
A transport that exits non-zero is a failed action: the envelope says so and so
does the process exit code.

| Transport result | `success` | process exit | `error` | `data` |
|---|---|---|---|---|
| exit 0 | `true` | `0` | `null` | `result_path`, `request_id`, `transport`, `exit_code: 0`, `duration_ms`, `stderr_tail` |
| exit 1..255 | `false` | the child's code | `{"message": "transport exited <N>", "failure_class": <child class>, "exit_code": <N>}` | the same fields, plus `failure_class` and `transport_exit_code` |
| exit < 1 (killed by a signal) or > 255 | `false` | `1` | `{"message": "transport exited <N>", "failure_class": ..., "exit_code": 1}` | the same fields, plus the raw `transport_exit_code` |

`data.failure_class` is the child's own class when the transport classified
itself (`remote_path_missing`, `permission`, …); otherwise it is `internal`.
`stderr` carries the redacted transport stderr (or the `transport exited N`
line when the child printed nothing).

`result.json` is written **before** the outcome is judged, so the audit record
of a failed execution survives. Its `exit_code` / `transport_exit_code` fields
are unchanged from earlier versions.

Validation failures keep their existing codes and never reach the transport:

| Code | `failure_class` |
|---|---|
| 50 | `workdir_outside_sandbox` |
| 51 | `path_in_denylist` |
| 53 | `artifact_invalid` / `artifact_expired` |
| 54 | `lock_held` |
| 56 | `sha256_mismatch` |
| 57 | `transport_failed` (non-JSON child output) |
| 59 | `delete_confirm_required` |
| 124 | `timeout` (the transport itself never returned) |

## Detached jobs

`exec_detached.py run --request <file>` does the same as `run_request --execute`, except:

- The command is wrapped in `nohup … > <remote-log> 2>&1 &`.
- Returns immediately with a `job_id`.
- A local manifest is written to `$SSH_GUARDED_HOME/reports/<project_id>/jobs/<job_id>.json`:
  ```json
  { "job_id": "...", "request_id": "...", "remote_pid": 12345,
    "remote_log": "~/workspace/<id>/logs/<job_id>.log",
    "started_at": "...", "status": "running" }
  ```

`exec_detached.py status <job_id>` polls `kill -0 <pid>` on the remote and reads the tail of the log. `wait` blocks. `tail-log` is plain `tail -n`.

## Failure modes

- **Stale request.** `expires_at` is past. Refuse, ask the user to redo intent.
- **Path drifted out of workdir.** Project's `workdir` field changed between request and execute. Refuse. Project state must be locked while a request is pending.
- **Checksum mismatch.** Local file changed since request. Refuse — content drift means the intent doesn't match the bytes any more.
- **Lock held.** Another execute is running. Wait or unlock manually (the lock includes the PID).
- **Transport failed.** The action did not happen. `success=false`, non-zero
  exit, `data.failure_class` names the cause. Read `result.json` for the
  redacted transport output before deciding whether to draft a new request.

## JSON envelope

Every ssh-guarded script emits the same six keys — the marketplace shape plus
the backwards-compatible `error` object:

```json
{
  "success": false,
  "exit_code": 53,
  "stdout": "",
  "stderr": "artifact_invalid: artifact not found: /tmp/x.json\n",
  "data": {"failure_class": "artifact_invalid"},
  "error": {"message": "artifact not found: /tmp/x.json",
            "failure_class": "artifact_invalid", "exit_code": 53}
}
```

`jq -r .exit_code` always prints a number — including when argparse rejects the
command line, which now emits `{"exit_code": 2, "error": {"failure_class":
"invalid_arguments"}, ...}` on stdout instead of nothing.
