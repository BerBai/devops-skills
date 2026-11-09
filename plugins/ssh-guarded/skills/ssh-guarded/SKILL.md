---
name: ssh-guarded
description: Safety-first SSH operations on top of ssh-core. Use when working on production servers, customer environments, or any context where every write must be auditable. Implements request→review→execute two-phase writes, redacted output by default, workdir-sandboxed file operations, and detached long-running jobs.
---

# ssh-guarded

**CRITICAL:** This skill turns Claude Code into a careful remote operator. **Do not bypass the request/execute split** by calling `ssh-core` directly for write operations when `ssh-guarded` is installed. If you find yourself wanting to, ask the user — they may genuinely want the fast lane.

`ssh-guarded` does not replace `ssh-core`. It adds a layer:

```
user intent → ssh-guarded (audit, redact, sandbox) → ssh-core (transport)
```

## When to use

Use `ssh-guarded` when **any** of these are true:

- The host is production, customer-facing, or shared infrastructure
- The action modifies files, services, or state on the remote host
- The transcript may be reviewed by another human or auditor
- The user said "be careful" / "don't break anything" / "production"
- You are about to run a command you cannot easily undo

Skip when the user is in a hurry on a sandbox host of their own and explicitly asks for the fast lane. In that case, default to `ssh-core` directly.

## Hard checkpoints (do not skip)

```
1.  Discover               — what's configured? what's reachable?
2.  Choose                  — which server? which account? which workdir?
3.  Check                   — BatchMode ssh ping, software scan if needed
4.  Read intent             — restate the user's goal back in one line
5.  Workdir resolve         — confirm sandbox path
6.  File-list / file-stat   — read before write
7.  Draft request           — request_*.py, never run yet
8.  Show request to user    — full payload + risk_summary, ask "execute?"
9.  Execute                 — run_request.py --execute, only after consent
10. Long task?              — switch to exec_detached, return job_id
11. Verify                  — file-stat / tail-log / status
12. Report                  — redacted summary; raw output only on demand
```

If you find yourself executing without the request artifact, stop and start over.

## Decision tree

```
Is this a read or a write?
├─ READ  → ssh-core directly is fine for transient reads
│         BUT prefer scan_software / inventory for *anything cached*
│
└─ WRITE → mandatory request → review → execute
   │
   ├─ command      → request_command.py
   ├─ file upload  → request_upload.py
   ├─ mkdir        → request_mkdir.py
   ├─ delete       → request_delete.py
   │
   └─ Will it run > 60 s?
       YES → after run_request, switch to exec_detached
       NO  → run_request --execute is fine
```

## Request artifact contract

Every write produces a JSON file under
`$SSH_GUARDED_HOME/reports/<project_id>/requests/<id>.json` (default home:
`~/.devops/ssh-guarded`; `--reports-dir` overrides the `reports` level):

```json
{
  "version": 1,
  "request_id": "command-a1b2c3d4",
  "operation": "command",
  "server": "prod-web-01",
  "reason": "rotate nginx access log before grep",
  "created_at": "2026-05-14T09:21:00Z",
  "payload": {
    "command": "logrotate -f /etc/logrotate.d/nginx",
    "workdir": "~/workspace/log-rotation"
  },
  "risk_summary": {
    "irreversible": false,
    "scope": "single-file",
    "blast_radius": "runs 'logrotate -f /etc/logrotate.d/nginx' as you@prod-web-01"
  }
}
```

The artifact is the **agreement** between you and the user. When you present it, paste the JSON in full. When the user says "execute", run:

```
run_request.py --request <artifact path> --execute
```

Without `--execute`, `run_request.py` only **validates** the request and reports what it would do. Use this for double-checks.

## Two-phase flow: what each phase means

```
draft   request_*.py  → artifact on disk; nothing has touched the host yet
review  (you + user)  → full payload + risk_summary; ask "execute?"
execute run_request.py --execute → lock, dispatch, result.json, real outcome
```

`success: true` means **the action completed**, not "the request was
dispatched". Two consequences worth stating explicitly:

- A drafter that exits 0 only means *a request exists*. The host is untouched.
- An executor whose transport exits non-zero reports `success: false`, a
  populated `error`, and the transport's own exit code from its own process.
  `--execute` is therefore safe to put in a shell `&&` chain or a CI step.

| Transport exit | `success` | process exit | `error.failure_class` |
|---|---|---|---|
| 0 | `true` | `0` | `null` |
| 1..255 | `false` | the same code | the transport's class, else `internal` |
| <1 or >255 | `false` | `1` | the transport's class, else `internal` |

`result.json` is written in both cases: the audit trail must survive a failure.
Validation failures keep their own codes (50/51/53/54/56/57/59/124) and never
reach the transport. See `references/request-execute.md` for the full table.

## JSON envelope

Every script in this plugin emits the same six top-level keys — the
marketplace-wide shape plus `error` for backwards compatibility:

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "drafted command-a1b2c3d4 -> ...\n",
  "stderr": "",
  "data": {"artifact_path": "...", "request_id": "command-a1b2c3d4"},
  "error": null
}
```

- `exit_code` is always the process exit code and is **never null** —
  `jq -r .exit_code` works on success, on classified failure, and on an
  argparse error (`2` + `failure_class: "invalid_arguments"`).
- `stdout` / `stderr` are the human-mode text, so a wrapper can log exactly
  what the user saw.
- `data` is the script-specific payload; `data.failure_class` is set on failure.
- `error` is `null` on success and `{message, failure_class, exit_code}` on
  failure, with `error.exit_code == exit_code`.

## Redaction policy

By default, **every output that quotes a remote source has the following fields replaced with `<redacted>`**:

- `HostName`
- `User`
- `Port`
- `IdentityFile`
- Any path under `~/.ssh/`
- Anything matched by the secret regex catalog (`AKIA…`, `gh[pus]_…`, `eyJ…` JWTs, RSA/EC private key headers)

The unredacted form is available behind `--show-sensitive` on the consuming script. Use it sparingly and never in chat transcripts you expect to share. See `references/redact.md`.

## Workdir sandbox

Every remote write is constrained to **one** directory per project:

```
~/workspace/<project_id>/
```

`project_id` is bound by `.devops/project.local.json` (the local file lives in the user's repo, not here). Writes are rejected when the resolved path:

- Starts with `/` (absolute), `~` other than `~/workspace/`, or a Windows drive letter
- Contains `..`
- Lands in `~/.ssh`, `.env`, `authorized_keys`, or any private key file

Local sources for `request_upload.py` are constrained to the project root by default. To upload from outside (`~/.aws/credentials`, etc.), the user must pass `--confirm-sensitive-local-upload` and `--reason "..."`. We log the reason on the request artifact.

See `references/workdir-sandbox.md`.

## Detached long jobs

For anything that may take longer than a few seconds (builds, snapshots, migrations), do not block:

```
exec_detached.py run --request <artifact path>
  → writes $SSH_GUARDED_HOME/reports/<project_id>/jobs/<job_id>.json
  → starts the command with nohup on the remote host
  → returns the job_id immediately
```

Then track:

```
exec_detached.py status <job_id>
exec_detached.py tail-log <job_id> --lines 200
exec_detached.py wait <job_id> --timeout 600
```

A synchronous SSH timeout is a **transport** boundary, not proof that the command failed. If you've already detached, never treat timeouts as failure — go look at the job log.

## Software scan

`scan_software.py` caches versions of common tools on each host:

```
scan_software.py prod-web-01
   → updates ~/.devops/cache/<host>/software.json
   → returns: python, conda, cuda, nvidia_driver, gcc, g++, cmake,
              docker, kubectl, terraform, helm, ansible, vivado, vitis
              (whichever are present, with versions)
```

After the first scan, `scan_software.py --name kubectl prod-web-01` reads the cache. No SSH round trip. Use this to answer "can I run X on this box?" without reconnecting.

## Housekeeping

Runtime state does **not** live in the skill directory. It lives under
`$SSH_GUARDED_HOME` (default `~/.devops/ssh-guarded`,
`%USERPROFILE%\.devops\ssh-guarded` on Windows), one directory per project:

```
$SSH_GUARDED_HOME/reports/<project_id>/requests/   # drafts, results, locks
$SSH_GUARDED_HOME/reports/<project_id>/jobs/       # detached job manifests
```

That keeps a read-only install usable, stops an upgrade from deleting pending
approvals, and keeps two projects' artifacts apart. `--reports-dir PATH` on the
drafters, `exec_detached.py` and `gc_artifacts.py` replaces the `.../reports`
level for CI and containers (`run_request.py` writes its `result.json` next to
the artifact it was given, so it needs no flag). Artifacts left in the old
in-tree `reports/` are reported once, never moved or deleted automatically.

`gc_artifacts.py` prunes the state tree, per project:

```
gc_artifacts.py                          # dry run, 7-day cut-off, all projects
gc_artifacts.py --older-than 30d --apply
gc_artifacts.py --project-id myproj --older-than 12h --apply
gc_artifacts.py --reports-dir /var/ci/reports --older-than 1d --apply
```

`--dry-run` is the default. A lock that is still held, and a request whose
`expires_at` has not passed, are never removed — see `reports/README.md` for
the full rule table.

## What `ssh-guarded` does *not* do

- It does **not** replace transport. `ssh-core` does all the actual work.
- It does **not** prompt the user itself — Claude Code is the prompt surface. The skill produces artifacts; Claude presents them.
- It does **not** encrypt or store secrets. Out of scope. Use `sops`, `age`, or the platform's secret manager.

## References

- `references/request-execute.md` — full lifecycle, file formats, edge cases
- `references/redact.md` — the redaction catalog and how to extend it
- `references/workdir-sandbox.md` — path rules, escape attempts and how we refuse them
