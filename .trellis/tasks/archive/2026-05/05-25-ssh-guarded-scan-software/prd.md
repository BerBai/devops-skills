# ship ssh-guarded scan_software v1.0

## Goal

把 `plugins/ssh-guarded/skills/ssh-guarded/scripts/scan_software.py` 从 v0.2 stub (`unimplemented` + `exit 2`，见当前文件:34-67) 推到 v1.0 完成态：在一台远端 host 上批量探测一个软件目录里所有工具的安装状态与版本号，把结果落到本地 cache，之后所有 read 查询都从 cache 走，0 SSH round-trip。本任务**完全独立于** request/execute pipeline (`_cross_cutting.md` §C1–C9 描述的 artifact 流水线)；scan_software 既不读 request artifact，也不写 result artifact。

## User value

- 用户敲 `scan_software.py pai` 一次，之后整个 session 里 Claude 都能离线回答 "pai 上有没有 docker?" / "kubectl 版本?"。
- 在 SKILL.md `Discover → Choose → Check` workflow (`SKILL.md:30-43` 第 1-3 步) 的 **Check** 阶段，Claude 用 `scan_software.py <alias> --has docker` 做条件分支判定，shell-style exit 0/1，无需解析 JSON。
- `--json` 让 Claude 链式把扫描结果喂给后续 `request_*` 起草，例如发现没有 `make` 才去触发 apt-install 请求。

## Confirmed facts (from repo inspection)

1. **CLI surface 已固定**（`scan_software.py:21-25` docstring）：
   ```
   scan_software.py <alias> [--refresh] [--json]
   scan_software.py <alias> --name kubectl       # one tool, cached
   scan_software.py <alias> --has docker         # exit 0/1
   ```
   v0.2 stub 已经把 5 个 flag 用 argparse 接上 (`scan_software.py:39-46`)，本任务直接沿用，不重新设计 surface。

2. **默认 catalog 已在 docstring 列了 33 个工具**（`scan_software.py:10-19`），按 language runtime / build / GPU / containers / IaC / shell utility / Xilinx 7 个 category 分组。本任务把这份列表编码成内置 `_DEFAULT_CATALOG`。

3. **传输底座**：本脚本不直接 import paramiko，也不复用 ssh_upload 的 `lib/sftp_transport.py`（不传文件）。它 shell out 到 `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py`，符合 ADR-001 D1 "subprocess + native ssh" 的全仓约定。

4. **Failure_class 命名空间已锁定**（`_cross_cutting.md` §C5 第 72 行）：scan_software 独占 `catalog_load_failed / cache_write_failed / cache_corrupt / ssh_batch_failed` 4 类 + exit code 75–79 区间。本 PRD 在 D8 里追加第 5 类 `tool_probe_corrupted`，与已锁定的 75–79 区间兼容。

5. **JSON envelope** 沿用 ssh-core/ssh-guarded 统一契约（`_cross_cutting.md` §C7）：`{success, data, error}`，外加 stdout/stderr passthrough。

6. **测试基线**：与 ssh-core 5 个已 ship 脚本一致 —— `tests/` 下默认套件 + `-m live_ssh` 在 pai 跑（`_cross_cutting.md` §C10）。

## Out of scope

明确 v1.1+（写进 PRD 而非埋雷）：

- **跨 host 库存聚合**（"我哪几台机器有 cuda？"）—— 这是 fleet-level inventory tool，scan_software v1.0 只管单 host。
- **License 检测**（vivado / vitis 的 license server 状态）—— 涉及 `lmutil lmstat`，单独 stub。
- **CVE / vulnerability 匹配**（"你这版 docker 有没有 CVE-XXX？"）—— 需要外接漏洞库，体量另一档。
- **包管理器全表 dump**（`apt list --installed`、`rpm -qa`、`pip freeze`）—— scan_software 只关心 `_DEFAULT_CATALOG` 那 33 个目标工具，不试图列全表。
- **服务运行状态**（"docker daemon 起来没？"）—— 那是 `systemctl is-active`，属于另一个未来 stub。
- **macOS / Windows 远端**—— v1.0 假定远端是 Linux（probe 用 `command -v` POSIX shell）。

## Requirements

### R1 — CLI surface
- 沿用 stub `scan_software.py:39-46`：`alias` 必填 positional + `--refresh / --name / --has / --json` 4 flag。
- 追加 `--no-refresh-stale`（默认 false，stale cache 触发自动 rescan；passed 时禁止自动 rescan，详见 D6）。
- 追加 `--catalog <path>`（默认 `~/.devops/software_catalog.json`，详见 D2）。
- `--name` 与 `--has` **互斥**；都不传 = full scan 模式。

### R2 — Default catalog (built-in)
- `_DEFAULT_CATALOG` 静态字面量，覆盖 docstring 列的 33 个工具，按 7 个 category 分组（详见 D1）。
- 每条 entry 字段固定为 `{name, probe_cmd, version_cmd, version_regex, category}`（详见 D2）。
- 用户 catalog 文件存在时与默认 **merge by name**：同名条目用户覆盖，异名 union。

### R3 — Batch remote probe
- 一次 SSH 连接（一次 `ssh_execute.py` 调用），通过 heredoc-fed shell script 在远端循环跑全 catalog（详见 D3）。
- 远端 emit NDJSON 到 stdout，每条 `{"name", "present", "version_raw"?}`；本地解析后跑 `version_regex` 萃取 canonical version。
- 远端单条 probe 失败（version_cmd 非 0 但 `command -v` 通过）→ `present=true, version="unknown"`，整体扫描不中断。

### R4 — Xilinx 特殊路径
- 7 个 category 里只有 Xilinx 不走 `command -v`：扫 `/opt/Xilinx/*` 目录名萃取版本（详见 D4）。
- 由 batch shell script 内联处理（不另开一次 SSH）。

### R5 — Cache I/O
- 路径：`~/.devops/cache/<alias>/software.json`（详见 D5）。
- 原子写：同目录 temp file + `os.replace`，避免半写状态。
- 读侧容错：JSON 解析失败 → failure_class `cache_corrupt` + 自动 re-scan（不删 cache 直到新 cache 写成功，避免读侧损坏导致丢历史）。

### R6 — TTL & staleness
- TTL = 7 天（从 `data.scanned_at` 起算）。
- 过期 → 自动 re-scan；`--no-refresh-stale` 时跳过 rescan，stderr 警告 + `data.cache_stale=true`，exit 0 仍然成立（详见 D6）。
- `--refresh` 永远忽略 cache，强制重新扫描。

### R7 — 三种调用模式
- 详见 D7。三种模式共享 cache 与 catalog，差异仅在输出形态与 exit code 语义。

### R8 — Failure classes & exit codes
- 5 个 failure_class（详见 D8），对应 exit code 75–79 区间，与 `_cross_cutting.md` §C5 锁定的命名空间 1:1 对齐。
- 通用：exit 0 成功 / exit 1 generic / exit 124 ssh timeout（ssh-core 透传）。

### R9 — Transport
- shell out 到 `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py`（详见 D9），不直接调 paramiko。
- 默认 `--timeout 60`；典型 33-tool 扫描 < 5 s，预留余量给慢盘 / 高延迟链路。

### R10 — Testing
- 单元 ~25 个（详见 D10）：catalog 默认值断言、catalog merge、cache write/read、TTL 边界、每个内置工具的 `version_regex` 单测、`--has` exit code、`--name` cache-hit。
- 集成 ~2 个 live_ssh：`test_full_scan_on_pai`（全量扫一遍）+ `test_has_subcommand_on_pai`（`--has bash` 必中、`--has nonexistent-tool` exit 1）。

## Acceptance Criteria

- [ ] `python scan_software.py --help` exit 0 列出 5 个 flag。
- [ ] `pytest tests/test_scan_software_unit.py` 全绿（~25 个 PASS）。
- [ ] `pytest tests/test_scan_software_integration.py -m live_ssh -v` 全绿在 pai。
- [ ] `scan_software.py pai --json` → `success=true`，`data.tools` 字典含 33 条（present + absent 都算），`~/.devops/cache/pai/software.json` 生成。
- [ ] 同 cmd 重跑（TTL 未过期 + 无 `--refresh`） → 不发起 SSH（用 mock 验证 subprocess 调用次数为 0）。
- [ ] `scan_software.py pai --has bash` exit 0；`scan_software.py pai --has totally-fake-tool-xyz` exit 1。
- [ ] `scan_software.py pai --name kubectl --json` → `data.tools.kubectl.{present, version, version_raw, scan_method, category}` 5 字段齐。
- [ ] Cache 文件损坏（手动 `echo "{" > software.json`）→ failure_class `cache_corrupt` 上报后自动 rescan，新 cache 写入成功，exit 0。
- [ ] Stale cache (`mtime - 8 days`) + 网络断开 + 不传 `--no-refresh-stale` → 自动 rescan 失败 → fall back 到 stale，stderr warn `cache stale and refresh failed`，`data.cache_stale=true`，exit 0（D6）。
- [ ] Catalog 加载失败（`--catalog /nonexistent.json` + 非默认路径）→ failure_class `catalog_load_failed` + exit 75；默认路径不存在则静默用 built-in（不算 failure）。
- [ ] `pytest tests/` 默认套件依旧全绿（不破坏已 ship 的 9 个 v1.0 脚本测试）。
- [ ] SKILL.md `## Software scan` 节（`SKILL.md:150-162`）字段与本 PRD 对齐；如需调整，本任务一并 commit。

## Notes

- 与 sibling task `05-25-ssh-guarded-request-pipeline` (`_cross_cutting.md` 拥有者) 共享 §C5 failure_class 命名空间与 §C7 JSON envelope；不依赖 §C1–C4 / §C8–C9 / §C11（不读写 request artifact，不用 lib/redact，不用 lib/lockfile）。
- D1–D10 全部在 `design.md` 展开，PRD 不重复细节。
- 沿用 deploy_pubkey / ssh_config_manager / ssh_server_transfer 已经确立的 **per-script failure_class namespace** 惯例（`05-24-devops-skills-v1-rollout/prd.md:86` "Per-script failure_class namespace pattern"），现在第 4 个先例。
