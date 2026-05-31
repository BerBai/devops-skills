# ship log-aggregator correlate v1.0

## Goal

把 `plugins/log-aggregator/skills/log-aggregator/scripts/correlate.py`
（当前 v0.2 stub: 66 LOC argparse skeleton + `unimplemented` + `exit 2`）推到
v1.0：在一组异构源里**找因果链**——以 anchor regex 定位时间锚 T，拉取所有源
在 `[T - window, T + window]` 内的事件，按归一化时间排序输出，并给出
`likely_root_cause_hint`（启发式：window 内最早的 critical 级日志）。这是 plugin
的**智力价值核心**：把多源日志合并成一条「围绕事件 X 的故事线」。

**前置依赖**：必须在 `05-25-log-aggregator-tail-multi` ship 完成后启动本任务，
因为 correlate 直接复用 `lib/source_spec.py` + `lib/fetchers.py` +
`lib/time_norm.py` + `lib/clock_skew.py`（不复用 `lib/filters.py`，原因见 R5）。

## User value

- 「nginx 14:02:02 报 500，发生了什么」→ anchor `"GET /api/charge.*500"`，
  window 30s，自动拉 nginx / payment / db 三源的窗口内事件并按时间排好：
  ```bash
  correlate.py --sources journal://web-3/nginx \
                         kube://local/payment/payment-svc-* \
                         docker://db-1/postgres \
              --anchor "GET /api/charge.*500" --window 30s
  ```
- 「这个 trace_id 经过了哪些服务」→ anchor `"trace_id=abc123"`，window 跨
  整条链路：所有源命中行按时间合并。
- 「span 时长」→ `--pair-mode` 找 anchor 的两次出现，输出 duration。
- **暴露最早的可能根因**：summary 段输出 `likely_root_cause_hint`（启发式
  "earliest critical event in window"），不是结论，让 Claude / 人判断。

## Confirmed facts (from repo inspection)

1. **CLI surface 已固定**（`scripts/correlate.py:4-9` docstring）：
   ```
   correlate.py --sources <spec>... --anchor <regex>
       [--window 30s] [--pair-mode] [--since 1h] [--json]
   ```
   stub 6 个 flag 已 argparse 接上（`scripts/correlate.py:31-42`），本任务沿用。

2. **关联模式已锁定** —— `references/correlation_patterns.md` 列了 5 类：
   trace-id / time-window / span (pair-mode) / causality-by-symptom /
   cross-host diff。本 v1.0 实现前 3 类（前 2 类共享代码路径，第 3 类靠
   `--pair-mode`）；第 4/5 类属 grep_across_sources 与 correlate 的组合使用，
   v1.0 文档说明而非实现。

3. **输出结构已锁定**（`references/correlation_patterns.md` "关联结果如何呈现"
   节）：三段式 `=== Anchor ===` / `=== Within window ===` / `=== Summary ===`，
   含 hint。本任务严格遵守这个 layout。

4. **复用 tail_multi lib**：本任务**不实现** source 解析、fetcher、normalize、
   clock skew —— 全部 `from lib.X import Y` 调用。如果 ship 时发现 lib 接口
   不便，升级到父 prd 评估 refactor，不在本 child 修 lib。

5. **Failure_class namespace 50-59**（父 prd.md X2）：
   `anchor_not_found / window_invalid / pair_unmatched` + 透传
   `fetcher_failed` / `source_spec_invalid`。Spec 同步：本任务 ship commit
   追加 50-59 段到 `error-handling.md`。

6. **JSON envelope** 沿用全仓契约；`--json` 模式下三段式以 `data` 字段嵌套，
   不再做 NDJSON 流式输出（与 tail_multi 不同：correlate 是一次性查询，
   一次性输出全部结果）。

7. **测试基线**：~25 unit + 2 live_ssh integration。

## Out of scope

- **OTel span tree 重建**：v1.0 仅识别共同 trace_id 与 pair_mode 的 span 起
  止；不重建嵌套 span。父 prd backlog 已记。
- **多 anchor 同时锚定**：v1.0 `--anchor` 接单个 regex。多 anchor 用户自行
  跑两次 correlate。
- **跨 trace_id 关联**（"找所有有 5xx 的 trace"）：v1.0 不做（属 grep
  pattern → correlate 的二跳工作流，文档示例）。
- **`--follow` live correlation**：v1.0 是一次性查询，**不**支持 follow。
- **自定义 hint 算法**：v1.0 用「earliest critical level (error/fatal/panic)
  in window」作 hint；用户不能改算法。
- **结果 export 到文件**：v1.0 仅 stdout（json + human），用户自己重定向。

## Requirements

### R1 — CLI surface
- 沿用 stub `scripts/correlate.py:31-42`：`--sources` + `--anchor` (required)
  + 4 个 flag。**禁止**新增 flag。
- `--window` 默认 `"30s"`；`--since` 默认 `"1h"`（搜 anchor 的回溯窗口）。
- `--pair-mode` 默认 false。
- `--anchor` 是 Python re 兼容 regex（不是 grep BRE/ERE）。

### R2 — 流程（design D1）
1. `lib.source_spec.resolve(args.sources)` → 解析源
2. `lib.clock_skew.probe(remote_hosts)` → 记录 skew
3. 对每源跑 `lib.fetchers.iter_lines(src, since=args.since, follow=False)`，
   收集所有行（不流式输出）
4. 对每行跑 `lib.time_norm.normalize` 得 (norm_time, body)
5. 收集后扫描所有行匹配 `args.anchor`，取**第一个匹配**作为 anchor T
   （`--pair-mode` 取第一个 + 最后一个）
6. 拉取所有源在 `[T - window, T + window]` 内的所有行（即步骤 3 收集结果
   的子集，按时间过滤）
7. 按 norm_time 全局排序
8. 输出三段：Anchor / Within window / Summary

### R3 — Anchor 搜索（→ correlate.py 内部）
- `find_anchor(all_lines, anchor_regex, pair_mode) -> Anchor | tuple[Anchor, Anchor] | None`
- 不区分大小写？**默认区分**（与 grep 默认一致），用户自己加 `(?i)`。
- 多源都有命中 → 取**全局最早**那个（按 norm_time）；`--pair-mode` 取最早 +
  最晚。
- 0 命中 → `failure_class=anchor_not_found` + `exit 50`。
- `--pair-mode` 但仅 1 个命中 → `failure_class=pair_unmatched` + `exit 52`。

### R4 — Window 解析
- `--window 30s` / `--window 2m` / `--window 1h` —— 支持 `s/m/h/d` 后缀。
- 解析失败（如 `--window abc` 或负值）→ `failure_class=window_invalid` +
  `exit 51`。
- Window 应用：anchor `T` ± window，**含端点**。`--pair-mode` 模式下 window
  应用于两个 anchor 各自的 ±window（即整个区间 = pair 跨度 + 两端 window
  扩展）。

### R5 — 不应用 filters（与 tail_multi 关键差异）
- 在 anchor window 内，**强制不过滤**（`references/noise_reduction.md`
  "不要默认丢什么" 节明确）—— 不管 noise deny-list / level / suppress 怎么
  配，window 内全部原始行都输出。
- 本任务**不使用** `lib/filters.py`。

### R6 — 三段输出（design D5）
- **Anchor 段**：
  ```
  === Anchor ===
  [web-3/nginx 14:02:02.034Z] GET /api/charge 500 8ms
  ```
  `--pair-mode` 时显示两条 + duration。
- **Within window 段**：所有源 window 内行，按 norm_time 排序，anchor 行
  用 `←  anchor` marker 高亮。
- **Summary 段**：
  ```
  sources_with_hits: 4
  anchor_severity: error
  likely_root_cause_hint:
    earliest_critical_event in window: db-1/postgres FATAL ...
  ```
  hint 算法详见 R7。
- `--json` 模式：完整 envelope 一次性输出，`data` 含 `anchor` / `window_lines`
  / `summary` 三 key。

### R7 — Likely root cause hint
- 算法：扫 Within window 内所有行，找 `level >= error` 的最早一条（按
  norm_time），输出 `<source_tag> <iso_short> <body[:100]>`。
- 同时输出 `anchor_severity`（anchor 行自己的 level）。
- 多个 same-time critical 事件 → 任选第一个（无 tiebreaker 设计）。
- 0 critical event → hint 字段为 `null`，不输出 "likely_root_cause_hint" 行。

### R8 — Failure classes & exit codes

| Code | failure_class | 触发 |
|---|---|---|
| 50 | `anchor_not_found` | 全部源扫描无 anchor 命中 |
| 51 | `window_invalid` | `--window` 解析失败 |
| 52 | `pair_unmatched` | `--pair-mode` 仅命中 1 次 |
| 53-59 | reserved | 未来扩展 |
| 40 | `source_spec_invalid` | 透传自 lib.source_spec |
| 44 | `no_sources_resolved` | 透传 |
| 41 | `fetcher_failed` | 透传：本任务即使所有 fetcher 失败也 try anchor 搜索（可能从 partial 数据找）；全部 fetcher 失败 + anchor 在剩余 0 行内未找到 → 主因是 `fetcher_failed` 还是 `anchor_not_found`？v1.0 规则：**优先报 anchor_not_found**（用户层更直观），fetcher 失败信息放 `data.failed_fetchers`。|
| 0 | (success) | 含 partial_fetch warning |
| 124 | (透传 ssh_execute) | ssh 端 timeout |

### R9 — Transport
- 复用 lib.fetchers 的 transport（即 `ssh_execute.py` shell-out）。
- 默认 `--timeout 120`（与 tail_multi 一致）。
- **不 follow**（R8 已说明）。

### R10 — Testing
- **Unit ~25**：
  - anchor 搜索 ~5（normal / 0 命中 / 多源都命中取最早 / pair_mode / regex 含
    特殊字符）
  - window 解析 ~4（s/m/h/d 后缀、负值、bad string、edge case 0s）
  - 三段输出格式 ~5（human 格式断言、json envelope 字段、empty window /
    only anchor）
  - hint 算法 ~3（找到 critical / 没有 critical / 多 critical 取最早）
  - CLI / main ~5（args bad / spec invalid / anchor_not_found / pair_unmatched
    / partial_fetch + anchor 找到）
  - lib 复用集成 ~3（mock fetcher 返回固定行，跑端到端 correlate）
- **Integration ~2 live_ssh**：
  - `test_correlate_remote_pai_journal` — pai 上跑
    `journal://pai/sshd --anchor "Accepted" --window 60s --since 30min`，
    断言 envelope 有 anchor + window_lines 行数 >= 1
  - `test_correlate_local_fixture_pair_mode` — 本地 file fixture
    （手写 50 行含 `span_id=s7` 两次）+ `--pair-mode`，断言 duration_ms
    与构造数据一致

### R11 — SKILL.md / references 同步
- SKILL.md decision tree `correlate.py` 段（`SKILL.md:35-41`）已是 v1.0
  描述，预期无需改。
- `references/correlation_patterns.md` 5 类模式 + 结果呈现节是 v1.0 描述，
  预期无需改。ship 时若发现实现与文档不一致 → 优先修订实现（除非 lib 接口
  已锁定），保持 references 为 SOT。
- 不修订 `references/noise_reduction.md`（correlate 不用 filters）。
- 不修订 `references/sources.md`（fetcher 由 tail_multi child 已 ship 时
  同步）。

## Acceptance Criteria

- [ ] `python correlate.py --help` exit 0，列出 6 个 flag 与 stub 一致。
- [ ] `pytest tests/test_correlate_unit.py` 全绿（~25 PASS）。
- [ ] `pytest tests/test_correlate_integration.py -m live_ssh -v` 全绿（~2 PASS）。
- [ ] `correlate.py --sources journal://pai/sshd --anchor "Accepted" --window 60s
      --since 30min --json` 在 pai 跑通；envelope `success=true,
      data.anchor.source` 存在，`data.window_lines.length >= 1`。
- [ ] `--anchor "PATTERN-NEVER-MATCH-ZZZZ"` → exit 50, failure_class =
      `anchor_not_found`。
- [ ] `--window "garbage"` → exit 51, failure_class = `window_invalid`。
- [ ] `--pair-mode` 但只命中 1 次 → exit 52, failure_class = `pair_unmatched`。
- [ ] `--json` 输出 envelope 单 JSON object（**不是** NDJSON）：
      `data = {anchor: {...}, window_lines: [...], summary: {...}}`。
- [ ] Human mode 输出三段 separator `=== Anchor ===` / `=== Within window ===` /
      `=== Summary ===` 与 references/correlation_patterns.md 锁定的 layout
      字符级一致。
- [ ] Anchor 在 window 内**不被任何 filter 跑掉**（即使本任务用了 user
      keep_always/drop 配置也强制忽略），符合 noise_reduction.md "不要默认
      丢什么"节。
- [ ] `.trellis/spec/backend/error-handling.md` 追加 50-59 段。
- [ ] `pytest tests/` 默认套件全绿，不破坏已 ship 的 tail_multi 测试。
- [ ] `lib/source_spec.py` / `lib/fetchers.py` / `lib/time_norm.py` /
      `lib/clock_skew.py` **保持原状**（correlate ship 不改 lib，如必须改 → 升
      级父 prd）。

## Notes

- 与 sibling `tail_multi` 共享 `lib/` —— 本任务 ship **不修改** lib 中任何
  函数签名。如发现 lib 接口不够用：先 stop ship、升级父 prd.md 商定 lib 变更
  方案、回到 tail_multi child 反补，再回 correlate。
- 与 sibling `grep_across_sources` 共享 anchor / window 解析？**不**：grep
  没有 anchor 概念，只有 pattern + since。两个 child 不共享代码（除 lib）。
- D1-D6 全部在 `design.md` 展开。
- Per-script failure_class namespace 第 7 个先例（前 6：see 父 prd.md X2）。
