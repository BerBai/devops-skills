# Design — ssh-guarded exec_detached v1.0

> Companion PRD: `prd.md`.
> Cross-child contract (request artifact schema, sandbox rules, redact
> catalog, lockfile, JSON envelope, failure-class regions): see archived
> Child A design §1 at
> `.trellis/tasks/archive/2026-05/05-25-ssh-guarded-request-pipeline/design.md`.
> This design references it; does **not** duplicate verbatim.

## 1. Architecture overview

```
exec_detached.py  (CLI, ~350 loc estimate)
├── lib/request_artifact.py   ← from Child A; validate + load request
├── lib/redact.py             ← from Child A; tail-log filter
├── lib/lockfile.py           ← from Child A; <artifact>.lock context manager
├── lib/sandbox.py            ← from Child A (workdir resolution only — exec_detached
│                                does not draft requests so sandbox check is implicit)
└── shell-out → ssh_execute.py (ssh-core)   for one-shot remote calls:
    - nohup launch    : `bash -c '...nohup wrapper...'`
    - liveness probe  : `kill -0 <pid> 2>/dev/null && echo alive || echo dead`
    - log tail (one-shot): `tail -n N <log>`
    - log follow      : direct `Popen(["ssh", host, "tail -F -n N <log>"])`
                        (Popen, NOT ssh_execute.py — needs streaming stdout;
                         ADR-001 §D6 ssh_execute is single-shot)
```

Data flow per subcommand:

```
run     → load artifact → validate.command-only → acquire lock →
          nohup_wrap(command) → ssh_execute(launcher) → parse PID →
          persist reports/jobs/<job_id>.json → release lock → emit
status  → load job json → ssh kill -0 → branch: alive | tail-sentinel | vanished
tail    → load job json → ssh tail (-F if --follow) → redact line-buffer → emit
wait    → loop(poll_cadence): status; break on terminal | timeout
list    → glob reports/jobs/*.json → (optional) probe each → emit
```

## 2. Design decisions

### D1. Request artifact ingest

`run --request <path>` calls `lib.request_artifact.RequestArtifact.load(path)`
from Child A. Reject (exit 60 `request_not_executable`) when:

```python
if art.operation != "command":
    return fail(60, "request_not_executable",
                f"exec_detached only runs operation='command'; "
                f"got '{art.operation}'. Use run_request.py for {art.operation}.")
```

Sandbox + denylist re-validation is **not** repeated here — Child A's
`request_artifact.load()` validates the artifact against the schema; the
`workdir` sandbox check is enforced by `run_request --execute` not by
`exec_detached`. Rationale: `exec_detached` launches the same command that
`run_request` would launch synchronously; both share the same sandbox guard
called from inside the artifact loader. If a future schema-version skip
lands, revisit.

### D2. nohup wrapper template

The shell snippet pushed to the remote — escapes the user's command into a
nested single-quoted body, runs it under `nohup`, captures both streams to
the per-job log, appends an exit-code sentinel, and echoes the launcher's
own PID (the immediate child of the SSH session) so the launcher exits and
we capture `$!` on stdout:

```
( nohup bash -c '<escaped-command>; echo __EXEC_DETACHED_EXIT__=$? >> <log>' \
    > <log> 2>&1 < /dev/null & echo $! )
```

Notes:
- `<log>` = `~/workspace/<project_id>/.exec-detached/<job_id>.log`. Parent
  `.exec-detached/` is `mkdir -p`-created in the same wrapper preamble:
  `mkdir -p <log_dir> && ( nohup ... )`. Failure to mkdir → exit 61
  `nohup_dispatch_failed`.
- `< /dev/null` keeps the backgrounded process from holding the SSH stdin.
- `echo $!` is the **last line of stdout**; the launcher SSH session exits
  immediately. We parse last non-empty line of stdout as integer PID.
- The trailing `echo __EXEC_DETACHED_EXIT__=$?` runs **inside** the same
  `bash -c '...'`, so `$?` is the user command's exit code. Sentinel is
  fixed-string so `tail -n 5 | grep` is reliable.

**Escaping**: the command body is wrapped in single quotes; any embedded
`'` in the user command is escaped via the standard `'\''` POSIX pattern.
For commands with truly nasty payloads (heredocs, multi-line scripts), the
v1.0 fallback is to refuse and recommend the user re-shape it as a single
shell line — we do **not** ship a "write a temp .sh on the remote first"
path in v1.0 (complexity vs how rare; revisit if Claude users hit it).
Escape implementation:

```python
def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
```

Stdout of the SSH launcher is a single line containing the PID. Robust
parser:

```python
last = next((line for line in reversed(proc.stdout.splitlines()) if line.strip()), "")
try:
    remote_pid = int(last.strip())
except ValueError:
    return fail(62, "pid_capture_failed", f"expected integer PID, got: {last!r}")
```

### D3. Job state file

Path: `reports/jobs/<job_id>.json`. `job_id` = `uuid4().hex[:8]`.

```json
{
  "version": 1,
  "job_id": "a1b2c3d4",
  "request_id": "command-deadbeef",
  "server": "prod-web-01",
  "remote_pid": 28341,
  "remote_log_path": "~/workspace/log-rotation/.exec-detached/a1b2c3d4.log",
  "started_at": "2026-05-25T15:32:11Z",
  "command_excerpt": "logrotate -f /etc/logrotate.d/nginx && systemctl reloa…",
  "cwd_on_remote": "~/workspace/log-rotation"
}
```

- `command_excerpt`: first 80 chars + `…` if truncated. Used by `list`
  so users recognise jobs without re-reading the request artifact.
- `cwd_on_remote`: copied from `payload.workdir`, or `~` if absent.
- Atomic write: `json.dump` → `.tmp` → `os.replace` to final.
- File **never updated** after creation (state lives on the remote: PID
  alive vs log sentinel). This keeps the local file immutable and avoids
  reasoning about concurrent writers.

### D4. `status` algorithm

```python
def status(job_id) -> dict:
    job = load_job(job_id)                       # exit 65 if corrupt
    probe = ssh_execute(job.server,
                       f"kill -0 {job.remote_pid} 2>/dev/null && echo alive || echo dead")
    if probe.stdout.strip() == "alive":
        return {"state": "running", "remote_pid": job.remote_pid}

    # PID is gone — look for the sentinel
    tail = ssh_execute(job.server, f"tail -n 5 {shquote(job.remote_log_path)}")
    if tail.exit_code != 0:
        return fail(63, "remote_log_unreachable", tail.stderr)

    m = re.search(r"__EXEC_DETACHED_EXIT__=(-?\d+)", tail.stdout)
    if m:
        return {"state": "exited", "exit_code": int(m.group(1)),
                "remote_pid": job.remote_pid}

    return fail(64, "job_vanished",
                f"remote PID {job.remote_pid} not running and no exit sentinel in log")
```

`kill -0` is racey by definition — same PID could be reassigned to a
different process between launch and probe. v1.0 accepts the race; the
sentinel disambiguates. If we see `dead + sentinel` → trust the sentinel.
If `alive` but the PID was reassigned → we'd misreport "running"; mitigated
by short job lifetimes and the fact that PIDs aren't reused within seconds
on a busy Linux system.

### D5. `tail-log` implementation

Two modes:

**Non-follow** (default):
```python
result = ssh_execute.main([job.server, f"tail -n {args.lines} {shquote(log)}", "--json"])
lines = parsed["stdout"].splitlines()
if not args.no_redact:
    lines = [redactor.apply(line)[0] for line in lines]
```

**Follow** (`--follow`): cannot use `ssh_execute.py` (single-shot per
ADR-001 §D6). Direct `Popen` with line-buffered stdout:

```python
argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        job.server, f"tail -F -n {args.lines} {shquote(log)}"]
proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, bufsize=1)
try:
    for line in proc.stdout:
        emit_line(redactor.apply(line)[0] if not args.no_redact else line)
except KeyboardInterrupt:
    proc.terminate(); proc.wait(timeout=2)
    return 0
```

Memory bound: line-by-line apply, no buffering of the full stream.
Catalog loaded once on `Redactor` instantiation.

### D6. `wait` polling cadence

```python
def _intervals():
    """1s × 30, then 5s × until 5 min, then 15s, capped at 30s."""
    for _ in range(30):  yield 1
    while True:           yield 5    # 30s..5 min
    # the loop body upgrades from 5s → 15s at 5 min and caps at 30s
```

Concretely, polls happen at: 1, 2, … 30, 35, 40, … 300, 315, 330, … then
`min(prev * 1.5, 30)` until `--timeout` reaches zero. Default `--timeout`
600. On timeout, return last-known state envelope with `exit_code=124`,
`data.failure_class=null` (timeout is not a job failure; it's the wait
caller's wall budget), but top-level exit code 124 per ssh-core convention.

### D7. `list`

```python
def list_jobs(probe: bool) -> list[dict]:
    rows = []
    for path in sorted(Path("reports/jobs").glob("*.json")):
        try:
            job = json.loads(path.read_text())
        except json.JSONDecodeError:
            rows.append({"job_id": path.stem, "state": "corrupt"})
            continue
        row = {"job_id": job["job_id"], "server": job["server"],
               "started_at": job["started_at"],
               "command_excerpt": job["command_excerpt"],
               "state": "unknown",
               "mtime": path.stat().st_mtime}
        if probe:
            try:
                s = status(job["job_id"])
                row["state"] = s["state"]
                if "exit_code" in s:
                    row["exit_code"] = s["exit_code"]
            except Exception as e:
                row["state"] = f"probe_failed: {e}"
        rows.append(row)
    return rows
```

No concurrency in v1.0 — sequential probes. With 10 jobs × 300ms/probe =
3s; acceptable. v1.1 could thread-pool if users complain.

### D8. Failure-class enum (region 60-69 per cross_cutting §C5)

| exit_code | failure_class | When |
|---|---|---|
| 60 | `request_not_executable` | non-command operation OR (subclass `request_already_executed` — see D10) |
| 61 | `nohup_dispatch_failed` | ssh_execute non-zero during launch or mkdir |
| 62 | `pid_capture_failed` | stdout's last line not parseable as int |
| 63 | `remote_log_unreachable` | `tail` of log fails with non-zero on the remote |
| 64 | `job_vanished` | PID dead + no `__EXEC_DETACHED_EXIT__` sentinel |
| 65 | `job_state_corrupt` | local job json missing / unparseable / missing keys |
| 124 | `null` (timeout class deliberately not used for `wait`) | `wait --timeout` reached |
| 1 | (transport pass-through) | underlying ssh_execute non-zero we don't classify |

Notes: this region — six per-script strings — matches the precedent set by
ssh_server_transfer (12 strings @ 20-29) and deploy_pubkey (8 @ 10-15).
The exit-code → failure_class mapping is constant; the failure_class is
always present in `data.failure_class` (null on success).

### D9. Redaction

`tail-log` instantiates `lib.redact.Redactor()` once with the default
catalog path (`~/.devops/redact.json`, fallback to compiled-in defaults
per cross_cutting §C8). Per-line apply for both modes.

Memory analysis: `tail-log` default `--lines 200` ≈ ~50 KB worst case;
`--follow` is unbounded across time but bounded per line. The regex
catalog is compiled once. No issue at expected scales (single-host log
streams in the MB/hour range).

`--no-redact` bypasses entirely (does not even construct the Redactor).
Documented warning in `--help`: "raw output; do not paste into shared
transcripts".

### D10. Concurrency / duplicate-run resolution

Two simultaneous `exec_detached run --request same.json` calls race for
`<artifact-path>.lock` via `lib.lockfile` (cross_cutting §C6,
`fcntl.flock(LOCK_EX | LOCK_NB)`). First wins, writes the job state, exits
0. Second:

- If lock-not-acquired → exit 60 `request_not_executable` with
  `data.subclass="lock_held"` and `data.holding_pid` (best-effort).
- If lock acquired AFTER first finished but a job state file for the same
  `request_id` already exists → exit 60 with `data.subclass="request_already_executed"`
  and `data.existing_job_id`.

Decision (per task brief): **single exit code 60** for both subclasses,
distinguished by `data.subclass`. This avoids growing the 60-69 region
unnecessarily and matches how `ssh_server_transfer` packs related
failures under one head class (e.g. `mode_deferred` + `flag_deferred`
both exit 20/21 under the deferred-feature family).

Lookup for the "already executed" branch: glob
`reports/jobs/*.json`; scan for `request_id == artifact.request_id`. O(N)
in number of jobs known on this workstation. Cache not warranted at v1.0.

### D11. Test strategy (~20 unit + 3 live)

Unit tests (`tests/test_exec_detached_unit.py`):

| # | Test | Covers |
|---|---|---|
| 1 | `test_help_does_not_crash` | argparse |
| 2 | `test_run_rejects_non_command_operation` | D1 / exit 60 |
| 3 | `test_run_rejects_locked_artifact` | D10 / lock_held subclass |
| 4 | `test_run_rejects_duplicate_executed` | D10 / request_already_executed |
| 5 | `test_nohup_wrapper_template_escapes_single_quotes` | D2 |
| 6 | `test_pid_parser_handles_trailing_blank_line` | D2 |
| 7 | `test_pid_parser_rejects_non_integer` | D2 / exit 62 |
| 8 | `test_run_writes_job_state_atomically` | D3 |
| 9 | `test_run_command_excerpt_truncates_at_80` | D3 |
| 10 | `test_status_alive_returns_running` | D4 |
| 11 | `test_status_dead_with_sentinel_returns_exited` | D4 |
| 12 | `test_status_dead_no_sentinel_returns_vanished` | D4 / exit 64 |
| 13 | `test_status_corrupt_job_file_returns_65` | D4 / exit 65 |
| 14 | `test_status_log_unreachable_returns_63` | D4 / exit 63 |
| 15 | `test_tail_log_redacts_by_default` | D5 / D9 |
| 16 | `test_tail_log_no_redact_passes_through` | D9 |
| 17 | `test_wait_returns_124_on_timeout` | D6 |
| 18 | `test_wait_intervals_schedule_matches_design` | D6 |
| 19 | `test_list_offline_scan` | D7 |
| 20 | `test_list_probe_calls_status_per_job` | D7 |

ssh_execute / Popen / lib.* dependencies are mocked. Use `tmp_path` for
`reports/jobs/`.

Integration tests (`tests/test_exec_detached_live.py`, marker `live_ssh`):

| # | Test | Flow |
|---|---|---|
| 1 | `test_run_sleep_then_status_then_exited` | request artifact for `sleep 3; echo done` → run → assert state=running → wait → assert state=exited, exit_code=0 |
| 2 | `test_tail_log_fetches_command_output` | run `for i in 1 2 3; do echo line-$i; sleep 1; done` → tail-log → assert lines present |
| 3 | `test_duplicate_run_returns_60` | run once → run again → second exits 60 with subclass=request_already_executed |

Fixture: same pai host as ssh-core live tests; project_id="exec-detached-it";
cleanup removes `~/workspace/exec-detached-it/.exec-detached/` and local
`reports/jobs/*.json`.

## 3. Module layout (file diff)

```
plugins/ssh-guarded/skills/ssh-guarded/scripts/
├── exec_detached.py             (stub → ~350 loc)
└── lib/                          (delivered by sibling Child A)
    ├── request_artifact.py       (imported)
    ├── redact.py                 (imported)
    ├── lockfile.py               (imported)
    └── sandbox.py                (imported — used only for project_id resolution
                                   when artifact lacks one; rare)

tests/
├── test_exec_detached_unit.py    (NEW, ~20 tests)
└── test_exec_detached_live.py    (NEW, ~3 tests, marker live_ssh)
```

No changes to `ssh-core` scripts. No changes to `pyproject.toml` (no new
dependencies; reuses paramiko transitively via ssh-core but does not
import it directly — paramiko forbidden by ADR-001 D1).

## 4. Error mapping summary

| Source | exit | failure_class | stderr template |
|---|---|---|---|
| artifact.operation != "command" | 60 | request_not_executable | "exec_detached only runs operation='command'; got '<op>'" |
| lock_held | 60 | request_not_executable (subclass lock_held) | "another exec_detached run holds <path>.lock (pid <holder>)" |
| duplicate request_id in jobs/ | 60 | request_not_executable (subclass request_already_executed) | "request <id> already dispatched as job <job_id>" |
| ssh_execute non-zero on launcher | 61 | nohup_dispatch_failed | "nohup dispatch failed: <stderr first line>" |
| PID parse fail | 62 | pid_capture_failed | "expected integer PID on stdout, got: <last line>" |
| tail log non-zero | 63 | remote_log_unreachable | "remote log unreachable: <path> (<remote stderr>)" |
| PID dead + no sentinel | 64 | job_vanished | "PID <pid> dead and no exit sentinel in <log>" |
| local job json bad | 65 | job_state_corrupt | "job state file invalid: <path> (<reason>)" |
| wait timeout | 124 | null | "wait timed out after <N>s; last state: <state>" |

## 5. Compatibility notes

- Does **not** modify ssh-core scripts, `lib/sftp_transport.py`, or any ADR.
- Depends on the shipped Child A helpers in
  `plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/`:
  `request_artifact.py`, `redact.py`, `lockfile.py`, and `sandbox.py`.
  If implementation finds API drift, treat archived request-pipeline
  design §1 / current lib docstrings as the contract and update this
  child rather than copying helper code.
- SKILL.md decision tree (`SKILL.md:48-64` and `SKILL.md:130-147`) already
  documents the v1.0 surface accurately; no SKILL.md changes anticipated.

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| PID reassignment between launch and probe gives false `running` | Short job lifetimes + sentinel disambiguates dead case; v1.0 ships with this; mention in references/troubleshooting.md if Child A doesn't already |
| User command contains characters that defeat `'\''` escaping (newlines + heredocs) | v1.0 refuses with a clear message in unit-test coverage; v1.1 evaluates temp-script-on-remote pattern |
| Remote disk full → `nohup` log write fails silently mid-job | Status reports `vanished` (no sentinel ever written); user inspects manually. v1.1 could pre-flight `df -h` |
| Redact catalog regex bug → tail-log eats output | Redactor is shared with `run_request`; bug surfaces system-wide; catch via Child A's redact_check unit tests |
| Child A's lib API drifts from archived request-pipeline design §1 | Treat the archived contract and current lib docstrings as source of truth; patch this child rather than reimplementing shared helpers |
| `tail -F` SSH session leaks if user Ctrl-C'd in a way the trap missed | `Popen.terminate()` in `finally`; on macOS/Linux SIGTERM gives ssh a chance to close cleanly |
