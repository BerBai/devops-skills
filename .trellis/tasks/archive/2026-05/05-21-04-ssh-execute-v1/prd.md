# ssh_execute v1.0 + diagnose_host end-to-end

## Goal

Land the first **real** (non-stub) implementations in the ssh-core /
remote-debug stack, validating that the shell-out architecture
(`remote-debug` Python invokes `ssh-core` CLI via subprocess) works in
practice. Three deliverables in one task because they are sequentially
dependent and individually too thin to justify separate Trellis overhead:

1. **`ssh-core/scripts/lib`** gains the reusable `filter_ssh_noise` and
   `classify_failure` helpers extracted from the PoC.
2. **`ssh-core/.../ssh_execute.py`** ships v1.0 per
   [`ADR-001`](../../spec/backend/adr-001-ssh-execute.md): `subprocess.run`
   + native ssh, JSON envelope, 4 failure classes, `local` route with
   shell-metacharacter refusal.
3. **`remote-debug/.../diagnose_host.py`** ships v1.0: invokes
   `ssh_execute.py` as a subprocess, runs uptime/load/disk/mem/zombie
   probes against the target host, emits a single JSON envelope with a
   severity score.

The end-to-end run that proves the architecture:
`python3 diagnose_host.py pai --json` produces a real health snapshot,
having shelled out to `ssh_execute.py pai '<probe>'` under the hood,
across N probes.

## Why now

`ssh-core` is the dependency root for 6 v0.2 diagnostic plugins. Until
one of them works end-to-end, the "diagnostic plugin shells out to
ssh-core CLI" claim in CONTRIBUTING.md:29 is untested. This task is the
proof. It also produces the reusable lib helpers that the remaining 5
diagnostic plugins (and `ssh_cluster`, `ssh_upload`, `ssh_download`)
will need when they ship.

## Background

- [`ADR-001`](../../spec/backend/adr-001-ssh-execute.md) locks the
  ssh_execute v1.0 implementation contract. This task does NOT relitigate
  those decisions; it implements them.
- The PoC spike code at
  `.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/spike-code/`
  is reference material — the v1.0 code is written fresh, but the
  control flow, helper function signatures, and regex sets carry over.
- Worktree `worktree-poc-ssh-execute` is still on disk (per user choice
  to keep it) and is a valid reference; v1.0 work happens on `main`, not
  in that worktree.
- Test host: `pai` alias (Debian 13, root, ED25519 key in ssh-agent),
  same as PoC. `/tmp/poc-ssh-config` is the scratch config preserved
  from the PoC and can be reused.

## Requirements

### R1 — Extend `ssh-core/scripts/lib/__init__.py`

Add two helpers next to the existing `json_result` / `emit` /
`msys_safe_env` / `alias_state_path` / `unimplemented`:

- `filter_ssh_noise(stderr: str) -> tuple[str, list[str]]` — split
  captured stderr into `(real_stderr, ssh_client_noise_lines)`. Initial
  pattern set: known_hosts "Permanently added" warning. Fail-open: any
  line not matching a known pattern stays in `real_stderr`.
- `classify_failure(returncode: int, stderr: str) -> str | None` — return
  one of `None` / `"network"` / `"auth"` / `"remote_error"` / `"timeout"`.
  Auth/network regexes are scoped to `returncode == 255` to avoid false
  positives from remote commands that legitimately emit "Permission
  denied" in their own stderr.

Both helpers are added to the "Stable surface" docstring at the top of
the file.

### R2 — Implement `ssh-core/.../ssh_execute.py` v1.0

Replace the stub body with a real implementation. Public surface
unchanged (preserves v0.2 interface contract from CONTRIBUTING.md):

```
python ssh_execute.py <host> "<command>" \
    [--timeout N] [--connect-timeout N] [--ssh-config PATH] [--json]
```

Behavior per ADR-001 (D1-D8). Specifically:

- argv list, never shell strings.
- `-o BatchMode=yes -o ConnectTimeout=N` on every ssh call.
- `host == "local"`: `shlex.split` + `subprocess.run`. Detect shell
  metacharacters (`|`, `>`, `<`, `;`, `&&`, `||`, `` ` ``, `$(`) before
  splitting and refuse with a clear error message. Users wanting shell
  semantics pass `bash -c '...'` explicitly.
- `host != "local"`: `subprocess.run(["ssh", *opts, host, command],
  capture_output=True, text=True, timeout=N)`.
- Top-level `exit_code` = remote command exit code (255 if ssh itself
  failed).
- `data.failure_class` populated via `lib.classify_failure`.
- `data.ssh_noise_lines` populated via `lib.filter_ssh_noise`.
- `data.raw_stderr` preserves original.
- Timeout fires `exit_code=124`, `failure_class="timeout"`.
- JSON / text output via existing `lib.emit`.

### R3 — Implement `remote-debug/.../diagnose_host.py` v1.0

Replace the stub. Public surface (preserving v0.2 argv):

```
python diagnose_host.py <host> [--json] [--check uptime,load,disk,mem,zombie]
```

Behavior:

- For each probe, shell out to ssh-core's `ssh_execute.py`:
  `python <ssh-core-script-path>/ssh_execute.py <host> '<probe cmd>' --json`
- Probes (initial, all read-only):
  - `uptime`: `uptime`
  - `load`: `cat /proc/loadavg`
  - `disk`: `df -P /` (single-line POSIX format)
  - `mem`: `free -m | head -2 | tail -1`
  - `zombie`: `ps -eo stat | grep -c '^Z'`
- Parse each probe's JSON envelope. Aggregate into:
  - `data.probes`: `{name: {success, exit_code, stdout, parsed: {...}}}`
  - `data.severity`: `"ok" | "warn" | "crit"` based on simple thresholds
    (load > cores: warn; load > 2x cores: crit; disk > 80%: warn;
    disk > 95%: crit; zombies > 5: warn).
- Single top-level JSON envelope. `success=True` iff all probes returned
  exit 0 AND severity != crit.
- `--check uptime,load` runs only the listed probes (CSV).

Locating `ssh_execute.py`: use `__file__`-relative path to
`../../../../ssh-core/skills/ssh-core/scripts/ssh_execute.py`. Match the
"shell out, don't import" rule from CONTRIBUTING.md:29. If the file
isn't found, emit envelope with `failure_class="precondition"` and a
clear stderr message.

### R4 — Update spec for exit_code=124 and shell-out path

- `.trellis/spec/backend/error-handling.md` already lists 124 (done by
  the previous task). This task verifies the new ssh_execute behavior
  matches that spec by adding example usage to the spec — TBD if
  promoted; do NOT touch the spec file in this task unless adding the
  example explicitly approved.

### R5 — Tests

Two layers, per the agreed test strategy:

- **Unit tests** (always run): mock `subprocess.run` and assert envelope
  shape, classify_failure correctness, filter_ssh_noise behavior, local
  route metacharacter detection, ssh_execute path computation in
  diagnose_host. Files:
  - `tests/test_ssh_core_lib.py` — `filter_ssh_noise`, `classify_failure`
  - `tests/test_ssh_execute_unit.py` — argparse, local route refusal,
    envelope shape, mock-driven failure-class branches
  - `tests/test_diagnose_host_unit.py` — probe orchestration logic,
    severity scoring thresholds, missing-script handling
- **Integration tests** (opt-in via pytest marker `live_ssh`): actual
  ssh to `pai` alias, end-to-end `diagnose_host.py pai --json`. Skipped
  by default; CI does not run them. Files:
  - `tests/test_ssh_execute_integration.py` — uses `pai`; H1-H6 from PoC
    re-run as automated tests against current code
  - `tests/test_diagnose_host_integration.py` — `diagnose_host.py pai`
    produces non-empty probes and ok/warn severity

Wire `live_ssh` marker into `pyproject.toml` `[tool.pytest.ini_options]`
so `pytest -m "not live_ssh"` (the default) cleanly skips them.

## Constraints

- **ADR-001 is authoritative.** Decisions in the ADR are not revisited
  here. If implementation forces a decision change, stop and update the
  ADR first (separate task).
- **v0.2 argv interface preserved.** Existing argparse surface
  (positional `host`, `command`; flags listed in R2) must stay
  backwards-compatible. Adding flags is allowed; removing or renaming
  is not.
- **No paramiko.** Per ADR-001 D1 and Q4 deferral.
- **No daemon.** Per ADR-001 D1.
- **Shell-out across plugins, never import.** `diagnose_host.py` invokes
  `ssh_execute.py` as a subprocess (CONTRIBUTING.md:29). No
  `from ssh_core import ...` anywhere.
- **JSON output contract** (`error-handling.md`) preserved on every
  script in every mode.
- **No new top-level files** outside `tests/` and the targeted
  `plugins/*/.../scripts/*.py` paths. No new dependencies in
  `pyproject.toml` beyond existing dev tools.

## Acceptance Criteria

### Behavioral

- [x] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py pai 'true' --json`
      returns `success=True`, `exit_code=0`, `data.failure_class=null`.
- [x] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py pai 'false' --json`
      returns `success=False`, `exit_code=1`, `data.failure_class="remote_error"`.
- [x] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py nonexistent.invalid.local 'uptime' --json`
      returns `success=False`, `exit_code=255`, `data.failure_class="network"`.
- [x] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py local 'echo hi'` prints `hi`.
- [x] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py local 'ls /tmp | head'`
      refuses with a stderr message naming the offending metacharacter and `exit_code=1`.
- [x] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py pai 'sleep 60' --timeout 3 --json`
      returns `exit_code=124`, `data.failure_class="timeout"` within ~4s.
- [x] `python3 plugins/remote-debug/skills/remote-debug/scripts/diagnose_host.py pai --json`
      returns `success=True` (assuming pai is healthy), with `data.probes`
      containing keys `uptime/load/disk/mem/zombie`, each with `success=True`.

### Test gate

- [x] `pytest tests/ -m "not live_ssh"` passes (the default-CI suite),
      including the existing 63 manifest tests plus the new unit tests.
- [x] `pytest tests/ -m live_ssh` passes when run locally against `pai`.
      Document in `tests/README.md` (create if absent) how to enable.

### Quality gate

- [x] `ruff check plugins/ssh-core/ plugins/remote-debug/ tests/` returns
      0 new violations (the 10 pre-existing I001 warnings remain — out of scope).
- [x] No `from <other-plugin> import ...` lines anywhere
      (`grep -rE "from (ssh_core|remote_debug|k8s_debug|docker_quick|log_aggregator|iac_state|cicd_debug)" plugins/`
      returns 0 hits).
- [x] No `shell=True` in any modified or new file.
- [x] All four modified script files (`lib/__init__.py`, `ssh_execute.py`,
      `diagnose_host.py`, and at least one test file) still pass
      `--help` (the `test_script_help` parametric test in
      `tests/test_manifests.py`).

### Contract preservation

- [x] `ssh_execute.py --help` still lists positional `host` and `command`,
      flags include at least `--timeout`, `--ssh-config`, `--json`.
- [x] `diagnose_host.py --help` still lists positional `host` and flag
      `--json`.
- [x] No plugin imports another plugin's Python (`grep` check above).

## Out of scope

- `ssh_daemon.py` / ControlMaster — v1.1.
- paramiko / password auth — v1.1.
- Windows MSYS behavior — separate spike + task.
- Large-stdout (>10 MB) handling — known gap in `findings.md`, deferred.
- `--ssh-opt KEY=VALUE` passthrough — v1.1.
- The other 5 diagnostic plugins (`k8s-debug`, `docker-quick`, etc.) —
  separate tasks, will follow the same shell-out pattern.
- Promoting the spike code into a shared util — the spike stays archived;
  this task writes fresh code against ADR-001.
- Updating CONTRIBUTING.md roadmap to mark ssh_execute / diagnose_host
  as "implemented" — small follow-up after this task lands.

## Risk register

| Risk | Mitigation |
|------|------------|
| Mocking subprocess+ssh leaves gaps the integration tests don't catch | Run live_ssh suite manually before marking AC done. Document any divergence in `tests/README.md`. |
| `pai` becomes unreachable mid-task | Use `local` route as fallback verification; integration tests can switch to `localhost` if user enables sshd on macOS. |
| `diagnose_host` shell-out path discovery is fragile (relative `__file__`) | Document the assumed layout; v1.0 ships with absolute resolution + clear error; v1.1 could use plugin manifest metadata. |
| Hidden v0.2 callers depend on stub `exit_code=2` "unimplemented" behavior | None in repo (verified by grep); external Claude Code callers should treat 2 as "not implemented yet" not "broken" — behavior change is the point of v1.0. |

## Notes

This is the first task where the user-facing CLI behavior changes (stubs
go away). Expect to iterate on small details once the live_ssh tests
expose corner cases. Time budget: ~1 day, broken into phases in
`implement.md`.
