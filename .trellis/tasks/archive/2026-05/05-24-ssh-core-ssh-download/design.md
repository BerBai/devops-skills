# Design — ssh-core ssh_download v1.0

> 配套 PRD: `prd.md`。
> 实施清单: `implement.md`。
> **本任务复用** sibling 任务 ssh_upload 沉淀的 `.trellis/spec/backend/adr-003-sftp-transport.md` 与 `plugins/ssh-core/skills/ssh-core/scripts/lib/sftp_transport.py`。

## 1. Architecture overview

```
ssh_download.py  (CLI, ~110 loc)   # 比 upload 少 --chmod 处理 ~10 loc
└── lib/sftp_transport.py          # 已由 ssh_upload 任务交付，本任务不重写
     ├── class SftpSession         (reused as-is)
     │     ├── get_chunked(remote, local, *, offset, callback, bwlimit)  ← 本任务使用
     │     ...
     ├── download_file(session, remote, local, *, resume, ...)  ← 本任务使用
     ├── download_tree(session, remote_dir, local_dir, *, ...)  ← 本任务使用
     └── helpers (reused)
```

数据流（与 upload 完全对称）：

```
argv → argparse → SftpSession(alias) → download_file/download_tree
                                      → returns TransferResult
                                      → script wraps to json_result envelope
                                      → emit(as_json=args.json)
```

## 2. CLI entry point (`ssh_download.py`)

薄壳，与 upload 几乎对称（删除 chmod 段，参数顺序调整）：

```python
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # local-existing-dir-without-recursive guard
    local_path = Path(args.local_path)
    if local_path.is_dir() and not args.recursive:
        # 单文件下载到 dir 不允许，与 upload 对称 (避免 ambiguous: 是放进 dir 还是覆写 dir?)
        result = json_result(success=False, exit_code=1,
                             stderr=f"local path '{args.local_path}' is a directory; "
                                    "pass --recursive or specify a file path",
                             data={"route": "sftp", "direction": "download",
                                   "failure_class": "remote_error"})
        emit(result, as_json=args.json)
        return 1

    try:
        with SftpSession(args.alias) as session:
            # remote-not-found / remote-is-dir guards happen inside download_file/tree
            if args.recursive:
                tr = download_tree(session, args.remote_path, args.local_path,
                                   resume=args.resume,
                                   block_size=args.block_size,
                                   bwlimit_kbs=args.bwlimit,
                                   progress=not args.no_progress)
            else:
                tr = download_file(session, args.remote_path, args.local_path,
                                   resume=args.resume,
                                   block_size=args.block_size,
                                   bwlimit_kbs=args.bwlimit,
                                   progress=not args.no_progress)
    except paramiko.AuthenticationException as e:
        # 同 upload 错误映射
        ...
    except paramiko.SSHException as e: ...
    except PermissionError as e: ...     # 本地写或远端读 → exit 8

    result = json_result(
        success=tr.success,
        exit_code=tr.exit_code,
        stderr=tr.stderr,
        data={
            "route": "sftp",
            "direction": "download",                          # ← 与 upload 区分
            "alias": args.alias,
            "remote_path": args.remote_path,
            "local_path": args.local_path,
            "recursive": args.recursive,
            "resume": args.resume,
            "bytes_transferred": tr.bytes_transferred,
            "elapsed_s": tr.elapsed_s,
            "sha256_verified": tr.sha256_verified,
            **({"sha256_local": tr.sha256_local, "sha256_remote": tr.sha256_remote}
               if tr.sha256_local and tr.sha256_local != tr.sha256_remote else {}),
            **({"bytes_resumed_from": tr.bytes_resumed_from}
               if tr.bytes_resumed_from is not None else {}),
            **({"failed_files": tr.failed_files} if tr.failed_files else {}),
            "failure_class": tr.failure_class,
        },
    )
    emit(result, as_json=args.json)
    return tr.exit_code
```

## 3. `download_file` 实现要点（在 `lib/sftp_transport.py` 中，但其骨架由 upload 任务已经 placeholder）

```python
def download_file(session, remote, local, *, resume=False, block_size=131072,
                  bwlimit_kbs=None, progress=True) -> TransferResult:
    # 1) stat remote — None → exit 1 + "remote path not found"
    # 2) if S_ISDIR(remote_stat.st_mode) → exit 1 + "remote is a directory; use --recursive"
    # 3) Path(local).parent.mkdir(parents=True, exist_ok=True)
    # 4) if resume:
    #      local_size = Path(local).stat().st_size if exists else 0
    #      remote_size = remote_stat.st_size
    #      case (local_size, remote_size):
    #          (0, _)              → offset=0; transfer; verify
    #          (a, a)              → offset=a; skip; verify
    #          (a, b) a < b        → offset=a; transfer remainder; verify
    #          (a, b) a > b        → exit 1 + "local larger than remote; refuse"
    # 5) else (no resume): offset=0; transfer; no verify
    # 6) session.get_chunked(remote, local, offset=offset, ...)
    # 7) if resume: sha256_local + sha256_remote → compare; mismatch → exit 9
    # 8) return TransferResult
```

## 4. `download_tree` 实现要点

```python
def download_tree(session, remote_dir, local_dir, *, ...) -> TransferResult:
    # 远端 walk (paramiko 无原生 walk): 收集成 list 而非 lazy generator,
    # 与 ssh_upload.upload_tree 的 single-pass materialize 策略对齐 —
    # progress separator 行需要预知总文件数 N (`--- file i/N: <path> ---`)。
    # Trade-off: 大目录时一次性发 listdir_attr round-trip,但避免 generator
    # 内 mutable counter 的复杂度;upload 实施验证可行。
    def remote_walk(rd):
        entries = session.sftp.listdir_attr(rd)
        files, dirs = [], []
        for e in entries:
            full = posixpath.join(rd, e.filename)
            if stat.S_ISDIR(e.st_mode):
                dirs.append(full)
            else:
                files.append(full)
        yield rd, dirs, files
        for sub in dirs:
            yield from remote_walk(sub)

    # 验证 remote_dir exists & is dir
    rs = session.stat(remote_dir)
    if rs is None: return TransferResult(success=False, exit_code=1,
                                          stderr="remote path not found", ...)
    if not stat.S_ISDIR(rs.st_mode): return TransferResult(... "remote is a file; omit --recursive" ...)

    Path(local_dir).mkdir(parents=True, exist_ok=True)
    failed_files = []
    total_bytes = 0
    start = time.monotonic()

    # 单 pass materialize: 先全量收集 (local_target, remote_path) 对,以便算 N 与 i/N 分隔。
    pairs = []
    for rd, dirs, files in remote_walk(remote_dir):
        rel = posixpath.relpath(rd, remote_dir)
        local_rd = Path(local_dir) / rel if rel != '.' else Path(local_dir)
        local_rd.mkdir(parents=True, exist_ok=True)
        for rf in files:
            pairs.append((rf, str(local_rd / posixpath.basename(rf))))

    N = len(pairs)
    for i, (rf, local_target) in enumerate(pairs, 1):
        if progress:
            print(f"--- file {i}/{N}: {rf} ---", file=sys.stderr)
        try:
            sub_tr = download_file(session, rf, local_target,
                                   resume=resume, ...)
            if sub_tr.success: total_bytes += sub_tr.bytes_transferred
            else: failed_files.append({"path": rf, "error": sub_tr.stderr})
        except Exception as e:
            failed_files.append({"path": rf, "error": str(e)})

    return TransferResult(
        success=(not failed_files),
        exit_code=0 if not failed_files else 1,
        bytes_transferred=total_bytes,
        elapsed_s=round(time.monotonic() - start, 3),
        ...
        failed_files=failed_files or None,
        failure_class=None if not failed_files else "remote_error",
    )
```

## 5. Error mapping (mirror upload §7)

| 来源 | exit_code | failure_class | stderr 模板 |
|---|---|---|---|
| `AuthenticationException` | 1 | auth | "ssh authentication failed for alias '<alias>': <msg>" |
| `SSHException` | 1 | network | "ssh transport error to '<alias>': <msg>" |
| 本地 `PermissionError` (写) | 8 | permission | "local write permission denied: <local_path>" |
| 远端 `IOError`/`PermissionError` (读) | 8 | permission | "remote read permission denied: <remote_path>" |
| remote stat None | 1 | remote_error | "remote path not found: <remote_path>" |
| remote is dir, no --recursive | 1 | remote_error | "remote '<path>' is a directory; pass --recursive" |
| local is dir, no --recursive | 1 | remote_error | "local '<path>' is a directory; pass --recursive or specify a file path" |
| sha256 mismatch | 9 | `transfer_verify_failed` (与 ssh_upload §7 + ADR-003 + error-handling.md 对齐) | "sha256 verification failed: local=<a> remote=<b>" — envelope 必带 `data.sha256_local` + `data.sha256_remote` |
| local_size > remote_size on --resume | 1 | remote_error | "local larger than remote; refuse to truncate" |

## 6. Module layout (file diff)

```
plugins/ssh-core/skills/ssh-core/scripts/
├── ssh_upload.py                     (unchanged, owned by sibling task)
├── ssh_download.py                   (rewrite: stub → ~110 loc)
└── lib/
    ├── __init__.py                   (unchanged)
    └── sftp_transport.py             (already exists from upload task;
                                       this task may append download_file/tree
                                       if upload task only placeholdered them)

.trellis/spec/backend/
└── adr-003-sftp-transport.md         (already exists, NOT modified)

tests/
├── test_ssh_download_unit.py         (NEW)
└── test_ssh_download_integration.py  (NEW)
```

## 7. Compatibility notes

- 不影响 ssh_upload 已通过的测试（不改 upload_file/upload_tree 签名）。
- 不修改 ADR-003。
- 不修改 pyproject.toml。
- 如果 ssh_upload 任务交付时 download_file/download_tree 是 placeholder（raise NotImplementedError），本任务把它们填完整。

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| ssh_upload 交付的 transport API 与本任务期望不一致 | Sibling 任务的 design.md §2 已明确 download_file/download_tree 签名；本任务实现严格遵循。如有差异，开 follow-up task 协调 |
| 远端 walk 性能（大目录）| paramiko `listdir_attr` 是一次 batch；递归层级深时可能慢。v1.0 不并发；v1.1 评估异步 |
| 本地写权限边角 case（如 partition 满）| `OSError` 兜底捕获，exit 8 + stderr "local write failed: <msg>"，避免吞异常 |
| Windows 路径分隔符（本地 backslash） | `Path(local).parent.mkdir(parents=True, exist_ok=True)` 自动处理；远端 `posixpath.join` 统一 |
