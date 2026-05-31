# Implementation Plan — ssh-guarded exec_detached v1.0

> Companion artifacts: `prd.md`, `design.md`, `implement.jsonl`.
> Progress note: user approved implementation on 2026-05-31 and the active
> Trellis task was switched to
> `.trellis/tasks/05-25-ssh-guarded-exec-detached`.

## 0. Pre-start Gate

- [x] Confirm user approval to move from planning to implementation.
- [x] Switch the active task:
  `python3 .trellis/scripts/task.py start .trellis/tasks/05-25-ssh-guarded-exec-detached`
- [x] Load Phase 2 implementation context via `trellis-before-dev`.
- [x] Re-check `git status --short` and preserve unrelated cicd-debug worktree
  changes.
- [x] Verify shipped dependencies exist:
  `plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/{request_artifact.py,redact.py,lockfile.py,sandbox.py}`.

## 1. API + Envelope Foundation

- [ ] Replace `exec_detached.py` scaffold with a real CLI while preserving
  `--help` success for the top-level parser and all subcommands.
- [ ] Add subcommand flags from PRD R1:
  `run --request`, `status <job_id>`, `tail-log <job_id> --lines --follow --no-redact`,
  `wait <job_id> --timeout`, `list --probe`, plus global `--json`.
- [ ] Implement ssh-core-shaped result helpers:
  `{success, exit_code, stdout, stderr, data}` with
  `data.failure_class` always present (`None` on success).
- [ ] Keep human output short and grep-friendly; write errors to stderr and
  normal results to stdout. Do not import `logging`.
- [ ] Before coding exit-code constants, update or reconcile
  `.trellis/spec/backend/error-handling.md` because the current spec lists
  `150`-range cicd-debug codes but does not yet list ssh-guarded `40`-`79`.

## 2. Job State Primitives

- [ ] Define constants for:
  `SCRIPTS_DIR`, `REPORTS_DIR`, `JOBS_DIR`, `SSH_EXECUTE`, default tail lines
  `200`, default wait timeout `600`, and sentinel
  `__EXEC_DETACHED_EXIT__=`.
- [ ] Implement `JobState` loading/validation for `reports/jobs/<job_id>.json`.
  Missing JSON, invalid JSON, or missing required keys returns exit `65`
  `job_state_corrupt`.
- [ ] Implement atomic job-state write with `mkstemp`/`os.replace`, mode
  `0o600`, and no updates after creation.
- [ ] Implement duplicate request scan over `reports/jobs/*.json` by
  `request_id`; tolerate corrupt unrelated files by skipping them in this
  path.
- [ ] Implement command excerpt helper: first 80 chars plus `...` when
  truncated; keep ASCII punctuation in new code.

## 3. SSH Dispatch Helpers

- [ ] Implement a thin `_ssh_execute(server, command, timeout=120)` helper
  that shells out to:
  `sys.executable plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py <server> <command> --json`.
- [ ] Parse the child JSON envelope defensively. Non-JSON or missing keys
  maps to the caller's current failure class (`nohup_dispatch_failed` for
  launch, `remote_log_unreachable` for tail).
- [ ] Keep subprocess argv as lists. No `shell=True`.
- [ ] Implement direct streaming SSH argv for `tail-log --follow`:
  `ssh -o BatchMode=yes -o ConnectTimeout=8 <server> tail -F -n <N> <log>`.
- [ ] Use `shlex.quote` for remote path arguments and `posixpath` for remote
  path joins.

## 4. `run --request`

- [ ] Load artifact with `RequestArtifact.load(Path(args.request))`.
- [ ] Validate expiration/server/sandbox by calling `art.validate()` with
  `project_id=resolve_project_id(...)`; this avoids detached execution of
  stale or out-of-sandbox command artifacts.
- [ ] Reject `operation != "command"` with exit `60`
  `request_not_executable`.
- [ ] Acquire `exclusive_lock(request_path)`. Lock contention returns exit
  `60`, `failure_class=request_not_executable`, `data.subclass="lock_held"`,
  and best-effort `data.holding_pid`.
- [ ] After lock acquisition, scan existing jobs for the same `request_id`;
  duplicate returns exit `60`, `data.subclass="request_already_executed"`,
  and `data.existing_job_id`.
- [ ] Generate `job_id = uuid4().hex[:8]`.
- [ ] Resolve `cwd_on_remote` from `payload.workdir` or default to
  `~/workspace/<project_id>`.
- [ ] Compose remote log path:
  `<cwd_on_remote>/.exec-detached/<job_id>.log`.
- [ ] Build nohup wrapper:
  create log dir, run `nohup bash -c '<command>; echo sentinel=$? >> log'`
  with stdout/stderr redirected to the log, background it, then echo `$!`.
- [ ] Parse last non-empty stdout line as an integer remote PID; failure exits
  `62` `pid_capture_failed`.
- [ ] Persist job state and emit `job_id`, `remote_pid`, `remote_log_path`,
  `server`, and `started_at`.

## 5. `status <job_id>`

- [ ] Load local job state.
- [ ] Probe `kill -0 <remote_pid> 2>/dev/null && echo alive || echo dead`
  through `ssh_execute.py`.
- [ ] If stdout is `alive`, emit `state="running"`.
- [ ] If not alive, tail the last 5 remote log lines.
- [ ] Tail failure returns exit `63` `remote_log_unreachable`.
- [ ] Sentinel present returns `state="exited"` and the parsed command exit
  code.
- [ ] Dead PID with no sentinel returns exit `64` `job_vanished`.

## 6. `tail-log <job_id>`

- [ ] Non-follow mode: run remote `tail -n <lines> <log>` through
  `ssh_execute.py`.
- [ ] Default redaction: instantiate `Redactor()` lazily and apply it line by
  line.
- [ ] `--no-redact` bypasses redactor construction and prints raw log text.
- [ ] `--json` non-follow mode returns `data.lines` as a list.
- [ ] Follow mode streams line-buffered stdout; on `KeyboardInterrupt`,
  terminate the ssh process, flush, and return 0.
- [ ] Decide during implementation whether `--json --follow` is refused
  clearly or streams JSON lines; recommendation: refuse with exit `1`
  because the PRD only defines JSON data for non-follow.

## 7. `wait <job_id>`

- [ ] Implement interval generator from design D6:
  1s for the first 30 polls, 5s until 5 minutes, then 15s, never exceeding
  30s.
- [ ] Poll status until `state in {"exited", "vanished"}` or timeout.
- [ ] On timeout, return process exit `124`, `data.failure_class=None`, and
  include last-known state.
- [ ] Unit-test without real sleeping by monkeypatching `time.sleep` and
  status calls.

## 8. `list [--probe]`

- [ ] Scan `reports/jobs/*.json` sorted by path.
- [ ] Offline mode returns `state="unknown"`, mtime, and job metadata.
- [ ] Corrupt job files produce a row with `state="corrupt"` rather than
  aborting the whole list.
- [ ] `--probe` sequentially calls status logic for each valid job and folds
  `state` / `exit_code` / probe failure into the row.

## 9. Tests

- [ ] Add `tests/test_exec_detached_unit.py` with the 20 cases listed in
  design D11.
- [ ] Mirror ssh-guarded import isolation from
  `tests/test_ssh_guarded_run_request_unit.py` because both ssh-core and
  ssh-guarded expose a top-level `lib` package.
- [ ] Mock `subprocess.run`, `subprocess.Popen`, `exclusive_lock`, and
  `time.sleep`; use `tmp_path` for reports/jobs.
- [ ] Add `tests/test_exec_detached_live.py` with `pytest.mark.live_ssh`,
  self-contained `POC_HOST`/ssh-agent reachability guards, and remote cleanup.
- [ ] Append exec_detached unit and live coverage to `tests/README.md`.

## 10. Validation Commands

- [ ] `python plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py --help`
- [ ] `python plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py run --help`
- [ ] `python plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py status --help`
- [ ] `python plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py tail-log --help`
- [ ] `python plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py wait --help`
- [ ] `python plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py list --help`
- [ ] `pytest tests/test_exec_detached_unit.py -q`
- [ ] `pytest tests/test_exec_detached_live.py -m live_ssh -q`
- [ ] `pytest tests/test_manifests.py -q`
- [ ] `ruff check plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py tests/test_exec_detached_*.py`
- [ ] `mypy plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py`

## 11. Risky Files and Rollback Points

Risky implementation files:

- `plugins/ssh-guarded/skills/ssh-guarded/scripts/exec_detached.py`
- `tests/test_exec_detached_unit.py`
- `tests/test_exec_detached_live.py`
- `tests/README.md`
- `.trellis/spec/backend/error-handling.md` if the missing 60-79
  namespace is corrected during implementation.

Rollback notes:

- If the CLI implementation becomes unstable, restore only
  `exec_detached.py` and keep tests/planning artifacts for the next pass.
- If live tests reveal pai-specific timing flake, keep unit coverage and
  mark the live case with an explicit skip condition rather than weakening
  command semantics.
- Do not modify ssh-core internals or `lib/sftp_transport.py`; transport
  changes belong in separate tasks.

## 12. Open Decision Before Coding

The current `exec_detached` PRD requires the ssh-core envelope
`{success, exit_code, stdout, stderr, data}`, while the archived
ssh-guarded request-pipeline contract and current drafters use
`{success, data, error}`. Recommendation: implement `exec_detached` with
the ssh-core envelope because `error-handling.md` and this PRD explicitly
require it, and because detached commands are transport-style operations
that callers will chain like `ssh_execute.py`.
