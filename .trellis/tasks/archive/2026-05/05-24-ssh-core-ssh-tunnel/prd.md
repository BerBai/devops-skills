# ship ssh-core ssh_tunnel v1.0

## Goal

(per stub `scripts/ssh_tunnel.py:1-20` + workflows.md §4 + SKILL.md L42 + L73-74)

Managed local→remote port forwards.

v1.0 surface (stub-locked, behavior collapses where indicated):

```
ssh_tunnel.py start <alias>
                   --remote-port N
                   [--local-port N]              from pool 10000-20000 if omitted
                   [--remote-host host]          default: localhost (forward target on dst side)
                   [--bind-address addr]         default: 127.0.0.1
                   [--idle-timeout N]            ACCEPTED BUT NO-OP in v1.0 (see D2)
                   [--connect-timeout N]         default: 8 seconds (mirrors ssh_execute)
ssh_tunnel.py stop <alias> [--port N]
ssh_tunnel.py status <alias>
ssh_tunnel.py list
[--json on all subcommands]
```

## Confirmed facts (from code / spec)

- **No existing detached-process pattern in shipped ssh-core code** (`grep fork|setsid|daemonize|Popen|start_new_session` returns zero implementation hits across the scripts dir). ssh_tunnel introduces it.
- **ssh_daemon v1.0 is deferred** (per `05-24-ssh-core-ssh-daemon` PRD verdict). No wire protocol available to reuse; ssh_tunnel ships standalone.
- **`lib.alias_state_path(alias, kind="ssh_tunnel")`** returns `$TMPDIR/ssh_tunnel/<md5(alias)>.json` — alias-keyed. ssh_tunnel needs `(alias, port)`-keyed state, so we compute `md5(f"{alias}-{local_port}")` ourselves and reuse the directory (mode 0o700 already managed by `alias_state_path`).
- **SKILL.md L107 + workflows.md §4 promise `--idle-timeout` exits the tunnel after N seconds** — this gets documented as v1.1 deferred (D2).
- **`subprocess.Popen(start_new_session=True)` is Python's standard way to detach a child from the parent's session** — calls `os.setsid()` in the child before `exec`. After parent exit, child becomes adopted by init (PID 1) and is not killed by SIGHUP. POSIX-only.
- **OpenSSH `-fN -L` is the textbook self-detach forward**, but `-f` makes ssh fork its own daemon after auth — the parent-ssh PID is NOT the daemon PID, and OpenSSH does not print the daemon PID to stderr. Recovering the daemon PID requires `lsof -iTCP:<port> -sTCP:LISTEN -t` or `pgrep -f`. **We avoid this by using Python-side `start_new_session=True` instead of `-f`** so `Popen.pid` IS the detached ssh PID.
- **ssh `ExitOnForwardFailure=yes` makes ssh exit immediately if the forward setup fails** (e.g. local port in use, remote refuses). Without it, ssh stays running but does nothing useful — bad for us. We always pass it.
- **`os.kill(pid, 0)` is the POSIX-portable "is process alive?" probe** (returns without error if alive, raises `ProcessLookupError` if dead).
- **No `--no-detach` in stub**, but useful for debug. Add as `--no-detach` flag in `start`: when set, run ssh in foreground (parent doesn't return until ssh exits / Ctrl-C). Internal-use; not in human-facing surface documentation.

## Design decisions (locked)

### D1. Detach mechanism: Python `start_new_session=True`, not OpenSSH `-f`

```python
proc = subprocess.Popen(
    [
        "ssh",
        "-N",                                       # no remote command
        "-L", f"{bind_address}:{local_port}:{remote_host}:{remote_port}",
        "-o", "ExitOnForwardFailure=yes",           # die fast on bind failure
        "-o", "BatchMode=yes",                      # no auth prompts
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=60",             # detect dead masters
        "-o", "ServerAliveCountMax=3",
        alias,
    ],
    start_new_session=True,                         # os.setsid() in child → survives parent exit
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,                         # parent reads briefly to detect early failure
    env=msys_safe_env(),
)
```

- `Popen.pid` is the detached ssh PID. No `lsof` / `pgrep` lookup needed.
- Parent waits up to `connect_timeout + 1` seconds for ssh to either:
  - exit non-zero (forward setup failed) → read stderr, classify failure, return error
  - stay alive → assume forward established, close stderr pipe (child inherits FD; nothing reads it after parent exits — kernel pipe buffers eventually drop writes to /dev/null behavior), write state file, parent exits 0
- POSIX-only — `start_new_session` only works on Unix. Windows: argparse parses; `start` subcommand fails with `failure_class="posix_only"`.

### D2. `--idle-timeout` is accepted but no-op in v1.0

- Argparse keeps `--idle-timeout N` (default 1800) for surface stability.
- When `start` succeeds, `data.flag_deferred=["--idle-timeout"]` is set + a stderr line warns: *"--idle-timeout is no-op in v1.0; tunnel will run until you call `stop` or kill the PID. v1.1 will implement idle exit (likely via ControlMaster integration)."*
- SKILL.md L107 and workflows.md §4 sentences about "exits after --idle-timeout" get edited to "v1.0: tunnel runs until `stop`; v1.1 will reintroduce idle exit". This is in the AC.

### D3. Local port allocation

- If `--local-port N` is passed: use N. If bind fails (port in use), fail with `failure_class="port_busy"`.
- If `--local-port` omitted: try `random.randint(10000, 20000)` up to 5 times, probing with a transient `socket.bind(("127.0.0.1", port))`-close round-trip. First success wins. All 5 fail → `failure_class="port_pool_exhausted"`.
- The probe-bind is racy with the ssh subprocess that comes right after, but `ExitOnForwardFailure=yes` catches the race honestly.

### D4. State file layout

Path: `$TMPDIR/ssh_tunnel/<md5(f"{alias}-{local_port}")>.json` mode 0o600.

Content (matches stub):
```json
{
  "pid": int,
  "local_port": int,
  "remote_host": str,
  "remote_port": int,
  "bind_address": str,
  "alias": str,
  "started_at": "<iso8601>"
}
```

Writes are best-effort atomic: write to `.tmp` + `os.rename`. (Concurrency between two `start` calls on the same (alias, port) is structurally avoided because the port-bind in D3 only one of them can succeed.)

### D5. `stop` semantics

- `stop <alias>` with `--port N`: locate state file for `(alias, N)`. If missing → `failure_class="tunnel_not_found"`. If present: `os.kill(pid, SIGTERM)`, wait up to 3 s, then `SIGKILL` if still alive. Remove state file regardless of kill outcome (avoid stale state outliving us). Emit `data.ports_stopped=[N]`.
- `stop <alias>` with `--port` omitted:
  - Zero state files → `tunnel_not_found`.
  - One state file → stop it (same as above with discovered port).
  - Multiple state files → `failure_class="ambiguous_stop"`, message lists existing ports + suggests `--port`.

### D6. `status` and `list`

- `status <alias>`: scan `$TMPDIR/ssh_tunnel/` (read each `*.json`, filter by `alias`). For each, probe `os.kill(pid, 0)`; tag entries `running=true/false`. Emit `data.forwards=[…]`. Does **not** auto-cleanup stale entries — that's `stop`'s job (avoids surprising concurrent removers).
- `list`: same scan, no alias filter.

### D7. failure_class taxonomy + exit codes

| `failure_class` | Trigger | exit_code |
|---|---|---|
| `null` | success | 0 |
| `posix_only` | running on Windows / no `os.setsid` | 30 |
| `port_busy` | explicit `--local-port` already bound | 31 |
| `port_pool_exhausted` | 5 retries on 10000-20000 all failed | 32 |
| `ssh_connect_failed` | ssh subprocess exited non-zero within `connect_timeout + 1` (network / DNS / refused) | 33 |
| `auth` | ssh stderr matches auth-failed regex (per `lib.classify_failure` AUTH_PATTERN) | 34 |
| `forward_setup_failed` | ssh exited due to `ExitOnForwardFailure` (port-forward refused remote-side, perms, etc.) | 35 |
| `tunnel_not_found` | `stop` / `status` and no matching state file | 36 |
| `ambiguous_stop` | `stop` with multiple forwards for alias and no `--port` | 37 |
| `stale_state_only` | `stop` found state file but pid dead — state removed, exit non-zero so callers see it | 38 |
| `kill_failed` | `os.kill(pid, SIGTERM)` then `SIGKILL` both failed (very rare, e.g. permission) | 39 |
| `timeout` | wall-clock timeout | 124 |
| `remote_error` | catch-all unexpected | 1 |

Pin exit codes in implement.md after grep-confirming no conflict with `error-handling.md`.

### D8. `--no-detach` flag (debug ergonomics)

Add to `start` subcommand. When set:
- skip `start_new_session=True` (ssh runs as child of current shell)
- skip writing state file (no detachment → no state semantics)
- parent process `Popen.wait()` until ssh exits / SIGINT
- exit code = ssh's exit code
- emit nothing to JSON (or `data.no_detach=true` if `--json` was also passed — but human use is the dominant case)

This is THE smoke-test invocation for debugging: `python ssh_tunnel.py start pai --remote-port 22 --no-detach` — you immediately see ssh's stderr if anything's wrong.

### D9. CLI surface (final)

```
start    <alias> --remote-port N
                 [--local-port N]
                 [--remote-host HOST]
                 [--bind-address ADDR]
                 [--idle-timeout N]      v1.0 no-op + warns
                 [--connect-timeout N]   default 8
                 [--no-detach]           debug; foreground
                 [--json]

stop     <alias> [--port N]              optional if exactly one forward for alias
                 [--json]

status   <alias>
                 [--json]

list
                 [--json]
```

No `--ssh-config` in v1.0 (ssh reads `~/.ssh/config` by default; thread through if a user reports needing it).

### D10. JSON envelope `data` payloads

**start** (success):
```json
{
  "alias": "...", "pid": int, "local_port": int,
  "remote_host": "...", "remote_port": int, "bind_address": "...",
  "state_file": "/tmp/ssh_tunnel/<digest>.json",
  "started_at": "<iso8601>",
  "flag_deferred": ["--idle-timeout"],          // only if user set it
  "failure_class": null
}
```

**stop** (success):
```json
{
  "alias": "...", "ports_stopped": [int, ...],
  "state_files_removed": ["/tmp/ssh_tunnel/...", ...],
  "failure_class": null
}
```

**status** / **list**:
```json
{
  "forwards": [
    { "alias", "pid", "local_port", "remote_host", "remote_port",
      "bind_address", "started_at", "running": bool, "state_file": "..." },
    …
  ],
  "failure_class": null
}
```

Human mode: one-line per forward (`<alias>:<local_port> → <remote_host>:<remote_port> (pid <pid>, running)`).

### D11. Test strategy

- **Unit (`test_ssh_tunnel_unit.py`)** — primary coverage. Mock `subprocess.Popen`, `socket.socket().bind`, `os.kill`, `os.rename`, `time.sleep`. Assert:
  - argv construction for all flag combinations of `start`.
  - Port allocator (D3): 5 retries, all fail → port_pool_exhausted; first success wins.
  - State file IO (D4): write/read round-trip preserves all fields.
  - `stop` with explicit/implicit `--port`, multi-tunnel ambiguity (D5).
  - `status` PID-alive probe (D6).
  - Each failure_class in D7 triggered by the right exception / argv combo.
  - Posix-only refusal on Windows (mock `sys.platform`).
  - Target ≥ 90% statement coverage.
- **Integration (`test_ssh_tunnel_integration.py`, `live_ssh` mark)** — uses POC_HOST=pai; each test uses try/finally to guarantee cleanup:
  - `start pai --remote-port 22 --local-port=<random>`, verify port listening locally, verify `ssh -p <local_port> 127.0.0.1 'echo ok'` returns ok (forwards to pai:22 = pai itself), `stop`, verify state file removed and port no longer listening.
  - `start --no-detach` lifecycle: start in subprocess with `--no-detach`, after a couple of seconds kill it, assert exit code reflects SIGTERM.
  - `status` with no tunnels → empty forwards list.
  - `stop pai` with no running tunnel → `tunnel_not_found`.
  - `--idle-timeout 5` is accepted but tunnel still alive at 10 s (proving D2 no-op behavior).
  - Cleanup is paramount: a fixture wraps every test in `try: … finally: ssh_tunnel.py stop pai` so a crashing test never leaks a tunnel.

## Requirements

1. CLI surface per D9; argparse `prog="ssh_tunnel.py"` + subparsers.
2. `subprocess.Popen(start_new_session=True, ...)` for detach (D1). No `os.fork`, no third-party daemon lib.
3. Port allocator per D3; state file IO per D4.
4. JSON envelope per D10; failure_class strings per D7.
5. `lib.msys_safe_env()` on the ssh spawn (consistency with the rest of ssh-core).
6. ruff + mypy clean.
7. Unit + integration tests per D11.
8. SKILL.md L107 + workflows.md §4 doc sync per D2.

## Acceptance Criteria

- [ ] `ssh_tunnel.py` no longer a stub: docstring says `(v1.0)`, no `unimplemented()`, loc in 250-400 range.
- [ ] `python ssh_tunnel.py --help` and each subcommand `--help` show the v1.0 surface and exit 0.
- [ ] All four subcommands (`start/stop/status/list`) implemented per D5-D6.
- [ ] `--idle-timeout` accepted, warns to stderr, sets `data.flag_deferred` (D2).
- [ ] `--no-detach` works for debugging (D8).
- [ ] State files written atomically to `$TMPDIR/ssh_tunnel/`, mode 0o600 (D4).
- [ ] `tests/test_ssh_tunnel_unit.py` ≥ 90% statement coverage, every failure_class in D7 exercised.
- [ ] `tests/test_ssh_tunnel_integration.py` `pytest.mark.live_ssh`; 5 cases per D11 pass against POC_HOST=pai. Cleanup fixture guarantees no leaked tunnels.
- [ ] `pytest tests/` default suite green.
- [ ] `pytest tests/ -m live_ssh -k ssh_tunnel -v` green.
- [ ] `ruff check plugins/ssh-core` + `mypy plugins/ssh-core/.../ssh_tunnel.py` clean.
- [ ] Exit codes (D7) verified non-conflicting against latest `error-handling.md`.
- [ ] **SKILL.md L107 + workflows.md §4** doc-synced: "v1.0: tunnel runs until `stop` is called; `--idle-timeout` is v1.1 deferred (likely landing with ControlMaster)."
- [ ] **Parent v1-rollout/prd.md Notes** has a 2026-05-25 line: "ssh_tunnel v1.0 ship Path B (`-N -L` + Python setsid detach); `--idle-timeout` deferred to v1.1 alongside ssh_daemon/ControlMaster."

## Out of scope

- Reverse tunnels (`ssh -R`). Defer.
- Dynamic SOCKS proxy (`ssh -D`). Defer.
- UNIX domain socket forwards. Defer.
- ControlMaster multi-forward sharing (v1.1; will likely replace the `start_new_session=True + Popen` approach with `ssh -O forward` against a persistent master, and re-light `--idle-timeout` at that point).
- Connection-aware idle (the IB option from baseline). v1.1+ if requested.
- Bandwidth limiting on the tunnel.
- Windows support (`start_new_session=True` is POSIX-only; explicit failure with `posix_only`).
- ssh_daemon integration (deferred).
- Auto-restart on connection drop (out of scope; SSH `ServerAliveInterval` keeps the connection probed but does not auto-reconnect on failure; document that the tunnel exits if SSH drops).
- TLS-wrapped tunnel (`ssh -L` is TLS-via-SSH already; "extra" wrapping is out of scope).

## Notes

- This is the v1.0 ssh-core script with the smallest delta from the stub surface but the **largest semantic delta** (idle exit promise is being held back). The doc sync (SKILL.md / workflows.md) is part of the work — done in the same commit as the implementation.
- The `start_new_session=True` detach pattern is the canonical Python way to start a daemon child while keeping `Popen.pid`. We document it in the source-file docstring so future readers (and Windows porters in v1.1) understand the choice.
