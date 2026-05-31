# ship ssh-core ssh_download v1.0

## Goal

把 `ssh_download.py` 从 v0.2 stub (`unimplemented()`) 推到 v1.0 完成态：通过 SFTP 把远端文件/目录拉到本地，支持断点续传、recursive、限速、5% 进度，遵循统一 JSON envelope。**直接复用 ssh_upload 任务沉淀的 `lib/sftp_transport.py`**，本任务不重写 transport。

## User value

- 用户敲 `python ssh_download.py prod-web-01 /var/log/app.log ./logs/` 即可拉日志，不依赖远端 rsync。
- `--resume` 处理网络中断后的续传，sha256 verify 保证完整性。
- `--bwlimit` 控制带宽避免影响生产网络。
- `--json` 让 Claude Code 解析后链式处理（如下载完后立刻分析）。

## Confirmed facts (from repo inspection)

1. **CLI surface 已固定**（`ssh_download.py` surface docstring）：
   ```
   ssh_download.py <alias> <remote-path> <local-path>
       [--resume] [--recursive] [--no-progress]
       [--bwlimit KB/s] [--block-size 131072]
       [--json]
   ```
   - **与 upload 的参数顺序对称反转**：upload 是 `<alias> <local> <remote>`，download 是 `<alias> <remote> <local>`。
   - **没有 `--chmod`**：下载到本地，用户用 OS umask + os.chmod 自己控制，不需要透传 CLI flag。

2. **依赖**：ssh_upload 任务已经把 `paramiko>=3.0,<4` 加到 `pyproject.toml` 并立项 ADR-003。本任务**不需要**改 pyproject 或写新 ADR。

3. **Transport 模块**：ssh_upload 任务交付了 `lib/sftp_transport.py`，含：
   - `class SftpSession`（与方向无关，复用）
   - `def download_file(...) / def download_tree(...)`（design.md §2 已定义签名）
   - `TransferResult` dataclass、`sha256_remote`、`make_pct_callback`、`throttle` 等 helpers（与方向无关，复用）

4. **JSON envelope** 与 exit code 与 ssh_upload 完全一致（共享 ADR-003）。

5. **测试基线**：与 upload 一致，复用 POC_HOST=pai + live_ssh marker。

## Decisions (继承自 ssh_upload 任务 + 方向反转差异)

| # | Decision | 来源 |
|---|---|---|
| Inherits D1–D6 | 全部继承 ssh_upload PRD 的 D1–D6 | 参考 `.trellis/tasks/05-24-ssh-core-ssh-upload/prd.md` |
| DD1 | 方向反转：sha256 verify 对比方向 = 远端 vs 本地（同 upload，对称），不需新决策 | surface docstring |
| DD2 | 远端文件不存在 → exit_code=1 + failure_class=remote_error + stderr "remote path not found"（对称 upload 的本地不存在） | 与 upload R3 对称 |
| DD3 | 远端是目录但未传 `--recursive` → exit_code=1 + stderr "use --recursive"（对称 upload 的 dir 检查） | 与 upload R3 对称 |
| DD4 | 本地路径已存在且为 dir + 未传 --recursive：失败 (拒绝把单文件下载到 dir 里)；本地路径已存在且为 file：默认 overwrite（对称 upload 默认 overwrite 远端） | 与 upload R3 对称 |
| DD5 | 本地写权限拒绝 → exit_code=8 (permission)；远端读权限拒绝 → exit_code=8（同一个 errno class） | 复用现有 permission 语义 |
| DD6 | Recursive 远端 walk：`sftp.listdir_attr(remote_dir)` 递归（paramiko 无 walk helper，手写 generator） | paramiko 限制 |

## Requirements

### R1 — CLI surface
- 严格按 surface docstring 实现 5 个 flag + 3 个 positional：`alias`, `remote_path`, `local_path` + `--resume / --recursive / --no-progress / --bwlimit / --block-size / --json`。
- `--block-size` 默认 131072（128 KiB），与 upload 一致。
- 无 `--chmod`。
- `--help` 标准 argparse 不 crash。

### R2 — Transport (复用 ssh_upload 沉淀)
- 直接 `from lib.sftp_transport import SftpSession, download_file, download_tree, TransferResult`。
- **本任务不修改 lib/sftp_transport.py 的现有接口**；如有 bug 反馈给 upload 任务的 transport 层（或开 follow-up task）。
- 允许在 lib/sftp_transport.py **追加** download-only helpers（如果 design.md 已预留的不够用），但不改 upload 已经依赖的签名。

### R3 — 单文件下载（fast path）
- 远端 path 必须存在且 stat OK，否则 exit_code=1 (DD2)。
- 远端是 dir 但未 --recursive → exit_code=1 (DD3)。
- 本地 path：
  - 不存在 → 直接写入。
  - 是文件 → overwrite (DD4)。
  - 是 dir 且未 --recursive → 失败 (DD4)。
- 默认 chunk-by-chunk，block_size 透传 `SftpSession.get_chunked`。
- `data.bytes_transferred / data.elapsed_s / data.path_remote / data.path_local / data.sha256_verified (bool)` 上报。

### R4 — Resume (`--resume`)
- stat 本地文件（不是远端，与 upload 方向反转）：
  - 不存在 → 等同 fast path，offset=0。
  - 存在且 `local_size == remote_size` → 直接 sha256 verify，不重传。
  - 存在且 `local_size < remote_size` → seek 本地到 local_size，从远端 offset=local_size 续读。
  - 存在且 `local_size > remote_size` → exit_code=1 + stderr "local larger than remote; refuse to truncate"。
- 续写完成后 **必跑 sha256 verify**（mirror upload R4）；mismatch → exit_code=9 + `data.sha256_local / data.sha256_remote`。

### R5 — Recursive (`--recursive`)
- 远端 walk：`sftp.listdir_attr(remote_dir)` → 区分 file/dir（用 `stat.S_ISDIR(entry.st_mode)`） → 递归
- 本地先 `os.makedirs(corresponding_local_dir, exist_ok=True)`。
- 每个远端 file 按 R3 下载；单文件失败 → 收 `data.failed_files`，不中断 batch（mirror upload R5）。
- Batch 收尾同 upload R5：全成功 → 0；部分失败 → 1。

### R6 — Progress / R7 — Bwlimit / R8 — JSON envelope / R9 — exit codes
- 与 ssh_upload 完全一致。复用 `make_pct_callback`（**调用时传 `verb="Downloading"`**，该参数在 ssh_upload Phase U 的 Fixup A 已经加上）+ throttle 逻辑（已在 lib/sftp_transport.py）。
- JSON `data.route = "sftp"`（与 upload 同 route 名）+ `data.direction = "download"`（区分 upload）。
- `failure_class` 枚举与 upload PRD R8 完全一致：`None / "auth" / "network" / "remote_error" / "permission" / "timeout" / "transfer_verify_failed"`。
- exit code 表与 upload PRD R9 完全一致：`0` (None) / `1` (auth/network/remote_error) / `8` (permission) / `9` (transfer_verify_failed，envelope 必带 `data.sha256_local` + `data.sha256_remote`) / `124` (timeout，v1.1)。

### R10 — Windows safety
- 本地 path 用 `os.path.join`；远端 path 用 `posixpath.join`。
- 与 upload R10 对称。

### R11 — Testing
- **Unit** (`tests/test_ssh_download_unit.py`)：mirror ssh_upload 的 22 个 test 结构（实际数量约 17–20，因为缺 `--chmod` 类不需要镜像，但加 download 特有的 `remote_not_found / remote_is_dir / local_is_dir` 3 个 guard）：
  - `test_help_does_not_crash`
  - `test_remote_not_found_returns_1`
  - `test_remote_is_dir_without_recursive_returns_1`
  - `test_local_is_dir_without_recursive_returns_1`
  - `test_fast_path_downloads`
  - `test_resume_missing_local_falls_back_to_fast_path`
  - `test_resume_equal_size_skips_transfer_runs_verify`
  - `test_resume_partial_continues_from_offset`
  - `test_resume_local_larger_returns_1`
  - `test_resume_sha256_mismatch_returns_9`
  - `test_bwlimit_throttles`
  - `test_progress_emits_at_5pct_buckets`
  - `test_recursive_continues_on_error_reports_failed_files`
- **Integration** (`tests/test_ssh_download_integration.py`, marker `live_ssh`)：
  - `test_single_file_happy_path`：下载 `/etc/hostname` → 验证内容
  - `test_resume_partial_then_complete`：远端造 10MB tempfile → 部分下载 → resume → sha256 verify
  - `test_resume_mismatch_returns_9`：partial → 本地损坏 → resume → exit 9
  - `test_recursive_simple_tree`：远端造 3 文件 + 1 子 dir → 下载 → 本地结构验证
  - `test_bwlimit_100kbs_roughly_10s`
  - `test_recursive_partial_failure_returns_1_with_failed_files`：远端造 1 个 0o000 文件 → 其他成功
  - cleanup：远端 `/tmp/download-fixture-<uuid>` 和本地 `tmp_path`（pytest 自动管理）

## Acceptance Criteria

- [ ] `python ssh_download.py --help` exit 0 + 列出所有 5 个 flag (无 --chmod)。
- [ ] `pytest tests/test_ssh_download_unit.py` 全绿，覆盖 R3–R10。
- [ ] `pytest tests/test_ssh_download_integration.py -m live_ssh -v` 全绿在 pai。
- [ ] 单文件 happy path：`ssh_download.py pai /etc/hostname /tmp/h-test --json` → success=true，本地内容 == `ssh pai cat /etc/hostname`。
- [ ] Resume：本地 5MB 部分下载 → kill → `--resume` → success=true，`data.sha256_verified=true`。
- [ ] Resume mismatch：人为修改本地 partial → `--resume` → exit_code=9。
- [ ] Recursive：远端临时 dir 树 → 全部 file 下载成功，本地目录结构与远端一致。
- [ ] Recursive partial failure：远端 1 个不可读文件 → 其他成功，`data.failed_files` 含失败项，exit_code=1。
- [ ] `--bwlimit 100`：100 KB/s × 1MB ≈ 10s ±2s。
- [ ] `--no-progress`：stderr 无 progress 行。
- [ ] JSON envelope 含 `data.direction="download"` 区分 upload。
- [ ] `tests/README.md` "What's covered today" 已追加 ssh_download unit + integration。
- [ ] SKILL.md 不需要改（surface 已准确）。
- [ ] **不**修改 lib/sftp_transport.py 的现有公开接口（除非追加新函数）。

## Out of scope

- 修改 lib/sftp_transport.py 的 upload 路径（属于上一任务责任）。
- 修改 ADR-003（共享同一份）。
- 修改 pyproject.toml（paramiko 已加）。
- 修改 SKILL.md（surface 已准确）。
- mtime/owner 保留（v1.1）、密码 auth（v1.1）、超时（v1.1）。

## Dependency on sibling task

- **必须先完成** `05-24-ssh-core-ssh-upload`（沉淀 `lib/sftp_transport.py` 与 ADR-003）才能 task.py start 本任务。
- 如果在 upload 任务实现过程中发现 transport 抽象缺漏，记到本任务 PRD 的 "Open questions" 而不是直接改 transport。
