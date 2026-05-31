# Design — ssh-core ssh_upload v1.0

> 配套 PRD: `prd.md`；规范立项: `.trellis/spec/backend/adr-003-sftp-transport.md` (本任务交付物之一)。
> 实施清单: `implement.md`。

## 1. Architecture overview

```
ssh_upload.py  (CLI, ~120 loc)
└── lib/sftp_transport.py  (new, ~400 loc)
     ├── class SftpSession      (low-level, ~150 loc)
     │     ├── connect / close
     │     ├── stat / mkdir / chmod
     │     ├── put_chunked(local_fp, remote_path, *, offset=0, callback, bwlimit)
     │     └── get_chunked(remote_path, local_fp, *, offset=0, callback, bwlimit)
     │
     ├── upload_file(session, local, remote, *, resume, chmod, ...)  (high-level)
     ├── upload_tree(session, local_dir, remote_dir, *, ...)         (high-level)
     ├── download_file(session, remote, local, *, resume, ...)       (mirror, used by ssh_download)
     ├── download_tree(session, remote_dir, local_dir, *, ...)       (mirror)
     └── helpers: sha256_local / sha256_remote / pct_callback / throttle
```

数据流：

```
argv → argparse → SftpSession(alias) → upload_file/upload_tree
                                      → returns TransferResult dataclass
                                      → script wraps to json_result envelope
                                      → emit(as_json=args.json)
```

## 2. Module contracts

### 2.1 `class SftpSession`

```python
class SftpSession:
    def __init__(self, alias: str, *, ssh_config: str | None = None) -> None:
        # paramiko.SSHConfig().parse(open(ssh_config or ~/.ssh/config))
        # paramiko.SSHClient + load_system_host_keys + connect(**config)
        # self.sftp = client.open_sftp()
        ...

    def __enter__(self) -> "SftpSession": ...
    def __exit__(self, *exc) -> None: self.close()

    def stat(self, remote: str) -> SFTPAttributes | None: ...   # None if not found
    def mkdir_p(self, remote_dir: str, mode: int = 0o755) -> None: ...  # idempotent
    def chmod(self, remote: str, mode: int) -> None: ...

    def put_chunked(
        self,
        local_path: str,
        remote_path: str,
        *,
        offset: int = 0,           # resume continuation byte
        block_size: int = 131072,
        bwlimit_kbs: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        # opens local "rb" + seek(offset)
        # opens remote mode="r+b" (resume) or "wb" (fresh) + seek(offset)
        # while chunk = local.read(block_size): remote.write(chunk); progress(bytes_done, total); throttle()
        # returns total bytes transferred this call
        ...

    def get_chunked(self, remote_path: str, local_path: str, **kw) -> int: ...
        # mirror; used by ssh_download
```

### 2.2 High-level `upload_file`

```python
@dataclass
class TransferResult:
    success: bool
    exit_code: int
    bytes_transferred: int
    elapsed_s: float
    sha256_verified: bool
    sha256_local: str | None
    sha256_remote: str | None
    bytes_resumed_from: int | None
    failed_files: list[dict] | None
    failure_class: str | None
    stderr: str

def upload_file(
    session: SftpSession,
    local: str,
    remote: str,
    *,
    resume: bool = False,
    chmod: int | None = None,
    block_size: int = 131072,
    bwlimit_kbs: int | None = None,
    progress: bool = True,
) -> TransferResult:
    # 1) validate local path exists & is file
    # 2) if resume: stat remote → choose offset:
    #      - missing → offset=0 (fast path)
    #      - remote_size == local_size → offset=local_size (skip transfer, go to verify)
    #      - remote_size <  local_size → offset=remote_size (continue)
    #      - remote_size >  local_size → return (success=False, exit=1, stderr="...")
    # 3) call session.put_chunked(local, remote, offset=offset, ..., progress=cb)
    # 4) if resume: compute sha256_local + sha256_remote (streaming) → compare; mismatch → exit=9
    # 5) if chmod: session.chmod(remote, chmod)
    # 6) return TransferResult
```

### 2.3 High-level `upload_tree`

```python
def upload_tree(
    session: SftpSession,
    local_dir: str,
    remote_dir: str,
    *,
    resume: bool = False,
    chmod: int | None = None,
    block_size: int = 131072,
    bwlimit_kbs: int | None = None,
    progress: bool = True,
) -> TransferResult:
    # os.walk(local_dir, followlinks=True)
    # for each dir: session.mkdir_p(corresponding remote_dir)
    # for each file: try upload_file(...) → if exception, append to failed_files
    # exit_code = 0 if no failures else 1
    # failure_class = None if exit_code 0 else "remote_error"
```

## 3. CLI entry point (`ssh_upload.py`)

薄壳逻辑：

```python
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # local-not-found guard (cheap, do before opening session)
    if not Path(args.local_path).exists():
        result = json_result(success=False, exit_code=1,
                             stderr=f"local path not found: {args.local_path}",
                             data={"route": "sftp", "failure_class": "remote_error"})
        emit(result, as_json=args.json)
        return 1

    # dir-without-recursive guard
    if Path(args.local_path).is_dir() and not args.recursive:
        result = json_result(success=False, exit_code=1,
                             stderr=f"'{args.local_path}' is a directory; pass --recursive",
                             data={"route": "sftp", "failure_class": "remote_error"})
        emit(result, as_json=args.json)
        return 1

    chmod_int = int(args.chmod, 8) if args.chmod else None

    try:
        with SftpSession(args.alias) as session:
            if args.recursive:
                tr = upload_tree(session, args.local_path, args.remote_path,
                                 resume=args.resume, chmod=chmod_int,
                                 block_size=args.block_size,
                                 bwlimit_kbs=args.bwlimit,
                                 progress=not args.no_progress)
            else:
                tr = upload_file(session, args.local_path, args.remote_path,
                                 resume=args.resume, chmod=chmod_int,
                                 block_size=args.block_size,
                                 bwlimit_kbs=args.bwlimit,
                                 progress=not args.no_progress)
    except paramiko.AuthenticationException as e:
        # map to envelope: exit 1 + failure_class=auth
        ...
    except paramiko.SSHException as e:
        # network/remote ssh-level: exit 1 + failure_class=network or remote_error
        ...
    except PermissionError as e:
        # remote write denied: exit 8
        ...

    result = json_result(
        success=tr.success,
        exit_code=tr.exit_code,
        stderr=tr.stderr,
        data={
            "route": "sftp",
            "alias": args.alias,
            "local_path": args.local_path,
            "remote_path": args.remote_path,
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

## 4. Resume protocol (detailed)

```
RemoteState = sftp.stat(remote) → None | SFTPAttributes
LocalSize = os.path.getsize(local)

case RemoteState:
    None                              → offset=0; transfer; sha256_verify required (resume path)
    size == LocalSize                 → offset=LocalSize; no transfer; sha256_verify required
    size <  LocalSize                 → offset=size; partial transfer; sha256_verify required
    size >  LocalSize                 → fail (exit 1) "remote larger than local; refuse"

sha256_verify:
    h_local = streaming hashlib.sha256(open(local, 'rb'))
    h_remote = streaming hashlib.sha256(sftp.open(remote, 'rb'))
    if h_local.hexdigest() != h_remote.hexdigest(): exit 9
```

## 5. Bandwidth throttle (paramiko has no native)

Implemented in `SftpSession.put_chunked` chunk-loop:

```python
target_bps = bwlimit_kbs * 1024
start = time.monotonic()
bytes_done = offset
while chunk := local.read(block_size):
    remote.write(chunk)
    bytes_done += len(chunk)
    if bwlimit_kbs:
        expected_elapsed = (bytes_done - offset) / target_bps
        actual_elapsed = time.monotonic() - start
        if expected_elapsed > actual_elapsed:
            time.sleep(expected_elapsed - actual_elapsed)
    if progress: progress(bytes_done, total)
```

`time.sleep` 在 chunk 之后；首 chunk 不限速；sha256_verify 阶段不限速（remote read 不计入用户 bwlimit 预期）。

## 6. Progress callback (5% bucket)

```python
def make_pct_callback(filename, total, no_progress):
    if no_progress or total == 0:
        return None
    last_bucket = [-1]
    start = time.monotonic()
    def cb(done, total):
        pct = int(done * 100 / total)
        bucket = pct // 5
        if bucket > last_bucket[0]:
            last_bucket[0] = bucket
            elapsed = time.monotonic() - start
            kbs = done / 1024 / max(elapsed, 0.001)
            print(f"Uploading {filename}: {pct}% ({done}/{total} bytes, {kbs:.1f} KB/s)",
                  file=sys.stderr)
    return cb
```

Recursive 模式额外打印 separator：

```python
for i, (local_path, remote_path) in enumerate(walk_pairs, 1):
    print(f"--- file {i}/{N}: {local_path} ---", file=sys.stderr)
    upload_file(...)
```

## 7. Error mapping

| Paramiko exception | exit_code | failure_class | stderr 模板 |
|---|---|---|---|
| `AuthenticationException` | 1 | auth | "ssh authentication failed for alias '<alias>': <msg>" |
| `SSHException` (含 socket errors) | 1 | network | "ssh transport error to '<alias>': <msg>" |
| `PermissionError` (远端) | 8 | permission | "remote write permission denied: <remote_path>" |
| `FileNotFoundError` (本地) | 1 | remote_error | "local path not found: <local_path>" |
| `IOError` (含 SFTP 错误) | 1 | remote_error | "sftp i/o error: <msg>" |
| sha256 mismatch | 9 | `transfer_verify_failed` (ADR-003 / error-handling.md) | "sha256 verification failed: local=<a> remote=<b>" — envelope 必带 `data.sha256_local` + `data.sha256_remote` |

## 8. Module layout (file list)

```
plugins/ssh-core/skills/ssh-core/scripts/
├── ssh_upload.py                     (rewrite: stub → ~120 loc)
├── ssh_download.py                   (NOT touched in this task)
└── lib/
    ├── __init__.py                   (unchanged)
    └── sftp_transport.py             (NEW, ~400 loc)

.trellis/spec/backend/
└── adr-003-sftp-transport.md         (NEW)

tests/
├── test_ssh_upload_unit.py           (NEW)
└── test_ssh_upload_integration.py    (NEW)
```

## 9. Compatibility notes

- 不影响现有 ssh_execute / ssh_cluster / ssh_config_manager（独立 transport）。
- 增加 paramiko 依赖：所有 plugin 的 install footprint +1 dep（paramiko 与 cryptography 共 ~10MB），可接受。
- `lib/__init__.py` 不动；`lib/sftp_transport.py` 是新增模块，import 路径独立。
- ssh_download 任务交付时，import `from lib.sftp_transport import SftpSession, download_file, download_tree` 即可。

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| paramiko 与 ~/.ssh/config 边角 case 不一致（e.g. `Match Host` 块） | v1.0 仅支持 `Host <alias>` block；遇到 unknown directive 直接忽略（paramiko 默认行为）；在 ADR-003 记录限制 |
| bwlimit 测试不稳定（CI 抖动） | Unit 测 throttle 公式逻辑（mock time）；integration 用 100 KB/s × 1 MB 文件 + ±0.5s 容差 |
| sha256_remote 流式读慢 | block_size=128KB 流式，1GB 文件 ~30s；在 ADR-003 记录这是 verify-path 代价；用户可不传 --resume 走 fast path 跳 verify |
| paramiko 3.x 与 cryptography ABI breakage | 锁版本 `paramiko>=3.0,<4`；CI 跑 pytest 检测早期 |
| recursive 模式打开很多 SFTP file → 远端 file-descriptor 耗尽 | 每个文件 open/close 是同步顺序（不并发），不会同时打开多 fd |

## 11. ADR-003 abstract（先于代码写入 spec）

```
# ADR-003: SFTP transport for ssh_upload / ssh_download

Status: Accepted (2026-05-24)

## Decision
- paramiko (v3+) for SFTP transport; new runtime dep in pyproject.
- New `lib/sftp_transport.py` providing SftpSession (low-level) +
  upload_file/upload_tree/download_file/download_tree (high-level).
- New exit code 9 = transfer_verify_failed (sha256 mismatch on resume path).
- ssh_upload/ssh_download scripts are thin CLI wrappers calling high-level API.

## Consequences
- Runtime dep matrix grows (paramiko + cryptography ~10MB).
- ssh_execute v1.0 remains subprocess+native ssh (no behavior change).
- Future ssh_server_transfer / ssh_cluster sync can reuse SftpSession.
```
