# ship log-aggregator tail_multi v1.0

## Goal

把 `plugins/log-aggregator/skills/log-aggregator/scripts/tail_multi.py`
（当前 v0.2 stub: 77 LOC argparse skeleton + `unimplemented` + `exit 2`）推到
v1.0：从一组异构源（journal / docker / kube / file，跨主机/容器/Pod）并发拉
日志，归一化时间戳，按可选噪音/level/抑制过滤后，**统一前缀 `[host/source ts]
<line>` 输出**（或 `--json` NDJSON）。本脚本是 plugin 的**底座**——把所有共用
能力（spec 解析、fetcher、time norm、clock skew、filters）沉淀到
`scripts/lib/`，供 correlate 与 grep_across_sources 复用。

## User value

- 「同时看 web-1 / web-2 / web-3 的 nginx，最近 10 分钟」→ 一行命令拉齐：
  ```bash
  tail_multi.py --sources journal://web-{1,2,3}/nginx --since 10min --follow
  ```
- 「journalctl + docker logs + 应用 log 一起看」→ 异构源合并到一条时间轴。
- 「同时间窗内 web-3 报 5xx，web-1/2 不报」→ 三机并排 tail，眼睛扫一眼差异。
- 跨主机时钟漂移**主动暴露**：启动时探一次 skew，输出加 `[clock_skew_ms=...]`
  元数据，避免「为什么 X 看起来比 Y 早」的诊断误导。

## Confirmed facts (from repo inspection)

1. **CLI surface 已固定**（`scripts/tail_multi.py:4-11` docstring）：
   ```
   tail_multi.py --sources <spec> [<spec>...]
       [--follow] [--since <duration>]
       [--min-level warn]
       [--suppress] [--aggressive-suppress]
       [--max-concurrent 8]
       [--no-default-filters]
       [--json]
   ```
   v0.2 stub 已经把 9 个 flag 用 argparse 接上 (`scripts/tail_multi.py:38-55`)，
   本任务直接沿用，不重新设计 surface。

2. **Source spec 语法已锁定**（SKILL.md "Source spec 语法" 节
   `plugins/log-aggregator/skills/log-aggregator/SKILL.md:47-71`）：
   - `journal://<host>/<unit?>`
   - `docker://<host>/<container>`
   - `kube://<host>/<namespace>/<pod>` 含 glob `payment-svc-*` 与
     `?label=<sel>`
   - `file://<host>/<path>` 含 `?ts_regex=<regex>&ts_format=<strftime>`
   - `<host>` 为 `local` 时直接执行，否则经 ssh-core
   - Brace expansion: `journal://web-{1,2,3}/nginx` → 3 个源

3. **每种 source 的远端命令、字段、坑都已写到字段级**：
   `plugins/log-aggregator/skills/log-aggregator/references/sources.md`。
   本任务**不重写 sources.md**，仅在发现实际命令与文档偏差时同步修订。

4. **Transport 底座**：shell out 到 `plugins/ssh-core/skills/ssh-core/scripts/
   ssh_execute.py`，符合 **ADR-001 D1** 「subprocess + native ssh」。本脚本
   **不直接 import paramiko**（与 ssh_upload/ssh_download 走 SFTP 不同——这里
   只是远端跑命令读 stdout，走 ssh_execute 即可）。

5. **Failure_class 命名空间 40-49**（父 prd.md X2）：
   `source_spec_invalid / fetcher_failed / clock_skew_failed / normalize_failed
   / no_sources_resolved / partial_fetch`。Spec 同步：本任务 ship commit 同时
   追加 40-49 段到 `.trellis/spec/backend/error-handling.md`（参父 prd.md X2
   Spec update 时机）。

6. **JSON envelope** 沿用全仓契约 `{success, exit_code, stdout, stderr, data,
   error?}`（`.trellis/spec/backend/error-handling.md:8-29`）；`--json` 模式
   下 `stdout` 持续吐 NDJSON event（一行一 event），最后一行非 NDJSON 而是
   envelope summary。

7. **测试基线**：与 ssh-core 5 个已 ship 脚本一致 ——
   `tests/test_tail_multi_unit.py`（~30 unit）+
   `tests/test_tail_multi_integration.py`（-m live_ssh，至少 2 个：journal
   远端拉取 + 本地 file fetcher）。

## Out of scope

明确 v1.1+（写进 PRD 而非埋雷）：

- **长时 `--follow` 心跳/重连**：v1.0 受 ssh-core idle timeout 限制（实际
  ~10 分钟，会触发 exit 124）。文档中明确建议 `--follow` 时长 ≤10 min。长时
  follow 依赖 ssh_daemon v1.1 ControlMaster 决议（父 prd X5）。
- **Multi-line stack trace 重组**：v1.0 每行独立输出（保留 source tag），不
  做 Java/Python traceback 合并。
- **结构化日志字段提取**：v1.0 把每行作为 `raw` 字段；`--json` 输出的
  `fields` 仅 4 类源原生有的（journal `-o json` 字段、docker 时间戳 etc.），
  不做 logfmt / k=v / regex 萃取。
- **应用层日志 deduping**（跨源识别同一逻辑事件）：v1.0 仅做**单源**内的
  syslog-style 速率抑制；跨源 dedup 是 correlate 的领域。
- **Aggressive parallelism**（N=20+ 源时的 connection-pool 优化）：v1.0 用
  ThreadPoolExecutor + `--max-concurrent 8` 默认上限。
- **macOS / BSD 远端 host**：v1.0 假定远端 Linux + systemd（journal）/ Docker
  CE / kubectl。macOS docker 走 `colima` 时也跑得通，但不做专门验证。

## Requirements

### R1 — CLI surface
- 沿用 stub `scripts/tail_multi.py:38-55`：`--sources` (required, nargs+) +
  8 个 flag。**禁止**新增 flag。
- `--max-concurrent` 默认 `8`；`--since` 默认 `"5min"`（"5 minutes ago"）。
- `--min-level` 默认 `"trace"`（即「不过滤」）。
- `--json` 与 human 模式互斥行为详见 R7。

### R2 — Source spec 解析（→ `lib/source_spec.py`）
- 接 list[str]，返回 `list[ResolvedSource]`。`ResolvedSource` 字段：
  `scheme, host, payload, query_dict, original_spec, source_tag`
  其中 `source_tag` 是用于输出前缀的简写（如 `web-1/nginx`、
  `payment-svc-x`），见 D2 章。
- 4 类 scheme 解析规则见 D2；brace `{1,2,3}` 与 glob `*` 展开规则见 D2。
- 解析失败 → `failure_class=source_spec_invalid` + `exit 40`。
- 解析后 0 源 → `failure_class=no_sources_resolved` + `exit 44`。

### R3 — Fetcher 分派（→ `lib/fetchers.py`）
- 每个 scheme 一个 fetcher，统一接口：
  ```python
  def iter_lines(source: ResolvedSource, *, since: str, follow: bool,
                 timeout: int) -> Iterator[RawLine]
  ```
- 4 类 fetcher（详见 `references/sources.md` 与本任务 D3）：
  - `journal_fetcher`: `journalctl [-u <unit>] -o json --since "@<epoch>" [-f] --no-pager`
  - `docker_fetcher`: `docker logs --timestamps [--follow] [--since <since>] <container>`
  - `kube_fetcher`: `kubectl logs -n <ns> <pod-or-glob> [--all-containers --prefix] --timestamps [--since <since>] [--follow] [--previous]`
    - glob `payment-svc-*` 先 `kubectl get pods -n <ns> -o name | grep` 展开
    - `?label=app=foo` 走 `-l <sel>`
  - `file_fetcher`: 本地 `Path.open` 流式读；远端走 `ssh_execute.py` 跑 `tail -n <N> [-F] <path>`
- 本地 source（`host == "local"`）跑本机 subprocess；远端 source shell out
  `ssh_execute.py --json <host> -- <cmd>`，解析 stdout 行。

### R4 — Time normalization（→ `lib/time_norm.py`）
- 接 `(scheme, raw_line)` → `(iso_utc, raw_body)` 元组；返回 `None` 表示行
  无法解析时间（fall back 到 fetcher 抓取时刻）。
- 4 类 scheme 的规则（参 `references/sources.md` "时间归一化" 表）：
  - `journal`: `__REALTIME_TIMESTAMP` μs → `datetime.fromtimestamp(us/1e6, tz=UTC)`
  - `docker`: RFC3339（每行行首） → 已 UTC
  - `kube`: `--timestamps` 给 RFC3339 → 已 UTC
  - `file`: 用户提供 `ts_regex` + `ts_format`，在源主机时区解析 → 转 UTC；
    未提供 → 用 fetcher 抓取时刻（精度退化为「读到行的时间」）
- 时间归一化失败计数 → `failure_class=normalize_failed`，但**不中断**：
  正常输出该行（用 fetcher 时刻），count 累积到 `data.normalize_failed_count`，
  仅当 count > 总行数 50% 时整体 exit 43。

### R5 — Clock skew probe（→ `lib/clock_skew.py`）
- 启动时并发对每个远端 host（`host != "local"`）跑
  `ssh_execute.py <host> -- date -u +%s.%N`，记录 `(local_now - remote_now)`。
- 输出 `dict[host, skew_ms]`，作为 envelope `data.clock_skew_ms` 与
  human 模式启动 banner 的 metadata。
- 单 host probe 失败 → 记 `skew_ms=null`，不中断（只是关联精度降级）；全部
  失败 → `failure_class=clock_skew_failed` + `exit 42` 仅当 `--strict-skew`
  时触发（v1.0 不添 `--strict-skew` flag，全失败也只警告）。
- 时间预算：probe 总耗时 cap 在 5s（`ThreadPoolExecutor(max_workers=8,
  timeout=5)`）。

### R6 — Filters（→ `lib/filters.py`）
- **Noise deny-list**（`references/noise_reduction.md` "默认丢什么"节）：
  保守 deny-list，每条 regex 都有理由；`--no-default-filters` 关。
- **Level filter**: `--min-level warn` 把 info/debug/trace 丢；level 推断规则
  per scheme（参 `noise_reduction.md` "level 染色/排序"节）。无法识别 level
  → 默认 `info` 等级（即 `--min-level warn` 时会被丢，文档里明确）。
- **Suppressor**: `--suppress` 启用 syslog-style 速率抑制；规范化键 = 去掉
  时间戳/PID/IP/trace_id 等高基数字段后的行。同键 1s 内重复 ≥5 次 → 抑制；
  每秒输出 `(×N suppressed)` 摘要。`--aggressive-suppress` 用更激进的规范化
  (e.g. 数字全替为 `<NUM>`)。
- **User overrides**: `~/.devops/log_filters.json` 的 `keep_always` 永远不
  过滤、`drop` 追加 deny-list。
- 过滤顺序：noise → user `drop` → level → suppress → user `keep_always`
  优先级最高（在最后做反向检查）。

### R7 — 输出形态
- **Human 模式**（默认）：每行 `[<source_tag> <iso_short>] <body>`，其中
  `iso_short` 是 `HH:MM:SS.fff`（不含日期，为节省宽度）。多源时间排序不严格
  —— 用 `--max-pending-buffer 256` 行的 reorder buffer（D4）。
- **`--json` 模式**：stdout 输出 NDJSON，每行一个 event：
  ```json
  {"source": "web-1/nginx", "time": "2026-05-25T14:02:01.234Z",
   "level": "info", "raw": "GET /api/charge 200 12ms",
   "fields": {"_SYSTEMD_UNIT": "nginx.service", ...}}
  ```
  流式吐 event 直到 stdin 关闭/`--follow` 退出；最后**追加一行 envelope
  summary**（`{success, exit_code, data: {sources, clock_skew_ms,
  events_emitted, ...}, stderr: ""}`），CLI 调用方按"最后一行 != event"
  识别 summary。
- 关键词高亮（human 模式）：命中 `error|fatal|panic|oom|crash|timeout|
  refused|denied|out of memory` 等 → ANSI red/yellow。`--no-color` 关
  （但 v1.0 不新增此 flag，靠 `NO_COLOR=1` 环境变量 + isatty 判断）。

### R8 — Follow 模式
- `--follow` 透传给 fetcher（每 fetcher 各自 follow）。
- 远端 follow：依赖 ssh_execute 的 idle timeout（默认 60s 无 stdout 输出
  会被透传 exit 124 截断）。**v1.0 明确文档建议 follow ≤10 min**；超时
  视为正常退出（不是 failure），最终 envelope `data.terminated_by =
  "ssh_idle_timeout"` 仅为信息。
- 本地 follow：用 `tail -f`（subprocess.Popen + readline 循环）。
- Ctrl-C / SIGTERM：清理所有 fetcher 子进程，输出 envelope summary，exit 0。

### R9 — Failure classes & exit codes
- 6 个 failure_class（详见 D8），对应 exit code 40-49，per-script namespace。
- `0` success / `1` generic / `124` ssh timeout（如 ssh_execute 透传）。

| Code | failure_class | 触发 |
|---|---|---|
| 40 | `source_spec_invalid` | spec 字符串无法解析 |
| 41 | `fetcher_failed` | 某个 fetcher 启动失败（远端 ssh 拒绝、本地命令不存在等），其他源继续 |
| 42 | `clock_skew_failed` | 启动 probe 全部失败（v1.0 仅警告，不实际触发；预留区间）|
| 43 | `normalize_failed` | 时间归一化失败 > 50%（极端情况） |
| 44 | `no_sources_resolved` | spec 解析后 0 个有效源 |
| 45 | `partial_fetch` | ≥1 个 fetcher 中途死亡（exit ≠ 0），其余源继续（warning, exit 0 时也设置此 failure_class 但 success=true） |
| 46-49 | reserved | 未来扩展 |

**注**：45 是「warning 性 failure_class」，envelope `success=true, exit_code=0,
data.failure_class="partial_fetch", data.failed_fetchers=[...]`，不是 exit 45。
这个二义性沿用 scan_software 的 `cache_stale` 模式（参 scan_software prd
R6）。

### R10 — Transport
- 远端拉取 shell out 到 `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py`，
  默认 `--timeout 120`（远端 journalctl 抓 1h 历史可能慢）；`--follow` 时不
  设 timeout（依赖 ssh_execute 自己的 idle timeout）。
- 不直接 import paramiko。
- 不依赖 ssh_daemon（已 defer 到 v1.1）。

### R11 — Testing
- **Unit ~30 个**（详见 D10）：
  - `source_spec` 4 个 scheme 各 ~3 个测（含 brace 展开、glob、query 字段）
  - `time_norm` 4 个 scheme 各 ~2 个测（正常、缺字段、异常时区）
  - `filters` noise/level/suppress/keep_always 各 ~3 个测
  - CLI 主流程 ~5 个测（args 合法/非法、`--json` 输出 NDJSON 形态、failure
    class、Ctrl-C 清理）
- **Integration ~2 个 live_ssh**：
  - `test_journal_remote_pai` — 在 pai 跑 `journal://pai/sshd` 拉 10s 数据
    + envelope `success=true, data.events_emitted > 0`
  - `test_file_local_fixture` — 本地 fixture 跑 `file://local/<temp.log>`
    验证 fetcher + normalize + envelope 完整路径

### R12 — SKILL.md / references 同步
- SKILL.md 三处 quick reference (`SKILL.md:90-109`) 已是 v1.0 描述；若 ship
  时发现 default 值偏差（如 `--max-concurrent` 默认从 8 改成别的）→ 同步
  修订。
- `references/sources.md` 4 个 scheme 描述若发现远端命令偏差 → 修订相应节。
- `references/noise_reduction.md` 默认 deny-list 若 ship 时调整 → 修订
  "默认丢什么" 节。
- `references/correlation_patterns.md` 本 child **不动**（属 correlate
  领域）。

## Acceptance Criteria

- [ ] `python tail_multi.py --help` exit 0，列出 9 个 flag 与 stub 一致。
- [ ] `pytest tests/test_tail_multi_unit.py` 全绿（~30 PASS）。
- [ ] `pytest tests/test_tail_multi_integration.py -m live_ssh -v` 全绿在 pai
      （~2 PASS）。
- [ ] `tail_multi.py --sources file://local/<fixture.log> --json` 输出
      NDJSON 行 + 末尾 envelope；envelope `success=true,
      data.events_emitted == 行数`。
- [ ] `tail_multi.py --sources journal://web-1/nginx journal://web-2/nginx --since 10min`
      在两台远端真机跑通；human 模式输出两个 source 的混合行，前缀格式正确。
- [ ] `tail_multi.py --sources journal://web-{1,2,3}/nginx --since 5min`
      brace 展开为 3 个源，envelope `data.sources.length == 3`。
- [ ] `tail_multi.py --sources "BAD-SPEC"` → exit 40，failure_class =
      `source_spec_invalid`。
- [ ] `tail_multi.py --sources journal://unreachable-host/sshd --since 5min`
      → 1 个 fetcher 失败但脚本不崩；envelope `success=true,
      data.failure_class="partial_fetch", data.failed_fetchers` 有 1 项。
- [ ] `tail_multi.py --sources journal://local/sshd --suppress` 在制造
      100 条同行后输出 ≤5 行 + `(×N suppressed)` 摘要。
- [ ] `tail_multi.py --sources journal://local/sshd --min-level warn` 把
      info 行全丢。
- [ ] `plugins/log-aggregator/skills/log-aggregator/scripts/lib/{source_spec,
      fetchers,time_norm,clock_skew,filters}.py` 5 个模块齐全，公共接口签名
      与父 prd.md X1 一致。
- [ ] `.trellis/spec/backend/error-handling.md` 追加 40-49 段 per-script
      exit code range。
- [ ] `pytest tests/` 默认套件（不含 live_ssh）保持全绿，不破坏已 ship 的
      9 个 v1.0 脚本。
- [ ] SKILL.md / `references/{sources,noise_reduction}.md` 与实际默认值
      / 命令一致；若调整则一并 commit。

## Notes

- 与 sibling child task `05-25-log-aggregator-correlate` /
  `05-25-log-aggregator-grep-across-sources` **必须按顺序 ship**：本任务先
  ship 完，`lib/` 接口稳定后再启动 correlate 与 grep。参父 prd.md "Children
  — 串行 ship 顺序" 节。
- D1–D10 全部在 `design.md` 展开，PRD 不重复细节。
- Per-script failure_class namespace 第 6 个先例（前 5：ssh_config_manager
  4-8、deploy_pubkey 10-15、ssh_server_transfer 20-29、scan_software 75-79、
  + 父 prd X2 的 ssh-guarded 50+ TBD）。
