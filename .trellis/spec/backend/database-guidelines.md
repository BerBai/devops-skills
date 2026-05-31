# Plugin local state file conventions

> This repo has no database. Persistent state is JSON files on disk.

---

## Overview

There is no ORM, no migrations, no connection pool. Anything a plugin needs
to remember between invocations lives in a local JSON file. Two patterns
exist, and only two:

1. **`ssh-guarded` request/job artifacts** under
   `plugins/ssh-guarded/skills/ssh-guarded/reports/{requests,jobs}/*.json`.
2. **Per-host / per-project caches** named `*.local.json`.

Both are runtime state, never committed.

---

## Pattern 1: request/job artifacts (ssh-guarded only)

The two-phase "request → review → execute" flow of `ssh-guarded` writes:

- `reports/requests/<request_id>.json` — drafted by `request_*.py`.
- `reports/requests/<request_id>.result.json` — written by `run_request.py --execute`.
- `reports/requests/<request_id>.lock` — concurrency lock held during execute.
- `reports/jobs/<job_id>.json` — manifest written by `exec_detached.py run`.

See `plugins/ssh-guarded/skills/ssh-guarded/reports/README.md` for the
authoritative description. Cleanup is the user's responsibility (a planned
`--gc-older-than 7d` flag handles bulk removal).

### Scenario: `exec_detached` Job State Files

#### 1. Scope / Trigger

- Trigger: `exec_detached.py run` creates long-lived local JSON state so
  later CLI invocations can find the remote PID and log path.
- Applies only to `plugins/ssh-guarded/skills/ssh-guarded/reports/jobs/*.json`.

#### 2. Signatures

- Create: `exec_detached.py run --request <artifact.json> [--json]`
- Read one: `exec_detached.py status <job_id> [--json]`
- Read log: `exec_detached.py tail-log <job_id> [--lines N] [--follow] [--no-redact] [--json]`
- Poll: `exec_detached.py wait <job_id> [--timeout SEC] [--json]`
- Scan: `exec_detached.py list [--probe] [--json]`

#### 3. Contracts

Job state schema:

```json
{
  "version": 1,
  "job_id": "8hexchars",
  "request_id": "command-8hexchars",
  "server": "ssh-alias",
  "remote_pid": 12345,
  "remote_log_path": "~/workspace/<project_id>/.../.exec-detached/<job_id>.log",
  "started_at": "2026-05-31T00:00:00Z",
  "command_excerpt": "first 80 chars...",
  "cwd_on_remote": "~/workspace/<project_id>/..."
}
```

Rules:

- Write atomically: temp file in `reports/jobs/`, then `os.replace`.
- File mode should be `0o600` on POSIX.
- The file is immutable after creation; runtime state is derived from the
  remote PID and remote log sentinel, not by updating the JSON file.
- Do not store secrets, SSH key paths, or command output in job state.

#### 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| Missing job file | exit `65`, `data.failure_class="job_state_corrupt"` |
| Invalid JSON / non-object | exit `65`, `job_state_corrupt` |
| Missing required key | exit `65`, `job_state_corrupt` |
| `remote_pid` not integer | exit `65`, `job_state_corrupt` |
| Duplicate `request_id` during `run` | exit `60`, `request_not_executable`, `data.subclass="request_already_executed"` |
| Corrupt unrelated file during `list` | include row `{job_id: <stem>, state: "corrupt"}`; do not abort the whole list |

#### 5. Good/Base/Bad Cases

- Good: `run` writes one complete state file and `status` can immediately
  load it to probe `kill -0`.
- Base: `list` with no job files returns an empty jobs array / human
  "no detached jobs" message.
- Bad: a half-written or hand-edited job file must not raise a traceback;
  emit the shared JSON envelope with exit `65`.

#### 6. Tests Required

- Unit: atomic write leaves no `*.tmp`, mode is `0o600` where meaningful,
  required fields round-trip, corrupt/missing cases map to exit `65`.
- Unit: duplicate request scan returns the existing job id.
- Integration: live `run -> status -> wait` proves local state can track a
  real remote background command.

#### 7. Wrong vs Correct

Wrong:

```python
path.write_text(json.dumps(job))
job["state"] = "running"
path.write_text(json.dumps(job))
```

Correct:

```python
tmp = write_tmp_json(job)
os.replace(tmp, path)
# Later status derives state from remote PID + log sentinel.
```

Anchors in `.gitignore` (lines 13-14):

```
plugins/ssh-guarded/skills/ssh-guarded/reports/requests/*.json
plugins/ssh-guarded/skills/ssh-guarded/reports/jobs/*.json
```

---

## Pattern 2: `*.local.json` per-host / per-project state

Any other persistent state file MUST end in `.local.json`. Examples already
allowlisted in `.gitignore` lines 15-16:

```
plugins/ssh-guarded/skills/ssh-guarded/server_list.local.json
plugins/ssh-guarded/skills/ssh-guarded/project.local.json
```

For per-host daemons/tunnels, the file name is keyed by `md5(alias)` so the
same host always resolves to the same file regardless of where the user
invokes from. This is documented for `ssh_tunnel.py` in CONTRIBUTING.md
line 45 ("state files keyed by `md5(alias)`") and reused by `ssh_daemon`
(SKILL.md line 102 — `$TMPDIR/ssh_daemon/<md5(alias)>.json`).

Rules for new state files:
- Filename ends in `.local.json` to match the existing convention. There is
  no broad `*.local.json` glob in `.gitignore` today, so you MUST also add
  the new path to `.gitignore` (alongside lines 15-16).
- If keyed by SSH alias / host, use `hashlib.md5(alias.encode()).hexdigest()`
  — match the existing convention; do not introduce a new hashing scheme.
- Schema is plain JSON, top-level object, one key per logical record.
- Do NOT store secrets in these files. Keys and passwords are read from
  `~/.ssh/config` / agent / paramiko at use time, never persisted.

---

## What `.gitignore` actually does

The two `.gitignore` passes that touch this area are independent:

1. **Explicit runtime-state paths** (lines 13-16) — every known JSON state
   file is listed by full path. There is no `*.local.json` glob; new state
   files require a new line here.
2. **Secrets blocklist** (lines 29-36) — `*.pem`, `*.key`, `id_rsa*`,
   `id_ed25519*`, `known_hosts`, `authorized_keys`, `.env`, `.env.*` (with
   `!.env.example` re-allowed on line 37). This catches credentials, not
   state.

Consequence: if a script writes state to an undocumented path, git will
track it unless you've already added the path. Fix the path bug AND extend
`.gitignore` in the same change.

---

## What NOT to do

- Do NOT introduce SQLite, DuckDB, or any embedded database. The repo
  philosophy is "no proprietary database, no lock-in"
  (`plugins/ssh-core/skills/ssh-core/SKILL.md` line 98).
- Do NOT commit anything under `reports/requests/`, `reports/jobs/`, or
  anything matching `*.local.json`.
- Do NOT serialize Python objects with `pickle` — JSON only, so the user
  can inspect the file with `cat`.
- Do NOT store credentials, key paths, or `IdentityFile` values in these
  files (see `logging-guidelines.md`).
