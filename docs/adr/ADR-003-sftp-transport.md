# ADR-003: SFTP transport for ssh_upload / ssh_download

**Status**: Accepted (2026-05-24)
**Date**: 2026-05-24
**Source evidence**: `.trellis/tasks/05-24-ssh-core-ssh-upload/{prd,design,implement}.md`, surface docstring at `plugins/ssh-core/skills/ssh-core/scripts/ssh_upload.py:1-22`, SKILL.md "Move bytes" branch (`plugins/ssh-core/skills/ssh-core/SKILL.md:36-40`).

---

> **Recovery note (2026-11-10).** This file is the verbatim text of
> `1a6eade^:.trellis/spec/backend/adr-003-sftp-transport.md`. The blob was deleted in
> `1a6eade` when the personal Trellis workspace stopped being tracked; it
> is restored here because `docs/adr/` is where the code comments expect
> it. No decision text was rewritten.

## Context

`ssh_upload.py` and `ssh_download.py` are the two scaffold-era stubs (each returning `unimplemented()`) named under the SKILL.md "Move bytes" decision tree:

```
├─ Move bytes
│   ├─ Local → Remote → ssh_upload.py
│   ├─ Remote → Local → ssh_download.py
│   └─ Remote → Remote → ssh_server_transfer.py
```

The v1.0 surface docstring (`ssh_upload.py:1-22`) already pins seven flags and one specific resume protocol:

```
v1.0 surface:
    ssh_upload.py <alias> <local-path> <remote-path>
        [--resume] [--recursive] [--no-progress]
        [--chmod 0644] [--bwlimit KB/s] [--block-size 131072] [--json]

Implementation notes:
    - Default 128 KB block size, configurable via --block-size.
    - Progress callback prints every 5% to stderr unless --no-progress.
    - --resume:
        1. SFTP stat remote target
        2. If exists and size < local, seek both files to remote size
        3. Continue write
        4. After EOF, compare sha256 of both ends; mismatch → fail.
```

The four operations the surface implies — chunk-level seek, mid-file offset writes, byte-accurate progress callbacks, and end-to-end sha256 verification — are not implementable with the **subprocess + native ssh** primitive that ADR-001 standardized for `ssh_execute`:

- `scp` provides no resume / offset write API at all.
- `sftp(1)` shell-out can `reput`, but exposes neither byte-granular callbacks nor a verify hook.
- `rsync` would work but requires `rsync` on the remote (out of our control) and forks the transport stack: half of ssh-core would run over subprocess+ssh and half over subprocess+rsync, doubling the failure-class matrix.

This is **not a reversal of ADR-001**. ADR-001 D1 stated `subprocess + native ssh` is "the only implementation primitive for v1.0 [of `ssh_execute`]". `ssh_upload` / `ssh_download` are different scripts with a different transport profile (file I/O with progress + resume) and need their own primitive. `ssh_execute` v1.0 keeps its subprocess+ssh stack; the SFTP path is additive.

The decisions below are intended to be stable enough that **both** the ssh_upload v1.0 task and its sibling ssh_download v1.0 task can quote them. Anything not decided here is an explicit "deferred to v1.1" entry at the bottom.

---

## Decision

### D1. Implementation primitive

`paramiko` (>= 3.0, < 4) is the SFTP transport for `ssh_upload` and `ssh_download`.

- New runtime dependency: `paramiko>=3.0,<4` added to `[project] dependencies` in `pyproject.toml` (not an optional extra — SKILL.md treats upload / download as core capabilities, so making them optional is self-defeating).
- One SFTP session per script invocation; no daemon, no connection pool in v1.0.
- ssh-agent + key auth only (matches ADR-001 v1.0 scope); password / keyboard-interactive auth is deferred to v1.1 alongside the equivalent gap in `ssh_execute`.
- `paramiko.SSHConfig().parse(open(~/.ssh/config))` is the alias resolver, with `--ssh-config <path>` override threaded through unchanged. `ProxyJump` is supported via paramiko 3.x's `ProxyCommand` integration.

**Rationale**: only paramiko exposes the four primitives the surface docstring requires:
1. `sftp.open(remote, "r+b").seek(offset)` for mid-file resume continuation.
2. `SFTPFile.write(chunk)` chunk-by-chunk semantics (so we can implement bwlimit in the chunk loop).
3. A user callback fired on every transferred chunk for byte-accurate progress.
4. `sftp.stat(remote)` + `sftp.open(remote, "rb")` streaming reads so end-to-end sha256 verify is a few-line helper rather than a remote shell-out.

`sftp(1)` shell-out and `rsync` were both considered (see Alternatives) and rejected for the four-primitive checklist.

### D2. Transport module abstraction

A new module `plugins/ssh-core/skills/ssh-core/scripts/lib/sftp_transport.py` provides a **two-layer** abstraction:

**Low-level**: `class SftpSession`
- `__init__(alias, *, ssh_config=None)`, `__enter__` / `__exit__`, `close()`
- `stat(remote_path) -> paramiko.SFTPAttributes | None` — returns `None` on FileNotFoundError / errno 2.
- `mkdir_p(remote_dir, mode=0o755)` — idempotent (ignore errno 17 / 21).
- `chmod(remote_path, mode)` — straight passthrough.
- `put_chunked(local_path, remote_path, *, offset=0, block_size=131072, bwlimit_kbs=None, progress=None) -> int` — opens local `rb` + seeks to offset; opens remote `r+b` (resume) or `wb` (fresh) + seeks to offset; chunk loop with optional bwlimit throttle and progress callback. Returns bytes written this call.
- `get_chunked(remote_path, local_path, *, offset=0, ...) -> int` — symmetric mirror, consumed by `ssh_download`.

**High-level**: pure functions returning a shared `TransferResult` dataclass.
- `upload_file(session, local, remote, *, resume=False, chmod=None, block_size=131072, bwlimit_kbs=None, progress=True) -> TransferResult`
- `upload_tree(session, local_dir, remote_dir, *, ...) -> TransferResult`
- `download_file(session, remote, local, *, resume=False, block_size=131072, bwlimit_kbs=None, progress=True) -> TransferResult`
- `download_tree(session, remote_dir, local_dir, *, ...) -> TransferResult`

Scripts are **thin CLI wrappers** (~110 LOC each: argparse + two guard branches + one call into a high-level function + envelope construction + `emit`). The high-level layer carries all of the resume-state-machine logic, sha256 verify, and failed-files aggregation; the script never talks to paramiko directly.

**Rationale**:
- `ssh_download.py` is a 0-implementation-cost reuse of `download_file` / `download_tree` from the same module — the sibling task only writes a CLI shell.
- Future `ssh_server_transfer.py` "stream" mode (open one `get_chunked` to source and one `put_chunked` to destination, pipe between them) reuses `SftpSession` without re-implementing connection setup.
- Future `ssh_cluster.py` sync-mode primitives (when a real use case appears) can drive `upload_tree` once per host.
- The `TransferResult` dataclass is the unique extension point: every new field a future caller wants becomes an additive attribute, and the CLI shell's envelope-construction sees them automatically.

### D3. Exit code 9 — `transfer_verify_failed`

A new exit code **`9`** with `data.failure_class = "transfer_verify_failed"` is introduced for the case "the transfer ran to completion but the end-to-end sha256 of the bytes on the wire disagrees with the source".

- Triggered exclusively by the `--resume` verify step in `upload_file` / `download_file`.
- Distinct from `1` (generic failure): the operation actually finished, the bytes are sitting on disk, but they're wrong. A caller deciding whether to retry treats this differently from "the upload failed mid-flight" (1) — for `9` the right next step is "re-upload from scratch" rather than "retry the resume".
- Distinct from `8` (permission): no permission was denied; the I/O succeeded.
- Distinct from `124` (timeout): no timeout fired.

When mismatch is observed:
- `data.sha256_local` and `data.sha256_remote` are both populated (so the caller can log the divergence).
- `data.failure_class = "transfer_verify_failed"`.
- `stderr` carries `"sha256 verification failed: local=<a> remote=<b>"`.

This is reusable by any future transfer script (`ssh_server_transfer.py` direct mode, for instance) that performs end-to-end content verification on a completed transfer.

### D4. Verify is bound to `--resume` only; bwlimit unit is KB/s = 1024 B/s

Two smaller decisions that affect the implementation:

- **sha256 verify is `--resume`-only.** A from-scratch upload (no `--resume`) does not run sha256 verify and does not set `data.sha256_verified`. This matches `scp` semantics — `scp` does not verify either. The user-visible cost (in trust) is the standard trade-off scp users already accept; the upside is that the fast path (90%+ of invocations) skips the second full-file remote read. If verify-always becomes a real need, the surface gains a `--verify` flag in v1.1; the underlying `sha256_streaming_local` / `sha256_streaming_remote` helpers are already in place from D2.
- **`--bwlimit KB/s` uses 1 KB = 1024 B/s** (consistent with `rsync --bwlimit`'s long-standing convention). The throttle is implemented in `SftpSession.put_chunked` / `get_chunked` chunk-loop, so it's transparently inherited by `ssh_download`. sha256 verify reads are *not* throttled (verify is a metadata-pace step; throttling it would extend the user-perceived "stuck on verify" window for no useful pacing reason).

---

## Consequences

### Positive

- **Surface-driven, zero spec drift.** Every flag and resume behavior in the `ssh_upload.py` v0.2 surface docstring maps 1:1 onto a paramiko primitive; nothing in the docstring needs to be retracted when v1.0 lands.
- **`ssh_download` v1.0 task is mostly free.** Sibling task implements `download_file` + `download_tree` bodies (mirrors of the upload variants) and a 110-LOC CLI shell. Connection setup, bwlimit, sha256 helpers, and exit-code wiring are already paid for.
- **Cross-script transport invariant established.** Any future "move bytes" script can adopt `SftpSession` rather than re-implementing OpenSSH-config parsing + agent auth + ProxyJump.

### Negative / accepted trade-offs

- **Runtime dependency surface grows.** `paramiko` pulls in `cryptography` (and transitively `cffi`, `bcrypt`, `pynacl`); the install footprint adds ~10 MB. `ssh_execute` v1.0 (and ssh_cluster, ssh_config_manager) does not import paramiko, so they keep zero-dep status. The pyproject change is one line and constrained (`>=3.0,<4`) so cryptography ABI breakage is contained.
- **Two transport stacks in ssh-core.** `ssh_execute` runs over subprocess+native-ssh; `ssh_upload` / `ssh_download` run over paramiko-SFTP. This is intentional (different primitives, different failure modes) but doubles the "how do I debug a connect failure?" surface. We mitigate by reusing `~/.ssh/config` as single-source-of-truth so both stacks resolve aliases identically.
- **New exit code requires `error-handling.md` update.** The same kind of incremental cost ADR-001 and ADR-002 both paid (`124`, `4–8`). Updated in lockstep with this ADR.
- **paramiko `AutoAddPolicy` chosen for v1.0 host key handling.** `load_system_host_keys()` is loaded first so known hosts match strict; unknown hosts are auto-added rather than refused. This matches the default user expectation set by `ssh -o StrictHostKeyChecking=accept-new`, but does mean the first connection to an unknown host trusts the presented key. Documented in the `SftpSession` class docstring; v1.1 may revisit if a fleet-rotation scenario emerges.

### Downstream impact (other plugins / future tasks)

- **`ssh_download.py` v1.0 task** consumes `from lib.sftp_transport import SftpSession, download_file, download_tree` and writes a ~110-LOC CLI shell. No transport code in the script itself.
- **`ssh_server_transfer.py`** (planned) can reuse `SftpSession` for "direct" mode (remote → SFTP → local → SFTP → remote staging) and "stream" mode (cross-session piping).
- **`ssh_cluster.py` sync-mode** (planned, post-v1.0) can fan out `upload_tree` per host.
- **`ssh_execute.py` is not affected.** No imports change; the subprocess+ssh transport is preserved verbatim.
- **`tests/test_manifests.py` `--help` smoke** must still pass for `ssh_upload.py` and `ssh_download.py` after their v1.0 rewrites; this is verified by both v1.0 tasks at their phase-D2 import-check gate.

---

## Alternatives considered

### A1. `subprocess` + `sftp(1)` shell-out

Rejected. `sftp(1)` interactive scripting can `reput` (resume upload) and `reget` (resume download), but:
- No chunk-granular progress callback — sftp prints its own progress to its stderr in a format that varies across OpenSSH builds.
- No end-to-end verify hook — would need a second remote shell-out to compute sha256 on the destination, splitting the transfer + verify into two transports.
- Argv would carry `local <path>` / `put <path> <remote>` sub-commands as multi-line stdin, brittle to filename quoting.

### A2. `subprocess` + `rsync`

Rejected. `rsync --partial --inplace` is the best-in-class resume primitive in the Unix world, but:
- Requires `rsync` on the **remote** side. Not always present on minimal images / managed hosts.
- Different transport stack from ssh_execute (`rsync -e ssh ...`) — same alias resolves to two different bytecode paths.
- Out-of-band metadata (`--info=progress2`) format is not stable enough to parse for our 5%-bucket progress callback.

### A3. paramiko + custom SFTP daemon (long-lived)

Rejected for v1.0. The single-shot model is simpler and matches the v1.0 scope of `ssh_execute` (one subprocess per call). Connection-pool optimization is a v1.1 follow-up tracked symmetrically with the ControlMaster work for `ssh_execute`.

### A4. Extending ADR-001 to "subprocess + ssh OR paramiko, route by feature"

Rejected. ADR-001's value is being a single primitive decision ("we use subprocess+ssh for command execution"). Routing-by-feature would require every reader of `ssh_execute` to also reason about which path their call goes down. Separating the SFTP-transport ADR keeps each ADR scoped to one tool's surface.

### A5. Verify-always (sha256 even on from-scratch upload)

Rejected for v1.0. From-scratch uploads pay a 2x remote-read cost (the upload itself + the verify read). This is the conservative-default that loses 90% of users the fast path; the trade-off matches scp's deliberate choice. A future `--verify` flag can opt in without breaking the v1.0 surface.

---

## Open questions / deferred to v1.1

- **ControlMaster / connection reuse** for the underlying SSH transport that paramiko opens. Symmetric with the v1.1 follow-up to ADR-001; the right time to do both is when the first bulk-transfer use case quantifies the per-call handshake cost.
- **`--preserve-times` / `--preserve-owner` recursive flags.** v1.0 ships scp-like recursive semantics (no metadata preservation, source mode copied, `--chmod` overrides). Add when a real "I need rsync-a" report comes in.
- **`--exclude` / `--include` filtering.** Not in the v1.0 surface; out of scope until requested.
- **Compression (`-z` analog).** paramiko has no native compression hook on the SFTP channel; would require either a custom transport wrapper or remote `gzip` shell-out. Defer.
- **Password / keyboard-interactive auth.** Symmetric with the v1.1 follow-up to ADR-001; both transports will gain it together.
- **Concurrent / parallel transfers within `upload_tree`.** v1.0 walks the tree sequentially. Threaded fan-out is the obvious v1.1 improvement once a 1000-file benchmark exists to justify the contention-failure-mode work.

---

## Cross-references

- `plugins/ssh-core/skills/ssh-core/scripts/ssh_upload.py` — v1.0 implementation (CLI shell).
- `plugins/ssh-core/skills/ssh-core/scripts/ssh_download.py` — sibling v1.0 implementation (planned).
- `plugins/ssh-core/skills/ssh-core/scripts/lib/sftp_transport.py` — shared transport module (introduced by this ADR).
- `.trellis/spec/backend/error-handling.md` — exit code `9` and `failure_class = "transfer_verify_failed"` added in lockstep with this ADR.
- `.trellis/spec/backend/adr-001-ssh-execute.md` — sibling ADR for the subprocess+ssh stack used by `ssh_execute`.
- `.trellis/spec/backend/adr-002-ssh-config-manager.md` — sibling ADR for the `~/.ssh/config` CRUD layer this ADR's `SftpSession` consumes.
- `.trellis/tasks/05-24-ssh-core-ssh-upload/{prd,design,implement}.md` — source planning artifacts.
