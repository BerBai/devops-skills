# Implementation — ssh_server_transfer v1.0

## Phase 0 — Pre-flight

- [ ] **0.1** Re-read `prd.md` (D1-D8 locked, M2 scope: stream + hybrid; direct/auto/--use-rsync deferred) and `design.md` (architecture, _pipe_file primitive, tree walk, failure-class table)
- [ ] **0.2** Exit codes 20–29 are now documented in `.trellis/spec/backend/error-handling.md` (spec updated 2026-05-25 alongside this task — the table now lists `0/1/2/4-9/10-15/20-29/124`). Per-script range `20-29` is namespaced to `ssh_server_transfer`. The numeric mapping in `design.md` D5 / Phase 1.4 `_EXIT_CODES` is pinned; no further verification needed. Confirm by `grep "20-29" .trellis/spec/backend/error-handling.md` returns a hit.
- [ ] **0.3** Confirm `lib/sftp_transport.py` exposes `SftpSession` with `.sftp` attribute (paramiko SFTPClient) — script depends on `src_session.sftp.open(path, "rb")` + `dst_session.sftp.open(path, "wb")` round-trip. Already verified during design (`sftp_transport.py:165`).
- [ ] **0.4** Read `ssh_upload.py` as the reference shape for "thin CLI shell over `SftpSession`" (per ADR-003 D2). Note the `with SftpSession(args.alias) as session:` pattern — ssh_server_transfer extends it to two-session `ExitStack`.

## Phase 1 — Foundation (parser + mode router + envelopes)

- [ ] **1.1** Replace `plugins/ssh-core/skills/ssh-core/scripts/ssh_server_transfer.py` stub docstring with the v1.0 docstring (mirror `ssh_upload.py:1-25` style: surface block + Implementation notes + `See:` block pointing to PRD/design + ADR-003).
- [ ] **1.2** Implement `build_parser()` per PRD top-of-file surface. All 5 flags (`--mode / --use-rsync / --recursive / --bwlimit / --json`) plus 4 positionals (`src_alias src_path dst_alias dst_path`). No `--connect-timeout`, no `--timeout`, no `--ssh-config` (per PRD D6).
- [ ] **1.3** Implement `_route_mode(args)` per design.md "Mode routing": refuses `--mode direct` → `mode_deferred`, `--use-rsync` → `flag_deferred`. Returns `None` for auto/hybrid/stream (all proceed to stream path).
- [ ] **1.4** Constants:
  ```python
  _EXIT_CODES = {None: 0, "mode_deferred": 20, "flag_deferred": 21,
                 "network_src": 22, "network_dst": 23, "auth_src": 24,
                 "auth_dst": 25, "src_path_missing": 26,
                 "src_is_directory": 27, "dst_path_invalid": 28,
                 "transfer_io": 29, "timeout": 124, "remote_error": 1}
  _HUMAN_HINTS = { ... }       # per design.md Human-mode table
  DEFAULT_BLOCK_SIZE = 131072  # 128 KiB
  ```
- [ ] **1.5** Implement `_success_envelope` + `_failure_envelope` per design.md (auto_resolved_to / hybrid_resolved_to markers conditional on original `args.mode`).

**Validation 1**:
```bash
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_server_transfer.py --help
# Expect all 5 flags shown.
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_server_transfer.py pai /a pai /b --mode direct --json
# Expect: {"success":false,"exit_code":20,"data":{"failure_class":"mode_deferred",...}}
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_server_transfer.py pai /a pai /b --use-rsync --json
# Expect: exit_code=21, failure_class=flag_deferred
```

## Phase 2 — Stream pipe primitive (single file)

- [ ] **2.1** Implement `_stat_or_none(session, path)`:
  ```python
  try: return session.sftp.stat(path)
  except (FileNotFoundError, IOError) as e:
      if getattr(e, "errno", None) == errno.ENOENT: return None
      raise
  ```
- [ ] **2.2** Implement `_ensure_dst_parent(dst_session, dst_path)`: `posixpath.dirname(dst_path)` + `dst_session.mkdir_p(parent)` if non-empty.
- [ ] **2.3** Implement `_pipe_file(src_session, src_path, dst_session, dst_path, *, block_size, bwlimit_kbs, start_time, running_bytes) -> int` exactly per design.md "Stream-mode pipe primitive". Uses `src_fp.prefetch()` + `dst_fp.set_pipelined(True)` paramiko knobs for parity with `lib.sftp_transport.put_chunked/get_chunked`.
- [ ] **2.4** Implement `_classify_paramiko_failure(exc, *, which_side)` per design.md failure-class table.

**Validation 2**:
```bash
# Mock-level smoke from a python -c, against a local sftp test server if available;
# easier: drive it through unit tests in Phase 5. Just confirm `_pipe_file` exists
# and is callable.
python3 -c "from plugins.ssh-core.skills.ssh-core.scripts import ssh_server_transfer as m; print(m._pipe_file.__name__)"
```

## Phase 3 — Tree walk

- [ ] **3.1** Implement `_pipe_tree(src_session, src_root, dst_session, dst_root, *, block_size, bwlimit_kbs) -> tuple[int, int]` per design.md "Tree walk":
  - Iterative BFS with `stack = [src_root]`
  - `posixpath.relpath` + `posixpath.join` for path mapping
  - `dst_session.mkdir_p(dst_dir)` before each dir's files
  - `src_session.sftp.listdir_attr(src_dir)` → branch on `stat.S_ISDIR` / `stat.S_ISREG`
  - Symlinks / specials skipped silently
  - Thread `start_time` + `running_bytes` into each `_pipe_file` call for bwlimit pacing
- [ ] **3.2** Add `import stat as _stat` + `import posixpath` at top; `import errno` if not already.

**Validation 3**:
```bash
# Same as Phase 2: unit tests carry the load.
python3 -c "from plugins.ssh-core.skills.ssh-core.scripts import ssh_server_transfer as m; print(m._pipe_tree.__name__)"
```

## Phase 4 — main() wiring

- [ ] **4.1** Implement `main(argv=None) -> int`:
  ```python
  args = build_parser().parse_args(argv)
  pre = _route_mode(args)
  if pre is not None:
      emit(pre, as_json=args.json); return pre["exit_code"]

  start_time = time.monotonic()
  try:
      with contextlib.ExitStack() as stack:
          try:
              src_session = stack.enter_context(SftpSession(args.src_alias))
          except Exception as e:
              fclass = _classify_paramiko_failure(e, which_side="src")
              elapsed = time.monotonic() - start_time
              return _emit(_failure_envelope(args, fclass, elapsed=elapsed,
                                             hint=...), args)
          try:
              dst_session = stack.enter_context(SftpSession(args.dst_alias))
          except Exception as e:
              fclass = _classify_paramiko_failure(e, which_side="dst")
              ...

          # src stat
          src_attr = _stat_or_none(src_session, args.src_path)
          if src_attr is None:
              return _emit(_failure_envelope(args, "src_path_missing", ...), args)

          if _stat.S_ISDIR(src_attr.st_mode):
              if not args.recursive:
                  return _emit(_failure_envelope(args, "src_is_directory", ...), args)
              try:
                  files, bytes_total = _pipe_tree(src_session, args.src_path,
                                                  dst_session, args.dst_path,
                                                  block_size=DEFAULT_BLOCK_SIZE,
                                                  bwlimit_kbs=args.bwlimit)
              except OSError as e:
                  # dst parent missing / perm OR mid-pipe transfer_io
                  fclass = "dst_path_invalid" if not_during_pipe else "transfer_io"
                  return _emit(_failure_envelope(args, fclass, ...), args)
          else:
              try:
                  bytes_done = _pipe_file(src_session, args.src_path,
                                          dst_session, args.dst_path,
                                          block_size=DEFAULT_BLOCK_SIZE,
                                          bwlimit_kbs=args.bwlimit,
                                          start_time=start_time, running_bytes=0)
                  files, bytes_total = 1, bytes_done
              except OSError as e:
                  ...
          elapsed = time.monotonic() - start_time
          return _emit(_success_envelope(args, "stream", bytes_total, files, elapsed), args)
  except Exception as e:
      elapsed = time.monotonic() - start_time
      return _emit(_failure_envelope(args, "remote_error", elapsed=elapsed,
                                     hint=str(e)), args)
  ```
- [ ] **4.2** Add `if __name__ == "__main__": sys.exit(main())`.
- [ ] **4.3** Verify imports: `argparse`, `contextlib`, `errno`, `posixpath`, `socket`, `stat as _stat`, `sys`, `time`, `pathlib.Path`, plus `paramiko` (for exception-type check in `_classify_paramiko_failure`), plus `sys.path.insert` + `from lib import json_result, emit, msys_safe_env` + `from lib.sftp_transport import SftpSession`.

**Validation 4**:
```bash
# End-to-end smoke without network: src_path_missing path
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_server_transfer.py \
    nonexistent-alias-xyz /tmp/x other-nonexistent-alias-yyy /tmp/y --json
# Expect: failure_class either network_src or auth_src, exit 22/24, fast (< 5s).
```

## Phase 5 — Unit tests

- [ ] **5.1** Create `tests/test_ssh_server_transfer_unit.py`. Header: standard `sys.path` + script import block (mirror `tests/test_ssh_upload_unit.py:1-30`).
- [ ] **5.2** Mock helpers: `_MockSftp` class (mimic `paramiko.SFTPClient` with `open`, `stat`, `listdir_attr`, `mkdir`); `_MockSession` (mimic `SftpSession` with `.sftp` attribute + `mkdir_p` no-op + `__enter__/__exit__`); use `monkeypatch.setattr("ssh_server_transfer.SftpSession", lambda alias, **kw: ...)`.
- [ ] **5.3** Cases per design.md "Test strategy" table:
  - `build_parser` 4 cases
  - `_route_mode` 5 cases
  - `_pipe_file` 6 cases
  - `_pipe_tree` 6 cases (including symlink skip + src_is_directory branch + partial-failure counter when IOError mid-tree)
  - `_classify_paramiko_failure` 6 cases (parameterized on exception type × which_side)
  - Envelope wiring 4 cases (auto_resolved_to / hybrid_resolved_to / failure populated)
  - `main()` integration 6 cases
- [ ] **5.4** Target ≥ 37 cases, ≥ 90% statement coverage.

**Validation 5**:
```bash
uv run pytest tests/test_ssh_server_transfer_unit.py -v
# Expect ≥ 37 PASS, ≥ 90% coverage on ssh_server_transfer.py.
```

## Phase 6 — Integration tests

- [ ] **6.1** Create `tests/test_ssh_server_transfer_integration.py`. Mirror `test_ssh_upload_integration.py` skeleton: `POC_HOST = os.environ.get("POC_HOST", "pai")`, module-level `pytestmark = pytest.mark.live_ssh`, `_skip_if_unreachable()` helper.
- [ ] **6.2** 5 cases per design.md "Integration" table:
  - `test_help_exits_zero`
  - `test_mode_direct_returns_mode_deferred` (no network — just CLI smoke)
  - `test_use_rsync_returns_flag_deferred`
  - `test_single_file_stream_pai_to_pai` — `ssh POC_HOST 'echo hello > /tmp/sst-src-<rand>'`; run; verify via `ssh POC_HOST 'cat /tmp/sst-dst-<rand>'`; cleanup both.
  - `test_recursive_tree_pai_to_pai` — 2-level dir on pai, 2 files; run with `--recursive`; verify both arrived; cleanup.
- [ ] **6.3** Every test that creates remote files uses `try/finally` to `ssh POC_HOST 'rm -rf /tmp/sst-*'` regardless of outcome.

**Validation 6**:
```bash
uv run pytest tests/test_ssh_server_transfer_integration.py -m live_ssh -v
# Expect 5 PASS.
uv run pytest tests/ -m live_ssh -v
# Expect prior live_ssh count + 5 (13 → 18). All PASS.
```

## Phase 7 — Docs sync

- [ ] **7.1** `SKILL.md` quick-reference: confirm the existing line still matches v1.0 signature:
  ```bash
  python scripts/ssh_server_transfer.py prod-db /var/backup/db.dump backup-host /restore/ --mode auto
  ```
  In v1.0 this routes to stream (auto resolves to stream). The line is still correct as a quick-ref. Add a short follow-up note under the line: `# v1.0 ships stream mode only; --mode direct / auto / hybrid all route to stream. --use-rsync deferred to v1.1.`
- [ ] **7.2** `SKILL.md` decision tree (lines 36-40): the existing `--mode auto|direct|stream|hybrid` text is misleading in v1.0. Either:
  - keep as-is (surface stability)
  - add a small parenthetical: `--mode stream` (v1.0); other modes deferred to v1.1
  Pick the latter — it's accurate and surface-stable.
- [ ] **7.3** `references/workflows.md` §3 4-mode table: annotate 3 of 4 rows as "v1.1 deferred", point to this PRD.
- [ ] **7.4** No new ADR. ADR-003 already covers SftpSession reuse. The ADR-003 §138 "direct" wording bug is **out of scope** for this task (it'll get a follow-up doc-only fix).

**Validation 7**:
```bash
grep -n "ssh_server_transfer" plugins/ssh-core/skills/ssh-core/{SKILL.md,references/}*.md
# Expect consistent v1.0-reality wording across all hits.
```

## Phase 8 — Commit + wrap-up

- [ ] **8.1** Pre-commit sanity:
  ```bash
  git status -s                         # confirm changed files only (no .coverage, no uv.lock noise)
  git restore uv.lock                   # if uv touched it
  uv run ruff check plugins/ssh-core/skills/ssh-core/scripts/ssh_server_transfer.py tests/test_ssh_server_transfer_*.py
  uv run pytest tests/ --tb=no | tail -1
  ```
- [ ] **8.2** `git add` exactly these files:
  - `plugins/ssh-core/skills/ssh-core/scripts/ssh_server_transfer.py`
  - `plugins/ssh-core/skills/ssh-core/SKILL.md`
  - `plugins/ssh-core/skills/ssh-core/references/workflows.md`
  - `tests/test_ssh_server_transfer_unit.py`
  - `tests/test_ssh_server_transfer_integration.py`
- [ ] **8.3** Commit message (HEREDOC):
  ```
  feat(ssh-core): ship ssh_server_transfer v1.0 stream mode

  Thin two-session piping shell over lib.sftp_transport.SftpSession (per
  ADR-003 D2 reuse target). Streams bytes between two SFTP sessions in
  local memory — never staged to local disk.

  v1.0 scope is stream-only with surface preservation. --mode direct,
  --mode auto, --mode hybrid are all routed to stream (auto/hybrid log
  data.{auto,hybrid}_resolved_to="stream"); --mode direct refuses with
  failure_class=mode_deferred. --use-rsync refuses with flag_deferred.
  Rationale: direct mode needs an src→dst auth strategy that ssh-core
  doesn't yet have, and src-side rsync prereq escalation; both light up
  in v1.1 alongside ControlMaster.

  Per-script failure_class namespace (12 strings) + exit codes 20-29
  (fresh range; consistent with ssh_config_manager / deploy_pubkey
  precedent). Bwlimit pacing threaded across tree walks so the cap is
  tree-wide, not per-file.

  N unit + 5 live_ssh integration cases (pai-as-both-src-and-dst pattern).

  Doc sync:
    - SKILL.md decision tree / quick reference: v1.0 mode collapse noted
    - references/workflows.md §3: 4-mode table annotated v1.1 deferred

  Refs: .trellis/tasks/05-24-ssh-core-ssh-server-transfer/{prd,design,implement}.md
        .trellis/spec/backend/adr-003-sftp-transport.md
  ```
- [ ] **8.4** Update parent `05-24-devops-skills-v1-rollout/prd.md` Notes with 2026-05-25 ship entry (commit hash + file count + test count + key decisions).
- [ ] **8.5** `python3 ./.trellis/scripts/task.py finish` + `archive --no-commit 05-24-ssh-core-ssh-server-transfer`.

**Validation 8**:
```bash
git log --oneline -1                            # Expect new feat(ssh-core) commit
uv run pytest tests/ --tb=no | tail -1          # Default green
uv run pytest tests/ -m live_ssh --tb=no | tail -1  # 18+ PASS
python3 ./.trellis/scripts/task.py list --mine | head
# Expect ssh-server-transfer archived; Phase 1: 4/7.
```

## Review gates

| Gate | When | What |
|---|---|---|
| G1 | Before Phase 2 | Parser + `_route_mode` smoke; `--mode direct` and `--use-rsync` both correctly emit deferred envelopes |
| G2 | Before Phase 3 | `_pipe_file` callable; bwlimit pacing threaded |
| G3 | Before Phase 4 | `_pipe_tree` iterative BFS validated (no recursion depth limit) |
| G4 | Before Phase 5 | `main()` end-to-end smoke without network passes (src_path_missing / mode_deferred cases) |
| G5 | Before Phase 6 | Unit suite ≥ 37 cases, ≥ 90% coverage |
| G6 | Before Phase 7 | Integration 5 cases pass against POC_HOST=pai; cleanup leaves /tmp/sst-* clean |
| G7 | Before Phase 8 | SKILL.md + workflows.md diffs reviewed by user |
| G8 | Done | Commit lands + baseline pytest green |

## Rollback points

- Phases 1-7 uncommitted: `git restore <file>` per-file reset
- Phase 8 commit: `git revert <hash>` — fully self-contained (no `lib/` changes, no cross-script imports)

## Dispatch note

Per parent task Notes §82 — sub-agent dispatch (`trellis-implement`) is the right shape (~400 loc + 37 unit + 5 integration; single phase span). Main session writes design.md/implement.md (this artifact), dispatches one `trellis-implement` for Phases 1-6 (code + tests). Phases 7-8 (docs + commit + wrap-up) come back to main session for review-gated handling.
