# Implementation — ssh-core ssh_download v1.0

> 配套 PRD: `prd.md`；技术设计: `design.md`。
> **依赖**：sibling 任务 `05-24-ssh-core-ssh-upload` 必须先完成（已交付 lib/sftp_transport.py + ADR-003 + paramiko 依赖）。

## Pre-flight 检查（task.py start 之前）

- [ ] `.trellis/spec/backend/adr-003-sftp-transport.md` 存在（由 upload 任务交付）
- [ ] `pip show paramiko` 显示 3.x（由 upload 任务装上）
- [ ] `plugins/ssh-core/skills/ssh-core/scripts/lib/sftp_transport.py` 存在
- [ ] `python -c "from plugins.ssh_core.skills.ssh_core.scripts.lib.sftp_transport import SftpSession, download_file, download_tree"` import 通
- [ ] `pytest tests/test_ssh_upload_unit.py tests/test_ssh_upload_integration.py -m 'live_ssh or not live_ssh'` 全绿

如果任何一项不满足 → STOP，转回 upload 任务先完成。

## Phase F — transport 层补全（如必要）

### F1：检查 download_file / download_tree 是否需要补完

- [ ] 读 `lib/sftp_transport.py`：upload 任务可能把 download_* 留成 `raise NotImplementedError("see ssh_download task")`
- [ ] 如果是 placeholder：实现 `download_file` 按 design.md §3
- [ ] 如果是 placeholder：实现 `download_tree` 按 design.md §4（含 remote_walk generator）
- [ ] 如果已完整：跳过 F1，直接进 F2

**Validation F1**：`python -c "from lib.sftp_transport import download_file; print(download_file.__doc__)"` 显示真实实现 docstring 而非 NotImplementedError。

### F2：手动 smoke transport

- [ ] 写一个 throwaway 脚本：用 SftpSession + download_file 拉 `pai:/etc/hostname` 到 `/tmp/hostname-smoke`
- [ ] 对比内容 == `ssh pai cat /etc/hostname`
- [ ] 删除 throwaway 脚本

**Validation F2**：手动 smoke 通过。

## Phase C — CLI 壳 ssh_download.py

### C1：重写 ssh_download.py

- [ ] 保留 build_parser（surface 已定，不动）
- [ ] 重写 main()：按 design.md §2
  - local-is-dir-without-recursive guard
  - try with SftpSession：args.recursive → download_tree；否则 download_file
  - paramiko 异常映射（AuthenticationException / SSHException / PermissionError）
  - json_result 包 envelope，data 含 `direction="download"`
- [ ] `python ssh_download.py --help` exit 0，列出所有 5 个 flag（无 --chmod）

**Validation C1**：
- `python ssh_download.py --help` exit 0
- `python ssh_download.py pai /etc/hostname /tmp/h-c1 --json` → envelope success=true
- 对比 `cat /tmp/h-c1` == `ssh pai cat /etc/hostname`

### C2：3 个 guard 路径手测

- [ ] 远端不存在：`python ssh_download.py pai /nonexistent /tmp/x --json` → exit 1 + stderr "remote path not found"
- [ ] 远端是 dir 不传 --recursive：`python ssh_download.py pai /etc /tmp/etc-no-recur --json` → exit 1 + "remote ... is a directory; pass --recursive"
- [ ] 本地是 dir 不传 --recursive：`mkdir /tmp/dir-c2 && python ssh_download.py pai /etc/hostname /tmp/dir-c2 --json` → exit 1 + "local ... is a directory"
- [ ] cleanup：`rm -rf /tmp/h-c1 /tmp/dir-c2`

**Validation C2**：3 个 guard 都返回 exit 1 + 明确 stderr。

## Phase RD — Resume + Recursive 手测

### RD1：Resume 手测

- [ ] 远端造 10MB tempfile：`ssh pai 'dd if=/dev/urandom of=/tmp/dl-resume-fixture bs=1M count=10'`
- [ ] 部分下载然后中断：`python ssh_download.py pai /tmp/dl-resume-fixture /tmp/dl-resume-local`（在 ~50% 时 ctrl-c）
- [ ] 检查本地 size 小于 10MB
- [ ] `python ssh_download.py pai /tmp/dl-resume-fixture /tmp/dl-resume-local --resume --json` → success=true, `data.sha256_verified=true`, `data.bytes_resumed_from` 非零
- [ ] cleanup：`rm /tmp/dl-resume-local && ssh pai 'rm /tmp/dl-resume-fixture'`

**Validation RD1**：resume happy path 通过。

### RD2：Resume mismatch 手测

- [ ] 远端造 fixture：`ssh pai 'echo "remote-content-original" > /tmp/dl-mismatch-fixture'`
- [ ] 本地造 partial：`echo "tampered-local-content" > /tmp/dl-mismatch-local`（不同内容、同 size）
- [ ] 实际让 partial size 比远端小：用 `truncate -s 10 /tmp/dl-mismatch-local` 让本地 10 bytes
- [ ] `python ssh_download.py pai /tmp/dl-mismatch-fixture /tmp/dl-mismatch-local --resume --json` → exit_code=9, `data.sha256_local != data.sha256_remote`
- [ ] cleanup

**Validation RD2**：mismatch 返回 exit 9 + sha256 字段。

### RD3：Recursive 手测

- [ ] 远端造 dir 树：
  ```
  ssh pai 'mkdir -p /tmp/dl-tree/sub && echo a > /tmp/dl-tree/a.txt && echo b > /tmp/dl-tree/sub/b.txt && echo c > /tmp/dl-tree/c.txt'
  ```
- [ ] `python ssh_download.py pai /tmp/dl-tree /tmp/dl-tree-local --recursive --json` → success=true
- [ ] 验证 `ls -R /tmp/dl-tree-local` 含 a.txt / sub/b.txt / c.txt
- [ ] cleanup：`rm -rf /tmp/dl-tree-local && ssh pai 'rm -rf /tmp/dl-tree'`

**Validation RD3**：recursive 完整下载。

### RD4：Recursive partial failure 手测

- [ ] 远端造 dir 树，含 1 个 0o000 文件：
  ```
  ssh pai 'mkdir -p /tmp/dl-partial && echo ok > /tmp/dl-partial/readable.txt && echo no > /tmp/dl-partial/blocked.txt && chmod 000 /tmp/dl-partial/blocked.txt'
  ```
- [ ] `python ssh_download.py pai /tmp/dl-partial /tmp/dl-partial-local --recursive --json` → exit_code=1, `data.failed_files` 含 blocked.txt, readable.txt 已下载
- [ ] cleanup：`rm -rf /tmp/dl-partial-local && ssh pai 'rm -rf /tmp/dl-partial'`

**Validation RD4**：partial failure 准确。

## Phase T — Tests

### T1：Unit tests

- [ ] 新建 `tests/test_ssh_download_unit.py`，结构 mirror `test_ssh_upload_unit.py`
- [ ] 13 个 test 按 PRD R11 列出
- [ ] mock SFTPClient 行为：listdir_attr, stat, open(mode='rb'), getfo callback
- [ ] `pytest tests/test_ssh_download_unit.py -v` 全绿

**Validation T1**：unit 套件全绿。

### T2：Integration tests (live_ssh)

- [ ] 新建 `tests/test_ssh_download_integration.py` with `pytestmark = pytest.mark.live_ssh`
- [ ] skip 守卫复用 upload integration 的 `_ssh_agent_has_key` + `_host_reachable("pai")` 工具函数（可考虑提到 conftest.py，但 v1.0 直接复制即可）
- [ ] 7 个 test 按 PRD R11 列出
- [ ] 每个 test 用 uuid 命名远端 fixture（`/tmp/download-fixture-<uuid>`），末尾 ssh 清理
- [ ] `pytest tests/test_ssh_download_integration.py -m live_ssh -v` 全绿在 pai
- [ ] 追加 `tests/README.md` "What's covered today" 一项

**Validation T2**：live_ssh 全绿；pai 无残留 fixture。

## Final validation commands

```bash
# 基线（含 upload 的 test 也应通过）
pytest tests/
# Unit
pytest tests/test_ssh_download_unit.py -v
# Integration
pytest tests/test_ssh_download_integration.py -m live_ssh -v
# Lint
ruff check plugins/ssh-core/skills/ssh-core/scripts/ssh_download.py
mypy plugins/ssh-core/skills/ssh-core/scripts/ssh_download.py
# Smoke
python plugins/ssh-core/skills/ssh-core/scripts/ssh_download.py pai /etc/hostname /tmp/h-smoke --json
diff <(cat /tmp/h-smoke) <(ssh pai cat /etc/hostname)  # 应空 diff
rm /tmp/h-smoke
# 残留检查
ssh pai 'ls -la /tmp/dl-* 2>/dev/null'  # 应空
```

## Review gates

- 进 F2 前：Pre-flight 5 项全部满足；F1 不再有 NotImplementedError
- 进 C1 前：F2 smoke 通过
- 进 RD1 前：C1 + C2 共 4 个手测通过
- 进 T1 前：RD1-RD4 共 4 个手测通过
- 进 T2 前：T1 unit 全绿
- 完成前：用户 review 全部 final validation commands 输出 + 确认 envelope 字段命中 PRD R8

## Rollback points

- F1 (transport 补完) 失败：`git checkout lib/sftp_transport.py`（仅 download_file/tree 段，保留 upload 部分）
- C1 (CLI 壳) 失败：`git checkout plugins/.../ssh_download.py`（保留 stub，等同 v0.2 状态）
- 整体 abort：`git checkout plugins/ssh-core/ tests/test_ssh_download_*.py`，不影响 upload 任务交付物
