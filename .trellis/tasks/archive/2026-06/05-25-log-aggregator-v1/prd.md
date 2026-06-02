# ship log-aggregator v1.0 (3 scripts)

## Goal

把 `plugins/log-aggregator/skills/log-aggregator/scripts/` 下 3 个 stub
（`tail_multi.py` / `correlate.py` / `grep_across_sources.py`）从 v0.2 占位
（`unimplemented` + `exit 2`，三脚本各 ~67 LOC）推到 v1.0 完成态：实现 SKILL.md
已锁定的多源日志聚合与关联能力，对齐 ssh-core/remote-debug/docker-quick 9 个
已 ship v1.0 脚本的质量基线（250–740 LOC 实现 + unit + `-m live_ssh`
integration + SKILL.md 同步 + 独立 commit）。

本 task 是 **plugin-level parent / 总览**，**不直接落代码**；3 个脚本各自一个
**child task**，独立 plan → implement → check → commit → archive，并按
PRD「Phase 1 → 2 → 3」顺序串行 ship（理由见下方 Children 节）。

## Background

- 父任务 `05-24-devops-skills-v1-rollout` PRD 把 log-aggregator 3 stub 列为
  **Phase 2**，明确「Phase 1（ssh-core）全部 ship 完成 + e2e validate 通过
  后，才开 Phase 2 children」。本 task 现在创建是为了**先把规划落地**（PRD /
  design / implement 三件套），但 3 个 child status 保持 `planning`，
  `task.py start` 触发延后到 Phase 1 收尾后。
- Phase 1 当前进度（截 2026-05-25）：4/7 ship（ssh_config_manager / deploy_pubkey
  / ssh_server_transfer / 其他已 ship 见父 PRD），1 defer（ssh_daemon
  v1.0 撤回到 v1.1 per ADR-001 A2），剩 ssh-tunnel / ssh-download / ssh-upload
  等。
- SKILL.md (`plugins/log-aggregator/skills/log-aggregator/SKILL.md`，4.6K) 已
  完整定义 plugin 三脚本的契约：CLI surface / source spec 语法 / 输出格式 /
  decision tree。本 v1.0 落地**不重新审视设计**（与 user 在规划会上的决定
  对齐：「按既定 SKILL.md 直接落地」）。
- 3 个 references 文件（`sources.md` / `correlation_patterns.md` /
  `noise_reduction.md`，共 ~12K）把每种 source 的抓取命令、时间归一化、关联
  模式、噪音过滤规则都写到字段级别。child task 的 design.md 只引用，不重复。

## Scope

### Children — 串行 ship 顺序（依赖 → 复用）

| Slug | Stub | Priority | 顺序理由 |
|---|---|---|---|
| log-aggregator-tail-multi | tail_multi.py | P2 | **底座**。三脚本共用的 source-spec 解析、fetcher 分派（journal/docker/kube/file 4 类）、time-skew 探测、时间归一化、normalizer / noise filter / level filter / suppressor —— 都首次在 tail_multi 落地。后两个脚本**复用**这层 lib。先 ship tail_multi 等于先建好 plugin 内部的「lib」。 |
| log-aggregator-correlate | correlate.py | P2 | 复用 tail_multi 的 fetcher + normalizer，新增 anchor-regex 搜索、window 展开、三段式输出（Anchor / Within window / Summary with hint）、`--pair-mode` 时长配对。是 plugin 的「智力价值」核心。 |
| log-aggregator-grep-across-sources | grep_across_sources.py | P3 | 复用 tail_multi 的 fetcher，最简单：N 源跑 `grep`，汇总命中数 + 样例。`--pattern` 为空时切到 heat-map 模式（log volume per source）。优先级最低因为价值密度低于 correlate（grep 更接近"全部源并发 grep"工程活儿）。 |

### Cross-cutting decisions（plugin-level，所有 child 必须遵守）

**X1 — 共享 lib 位置与命名**
tail_multi 先 ship 时把以下通用能力**沉淀到 `plugins/log-aggregator/skills/log-aggregator/scripts/lib/`**（与 ssh-core 的 `lib/sftp_transport.py` 同构）：

- `lib/source_spec.py` — spec 字符串解析（`scheme://host/path?query`）+ brace/glob 展开
- `lib/fetchers.py` — 4 类 fetcher：journal / docker / kube / file，统一接口 `iter_lines(source, *, since, follow) -> Iterator[RawLine]`
- `lib/time_norm.py` — 4 类 source 时间字段 → ISO 8601 UTC（`Z` 后缀）的归一化器，含 `ts_regex/ts_format` 自定义解析
- `lib/clock_skew.py` — 启动时并发 ping 远端 `date -u +%s.%N` 记录 skew，对外 dict `{host: skew_ms}`
- `lib/filters.py` — noise deny-list / level filter / suppressor（速率抑制，1s 窗 ≥5 次同规范化行）

correlate 和 grep_across_sources 的 design.md 只调 `from lib.fetchers import iter_lines` 等接口，**不复制 fetcher 实现**。3 child 的 design.md 引用本 prd 这一节作为 lib 接口的 source of truth。

**X2 — Failure_class namespace（per-script）**
沿用 ssh-core 的 per-script namespace 惯例（已有 4 个先例：ssh_config_manager 4-8、deploy_pubkey 10-15、ssh_server_transfer 20-29、scan_software 75-79）。3 child 独占：

| Script | Exit code range | 预留 failure_class（最终在各 child design.md D8 锁定）|
|---|---|---|
| tail_multi | 40-49 | source_spec_invalid / fetcher_failed / clock_skew_failed / normalize_failed / no_sources_resolved / partial_fetch (warning, exit 0) |
| correlate | 90-99 | anchor_not_found / window_invalid / pair_unmatched / 与 tail_multi 共享的 fetcher_failed 等透传 |
| grep_across_sources | 180-189 | pattern_invalid / 与 tail_multi 共享的 fetcher_failed 等透传 |

通用：`0` success / `1` generic / `124` ssh timeout（ssh-core 透传）。

**Spec update 时机**：参照 ssh_server_transfer 的做法（`.trellis/tasks/archive/2026-05/05-24-ssh-core-ssh-server-transfer/implement.md` Phase 0.2「Exit codes 20–29 are now documented in `.trellis/spec/backend/error-handling.md` (spec updated 2026-05-25 alongside this task)」），**每个 child 在自己的 ship commit 里同步追加自己那一段到 `.trellis/spec/backend/error-handling.md`** —— 即 tail_multi ship 时加 40-49 行、correlate ship 时加 90-99 行、grep_across_sources ship 时加 180-189 行。本规则把 spec update 与 ship 强绑定，避免 spec drift。

**X3 — JSON envelope 契约**
沿用 ssh-core/ssh-guarded 统一契约 `{success, exit_code, stdout, stderr, data, error?}`。3 脚本均支持 `--json`；其中 `grep_across_sources` 额外有 `--format json` 别名（v1.0 让两者等价，互不冲突）。

**X4 — Transport 底座**
远端日志拉取走 `ssh-core/scripts/ssh_execute.py` shell-out（subprocess + native ssh），遵守 **ADR-001 D1** 「subprocess + native ssh，不直接 import paramiko」。tail_multi 不依赖 ssh_daemon（已 defer 到 v1.1），每个 remote source 一次独立 SSH 连接；并发上限走 `--max-concurrent 8`（参 `noise_reduction.md` "性能注意" 节）。

**X5 — Live tail（`--follow`）的 idle timeout 兜底**
`journalctl -f` / `docker logs --follow` / `kubectl logs -f` 经 ssh-core 远端时会被 idle timeout 截断（`sources.md` 已标注此坑）。v1.0 采取**保守策略**：

- `tail_multi --follow` 文档明确建议 ≤ 10 分钟，超时由 ssh-core 透传 exit 124
- 长时心跳/重连 → 写入 v1.1 backlog（与 ssh_daemon v1.1 一起评估）
- correlate / grep_across_sources 默认**不 follow**（一次性窗口查询）

**X6 — SKILL.md 与 references/ 同步**
SKILL.md 当前 4.6K + 3 references 共 ~12K **已经描述 v1.0 行为**，不是规划稿。
ship 时各 child 在自己的 commit 里仅同步**已变化的事实**：

- exit code 区间（写入 SKILL.md frontmatter 或 quick reference）
- 实际默认值（如默认 `--max-concurrent 8` 与 `--since 5min`）
- 若实现时发现 SKILL.md 与现实不一致 → 在该 child design.md D-X 节记录偏差，commit 时同步修订

**禁止**在某个 child 改动跨脚本契约（例如重命名 source scheme）；这种偏差必须升级到本 parent prd 修订后再动手。

**X7 — 测试基线**
3 child 各自必备：

- ~20–35 unit tests（覆盖 spec 解析、fetcher mock、time norm、filter 边界、CLI 主流程）
- 2–3 integration tests `-m live_ssh`（参考 ssh-server-transfer 的 5 个 live test）
  - 至少 1 个跑通真实远端 journal 拉取
  - 至少 1 个本地 fixture（启 docker container 或本地 file，验证非 SSH 路径）

**禁止**只有 mock 单测、没有 live integration（参 `quality-guidelines.md`）。

## In-scope（parent 维度）

1. 维护 3 个 child task 总体进度 + 顺序约束（tail_multi → correlate → grep_across_sources，串行 ship 避免 lib 接口反复 churn）
2. 跨 child 的共性问题集中记本 prd 「Notes」节：如 lib 接口签名调整、Phase 1 ssh-tunnel ship 完后 follow 重连策略再评估、跨 child 的实测 perf 数据
3. Plugin Phase 收尾后开新 e2e validation task（参考 `05-24-e2e-validate-skills-on-pai`），跑在 pai + 本地 docker fixture 上
4. **不写代码**；不动 SKILL.md（child 各自负责自己段的微调）

## Out of scope

- 实现细节、unit/integration test 设计 → 各 child task 的 prd / design / implement
- 日志存储 / 索引引擎（Loki / Elastic / OpenSearch / Splunk）—— 本 plugin 是 **只读聚合**，不持久化
- 实时告警 / stream 触发 action —— 那是 Alertmanager / OTel pipeline
- 二进制 / 结构化协议日志（除非源本身吐 JSON 或纯文本）
- alert 规则生成 / 自动根因判定 —— `correlate.py` 只输出 `likely_root_cause_hint`（启发式），不写结论
- ssh_daemon / 长连接复用 → 已 defer v1.1（per ADR-001 A2）；本 plugin v1.0 每源独立 SSH 连接
- v1.1+ 已识别 backlog（也写在这避免散落）：
  - `--follow` 心跳/重连（依赖 ssh_daemon v1.1 ControlMaster 决议）
  - OTel span tree 重建（`correlate.py` 当前仅识别共同 `trace_id`，不重建嵌套 span）
  - Aggressive parallelism（N=20+ 源时的 connection-pool 优化）
  - Multi-line stack trace 重组（v1.0 每源每行独立输出）

## Constraints

- **顺序约束**：tail_multi 必须先 ship 完（lib/ 接口稳定）再启动 correlate / grep。三 child 之间不并行 implement。
- **依赖约束**：本 plugin 所有 ship 必须在 ssh-core Phase 1（`05-24-devops-skills-v1-rollout` 的 ssh-core 子集）全部完成 + e2e validate 通过之后。本 task 创建时此约束未满足，3 child status 必须停 `planning`。
- 每个 child 维持 docstring 契约：`v1.0 surface:` block → 实际实现签名 1:1 对齐。
- JSON output 字段一致（X3）。
- 不破坏已 ship 的 9 个 ssh-core/remote-debug/docker-quick v1.0 脚本及其 40+ live_ssh 测试。
- Plugin 所有 commit 不动 `.trellis/spec/backend/` 之外的全仓 spec（per `05-24-devops-skills-v1-rollout/prd.md:84`「`.trellis/spec/backend/` 是个人 workspace（gitignored）」）。

## Acceptance Criteria（plugin-level）

- [x] 3 个 child task 全部 `completed/archived`
- [x] 3 个对应 v1.0 脚本通过自身 unit + integration（`-m live_ssh`）测试
- [x] `pytest tests/` 默认套件全绿、`pytest -m live_ssh` 全绿、ruff clean、mypy clean
- [x] `plugins/log-aggregator/skills/log-aggregator/scripts/lib/` 含 5 个共享模块（X1），correlate 和 grep_across_sources 复用、不复制 fetcher 实现
- [x] e2e validation 报告落仓：2026-06-01 post-ship pai rerun 记录在本 PRD Notes，3 脚本在 pai/local fixture 真机路径通过或环境跳过
- [x] SKILL.md frontmatter + decision tree + quick reference 与实际 CLI 一致；3 references/ 不需要重写（已是 v1.0 描述）；若 ship 中发现偏差，对应 child 同步 commit 修订
- [x] `.trellis/spec/backend/error-handling.md` 追加 40–49 / 90–99 / 180–189 三段 per-script exit code range（每个 child ship 时同步追加自己那一段，参 X2 节 Spec update 时机）

## Tracking

- 3 个 child 进度看 `task.py list` 中 `log-aggregator-*` 子集
- 共性 finding / decision 追加到本 prd 的「Notes」节（不写实现细节）
- ship 节奏更新到父任务 `05-24-devops-skills-v1-rollout` 的 Notes

## Notes

- 2026-05-25 创建。Phase 2 plugin 第 1 个 rollup（ssh-guarded 是另一个 Phase 2 plugin，4 child 在前置规划中）。
- 2026-05-25 三 child 全部 `planning`；`task.py start` 触发延后到 Phase 1 (ssh-core) 收尾后。本 task 也保持 `planning`。
- 2026-06-01 **grep_across_sources v1.0 shipped** — commit `c6e77e4`. 4 files / script + 2 tests + error-handling spec. 20 unit + 3 integration (`-m live_ssh`; local fixture PASS, remote pai SKIP because host unreachable) PASS. Full default `pytest tests/` PASS with environment skips; full `pytest tests/ -m live_ssh` PASS with environment skips. Key decision: stale `60-69` plan conflicted with `ssh-guarded exec_detached`, so this child moved to `180-189` with `180 pattern_invalid`. Implementation outputs all raw/json events; `--max-samples` only limits summary samples. **log-aggregator plugin scripts are now 3/3 v1.0 shipped**; e2e validation task remains.
- 2026-06-01 **post-ship pai validation rerun** — user reported `pai` reachable; `ssh -o BatchMode=yes -o ConnectTimeout=5 pai true` PASS. `POC_HOST=pai uv run pytest tests/test_tail_multi_integration.py tests/test_correlate_integration.py tests/test_grep_across_sources_integration.py -m live_ssh -v` → **6 PASS / 1 SKIP** (`correlate` remote anchor skipped because no recent sshd `Accepted` line). `POC_HOST=pai uv run pytest tests/ -m live_ssh -q` → PASS with 10 environment skips (Docker daemon unavailable, `/tmp/poc-ssh-config` missing, one root chmod edge, same `correlate` no-anchor skip). This confirms the new `grep_across_sources` remote journal path works on `pai`.
- 2026-06-02 **plugin rollup closed** — all 3 child scripts are shipped/archived, shared lib + exit-code spec acceptance is satisfied, and the 2026-06-01 pai rerun is the persisted e2e validation evidence for this plugin rollup.
- 跨 plugin 经验沉淀（来自 ssh-core 5 个已 ship 脚本，复用到本 plugin）：
  - 共享 argparse 父 parser 用 `default=argparse.SUPPRESS` + `main()` `getattr` 兜底，避免 subparser default 覆盖（`05-24-devops-skills-v1-rollout/prd.md:84`）
  - `uv run` 副作用：commit 前 `git restore uv.lock` 隔离意外依赖升级（`prd.md:87`）
  - Sub-agent dispatch 适合 ~500 LOC 实现 + 30+ unit 单次完成 Phase 1-5；本 plugin 估算 tail_multi ~550 LOC、correlate ~400 LOC、grep_across_sources ~300 LOC，均适合 trellis-implement 单次 dispatch
  - Per-script failure_class namespace 已是项目惯例（5 个先例）；本 plugin 沿用，区间 40-49/90-99/180-189 已在 X2 锁定（2026-06-01 从旧规划的 60-69 修正，因该区间已被 ssh-guarded exec_detached 占用）
