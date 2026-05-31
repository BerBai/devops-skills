# ship ssh-guarded exec_detached v1.0

## Goal

Promote `plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py:1-84`
from v0.2 stub (returns `UNIMPLEMENTED_NOTE` for every subcommand) to v1.0:
fire-and-track long remote commands via nohup wrap + remote PID capture +
remote log file + local job-state file. Five subcommands — `run`, `status`,
`tail-log`, `wait`, `list`. Consumes the request artifact format delivered by
the archived **ssh-guarded-request-pipeline** child (see
`.trellis/tasks/archive/2026-05/05-25-ssh-guarded-request-pipeline/design.md`
§1); does not redefine schema.

## User value

- Claude Code dispatches a long build / migration / model-pull through one
  `exec_detached run --request <id>.json` call, gets back a `job_id`, and is
  free to do other work — the SSH timeout boundary is no longer a wall-clock
  ceiling on remote work (SKILL.md:130-147).
- `status <job_id>` and `tail-log <job_id>` answer "is it still running, and
  what is it printing" without re-attaching a shell.
- `wait <job_id> --timeout N` is the polite poll loop with backoff baked in
  (D6) — Claude does not need to re-invent it per task.
- `list` makes "what did I leave running on what host?" a single command.

## Confirmed facts (from repo inspection)

1. **CLI surface fixed** (`exec_detached.py:8-27` docstring, mirrored by
   `SKILL.md:130-147`):
   ```
   exec_detached.py run --request reports/requests/<id>.json [--json]
   exec_detached.py status <job_id> [--json]
   exec_detached.py tail-log <job_id> [--lines N] [--follow] [--no-redact] [--json]
   exec_detached.py wait <job_id> [--timeout SEC] [--json]
   exec_detached.py list [--probe] [--json]
   ```
   The scaffold parser at `exec_detached.py:41-62` already has the right
   subcommands; v1.0 adds `--probe` to `list` and `--no-redact` to `tail-log`
   on top of the existing flags.

2. **Operation restriction**: only `operation == "command"` request artifacts
   are executable detached (D1). Upload / mkdir / delete are inherently
   one-shot SFTP calls — detaching them adds no value and complicates remote
   PID semantics.

3. **Transport**: every remote call (nohup launch, `kill -0` probe, `tail`)
   shells out to `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py`
   per ADR-001 D1 (subprocess + native ssh; no paramiko). Streaming
   `tail --follow` uses `subprocess.Popen` against `ssh ... 'tail -F'`
   directly — `ssh_execute.py` is single-shot only (ADR-001 §D6).

4. **Shared lib delivered by Child A and present in the repo** (see archived
   request-pipeline design.md §1):
   `lib/request_artifact.py`, `lib/redact.py`, `lib/lockfile.py`,
   `lib/sandbox.py`. This task imports them; does **not** reimplement.

5. **Failure-class region**: exit codes 60-69 reserved for exec_detached
   (cross_cutting §C5). Per-script namespace pattern is now the project
   convention (3 precedents: ssh_config_manager 4-8, deploy_pubkey 10-15,
   ssh_server_transfer 20-29 — see parent PRD Notes).

## v1.0 surface

| Subcommand | Flags | Returns (data.*) |
|---|---|---|
| `run --request <path>` | `--json` | `job_id`, `remote_pid`, `remote_log_path`, `server`, `started_at` |
| `status <job_id>` | `--json` | `state` (`running`/`exited`/`vanished`), `exit_code?`, `remote_pid` |
| `tail-log <job_id>` | `--lines N` (default 200), `--follow`, `--no-redact`, `--json` | `lines` (list, non-follow) or streams to stdout (follow) |
| `wait <job_id>` | `--timeout SEC` (default 600), `--json` | terminal `state` + `exit_code` |
| `list` | `--probe`, `--json` | array of `{job_id, server, started_at, state, command_excerpt}` |

## In-scope

1. Implement all five subcommands per D1-D11 (design.md).
2. Job state file at `reports/jobs/<job_id>.json` (D3 schema).
3. Remote log at `~/workspace/<project_id>/.exec-detached/<job_id>.log`.
4. Concurrency guard via `<request-path>.lock` (D10) — first `run` wins,
   second exits 60 `request_already_executed`.
5. Redaction on `tail-log` output by default (D9), bypass with `--no-redact`.
6. Unit tests (~20) + live_ssh integration (~3 on pai).
7. SKILL.md decision tree already covers exec_detached (lines 130-147); only
   minor wording tweaks if shipping reveals divergence — no structural rewrite.

## Out-of-scope (deferred to v1.1)

- **Daemon-mode supervisor** that keeps watching jobs across user shell
  sessions. v1.0 is a stateless CLI; state lives entirely in
  `reports/jobs/*.json` and remote PID + log file.
- **Detached jobs orchestration across multiple hosts** (one `run` =
  one host per artifact). Multi-host fan-out is a separate child.
- **Job retries** on transport failure during dispatch. v1.0 returns the
  failure; caller decides whether to re-`run`.
- **Job cancellation** (`exec_detached kill <job_id>`). v1.0 does not own a
  remote `kill` path — user does it themselves with ssh-core, or accepts the
  process runs to completion.
- **Cross-machine job visibility**. `list` shows only jobs `run` from this
  workstation (reports/jobs/ is local).
- **Streaming output capture** into the result artifact (run_request's
  `.result.json` is for synchronous run only).
- **Live `wait` log streaming**. `wait` polls `status`; `tail-log --follow`
  is the streaming path.

## Requirements

### R1 — CLI surface
- Exact subcommand + flag set from "v1.0 surface" table above.
- `--help` on every subcommand exits 0.
- Top-level `--json` accepted by every subcommand; non-JSON is human-readable.

### R2 — `run --request <path>`
- Load + validate request via `lib.request_artifact.RequestArtifact.load`.
- Reject non-`command` operation: exit 60 `request_not_executable`.
- Acquire `<artifact-path>.lock` via `lib.lockfile` (cross_cutting §C6).
  Lock held → exit 60 `request_already_executed` (re-use code 60 for the
  "already running" class; lock-not-acquired vs already-executed
  distinguished by presence of an existing `reports/jobs/*.json` referencing
  the same `request_id`).
- Compose nohup wrapper (D2), dispatch via `ssh_execute.py`, capture single
  line of stdout (= remote PID). Empty PID or non-numeric → exit 62
  `pid_capture_failed`.
- Persist `reports/jobs/<job_id>.json` (D3) atomically (write `.tmp` then
  `os.replace`). job_id = `uuid4().hex[:8]`.
- Emit envelope with `job_id`, `remote_pid`, `remote_log_path`. Release lock.

### R3 — `status <job_id>`
- Read `reports/jobs/<job_id>.json`; missing or unparseable → exit 65
  `job_state_corrupt`.
- SSH-exec `kill -0 <remote_pid>` (D4). On exit 0 → `state="running"`.
- On non-zero kill probe: SSH-exec `tail -n 5 <remote_log>` looking for
  `__EXEC_DETACHED_EXIT__=N` sentinel → `state="exited"`, `exit_code=N`.
- No sentinel + dead PID → `state="vanished"`, exit 64 `job_vanished`.
- Log unreadable (SSH OK, file gone) → exit 63 `remote_log_unreachable`.

### R4 — `tail-log <job_id>`
- Default: SSH-exec `tail -n <lines> <log>`; redact every line through
  `lib.redact.Redactor` unless `--no-redact`.
- `--follow`: `subprocess.Popen(["ssh", ..., host, f"tail -F -n {lines} {log}"])`;
  line-buffered redaction loop on stdout; SIGINT (Ctrl-C) → kill ssh,
  flush partial line, exit 0.
- Missing log on remote → exit 63 `remote_log_unreachable`.

### R5 — `wait <job_id> [--timeout SEC]`
- Poll cadence per D6: 1s × 30, then 5s × until 5 min mark, then 15s up to
  the 30s cap. Default `--timeout 600`.
- Returns terminal envelope when state ∈ {`exited`, `vanished`}; on timeout
  → exit 124 with last-known state.

### R6 — `list [--probe]`
- Scan `reports/jobs/*.json`. Offline mode: report `state="unknown"` + file
  mtime. `--probe`: re-run R3 status for each (fan out sequentially; v1.0
  no concurrency).

### R7 — JSON envelope
- Same shape as ssh-core / request-pipeline (cross_cutting §C7):
  `{success, exit_code, stdout, stderr, data}`. `data.failure_class` always
  set (`null` on success, one of the 6 strings on failure per D8).

### R8 — Failure classes + exit codes (per-script region 60-69)
- 60 `request_not_executable` — non-command operation OR
  `request_already_executed` (lock or duplicate job state).
- 61 `nohup_dispatch_failed` — ssh_execute returned non-zero on launch.
- 62 `pid_capture_failed` — stdout from launch wasn't a clean integer.
- 63 `remote_log_unreachable` — `tail`/`stat` of remote log failed.
- 64 `job_vanished` — PID gone, no sentinel in log.
- 65 `job_state_corrupt` — local job JSON missing keys / invalid JSON.
- Shared: 0 ok, 1 generic, 124 timeout (per cross_cutting §C5 footnote).

### R9 — Redaction
- `lib.redact.Redactor` instantiated lazily on `tail-log`. `--no-redact`
  bypasses. Live tail uses line-buffered apply; memory bound is one line
  at a time (D9).

### R10 — Testing
- Unit (`tests/test_exec_detached_unit.py`, ~20 tests): nohup-template
  rendering, command escaping, PID parser, status state machine
  (running/exited/vanished/corrupt), wait backoff schedule, list scan,
  redaction on/off, lock contention.
- Integration (`tests/test_exec_detached_live.py`, marker `live_ssh`):
  (a) `run` a 5s `sleep` + status running → wait → exited; (b) `tail-log`
  fetches captured output; (c) duplicate `run` on same artifact → exit 60.

### R11 — Cross-platform
- Local paths via `pathlib.Path`; remote via `posixpath`. Same posture as
  ssh-core scripts (`.trellis/tasks/archive/2026-05/05-24-ssh-core-ssh-server-transfer/design.md:55-85`).

## Acceptance Criteria

- [ ] `python exec_detached.py --help` and every subcommand `--help` exit 0.
- [ ] `pytest tests/test_exec_detached_unit.py` green.
- [ ] `pytest -m live_ssh tests/test_exec_detached_live.py` green on pai.
- [ ] `run --request <upload-op.json>` → exit 60 `request_not_executable`.
- [ ] Happy path: `run --request <command-op.json>` → exit 0, `data.job_id`
      8 hex chars, `reports/jobs/<job_id>.json` exists with all D3 fields.
- [ ] `status <job_id>` returns `running` while remote PID alive; flips to
      `exited` with the command's real exit code once `__EXEC_DETACHED_EXIT__`
      sentinel lands in the log.
- [ ] `tail-log` redacts AKIA / `gh[pus]_` / RSA-PRIVATE-KEY by default;
      `--no-redact` shows raw.
- [ ] `wait --timeout 5` on a long sleep returns exit 124 with last state.
- [ ] Duplicate `run` against the same artifact → exit 60
      `request_already_executed`; first job state file untouched.
- [ ] `list` shows the entry without re-probing; `list --probe` re-runs
      status for each.
- [ ] No code in `lib/sftp_transport.py` modified (this task does not touch
      ssh-core internals).
- [ ] `tests/README.md` "What's covered today" appended with exec_detached
      unit + integration counts.

## Notes

- Open question parked: should `request_already_executed` get its own code
  (60-69 already 6 strings) or fold into `request_not_executable` exit 60?
  Current decision (D10): **fold**, distinguished by stderr message + a
  `data.subclass` string. Revisit if Claude Code parsers find this ambiguous.
- Open question parked: redaction catalog hot-reload? v1.0 reads catalog
  once per invocation; `tail-log --follow` does **not** re-read mid-stream.
  Acceptable since the catalog is user-owned and changes are rare.
- Open question parked: `wait` cadence vs SSH connection overhead — every
  poll is a fresh SSH (no daemon, ADR-001 D1). 30s cap mitigates; if pai
  benchmarks show >300ms/probe, revisit in v1.1.
