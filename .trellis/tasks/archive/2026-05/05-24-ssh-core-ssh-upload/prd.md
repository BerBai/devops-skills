# ship ssh-core ssh_upload v1.0

## Goal

把 `ssh_upload.py` 从 v0.2 stub (`unimplemented()`) 推到 v1.0 完成态：通过 SFTP 将本地文件/目录上传到远端 alias，支持断点续传、recursive、限速、远端 chmod、5% 进度，遵循仓库统一的 JSON envelope 契约（spec/backend/error-handling.md）。下游目标：ssh_download 完全复用本任务沉淀的 transport 层。

## User value

- 用户敲 `python ssh_upload.py prod-web-01 ./release.tar.gz /srv/releases/ --resume` 即断点续传，不依赖远端有 rsync。
- 带 `--json` 输出统一 envelope，AI agent（Claude Code）可解析后链式调用。
- `--bwlimit` 控制带宽，避免抢线上业务流量。
- `--chmod 0755` 一次设置远端文件 mode，少发一次 ssh chmod。

## Confirmed facts (from repo inspection)

1. **CLI surface 已固定**（`ssh_upload.py` surface docstring）：
   ```
   ssh_upload.py <alias> <local-path> <remote-path>
       [--resume] [--recursive] [--no-progress]
       [--chmod 0644] [--bwlimit KB/s] [--block-size 131072]
       [--json]
   ```
2. **JSON envelope** (`spec/backend/error-handling.md` + `adr-001-ssh-execute.md` D3)：5 字段 `success/exit_code/stdout/stderr/data`；`data.failure_class` ∈ {None, network, auth, remote_error, timeout, ...}。新 exit code 须配套 ADR。
3. **共享 lib** (`lib/__init__.py`)：已提供 `json_result/emit/msys_safe_env/filter_ssh_noise/classify_failure`；**无 SFTP helper**，本任务新建。
4. **依赖现状**：`pyproject.toml` `dependencies = []`，本任务需 add `paramiko>=3.0`。ADR-001 D1 不冲突（那是针对 ssh_execute 的 transport 选择）。
5. **测试基线**：marker `live_ssh` 已注册；POC_HOST=pai；unit 走 monkeypatch SFTPClient，integration 跳过条件 = ssh-agent 无 key 或 host 不可达。
6. **SKILL.md Quick reference** 已暴露 `ssh_upload.py ... --resume` 用户接口；`~/.ssh/config` 是 single source of truth → paramiko 必须能解析 OpenSSH config (ProxyJump / IdentityFile / agent forwarding)。
7. **Surface docstring 的 resume 算法**：SFTP stat → 若 exists 且 `remote_size < local_size`，seek 双端到 `remote_size` 续写 → EOF 后两端 sha256 对比，mismatch 即 fail。

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | 底层用 paramiko (v3+)。不是 sftp(1) shell-out，也不是 rsync。 | 只有 paramiko 能精确做 chunk-level seek、offset 续传、5% callback、streaming sha256；surface docstring 已含 SFTP-specific 语义；新 dep 在 ADR-003 立项。 |
| D2 | sha256 verify 仅在 `--resume` 路径必跑；from-scratch 上传 fast path 不校验；mismatch 用新 exit code **9 = transfer_verify_failed**。 | 与 surface docstring 原文 "After EOF, compare sha256 of both ends" 一致（绑 --resume）；fast path 与 scp 行为对齐；mismatch 不能复用现有 8/124 的语义，需新立项。 |
| D3 | Recursive profile = **scp-like**：跟随 symlinks、不保留 mtime/owner、保留 source mode (--chmod 覆盖)、隐藏文件全传、默认 overwrite、continue-on-error (`data.failed_files` 末尾汇总，exit_code=1 标 batch-partial-failure)。 | scp 语义熟悉度最高；continue-on-error 让大批量上传不会因单文件 perms 全 abort；rsync-a 的 metadata 保留可推后到 v1.1 加 `--preserve-times` flag。 |
| D4 | (a) `--bwlimit` 单位 KB/s = 1024 B/s（与 rsync 对齐）。(b) `--chmod` 应用到上传的文件（不含 dir）；dir 用 0o755 创建（受远端 umask 影响）。(c) paramiko 加到 `[project] dependencies`（runtime），不是 optional extra。(d) 本地文件不存在 → exit_code=1 + failure_class=remote_error，stderr 明文。远端路径不可写 → 复用 exit_code=8 (permission)。 | (a) rsync 已建立的用户惯例；(b) chmod 文件用户最常需要、dir mode 用 ssh chmod 后置；(c) SKILL.md 已把 upload/download 当核心能力，optional 等于自废；(d) 不新开 exit code，复用现有语义。 |
| D5 | Transport 模块 hybrid 抽象：`lib/sftp_transport.py` 含 (i) low-level `SftpSession`（open/close/chmod/mkdir/stat/putfo/getfo with callback），(ii) high-level `upload_file / upload_tree / download_file / download_tree` 函数。Script = argparse + 调 high-level + emit JSON。 | 下游 ssh_download 0 改动复用 high-level；将来 ssh_server_transfer/ssh_cluster sync 复用 SftpSession；脚本 ~80-120 loc 薄壳易测试。 |
| D6 | ADR-003 落 `.trellis/spec/backend/adr-003-sftp-transport.md`，**先于 transport 代码写**。 | 与 ADR-001/002 同层，下游 download 任务跨任务引用规范；implement.md 首步即 "write ADR-003"，避免代码先行 spec 后补。 |

## Requirements

### R1 — CLI surface
- 严格按 surface docstring 实现 7 个 flag + 3 个 positional：`alias`, `local_path`, `remote_path` + `--resume / --recursive / --no-progress / --chmod / --bwlimit / --block-size / --json`。
- `--block-size` 默认 131072 (128 KiB)。`--no-daemon` 不需要（仅 ssh_execute 有，且是 v0.2 backwards-compat shim）。
- `--help` 标准 argparse，不 crash。

### R2 — Transport (paramiko)
- 通过 `paramiko.SSHConfig().parse(~/.ssh/config)` 解析 alias（含 `--ssh-config <path>` override 透传给 paramiko）。
- 支持 ssh-agent；不支持 password auth (v1.0 假设 key auth；与 ssh_execute 对齐)。
- 支持 ProxyJump（paramiko 3.x 原生）。
- 一个 SFTP session per script invocation，跑完即关。

### R3 — 单文件上传（happy path）
- 本地路径必须存在且可读，否则 exit_code=1。
- `os.path.isdir(local)` 但未传 `--recursive` → exit_code=1 + stderr "use --recursive for directories"。
- 默认 chunk-by-chunk write，每 chunk = `--block-size` bytes。
- 远端文件已存在且**未传 --resume** → overwrite。
- 写入完成后 `sftp.chmod(remote, args.chmod)` 仅当传了 `--chmod`。
- `data.bytes_transferred / data.elapsed_s / data.path_remote / data.path_local / data.sha256_verified (bool)` 上报。

### R4 — Resume (`--resume`)
- SFTP stat 远端：
  - 不存在 → 等同 from-scratch，走 R3 (fast path)。
  - 存在且 `remote_size == local_size` → 走 sha256 verify 直接，不重传。
  - 存在且 `remote_size < local_size` → seek 本地到 `remote_size`，open 远端 mode='r+b' seek 到 `remote_size` 续写。
  - 存在且 `remote_size > local_size` → exit_code=1 + stderr "remote larger than local; refuse to truncate"。
- 续写完成后 **必跑 sha256**：本地 hashlib 流式 + 远端 SFTP open(remote, 'rb') 流式 read + hashlib。Mismatch → **exit_code=9** + `data.sha256_local / data.sha256_remote`。
- `data.sha256_verified=true` 且 `data.bytes_resumed_from = <remote_size_before>`。

### R5 — Recursive (`--recursive`)
- `os.walk(local)` 跟随 symlinks（symlinks 指向的真实文件被上传，链接结构不保留）。
- 远端先 `sftp.mkdir(remote_dir, mode=0o755)`，若 exists 忽略 errno=21。
- 每个文件按 R3 上传（无论 --resume 与否，每个文件独立判定）。
- 单文件失败：捕获异常 → 追加到 `data.failed_files: [{path, error}]`，**不中断 batch**。
- Batch 收尾：
  - 全部成功 → exit_code=0, success=true。
  - 部分失败 → exit_code=1, success=false, `data.failure_class=remote_error`, stderr 列出失败文件数。
  - 全部失败 → 同上。

### R6 — Progress (`--no-progress` 关掉)
- paramiko `SFTPClient.putfo(callback=cb)` 的 `cb(transferred, total)` 计算百分比，**每 5%** 打印到 **stderr** 一行：
  ```
  Uploading <local_filename>: 25% (262144/1048576 bytes, 850.2 KB/s)
  ```
- `--no-progress` 时整段静默。
- `--json` 模式下 progress 仍打到 stderr (不污染 stdout JSON)。
- Recursive 模式：每个文件一个 progress lifecycle，文件间打印 separator line `--- file <i>/<N>: <path> ---`。

### R7 — Bwlimit (`--bwlimit`)
- 在 chunk-loop 内实现：每次 write 后比对实际速率与目标速率，sleep `max(0, expected_elapsed - actual_elapsed)`。
- 实现位置：transport 层（`lib/sftp_transport.py`），所以 download 自动继承。
- 单位 KB/s = 1024 B/s。
- 不限制 sha256 verify 阶段的远端 read（verify 是 metadata-pace，量级远小于 transfer）。

### R8 — JSON envelope
- 5 字段统一，符合 `spec/backend/error-handling.md`。
- `data` 字段：`route="sftp"` (与 ssh_execute 的 local/remote 区分)、`alias`、`local_path`、`remote_path`、`recursive (bool)`、`resume (bool)`、`bytes_transferred (int)`、`elapsed_s (float)`、`sha256_verified (bool)`、`sha256_local / sha256_remote (only on mismatch)`、`bytes_resumed_from (int, only when resume)`、`failed_files (list, only when recursive)`、`failure_class`。
- `failure_class` 枚举：`None / "auth" / "network" / "remote_error" / "timeout" / "permission" / "transfer_verify_failed"`（后者由 ADR-003 立项，绑 exit 9）。

### R9 — 错误码 (含已立项 + 新立项 9)
| Code | failure_class | Trigger |
|---|---|---|
| 0 | `None` | 全部成功 |
| 1 | `"auth"` / `"network"` / `"remote_error"` | 通用失败（本地不存在 / `--recursive` 缺失 / batch 部分失败 / ssh auth or transport / 其他 raw fail） |
| 8 | `"permission"` | 远端 permission 拒绝（已立项） |
| 9 | `"transfer_verify_failed"` | **NEW**: sha256 verify failed (resume path) — 新 ADR-003 立项；envelope 必带 `data.sha256_local` + `data.sha256_remote` |
| 124 | `"timeout"` | 超时（如果加 --timeout，v1.0 不加；推到 v1.1） |

### R10 — Windows safety
- 所有 subprocess 调用（如果有）走 `msys_safe_env()`（MSYS_NO_PATHCONV=1）。
- paramiko 不走 subprocess，但本地文件路径 `os.path.join` 在 Windows 用反斜杠 → 远端 path 必须强制正斜杠（POSIX 远端）。

### R11 — Testing
- **Unit** (`tests/test_ssh_upload_unit.py`)：monkeypatch `paramiko.SSHClient` + `SFTPClient`；覆盖
  - argv parse / --help 不 crash
  - 本地不存在 → exit 1
  - dir 无 --recursive → exit 1
  - remote_size > local_size on --resume → exit 1
  - sha256 mismatch → exit 9
  - --chmod 应用
  - --bwlimit throttle 逻辑（mock time.monotonic）
  - recursive failed_files 聚合
- **Integration** (`tests/test_ssh_upload_integration.py`, marker `live_ssh`)：
  - 单文件上传 happy path → pai：`/tmp/upload-test-<pid>` 临时文件，验证 size + sha256
  - --resume：先上传一半（kill 模拟）→ resume → 验证完整 sha256
  - --recursive：临时 dir 树（3 文件 + 1 子 dir）→ 验证文件数 + content
  - --chmod 0600：验证远端 `stat -c %a`
  - 隐式 cleanup：每个 test 末尾 `ssh pai rm -rf /tmp/upload-test-<pid>`
- pytest 默认套件全绿；live_ssh 套件在 pai 上可通过。

## Acceptance Criteria

- [ ] `python ssh_upload.py --help` 正常退出（exit 0）+ 列出所有 7 个 flag。
- [ ] `pytest tests/test_ssh_upload_unit.py` 全绿，覆盖 R3–R10。
- [ ] `pytest tests/test_ssh_upload_integration.py -m live_ssh -v` 全绿在 pai 上。
- [ ] 单文件 happy path：`ssh_upload.py pai ./LICENSE /tmp/license-test --json` → success=true，远端 sha256 == 本地 sha256。
- [ ] Resume：本地 10MB 文件 → 上传一半 kill → `--resume` → success=true，`data.sha256_verified=true`。
- [ ] Resume mismatch：人为损坏远端 partial → `--resume` → exit_code=9，`data.sha256_local != data.sha256_remote`。
- [ ] Recursive：临时 dir 树 → 全部 file 上传成功，远端目录结构与本地一致。
- [ ] Recursive partial failure：1 文件无权限 → 其他成功，`data.failed_files` 含失败项，exit_code=1。
- [ ] `--chmod 0600`：远端 stat 出来确实是 0600。
- [ ] `--bwlimit 100`：100 KB/s 上传 1MB 文件耗时约 10±0.5s（粗校验）。
- [ ] `--no-progress`：stderr 无 progress 行（仅 JSON 模式 stdout 仍出 envelope）。
- [ ] JSON envelope 包含 R8 列出的所有 `data` 字段（按 trigger 条件）。
- [ ] ADR-003 (.trellis/spec/backend/adr-003-sftp-transport.md) 已写：立项 paramiko 依赖 + exit_code 9 + transport 模块抽象边界。
- [ ] `pyproject.toml` `dependencies` 已加 `paramiko>=3.0,<4`。
- [ ] `tests/README.md` "What's covered today" 已追加 ssh_upload unit + integration test。
- [ ] SKILL.md 不需要改（surface 已在 Quick reference 里准确暴露）。

## Out of scope

- Password / keyboard-interactive auth（与 ssh_execute 一致，推 v1.1）。
- 保留 mtime / owner / preserve symlinks（推 v1.1 `--preserve-times` / `--preserve-links`）。
- `--exclude` / `--include` 过滤（不在 surface）。
- `--delete` 远端孤儿文件（不在 surface；这是 rsync sync 语义，非 upload）。
- 上传过程中的超时 `--timeout`（v1.0 假设网络稳定；v1.1 加）。
- 加密层之外的压缩 `-z`（paramiko 没有原生 compression hook）。
- ControlMaster / 连接池复用（与 ssh_execute v1.0 一致，v1.1 评估）。
- 修改 SKILL.md（surface 已经准确）。
- 修改 ssh_download.py（独立 sibling 任务，仅共享本任务沉淀的 `lib/sftp_transport.py`）。

## Open questions blocking planning

(已全部解决)

## Cross-task notes

- 本任务沉淀的 `lib/sftp_transport.py` 是 ssh_download 任务直接复用的核心。download 任务的 PRD 只需描述方向反转的差异（无 --chmod、stat 本地而非远端、sha256 对比方向反转）。
- ADR-003 同时覆盖 upload 与 download 的 paramiko 决策与 transport 抽象边界，避免重复立项。
