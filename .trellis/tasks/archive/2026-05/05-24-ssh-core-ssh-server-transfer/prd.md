# ship ssh-core ssh_server_transfer v1.0

## Goal

(per stub `scripts/ssh_server_transfer.py:1-25` + workflows.md §3 + SKILL.md quick-reference)

Move bytes **between two remote hosts** without local staging. v1.0 ships **stream mode only**; `direct` / `auto` / `--use-rsync` are deferred to v1.1.

Argv (v1.0 surface):

```
ssh_server_transfer.py <src_alias> <src_path> <dst_alias> <dst_path>
    [--mode auto|direct|stream|hybrid]   default: auto, but auto/direct/hybrid all route to stream in v1.0 (see D2)
    [--use-rsync]                        accepted but refused with failure_class="flag_deferred"
    [--recursive]                        tree copy
    [--bwlimit KB/s]                     bandwidth cap (chunk-loop throttle)
    [--json]
```

The CLI surface is preserved exactly so a future v1.1 can light up `direct` mode without breaking callers; behavior differs only in what each mode resolves to.

## Confirmed facts (from code / spec)

- **`lib/sftp_transport.py` is shipped** (538 loc, ADR-003 D1/D2). Provides `SftpSession` (low-level), `upload_file` / `download_file` / `upload_tree` / `download_tree` (high-level), `TransferResult` dataclass.
- **paramiko is a runtime dep** (`pyproject.toml`, ADR-003 D1).
- **ADR-003 §138 names ssh_server_transfer as a SftpSession reuse target**, but its "direct" wording (*"remote → SFTP → local → SFTP → remote staging"*) **is incorrect** — that's local-staging, not direct. Authoritative direct semantics live in the stub + workflows.md §3 (*"src machine runs `scp`/`rsync` → target machine"*). Since v1.0 doesn't ship direct, the wording bug isn't load-bearing for this task; flagged for ADR-003 follow-up.
- **`SftpSession.AutoAddPolicy`** for unknown host keys (`sftp_transport.py:103`, ADR-003 D2 Negative).
- **`ssh_execute` v1.0 is BatchMode-only** (ADR-001 D1).
- **`pyproject.toml` already includes `paramiko>=3.0,<4`**.
- **No existing test exercises ssh_server_transfer beyond manifest `--help`**.
- **Test infrastructure** has only `POC_HOST=pai`. Cross-host integration test requires a workaround (see D7).

## Design decisions (locked)

### D1. Stream is the only implemented mode in v1.0

- The script opens **two `SftpSession`s** (one per alias) in a context-manager pair, walks `get_chunked` from src → in-memory chunk → `put_chunked` to dst, no local disk staging.
- Both sessions resolve aliases via `~/.ssh/config` (paramiko `SSHConfig`); `ProxyJump` works because `SftpSession` already plumbs it.
- AutoAddPolicy for both sessions (inherited from `SftpSession`; ADR-003 D2 Negative). Documented in script docstring.

### D2. `--mode` routing in v1.0

All four `--mode` values are accepted, but route as follows:

| Flag value | v1.0 behavior |
|---|---|
| `--mode stream` | runs stream (D1) |
| `--mode auto` (default) | runs stream — log to `data.mode_used="stream"` + `data.auto_resolved_to="stream"` |
| `--mode hybrid` | runs stream — log to `data.mode_used="stream"` + `data.hybrid_resolved_to="stream"` |
| `--mode direct` | fails fast: `success=false`, `exit_code` per D5, `failure_class="mode_deferred"`, `stderr="--mode direct is deferred to v1.1; use --mode stream"` |

Rationale: preserves surface for v1.1; auto / hybrid in v1.0 are "stream is the safe default" — they degrade to stream because direct doesn't exist; matches the stub spirit (auto's else-branch was always stream). `direct` is the only mode that **changes** the data path materially, so it's the one we refuse.

### D3. `--use-rsync` in v1.0

Accepted by argparse for surface stability; if present, fails fast with `failure_class="flag_deferred"`, `stderr="--use-rsync requires --mode direct, which is deferred to v1.1"`. Not silently ignored — silent acceptance would mislead callers who think they got rsync semantics.

### D4. Stream mode operational details

- **Chunk size**: 128 KB (matches `sftp_transport.put_chunked` default; no flag in v1.0).
- **bwlimit**: implemented in the script's piping loop using `time.monotonic()` pacing (mirrors `put_chunked`'s `bwlimit_kbs`). 1 KB = 1024 B/s (ADR-003 D4).
- **Recursive walk** (when `--recursive`): use `SftpSession.sftp.listdir_attr` recursively on src; for each file, do a single-file piping pass. Directories on dst are `mkdir_p`'d (paramiko `SftpSession.mkdir_p` already exists, ADR-003 D2). If src is a file but `--recursive` is set, treat as single-file copy (silent). If src is a dir but `--recursive` is NOT set, fail with `failure_class="src_is_directory"`.
- **No resume**: not in stub surface; deferred to v1.1 (would need cross-session resume coordination not present in current `sftp_transport`).
- **No sha256 verify**: matches stub (no `--verify`); matches scp semantics; matches ADR-003 D4 verify-bound-to-resume principle.
- **Progress**: no `--no-progress` flag in stub; v1.0 emits a single 1-line per-file completion to stderr unless `--json` is set, no chunk-by-chunk progress callback (matches the lower-fanfare cousin of ssh_upload's surface; ssh_upload's `--no-progress` is needed because uploads emit 5%-bucket progress, server-transfer file-by-file is quieter).

### D5. failure_class taxonomy + exit codes (per-script namespace)

| `failure_class` | Trigger | exit_code |
|---|---|---|
| `null` | success | 0 |
| `mode_deferred` | `--mode direct` in v1.0 | 20 |
| `flag_deferred` | `--use-rsync` in v1.0 | 21 |
| `network_src` | paramiko fails to connect src (DNS, refused, timeout-at-connect) | 22 |
| `network_dst` | paramiko fails to connect dst | 23 |
| `auth_src` | paramiko AuthenticationException on src | 24 |
| `auth_dst` | paramiko AuthenticationException on dst | 25 |
| `src_path_missing` | `sftp.stat(src)` returns None | 26 |
| `src_is_directory` | src is a dir but `--recursive` not set | 27 |
| `dst_path_invalid` | dst parent doesn't exist / write permission denied | 28 |
| `transfer_io` | mid-flight IOError (chunk write failed, connection dropped) | 29 |
| `timeout` | wall-clock timeout (TBD if v1.0 adds `--timeout`; otherwise N/A) | 124 |
| `remote_error` | catch-all unexpected exception | 1 |

Exit codes 20–29 are a fresh contiguous range; pin them during implement.md after grep-confirming no conflict with `error-handling.md` (which currently has 0, 1, 2, 4-9, 124 from ssh_execute / ssh_config_manager / sftp_transport).

### D6. CLI surface parity

No `--connect-timeout` / `--timeout` / `--ssh-config` in v1.0. Reasoning:
- paramiko's `SftpSession.__init__` has no exposed timeout knob today.
- Adding `--ssh-config` would require threading through both `SftpSession` constructions; defer until a real use case arrives.
- Surface stays minimal; matches the stub exactly except for the 4 internal routing decisions documented in D2-D3.

### D7. Test strategy

- **Unit (`test_ssh_server_transfer_unit.py`)** — primary coverage. Mock `paramiko.SSHClient` / `SFTPClient` (or `SftpSession` via monkey-patching `lib.sftp_transport.SftpSession`); assert:
  - argv parsing for all flag combinations.
  - `--mode direct` triggers `mode_deferred` (D2).
  - `--use-rsync` triggers `flag_deferred` (D3).
  - `--mode auto` / `--mode hybrid` / no `--mode` all resolve to stream + correct `data.mode_used`.
  - Single-file pipe (mocked `stat` / `get_chunked` / `put_chunked`).
  - Tree pipe with `--recursive` (mocked `listdir_attr`).
  - Each failure_class row in D5 triggered by the right exception type.
  - Target ≥ 90% statement coverage.
- **Integration (`test_ssh_server_transfer_integration.py`, `live_ssh` mark)** — uses pai as both src and dst (the (A) workaround from O5):
  - Single-file stream from `pai:/tmp/sst-src-<rand>` to `pai:/tmp/sst-dst-<rand>`, verify by `ssh pai cat` of dst.
  - Recursive tree stream (3 files in 2 dirs), verify by `ssh pai 'find … -type f | sort'`.
  - `--mode direct` fails with `failure_class="mode_deferred"` (network not touched).
  - `--use-rsync` fails with `failure_class="flag_deferred"`.
  - Wrong src path → `src_path_missing`.
  - Cleanup: every test removes its `/tmp/sst-*` setup in teardown.

### D8. `data` payload

| Key | Type | Meaning |
|---|---|---|
| `mode_used` | `"stream"` only (in v1.0) | Resolves auto/hybrid → stream |
| `auto_resolved_to` | `"stream"` \| absent | Set only when `--mode auto` was passed |
| `hybrid_resolved_to` | `"stream"` \| absent | Set only when `--mode hybrid` was passed |
| `bytes` | int | Sum across tree |
| `files_transferred` | int | 1 for single file; N for tree |
| `elapsed_s` | float | Wall-clock |
| `src` | str | `"<alias>:<path>"` |
| `dst` | str | `"<alias>:<path>"` |
| `failure_class` | str \| null | Per D5 |

## Requirements

1. CLI surface per the top of this PRD; argparse with `prog="ssh_server_transfer.py"`, description matching SKILL.md decision-tree entry.
2. paramiko via `lib.sftp_transport.SftpSession`; **no direct paramiko imports** in `ssh_server_transfer.py` (mirrors ADR-003 D2's thin-CLI principle for ssh_upload / ssh_download).
3. JSON envelope per D8, emitted via `lib.emit`.
4. `failure_class` strings per D5, exit codes per D5 (verified non-conflict during implement.md).
5. Human-mode output (no `--json`): per-file completion lines to stderr; summary line "transferred N file(s), M bytes in T s via stream" on success; failure_class + remediation hint on failure.
6. Source file header docstring: "(v1.0)" tag, surface block, `See:` block pointing to `workflows.md §3`, ADR-003, this PRD.
7. ruff + mypy clean.
8. Unit + integration tests per D7.

## Acceptance Criteria

- [ ] `ssh_server_transfer.py` no longer a stub: docstring says `(v1.0)`, no `unimplemented()`, loc in 300-450 range.
- [ ] `python ssh_server_transfer.py --help` shows the v1.0 surface and exits 0.
- [ ] All 5 argparse flags from the surface parse correctly.
- [ ] `tests/test_ssh_server_transfer_unit.py` ≥ 90% statement coverage, every `failure_class` in D5 exercised.
- [ ] `tests/test_ssh_server_transfer_integration.py` with `pytest.mark.live_ssh`; six cases per D7 pass against POC_HOST=pai.
- [ ] `pytest tests/` default suite green.
- [ ] `pytest tests/ -m live_ssh -k server_transfer -v` green (manual, against POC_HOST=pai).
- [ ] `ruff check plugins/ssh-core` + `mypy plugins/ssh-core/.../ssh_server_transfer.py` clean.
- [ ] Exit-code table (D5) verified non-conflicting against latest `error-handling.md`.
- [ ] SKILL.md decision-tree entry for ssh_server_transfer updated to note v1.0 ships only `--mode stream` (auto/hybrid resolve to stream; direct deferred).
- [ ] workflows.md §3 4-mode table annotated: 3 modes marked "v1.1 deferred" with cross-ref to this PRD.

## Out of scope

- direct mode (deferred v1.1)
- auto-decision logic / reachability probe (deferred v1.1; auto resolves to stream)
- hybrid fallback (deferred v1.1; hybrid resolves to stream)
- `--use-rsync` (deferred v1.1; refused with `flag_deferred`)
- `--resume` (not in stub; deferred)
- sha256 verify (not in stub; deferred — matches scp / ADR-003 D4)
- `--no-progress` / chunk-level progress (not in stub for this script)
- Parallel / concurrent file transfers within `--recursive` (sequential in v1.0; ADR-003 §182)
- Compression (ADR-003 §180)
- Password / keyboard-interactive auth (ADR-001 / ADR-003 deferred symmetrically)
- `--exclude` / `--include` / metadata preservation (ADR-003 §178-179)
- Fixing ADR-003 §138 "direct" wording bug (separate doc-only follow-up under parent v1-rollout; not blocking)

## Notes

- v1.0 is **stream-only** but **surface-preserving**: callers who pass `--mode direct` get a clear deferral message, not a silent fallback. This makes the v1.1 light-up additive.
- The 4 mode → 1 mode collapse is recorded in `data.auto_resolved_to` / `data.hybrid_resolved_to` so audit logs explain "you asked for auto, you got stream."
