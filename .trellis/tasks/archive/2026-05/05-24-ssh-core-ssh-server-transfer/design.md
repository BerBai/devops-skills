# Design — ssh_server_transfer v1.0

## Architecture overview

```
                  ┌──────────────────────────────────────┐
CLI args ───────▶ │ argparse.build_parser                │
                  └────────┬─────────────────────────────┘
                           │
                           ▼
                  ┌──────────────────────────────────────┐
                  │ _route_mode(args)                    │
                  │   --mode direct  → mode_deferred     │
                  │   --use-rsync    → flag_deferred     │
                  │   --mode auto    → stream            │
                  │   --mode hybrid  → stream            │
                  │   --mode stream  → stream            │
                  └────────┬─────────────────────────────┘
                           │ (only if stream)
                           ▼
                  ┌──────────────────────────────────────┐
                  │ Open two SftpSessions (lib.sftp_     │
                  │ transport.SftpSession) under one     │
                  │ ExitStack so both close cleanly.     │
                  │                                      │
                  │ src_session = SftpSession(src_alias) │
                  │ dst_session = SftpSession(dst_alias) │
                  └────────┬─────────────────────────────┘
                           │
                           ▼
                  ┌──────────────────────────────────────┐
                  │ Branch on src_stat.S_ISDIR + flags   │
                  │                                      │
                  │   single file       → _pipe_file()   │
                  │   tree + --recursive → _pipe_tree()  │
                  │   src is dir + no -r → src_is_dir    │
                  └────────┬─────────────────────────────┘
                           │
                           ▼
                  ┌──────────────────────────────────────┐
                  │ Build envelope (success or failure)  │
                  │ via _success_envelope /              │
                  │ _failure_envelope                    │
                  └────────┬─────────────────────────────┘
                           │
                           ▼
                  ┌──────────────────────────────────────┐
                  │ lib.emit(result, as_json=args.json)  │
                  └──────────────────────────────────────┘
```

Single-process, single-syscall-tree, no daemon, no state files. The transfer is **synchronous** with two open SFTP sessions for its duration; sessions close on `main()` return.

## Module structure

```python
# ssh_server_transfer.py
def build_parser() -> argparse.ArgumentParser: ...

# routing
def _route_mode(args) -> dict | None:
    """Return a failure envelope if mode is deferred/refused; None to proceed."""

# pipe primitives
def _pipe_file(src_session, src_path, dst_session, dst_path, *,
               block_size: int, bwlimit_kbs: int | None,
               start_time: float, running_bytes: int) -> int:
    """Stream one file src→dst chunk-by-chunk. Returns bytes transferred.
    `start_time` + `running_bytes` are threaded in so bwlimit pacing stays
    accurate across multiple file calls (tree walk)."""

def _pipe_tree(src_session, src_root, dst_session, dst_root, *,
               block_size: int, bwlimit_kbs: int | None) -> tuple[int, int]:
    """Recursive: walk src_root, pipe each file. Returns (files, bytes)."""

# helpers
def _stat_or_none(session, path): ...
def _ensure_dst_parent(dst_session, dst_path): ...
def _classify_paramiko_failure(exc, *, which_side: str) -> str:
    """Map paramiko exception → failure_class. `which_side` is 'src' or 'dst'
    so we can pick between network_src vs network_dst etc."""

# envelopes
def _success_envelope(args, mode_used, bytes_total, files, elapsed, extra=None) -> dict
def _failure_envelope(args, fclass, partial_bytes=0, partial_files=0,
                      elapsed=0.0, hint="") -> dict

# main
def main(argv=None) -> int: ...
```

~12 functions; ~350-450 LOC total including the docstring + argparse blob + envelope wiring.

## Stream-mode pipe primitive

```python
def _pipe_file(src_session, src_path, dst_session, dst_path, *,
               block_size, bwlimit_kbs, start_time, running_bytes):
    src_attr = src_session.sftp.stat(src_path)
    total = int(src_attr.st_size or 0)
    bytes_done = 0
    target_bps = bwlimit_kbs * 1024 if bwlimit_kbs else None

    _ensure_dst_parent(dst_session, dst_path)

    with src_session.sftp.open(src_path, "rb") as src_fp, \
         dst_session.sftp.open(dst_path, "wb") as dst_fp:
        src_fp.prefetch()             # paramiko: read-ahead on the src side
        dst_fp.set_pipelined(True)    # paramiko: pipelined writes on dst
        while True:
            chunk = src_fp.read(block_size)
            if not chunk:
                break
            dst_fp.write(chunk)
            bytes_done += len(chunk)
            if target_bps:
                expected = (running_bytes + bytes_done) / target_bps
                actual = time.monotonic() - start_time
                if expected > actual:
                    time.sleep(expected - actual)
    return bytes_done
```

Notes:
- **`prefetch()` + `set_pipelined()`** are paramiko-specific perf knobs already exercised in `lib/sftp_transport.put_chunked` / `get_chunked`; reuse the same pattern for consistency.
- **bwlimit threading**: `running_bytes` and `start_time` come from the *caller* so a tree-walk with bwlimit doesn't reset the pacing window between files. Single-file callers pass `running_bytes=0, start_time=time.monotonic()`.
- **No resume / no sha256 verify** in v1.0 stream mode (PRD D4): the dst file is opened `"wb"` (truncate). Resume is v1.1, mirroring `sftp_transport`'s `--resume`-only verify rule (ADR-003 D4).
- **`dst_fp.close()` happens via the `with`** — paramiko flushes on close. If the connection drops mid-write, paramiko raises; the script catches it as `transfer_io`.

## Tree walk

```python
def _pipe_tree(src_session, src_root, dst_session, dst_root, *,
               block_size, bwlimit_kbs):
    files = 0
    total_bytes = 0
    start_time = time.monotonic()

    src_session.sftp.lstat(src_root)                 # validates src exists
    dst_session.mkdir_p(dst_root)

    # Iterative BFS over src tree; preserve relative paths into dst.
    stack = [src_root]
    while stack:
        src_dir = stack.pop()
        rel_dir = posixpath.relpath(src_dir, src_root)
        dst_dir = dst_root if rel_dir == "." else posixpath.join(dst_root, rel_dir)
        dst_session.mkdir_p(dst_dir)

        for entry in src_session.sftp.listdir_attr(src_dir):
            src_p = posixpath.join(src_dir, entry.filename)
            dst_p = posixpath.join(dst_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                stack.append(src_p)
            elif stat.S_ISREG(entry.st_mode):
                bytes_done = _pipe_file(
                    src_session, src_p, dst_session, dst_p,
                    block_size=block_size, bwlimit_kbs=bwlimit_kbs,
                    start_time=start_time, running_bytes=total_bytes,
                )
                files += 1
                total_bytes += bytes_done
            # else: symlink / device / fifo — skip silently in v1.0 (matches scp -r default-ish)

    return files, total_bytes
```

- Iterative (not recursive) to avoid stack-depth issues on deep trees
- `posixpath` not `os.path` — remote paths are always POSIX even when local is Windows
- Symlinks / specials skipped silently in v1.0; document in the script docstring + this design's "Out of scope"

## Mode routing

```python
def _route_mode(args) -> dict | None:
    if args.use_rsync:
        return _failure_envelope(args, fclass="flag_deferred",
            hint="--use-rsync requires --mode direct, which is deferred to v1.1")
    if args.mode == "direct":
        return _failure_envelope(args, fclass="mode_deferred",
            hint="--mode direct is deferred to v1.1; use --mode stream")
    # auto / hybrid / stream all proceed to the stream path
    return None
```

`data.auto_resolved_to` / `data.hybrid_resolved_to` markers are added in `_success_envelope` based on the **original** `args.mode` so the audit trail tells callers "you asked for auto, you got stream."

## Failure-class taxonomy (per PRD D5)

Per-script namespace; do not extend `lib.classify_failure`'s 4-class set.

Classification by paramiko exception type + `which_side` (src or dst):

| Exception (or condition) | which_side | `failure_class` | exit_code |
|---|---|---|---|
| `paramiko.AuthenticationException` raised by `SftpSession.__init__` for src | src | `auth_src` | 24 |
| Same, for dst | dst | `auth_dst` | 25 |
| `socket.gaierror` / `ConnectionRefusedError` / `socket.timeout` from src open | src | `network_src` | 22 |
| Same, from dst open | dst | `network_dst` | 23 |
| `_route_mode` returns `mode_deferred` | — | `mode_deferred` | 20 |
| `_route_mode` returns `flag_deferred` | — | `flag_deferred` | 21 |
| `src_session.sftp.stat(src)` raises FileNotFoundError | src | `src_path_missing` | 26 |
| Src is a directory but `--recursive` not set (caught via stat S_ISDIR) | src | `src_is_directory` | 27 |
| `OSError` writing to dst (permission, parent missing, fs full) before pipe begins | dst | `dst_path_invalid` | 28 |
| `OSError` / paramiko IO mid-pipe (connection drop, transient) | — | `transfer_io` | 29 |
| Wall-clock timeout fires (only if a `--timeout` is later added; v1.0 has none → unreachable) | — | `timeout` | 124 |
| Any other unexpected exception | — | `remote_error` | 1 |

**Exit codes 20-29 must be verified non-conflicting** vs `error-handling.md` during implement.md Phase 0 (current allocation: 0/1/2 universal, 4-9 ssh_config_manager + sftp_transport, 10-15 deploy_pubkey, 124 ssh_execute timeout). 20-29 is fresh.

**Classification helper** uses `which_side` parameter explicitly because the same exception type means different things depending on which `SftpSession.__init__` raised it:

```python
def _classify_paramiko_failure(exc, *, which_side):
    if isinstance(exc, paramiko.AuthenticationException):
        return "auth_src" if which_side == "src" else "auth_dst"
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError, socket.timeout)):
        return "network_src" if which_side == "src" else "network_dst"
    if isinstance(exc, paramiko.SSHException):
        # auth or transport hiccup we couldn't disambiguate
        return "auth_src" if which_side == "src" else "auth_dst"
    return "remote_error"
```

## JSON envelope `data` payload (per PRD D8)

```python
def _success_envelope(args, mode_used, bytes_total, files, elapsed):
    data = {
        "mode_used": mode_used,                   # always "stream" in v1.0
        "bytes": bytes_total,
        "files_transferred": files,
        "elapsed_s": round(elapsed, 3),
        "src": f"{args.src_alias}:{args.src_path}",
        "dst": f"{args.dst_alias}:{args.dst_path}",
        "failure_class": None,
    }
    if args.mode == "auto":
        data["auto_resolved_to"] = "stream"
    if args.mode == "hybrid":
        data["hybrid_resolved_to"] = "stream"
    return json_result(success=True, exit_code=0, stdout="", stderr="", data=data)

def _failure_envelope(args, fclass, *, partial_bytes=0, partial_files=0,
                     elapsed=0.0, hint=""):
    data = {
        "mode_used": "stream" if args.mode != "direct" else None,
        "bytes": partial_bytes,
        "files_transferred": partial_files,
        "elapsed_s": round(elapsed, 3),
        "src": f"{args.src_alias}:{args.src_path}",
        "dst": f"{args.dst_alias}:{args.dst_path}",
        "failure_class": fclass,
    }
    return json_result(success=False, exit_code=_EXIT_CODES[fclass],
                       stdout="", stderr=hint, data=data)
```

## Human-mode summary

Success:
```
transferred 7 file(s), 142.5 MiB in 11.3s via stream (pai:/tmp/src → pai:/tmp/dst)
```

Failure: failure_class + hint on stderr, one line each.

| fclass | hint |
|---|---|
| `mode_deferred` | `--mode direct is deferred to v1.1; use --mode stream` |
| `flag_deferred` | `--use-rsync requires --mode direct (v1.1); drop the flag` |
| `network_src` | `cannot connect to source alias {src_alias}` |
| `network_dst` | `cannot connect to destination alias {dst_alias}` |
| `auth_src` / `auth_dst` | `authentication failed against {src_alias|dst_alias}` |
| `src_path_missing` | `source path does not exist on {src_alias}: {src_path}` |
| `src_is_directory` | `source is a directory on {src_alias}; pass --recursive` |
| `dst_path_invalid` | `cannot write to destination on {dst_alias}: {dst_path} (check parent exists / permissions)` |
| `transfer_io` | `transfer interrupted after {partial_bytes} bytes: {short_exc}` |
| `remote_error` | `unexpected error: {short_exc}` |

## Test strategy

### Unit (`tests/test_ssh_server_transfer_unit.py`)

Mock `lib.sftp_transport.SftpSession` (the script imports it by name; patch at the call site `ssh_server_transfer.SftpSession`). Goal ≥ 90% coverage, ≥ 35 cases.

| Group | Cases | Key behaviors |
|---|---|---|
| build_parser | 4 | required positionals / --mode choices / --bwlimit type=int / `--help` |
| _route_mode | 5 | direct → mode_deferred; --use-rsync → flag_deferred; auto/hybrid/stream all return None |
| _pipe_file | 6 | empty file / small file / multi-chunk / bwlimit slows down / mkdir_p called for parent / paramiko IOError mid-pipe → propagated |
| _pipe_tree | 6 | single file in flat dir / nested 3-deep / mixed files+symlinks (symlinks skipped) / empty src dir / `--recursive=False` + dir src → src_is_directory before walk / partial-failure counter |
| _classify_paramiko_failure | 6 | AuthenticationException × {src,dst} / gaierror × {src,dst} / SSHException → auth_{side} / unknown → remote_error |
| Envelope wiring | 4 | success on single file / success on tree / auto_resolved_to set / hybrid_resolved_to set / failure_class populated correctly |
| main() integration | 6 | direct → mode_deferred exit 20 / use-rsync → 21 / src_path_missing → 26 / src_is_directory no recursive → 27 / dst_path_invalid → 28 / stream happy path single file |

Target ≈ **37 cases**.

### Integration (`tests/test_ssh_server_transfer_integration.py`, `live_ssh`)

POC_HOST=pai serves as BOTH `src_alias` AND `dst_alias` (PRD D7 (A) workaround). 5 cases:

| Case | Setup | Assertion |
|---|---|---|
| `--help` exits 0 | none | smoke |
| `--mode direct` → mode_deferred | argparse only, no network | `exit_code == 20`, `failure_class == "mode_deferred"` |
| `--use-rsync` → flag_deferred | argparse only | `exit_code == 21` |
| Single-file stream pai → pai | `ssh pai 'echo hello > /tmp/sst-src-<rand>'`; run; `ssh pai 'cat /tmp/sst-dst-<rand>' == "hello\n"`; cleanup both | `success=True`, `bytes=6`, `files_transferred=1`, `mode_used="stream"` |
| Recursive tree stream pai → pai | `ssh pai 'mkdir -p /tmp/sst-src-<rand>/a/b; echo x > .../a/x.txt; echo y > .../a/b/y.txt'`; run with `--recursive`; assert both files arrived at dst | `success=True`, `files_transferred=2`, `bytes=4` (2 × "x\n"), correct tree structure on dst |

Cleanup pattern: each test wraps its `pai:/tmp/sst-*` setup in try/finally that `ssh pai 'rm -rf /tmp/sst-*'`. Same `POC_HOST` reachability skip pattern as `test_ssh_execute_integration.py:1-30`.

## Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| paramiko `prefetch()` over-reads if connection drops late, leaving src_fp's buffer un-acked | mid-pipe IOError, no harm to dst file content | dst file is closed (with-block); `transfer_io` failure_class surfaces |
| Tree walk on a 100k-file dir blows memory holding `listdir_attr` arrays | OOM | iterative BFS, only one dir's listdir held at a time; warn on `--recursive` with > 10k entries (v1.1) |
| Same `pai` alias for src + dst hits OpenSSH `MaxSessions=10` limit fast | integration test SSH refusal | both SftpSessions are SFTP-multiplexed onto separate TCP connections (paramiko default); 2 sessions × 1 user × pai is well under any default MaxSessions (10) |
| bwlimit pacing drifts across tree walk because per-file `_pipe_file` resets `start_time` | bwlimit isn't enforced globally | thread `start_time` + `running_bytes` from `_pipe_tree` into each `_pipe_file` call so pacing window is tree-wide |
| Symlinks in src tree could become loops / dangling on dst | corrupt dst tree | v1.0 skip symlinks silently (S_ISREG-only); document as out-of-scope in script docstring |
| Wide-character / non-UTF-8 remote filenames break paramiko `listdir_attr` | TypeError mid-walk | paramiko 3.x returns bytes for non-UTF-8 names; ssh_server_transfer's iter loop handles `bytes` filenames by decoding with `errors="surrogateescape"` (v1.1 if reports come in) |
| Concurrent ssh_server_transfer runs targeting the same (src, dst) path | data race on dst | document as user's responsibility; no locking in v1.0 |

## Compatibility / Rollback

- **Pure consumer of `lib.sftp_transport.SftpSession`** — no `lib/` changes (mirrors ADR-003 D2's thin-CLI principle).
- **No other v1.0 script imports ssh_server_transfer.** Rollback = revert the script + 2 test files; nothing else moves.
- **Forward (v1.1)**:
  - `--mode direct` lights up when src→dst auth strategy is chosen (PRD O1 candidate set: agent forwarding vs src-managed ssh-config). `--use-rsync` lights up alongside.
  - `--resume` / sha256 verify can be added behind the existing flag-deferred error path.
  - `--bwlimit` semantics stay stable (KB/s = 1024 B/s).
- **v0.2 surface compat**: stub argparse accepts all 5 flags; v1.0 preserves them. The behavior change is that `--mode direct` and `--use-rsync` now return a structured failure rather than `unimplemented()`. This is **more** caller-friendly than v0.2.
