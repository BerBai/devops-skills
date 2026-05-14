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

Every write produces a JSON file under `reports/requests/<id>.json`:

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
  "risk_summary": [
    "modifies remote workdir content",
    "affects nginx logging — service does not restart but log handle rotates"
  ]
}
```

The artifact is the **agreement** between you and the user. When you present it, paste the JSON in full. When the user says "execute", run:

```
run_request.py --request reports/requests/command-a1b2c3d4.json --execute
```

Without `--execute`, `run_request.py` only **validates** the request and reports what it would do. Use this for double-checks.

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
exec_detached.py --request reports/requests/command-xyz.json
  → writes reports/jobs/<job_id>.json
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

## What `ssh-guarded` does *not* do

- It does **not** replace transport. `ssh-core` does all the actual work.
- It does **not** prompt the user itself — Claude Code is the prompt surface. The skill produces artifacts; Claude presents them.
- It does **not** encrypt or store secrets. Out of scope. Use `sops`, `age`, or the platform's secret manager.

## References

- `references/request-execute.md` — full lifecycle, file formats, edge cases
- `references/redact.md` — the redaction catalog and how to extend it
- `references/workdir-sandbox.md` — path rules, escape attempts and how we refuse them
