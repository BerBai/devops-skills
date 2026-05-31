# Implementation — ssh-core ssh_upload v1.0

> 配套 PRD: `prd.md`；技术设计: `design.md`；规范立项: `.trellis/spec/backend/adr-003-sftp-transport.md` (D0 交付物)。

## Phase D — 规范先行 (D0/D1/D2)

### D0：写 ADR-003（先于代码）

- [ ] 新建 `.trellis/spec/backend/adr-003-sftp-transport.md`，按 design.md §11 abstract 展开：
  - Context（为什么需要 SFTP transport，PoC 沉淀的 surface docstring）
  - Decision（paramiko v3+ / new lib/sftp_transport.py / exit code 9 / thin CLI 壳）
  - Consequences（依赖矩阵 +paramiko+cryptography、ssh_execute v1.0 不变、未来 server-transfer 复用 SftpSession）
  - Alternatives considered（sftp(1) shell-out、rsync — 参考 PRD D1 rationale）
- [ ] 在 `.trellis/spec/backend/index.md` 添加 ADR-003 链接（如果 index 存在）
- [ ] 在 `.trellis/spec/backend/error-handling.md` 的 exit_code 表追加 `9 = transfer_verify_failed`，引用 ADR-003

**Validation D0**：`adr-003-sftp-transport.md` 完整、`error-handling.md` exit code 表含 9。

### D1：加 paramiko 到 pyproject.toml

- [ ] 编辑 `pyproject.toml`：`dependencies = []` → `dependencies = ["paramiko>=3.0,<4"]`
- [ ] `pip install -e .` 验证安装成功
- [ ] `pytest tests/` 跑一次确认基线不破（v0.2 stub 仍工作）

**Validation D1**：`pip show paramiko` 显示 3.x；默认 pytest 全绿。

### D2：建 transport 模块骨架

- [ ] 新建 `plugins/ssh-core/skills/ssh-core/scripts/lib/sftp_transport.py`
- [ ] 实现 `class SftpSession`：__init__ / __enter__ / __exit__ / close / stat / mkdir_p / chmod
- [ ] 实现 `TransferResult` dataclass
- [ ] 占位 `upload_file / upload_tree / download_file / download_tree` 抛 `NotImplementedError`
- [ ] `python -c "from plugins.ssh_core...lib.sftp_transport import SftpSession; print('ok')"` import 通

**Validation D2**：import 不报错；session connect 到 pai 不报错（手动跑一次）。

## Phase U — Upload 单文件路径 (U1–U3)

### U1：put_chunked + upload_file (fast path, no resume)

- [ ] 实现 `SftpSession.put_chunked(local, remote, *, offset=0, block_size, bwlimit_kbs, progress)` 完整 chunk-loop + throttle + callback
- [ ] 实现 `upload_file(...)` fast path：本地存在检查 → put_chunked(offset=0) → 可选 chmod → 返回 TransferResult
- [ ] 不实现 resume / sha256 / failed_files 这些 U2/U3/U4 才需要的分支

**Validation U1**：手动 `python ssh_upload.py pai ./LICENSE /tmp/license-u1` 成功；`ssh pai sha256sum /tmp/license-u1` == 本地 sha256sum。

### U2：upload_file resume path + sha256 verify

- [ ] 在 `upload_file` 里加入 resume 分支：sftp.stat → 4 个 case (missing/equal/lt/gt) 按 design.md §4
- [ ] 新增 helpers `sha256_local(path)` / `sha256_remote(session, path)` 流式
- [ ] resume 完成后 verify；mismatch → TransferResult(exit_code=9, failure_class=None, sha256_local=..., sha256_remote=...)
- [ ] remote_size > local_size → TransferResult(exit_code=1, stderr="...")

**Validation U2**：
  - 手动测试：本地 10 MB 文件，上传 → kill → `--resume` 再跑 → success + sha256_verified=true
  - 手动测试：人为修改远端 partial（`ssh pai 'dd if=/dev/urandom of=/tmp/x bs=1 count=10 conv=notrunc'`）→ `--resume` → exit_code=9

### U3：CLI 壳 ssh_upload.py 接通 upload_file

- [ ] 重写 `plugins/ssh-core/skills/ssh-core/scripts/ssh_upload.py`：保留 build_parser 不变（surface 已定），main() 按 design.md §3 实现
- [ ] 包含 local-not-found / dir-without-recursive 两道前置 guard
- [ ] 异常映射：paramiko.AuthenticationException → failure_class=auth；SSHException → network/remote_error；PermissionError → exit 8
- [ ] `python ssh_upload.py --help` 列出所有 flag

**Validation U3**：
  - `python ssh_upload.py --help` exit 0
  - `python ssh_upload.py pai ./LICENSE /tmp/license-u3 --json` → envelope success=true
  - `python ssh_upload.py pai /nonexistent /tmp/x --json` → envelope success=false exit 1
  - `python ssh_upload.py pai ./tests /tmp/tests-u3 --json` → exit 1 stderr 含 "use --recursive"

## Phase R — Recursive (R1)

### R1：upload_tree

- [ ] 实现 `upload_tree(session, local_dir, remote_dir, ...)`：
  - `os.walk(local_dir, followlinks=True)`
  - for each dir: `session.mkdir_p(remote_dir_equivalent, mode=0o755)`，已存在忽略 errno=21
  - for each file: try `upload_file(...)`; on exception → append `{path, error}` 到 `failed_files`
  - 末尾：success=(failed_files is empty)、exit_code=0/1、failure_class=None/"remote_error"
- [ ] 远端 path join：始终用 `posixpath.join`（本地 Windows 路径转 POSIX）
- [ ] 在 `ssh_upload.py main()` 里 args.recursive → 调 upload_tree

**Validation R1**：
  - 本地造 dir 树（3 文件 + 1 子 dir）→ `python ssh_upload.py pai ./tmptree /tmp/tree-r1 --recursive --json` → envelope success=true，远端 `ls /tmp/tree-r1` 结构一致
  - 人为给 1 个文件 `chmod 000` 不可读 → `--recursive` 上传 → exit 1 + failed_files 含该文件 + 其他成功

## Phase T — Tests (T1/T2)

### T1：Unit tests

- [ ] 新建 `tests/test_ssh_upload_unit.py`
- [ ] 测试用例（mock paramiko.SSHClient + SFTPClient）：
  - `test_help_does_not_crash`
  - `test_local_not_found_returns_1`
  - `test_dir_without_recursive_returns_1`
  - `test_fast_path_uploads_and_calls_chmod`（mock SFTPClient.put_chunked + chmod）
  - `test_resume_missing_remote_falls_back_to_fast_path`
  - `test_resume_equal_size_skips_transfer_runs_verify`
  - `test_resume_partial_continues_from_offset`
  - `test_resume_remote_larger_returns_1`
  - `test_resume_sha256_mismatch_returns_9`
  - `test_bwlimit_throttles`（mock `time.monotonic` 与 `time.sleep`，断言 sleep 被调用且参数大致合理）
  - `test_progress_emits_at_5pct_buckets`
  - `test_recursive_continues_on_error_reports_failed_files`
  - `test_chmod_applied_to_files_not_dirs`
- [ ] `pytest tests/test_ssh_upload_unit.py -v` 全绿

**Validation T1**：unit 套件全绿，coverage 覆盖 R3–R10。

### T2：Integration tests (live_ssh)

- [ ] 新建 `tests/test_ssh_upload_integration.py` with `pytestmark = pytest.mark.live_ssh`
- [ ] skip 守卫：`_ssh_agent_has_key` + `_host_reachable("pai")`（复用 ssh_execute integration 模板）
- [ ] 测试用例（每个测试唯一 `/tmp/upload-test-<uuid>` 路径，末尾 cleanup）：
  - `test_single_file_happy_path`：上传 LICENSE，远端 sha256 == 本地
  - `test_resume_partial_then_complete`：tempfile 10MB → 部分上传 → resume → 完整 sha256 verify
  - `test_resume_mismatch_returns_9`：partial → 远端追加 noise → resume → exit 9
  - `test_recursive_simple_tree`：3 文件 + 1 子 dir → 远端 ls -R 验证
  - `test_chmod_applied`：上传 → ssh pai stat -c %a → 验证
  - `test_bwlimit_100kbs_roughly_10s`：100 KB/s × 1MB ≈ 10s ±2s
  - `test_recursive_partial_failure_returns_1_with_failed_files`
- [ ] `pytest tests/test_ssh_upload_integration.py -m live_ssh -v` 全绿在 pai
- [ ] 添加 `tests/README.md` "What's covered today" 一项：ssh_upload unit + integration

**Validation T2**：live_ssh 套件全绿；pai 无残留 `/tmp/upload-test-*`。

## Final validation commands

```bash
# 基线
pytest tests/                                                 # 默认套件全绿
# Unit
pytest tests/test_ssh_upload_unit.py -v                       # 13 个 test 全绿
# Integration
pytest tests/test_ssh_upload_integration.py -m live_ssh -v    # 7 个 test 全绿
# Lint
ruff check plugins/ssh-core/skills/ssh-core/scripts/ssh_upload.py
ruff check plugins/ssh-core/skills/ssh-core/scripts/lib/sftp_transport.py
mypy plugins/ssh-core/skills/ssh-core/scripts/ssh_upload.py
# 残留检查
ssh pai 'ls -la /tmp/upload-test-* 2>/dev/null; ls -la /tmp/license-* 2>/dev/null'  # 应空
# Smoke
python plugins/ssh-core/skills/ssh-core/scripts/ssh_upload.py pai ./LICENSE /tmp/license-smoke --json
ssh pai 'sha256sum /tmp/license-smoke && rm /tmp/license-smoke'
```

## Review gates

- 进 U1 前：D0/D1/D2 全部完成（ADR-003 写好、paramiko 装好、SftpSession import 通）
- 进 U2 前：U1 单文件 fast path 手测通 pai
- 进 U3 前：U2 resume + sha256 两条路径手测通
- 进 R1 前：U3 CLI 壳完整、--help 显示所有 flag、3 个 guard 路径手测通
- 进 T1 前：R1 recursive 手测含 partial-failure 路径
- 进 T2 前：T1 unit 全绿
- 完成前：用户 review 全部验证命令输出 + 报告 `data.failed_files` 等 JSON 字段是否符合 PRD R8

## Rollback points

- D0 (ADR-003) 是文档操作，纯文本可 git checkout 撤销
- D1 (pyproject 加 paramiko) 是单行改动，`git checkout pyproject.toml` 撤销 + `pip uninstall paramiko cryptography`
- D2-R1 任何阶段失败：`git checkout plugins/ssh-core/skills/ssh-core/scripts/`（不影响其他 skill）
- 整个任务 abort：`git checkout main` + `pip uninstall paramiko cryptography`，不留痕迹
