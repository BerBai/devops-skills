# request → execute lifecycle

The full mechanic behind the "two-phase write" you see in `SKILL.md`.

## Why this exists

Agent tools that execute writes directly conflate **intent** with **action**. The transcript shows "I ran X" but not "I considered Y first". When something breaks, the human reading the transcript can't reconstruct what the agent was trying to do, only what it ended up doing.

A request artifact splits the two. The agent declares intent (request), the human (or the agent on a second pass) reviews it, then execution happens against the artifact. Auditable, reproducible, replayable.

## Artifact location

```
plugins/ssh-guarded/skills/ssh-guarded/reports/
├── requests/
│   ├── command-a1b2c3d4.json
│   ├── upload-e5f6g7h8.json
│   └── delete-i9j0k1l2.json
└── jobs/
    └── <job_id>.json   (only when exec_detached is used)
```

Paths are gitignored by default — they contain user intent and partial command output. Don't commit them.

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
   - `upload` → `ssh_upload.py`
   - `mkdir`/`delete` → `ssh_execute.py` with the obvious one-liner
4. Capture stdout / stderr / exit code into `<request_id>.result.json`.
5. Print the redacted summary. Raw output is in the result file.

On non-zero exit, the artifact stays. Re-execution requires a new request (same payload is fine, new id, new reason).

## Detached jobs

`exec_detached.py --request <file>` does the same as `run_request --execute`, except:

- The command is wrapped in `nohup … > <remote-log> 2>&1 &`.
- Returns immediately with a `job_id`.
- A local manifest is written to `reports/jobs/<job_id>.json`:
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
