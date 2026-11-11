# ADR-001: ssh_execute v1.0 implementation

**Status**: Accepted (2026-05-21)
**Date**: 2026-05-21
**Source evidence**: `.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/findings.md`, `.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/questions.md`, `.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/spike-code/results/*.txt`

---

> **Recovery note (2026-11-10).** This file is the verbatim text of
> `1a6eade^:.trellis/spec/backend/adr-001-ssh-execute.md`. The blob was deleted in
> `1a6eade` when the personal Trellis workspace stopped being tracked; it
> is restored here because `docs/adr/` is where the code comments expect
> it. No decision text was rewritten.

## Context

`ssh_execute.py` is the front door for `ssh-core` and, transitively, for every v0.2 diagnostic plugin that needs to run a command on a remote host. Before this ADR, the script existed only as a stub (`plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py:1-49`, returning `unimplemented()`). The CONTRIBUTING.md roadmap (line 43) described intended v1.0 behavior in one line: *"daemon-aware front door; native ssh for key auth, paramiko fallback for password"*.

The PoC validated that **the simpler version of that line — native ssh + key auth, no daemon, no paramiko — is sufficient for v1.0**. Six hypotheses CONFIRMED, one REFINED, all four open questions answered with evidence.

The decisions in this ADR are intended to be **stable enough that the v1.0 implementation task can quote them directly**. Anything not decided here is an explicit "follow-up gap" listed in `findings.md`.

---

## Decision

### D1. Implementation primitive

`subprocess.run(["ssh", ...argv, host, command], capture_output=True, text=True, timeout=N)` is the **only** implementation primitive for v1.0.

- argv is **always a list**; `shell=True` is forbidden (matches `quality-guidelines.md:92` and `error-handling.md:54-58`).
- No paramiko, no custom socket protocol, no daemon.
- One subprocess per call. Connection reuse (ControlMaster or `ssh_daemon.py`) is a v1.1 optimization (see Consequences).

### D2. Required ssh argv flags

Every invocation includes:

| Flag | Purpose |
|------|---------|
| `-o BatchMode=yes` | Prevent ssh from prompting for passwords / passphrases (causes silent hang in non-TTY). |
| `-o ConnectTimeout=N` | Bound network establishment time. Default `N=8`; `--connect-timeout` flag overrides. |
| `-F <config>` | Optional. If `--ssh-config <path>` is passed; otherwise `~/.ssh/config` is used (ssh default). |

Other flags pass through via a `--ssh-opt KEY=VALUE` accumulator (deferred to v1.1 unless a use case forces it earlier).

### D3. JSON envelope

Top-level structure unchanged from v0.2 contract:

```json
{
  "success": <bool>,
  "exit_code": <int>,
  "stdout": "<str>",
  "stderr": "<str>",
  "data": { ... }
}
```

**Semantics**:
- `exit_code` is **always the remote command's exit code** (validated by H1). For ssh-client-level failures (DNS, auth), `exit_code` is whatever ssh itself returned (255 in practice). The `data.failure_class` field disambiguates.
- `stdout` and `stderr` are the remote command's streams; ssh-client-level warnings (known_hosts noise) are filtered into `data.ssh_noise_lines`.

**`data` payload** (additive — callers must tolerate unknown keys):

| Key | Type | Meaning |
|-----|------|---------|
| `route` | `"local"` \| `"remote"` | Routing decision |
| `failure_class` | `null` \| `"network"` \| `"auth"` \| `"remote_error"` \| `"timeout"` | Classification (D5) |
| `ssh_argv` | `list[str]` | Exact argv passed to subprocess. For reproduction. |
| `elapsed_s` | `float` | Wall-clock duration. |
| `ssh_noise_lines` | `list[str]` | Filtered SSH-client stderr lines (D4) |
| `raw_stderr` | `str` | Verbatim original stderr, for audit |

### D4. Stderr noise filtering

A small registry of known SSH-client warning patterns is filtered out of top-level `stderr` and preserved in `data.ssh_noise_lines`. v1.0 ships at minimum:

```
^Warning: Permanently added .* to the list of known hosts\.$
```

**Default policy: fail-open.** Any stderr line that does not match a known pattern stays in top-level `stderr`. Never silently drop unknown warnings. The library of patterns grows over time; the default protects against silently swallowing real errors.

### D5. Failure classification

After subprocess completion, classify according to:

```python
if returncode == 0:
    failure_class = None
elif raised subprocess.TimeoutExpired:
    failure_class = "timeout"
elif returncode == 255 and stderr matches network regex:
    failure_class = "network"
elif returncode == 255 and stderr matches auth regex:
    failure_class = "auth"
else:
    failure_class = "remote_error"
```

The `auth` and `network` regexes must be **scoped to `returncode == 255`** to prevent false positives from remote commands that legitimately produce "Permission denied" output (e.g. `cat /etc/shadow` as a non-root user).

### D6. Timeout

- Implementation: `subprocess.run(..., timeout=args.timeout)`.
- On `subprocess.TimeoutExpired`: emit envelope with `exit_code=124`, `data.failure_class="timeout"`, `data.elapsed_s == args.timeout`, `stderr="timed out after Ns"`.
- The choice of `124` matches GNU `timeout(1)` convention. **This requires a spec update to `error-handling.md`** which currently lists only `{0, 1, 2}` as allowed (see Consequences).

### D7. `local` route policy

When `host == "local"`, bypass ssh and run via `subprocess.run(shlex.split(command))`. **No `shell=True`.**

Shell metacharacters (`|`, `>`, `<`, `;`, `&&`, `||`, `` ` ``, `$(`) are treated as literal arguments and will produce semantic surprises (H6-5 evidence). To prevent silent misbehavior:

- v1.0 detects these characters in the command string **before** invoking subprocess.
- On detection, the script **refuses with a clear error**: `"local route does not support shell metacharacters: '|' '>' '<' ';' '&&' '||'. Use 'bash -c \"...\"' to opt into a shell."`
- Users who want shell features pass `bash -c 'their command'` explicitly. This makes the intent visible in argv.

### D8. Local invocation transparency

When `--json` is **not** passed, output is raw passthrough (stdout → stdout, stderr → stderr, no prefixes), as already implemented by `lib/emit()`. v1.0 ssh_execute calls `emit(result, as_json=args.json)` without modification.

---

## Scenario: ssh_execute ControlMaster opt-in (V1.1)

### 1. Scope / Trigger

- Trigger: V1.1 adds the first connection-reuse slice to `ssh_execute.py`.
- Scope: CLI flags, remote argv construction, ControlPath derivation, local-route behavior, and unit-test assertions.
- Non-scope: custom Python `ssh_daemon.py`, password / keyboard-interactive auth, default-on pooling, and applying reuse to sibling scripts.

### 2. Signatures

```bash
ssh_execute.py <host> "<command>" \
  [--timeout N] [--connect-timeout N] [--ssh-config PATH] \
  [--control-master off|auto] [--control-persist SECONDS] \
  [--control-path-dir PATH] [--json]
```

- `--control-master` defaults to `off`.
- `--control-persist` defaults to `600`.
- `--control-path-dir` is optional and exists for tests/debugging; without it, use `<tmp>/ssh_controlmaster/`.

### 3. Contracts

- Default remote argv is unchanged from V1.0:
  `ssh -o BatchMode=yes -o ConnectTimeout=N [-F config] <host> <command>`.
- When `--control-master auto` is used on a remote route, append OpenSSH options before `<host> <command>`:
  `-o ControlMaster=auto -o ControlPersist=<seconds> -o ControlPath=<path>`.
- Local route ignores ControlMaster flags and keeps the D7 local-route contract.
- `data.ssh_argv` still records the exact argv passed to subprocess.
- ControlPath is deterministic and does not leak raw aliases or config paths:
  `<base>/<sha256(host + "\0" + ssh_config_or_empty)[:24]>`.
- The ControlPath base directory is created with mode `0o700`.

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| `--control-master off` or omitted | Emit V1.0 argv exactly. |
| `--control-master auto` on remote route | Add `ControlMaster`, `ControlPersist`, and hashed `ControlPath` options. |
| `--control-master auto` on local route | Ignore the flag; local command handling is unchanged. |
| Same host + same ssh config | Produce the same ControlPath digest. |
| Same host + different ssh config | Produce a different ControlPath digest. |

### 5. Good/Base/Bad Cases

- Good: repeated remote calls use `--control-master auto` and OpenSSH reuses a live master socket.
- Base: ordinary callers omit the flag and get byte-for-byte V1.0 argv.
- Bad: a custom daemon is started by `ssh_execute.py`; this violates the V1.1 first-slice boundary.

### 6. Tests Required

- `ssh_execute.py --help` lists `--control-master`, `--control-persist`, and `--control-path-dir`.
- Unit tests assert default remote argv with and without `--ssh-config` is unchanged.
- Unit tests assert `--control-master auto` adds all three OpenSSH options.
- Unit tests assert ControlPath is deterministic, hash-only, and uses a private directory.
- Parser dispatch tests assert default values and explicit values passed to `_run_remote`.

### 7. Wrong vs Correct

#### Wrong

```text
/tmp/ssh_controlmaster/prod-web-01.sock
```

This leaks the alias in a world-browsable temp tree and risks Unix-domain socket length issues.

#### Correct

```text
<tmp>/ssh_controlmaster/1f0e8d0c4a2b7c9e3d5f6a01
```

The basename is short, deterministic, and derived from `sha256(host + "\0" + ssh_config_or_empty)`.

---

## Consequences

### Positive

- **Simple, single-file v1.0 implementation.** Estimated 150-200 lines of Python (the spike is 150 already, mostly reusable).
- **No new package dependencies.** paramiko deferred; nothing else needed.
- **Spec already covers most of this.** `error-handling.md` and `quality-guidelines.md` only need exit_code `124` added.
- **PoC code is largely promotable** — once promoted, it shares the `lib/json_result` and `lib/emit` helpers, deletes its redundant copies, and follows the v0.2 contract verbatim.

### Negative / accepted trade-offs

- **No connection pooling in v1.0.** Each call pays ~200-300 ms for SSH handshake. Acceptable for diagnostic plugins (one-shot commands). Real bulk operations (`ssh_cluster`) will feel this. v1.1 introduces ControlMaster (likely simpler than building `ssh_daemon.py` from scratch).
- **No password / keyboard-interactive auth.** Users with password-only targets must arrange key auth or use `sshpass` themselves.
- **Local route refuses shell metacharacters.** Slightly less ergonomic than typing `local "ls | head"`, but eliminates the silent-misbehavior class.
- **Auth-class false positives possible** if a remote command produces `Permission denied` in its own stderr at the same time ssh returns 255. Mitigated by scoping the regex to `returncode == 255`, but corner cases remain. v1.0 ships with this; if reports surface, tighten.
- **exit_code=124 requires spec update.** Single line in `error-handling.md`. Cost is trivial; benefit is `timeout(1)` compatibility.

### Downstream impact (other plugins)

Every v0.2 diagnostic plugin (`k8s-debug`, `docker-quick`, `log-aggregator`, `iac-state`, `cicd-debug`, `remote-debug`) shells out to `ssh-core` CLIs (CONTRIBUTING.md:29). This ADR confirms they can rely on:

- `ssh_execute.py <host> <command> --json` returning the v0.2 contract envelope.
- `exit_code` being the remote command's exit code.
- `data.failure_class` for branching on failure mode (e.g. "auth failed → tell user to fix config, network failed → suggest port check").

Plugins **do not need** to invoke ssh themselves. They invoke `python ssh_execute.py ...` and parse the JSON.

---

## Alternatives considered

### A1. paramiko-first v1.0

Rejected. Adds dependency, security surface, and complexity for the rare password-auth case. Native ssh covers the common case. paramiko deferred to v1.1.

### A2. Custom daemon protocol from day 1 (`ssh_daemon.py` v1.0)

Rejected. PoC confirms subprocess works correctly. Daemon adds: protocol design, crash recovery, port conflicts, IPC complexity. v1.0 ships without; v1.1 evaluates OpenSSH ControlMaster (zero Python, free reliability) before considering a custom daemon.

### A3. `local` route with `shell=True`

Rejected as default. Violates the "argv list, never shell strings" rule (CONTRIBUTING.md:37, `error-handling.md:54-58`). May be reconsidered if `local` use cases turn out to be common shell-pipeline scenarios; the explicit `bash -c '...'` escape hatch keeps the door open.

### A4. Top-level `remote_exit_code` separate from `local_exit_code`

Rejected. PoC shows ssh transparently propagates the remote exit code (H1), so there's no distinction to expose at top level. ssh-client-level failure is captured by `failure_class` in `data`. Adding a 6th top-level field would break the v0.2 contract for no benefit.

---

## Open questions / deferred to v1.0 task

These are NOT decided here; the v1.0 implementation task should answer them:

- Exact size cap for streams (spec mentions 10 MB; need to implement).
- Default `--timeout` value (PoC used 15s; 120s is more forgiving for slow commands).
- Whether `--ssh-opt KEY=VALUE` accumulator ships in v1.0 or v1.1.
- Naming: `--connect-timeout` vs `--ssh-timeout` vs `--timeout` for distinguishing connect vs total budget.
