# ship log-aggregator grep_across_sources v1.0

## Goal

把 `plugins/log-aggregator/skills/log-aggregator/scripts/grep_across_sources.py`
（当前 v0.2 stub: 66 LOC argparse skeleton + `unimplemented` + `exit 2`）推到
v1.0：在 N 个异构源上**并发跑同一个 regex**，输出每源命中数 + 样例的 summary
表（默认）/ 全行合并 raw 流 / NDJSON 三种格式。`--pattern` 留空时切到
**heat-map 模式**：每源的窗口内总行数，用于看 log volume 分布。

**前置依赖**：必须在 `05-25-log-aggregator-tail-multi` ship 完成后启动，因为
本任务复用 `lib/source_spec.py` / `fetchers.py`（不强依赖 time_norm / clock_skew，
原因见 R5）。

## User value

- 「全 K8s namespace 搜 OOMKilled」→
  ```bash
  grep_across_sources.py --sources kube://prod/all/?label=app \
                                   --pattern "OOMKilled|137" --since 1h
  # 输出每个 Pod 的命中数 + 第一条样例
  ```
- 「三主机 nginx 都报 5xx 吗，哪台更严重」→
  ```bash
  grep_across_sources.py --sources journal://web-{1,2,3}/nginx \
                                   --pattern "5\\d\\d" --since 5min
  # summary 表：每源命中数 + 样例，眼睛扫一眼能看出 web-3 命中明显高
  ```
- 「我不知道找什么，先看哪些源最吵」→ `--pattern ""` 启 heat-map：
  ```
  Source                 Lines    First
  journal://web-1/nginx   234     2026-05-25T13:55:01Z
  journal://web-2/nginx   189     2026-05-25T13:55:02Z
  kube://prod/payment/*  18342    2026-05-25T13:55:00Z   ← 异常吵
  ```

## Confirmed facts (from repo inspection)

1. **CLI surface 已固定**（`scripts/grep_across_sources.py:4-7` docstring +
   `:34-44` 实现）：
   ```
   grep_across_sources.py --sources <spec>... --pattern <regex>
       [--since 1h] [--format summary|raw|json]
       [--max-samples 5]
   ```
   stub 6 个 flag 已就位，本任务沿用。注意 `--pattern` 是 optional（empty →
   heat-map），与 tail_multi / correlate 的 anchor 必填不同。

2. **`--json` 与 `--format json` 别名**：stub `:42-43` 已实现"等价"逻辑
   `args.json or args.format == "json"` 都触发 json 输出。本任务沿用。

3. **三种 format 输出形态**（docstring + 父 prd.md X3）：
   - `summary` (默认)：表格 `Source / Hits / Sample (first match)`
   - `raw`：所有匹配行合并 `[source ts] body` 输出（类似 grep -r）
   - `json`：NDJSON，每行一个 match event；末尾追加 envelope summary 行
     （与 tail_multi 同构）

4. **复用 tail_multi lib**：`source_spec` + `fetchers` 必用；`time_norm`
   仅在 `--format raw` 输出 timestamp 时用；不用 `clock_skew`（grep 不关心
   时钟漂移 —— 用户只看命中数，不做时间对齐）；不用 `filters`（grep 用户
   想看全部命中，过滤是反作用）。

5. **Failure_class namespace 60-69**（父 prd.md X2）：
   `pattern_invalid` + 透传 `source_spec_invalid` / `no_sources_resolved`。

6. **测试基线**：~20 unit + 2 live_ssh integration（最少的一个 child）。

## Out of scope

- **多 pattern**（grep -e A -e B）：v1.0 单 regex，用户自己写
  `pattern1|pattern2`。
- **反向 grep（-v）**：v1.0 不实现，与 SKILL.md 暴露给 Claude 的契约一致。
- **上下文行（-A/-B/-C）**：v1.0 不实现 —— 那是 correlate 的领域（用 window
  + 命中行作 anchor）。
- **`--follow` live grep**：v1.0 是一次性查询。
- **结构化字段匹配**（如 `--field level=error`）：v1.0 纯 regex。
- **Heat-map 跨源 normalize 排序**：v1.0 heat-map 表按命中数降序排（不按
  source order），便于一眼看出最吵的源；按 source order 排是 v1.1+。

## Requirements

### R1 — CLI surface
- 沿用 stub `:34-44`：`--sources` (required) + `--pattern` (optional) +
  4 flag。**禁止**新增 flag。
- `--since` 默认 `"1h"`；`--format` 默认 `"summary"`；`--max-samples` 默认
  `5`。
- `--pattern` 留空 / 不传 → heat-map 模式（R7）。

### R2 — 流程
1. `lib.source_spec.resolve(args.sources)` → 失败 exit 40 / 空集 exit 44
2. `_validate_pattern(args.pattern)` → 失败 exit 60
3. 并发跑 fetcher（同 correlate `_collect_all_lines` 模式，但**不**做
   normalize/level inference，纯 raw_body）
4. 对每行跑 `pattern.search(body)`（pattern 为空则跳过此步）
5. 按 source 聚合：`{source_tag: HitGroup(hits=int, samples=[...])}`
6. 按 `--format` 选输出路径

### R3 — Pattern 校验（→ grep_across_sources.py 内部）
- `_validate_pattern(s) -> re.Pattern | None`：
  - `s` 为 None / 空 → 返回 None（heat-map 模式）
  - `s` 是合法 regex → 返回编译对象
  - 编译失败（`re.error`）→ `failure_class=pattern_invalid` + `exit 60`
- 不强制大小写敏感（grep 默认敏感），用户加 `(?i)` 自己开关。

### R4 — Output: summary 格式
```
Source                       Hits   Sample (first match)
─────────────────────────────────────────────────────────────────────────
journal://web-1/nginx        142    [14:01:55] GET /api/charge 500 12ms
journal://web-2/nginx        138    [14:01:56] GET /api/charge 500 14ms
journal://web-3/nginx        201    [14:01:54] GET /api/charge 500 8ms
kube://prod/payment/* 1832   [14:01:50] OOMKilled exitCode=137
─────────────────────────────────────────────────────────────────────────
Total: 4 sources, 2313 hits
```
- 每源最多 `--max-samples` 个样例；默认 5，summary 模式仅显示**首条**样例
  （`--max-samples` 在此 format 下不直接体现，仅记入 raw/json 模式）
- 行宽 auto-fit：source 列宽 = max(len(tag) for tag in sources)
- Sample 截断到 80 列（剩余 `...`）
- 按 hits 降序排（最吵的在最上方）；hits 同数按 source_tag 字典序

### R5 — Output: raw 格式
```
[web-1/nginx 14:01:55Z] GET /api/charge 500 12ms
[web-2/nginx 14:01:56Z] GET /api/charge 500 14ms
[web-3/nginx 14:01:54Z] GET /api/charge 500 8ms
[web-1/nginx 14:01:55.123Z] GET /api/charge 500 14ms
...
```
- 所有命中行合并输出
- 时间戳取自 raw_line.raw_ts（**不**走 lib.time_norm 完整归一化；只截
  HH:MM:SS 显示，避免性能成本）
- 行内无 sort（按 fetcher 抓取顺序），用户想排序自己 `| sort`
- 不受 `--max-samples` 限制

### R6 — Output: json 格式
NDJSON 流式：
```jsonl
{"source": "web-1/nginx", "time": "2026-05-25T14:01:55Z", "raw": "GET /api/charge 500 12ms"}
...
{"success":true,"exit_code":0,"data":{"total_hits":2313,"per_source":{"web-1/nginx":142,...},"sources":[...],"failed_fetchers":[]}}
```
- 与 tail_multi `--json` 同构：流式 events + 末尾 envelope summary
- envelope `data.per_source` 含每源 hit count
- `--json` flag 与 `--format json` 完全等价（CLI 兼容性）

### R7 — Heat-map 模式（`--pattern` 留空）
- 不跑 regex search，直接计每源行数
- 输出 summary 格式（heat-map 仅 summary，不影响 raw/json）：
  ```
  Source                       Lines    First seen
  ──────────────────────────────────────────────────────────────────
  kube://prod/payment/*       18342    2026-05-25T13:55:00Z
  journal://web-1/nginx        234     2026-05-25T13:55:01Z
  ──────────────────────────────────────────────────────────────────
  Total: 2 sources, 18576 lines
  ```
- `--format raw` + 空 pattern：输出所有源所有行（用户大概率不想，但合法）
- `--format json` + 空 pattern：events 是全部行，不只匹配行；envelope
  `data.total_hits` 改名 `data.total_lines`

### R8 — Failure classes & exit codes

| Code | failure_class | 触发 |
|---|---|---|
| 0 | (none) | 成功（含 partial_fetch warning） |
| 1 | generic | 未预期异常 |
| 40 | source_spec_invalid | 透传 |
| 44 | no_sources_resolved | 透传 |
| 60 | pattern_invalid | regex 编译失败 |
| 61-69 | reserved | 未来扩展 |
| 124 | (透传) | ssh timeout |

Partial fetch 处理同 correlate：≥1 fetcher 失败但其他成功 → exit 0 + envelope
`data.failed_fetchers`。

### R9 — Transport
- 复用 lib.fetchers 的 ssh_execute shell-out 路径。
- 默认 `--timeout 120`（与 tail_multi 一致）。
- 不 follow。

### R10 — Testing
- **Unit ~20**：
  - `_validate_pattern` ~3（normal / empty / invalid regex）
  - summary 格式 ~4（排序、宽度对齐、sample 截断、`max_samples=1`）
  - raw 格式 ~3（multi-source 合并、空命中、时间戳显示）
  - json 格式 ~3（NDJSON 流式、envelope shape、空 pattern → total_lines）
  - heat-map 模式 ~3
  - CLI / main ~4（args bad / pattern invalid / partial_fetcher / 0 命中
    summary 输出全 0）
- **Integration ~2 live_ssh**：
  - `test_grep_remote_pai_journal_pattern` — `journal://pai/sshd
    --pattern "sshd" --since 30min --json`，断言 envelope total_hits > 0
  - `test_grep_local_fixture_heatmap` — 本地 2 个 file fixture（一个 10 行、
    一个 100 行），跑 heat-map，断言 envelope `total_lines == 110`、
    `per_source` 含 2 个键

### R11 — SKILL.md / references 同步
- SKILL.md decision tree `grep_across_sources.py` 段（`SKILL.md:42-45`）
  与 quick reference (`:103-108`) 已是 v1.0 描述。
- 不修订 `references/correlation_patterns.md`（不属本 child）。
- 不修订 `references/noise_reduction.md`（grep 不用 filters）。
- `references/sources.md` 已由 tail_multi child 同步。

## Acceptance Criteria

- [ ] `python grep_across_sources.py --help` exit 0，列出 6 个 flag。
- [ ] `pytest tests/test_grep_across_sources_unit.py` 全绿（~20 PASS）。
- [ ] `pytest tests/test_grep_across_sources_integration.py -m live_ssh -v`
      全绿（~2 PASS）。
- [ ] `grep_across_sources.py --sources file://local/<fixture> --pattern "ERROR" --json`
      输出 NDJSON events + envelope，`data.total_hits` 等于实际匹配行数。
- [ ] `grep_across_sources.py --sources file://local/<fixture> --format summary`
      输出表格，按 hits 降序，header 与 R4 字符一致。
- [ ] `--pattern "[bad regex"` → exit 60, failure_class=pattern_invalid。
- [ ] `--pattern ""` 或不传 → heat-map 模式输出，envelope `data.total_lines`
      字段（不是 total_hits）。
- [ ] `--format json` 与 `--json` 输出完全相同。
- [ ] `.trellis/spec/backend/error-handling.md` 追加 60-69 段。
- [ ] `pytest tests/` 默认套件全绿，不破坏 tail_multi/correlate 测试。
- [ ] `lib/` 保持原状。

## Notes

- D1-D5 在 `design.md` 展开（本 child design 最简，仅 5 节）。
- Per-script failure_class namespace 第 8 个先例（前 7：see 父 prd.md X2）。
- ship 完即 log-aggregator plugin 全 v1.0；下一步是 e2e validation child（
  另开任务，参 `05-24-e2e-validate-skills-on-pai` 模板）。
