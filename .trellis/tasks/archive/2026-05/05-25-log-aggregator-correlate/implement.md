# Implementation — correlate v1.0

> 前置条件：
> 1. 父任务 `05-25-log-aggregator-v1` 与本任务均 `planning`。
> 2. **必须**在 `05-25-log-aggregator-tail-multi` `completed` 后启动，因为
>    本任务复用 `lib/source_spec.py` / `fetchers.py` / `time_norm.py` /
>    `clock_skew.py`。
> 3. 等 `05-24-devops-skills-v1-rollout` Phase 1（ssh-core）全部 ship 完成 +
>    e2e validate 通过后，再 `task.py start 05-25-log-aggregator-correlate`
>    触发 in_progress。

## Phase 0 — Pre-flight

- [ ] **0.1** Re-read `prd.md` (R1-R11) + `design.md` (D1-D8) 确认未变。
- [ ] **0.2** Verify tail_multi ship 完成度：
      ```bash
      ls plugins/log-aggregator/skills/log-aggregator/scripts/lib/
      # Expect: __init__.py source_spec.py fetchers.py time_norm.py clock_skew.py filters.py
      python3 -c "
      from plugins.log_aggregator... import source_spec, fetchers, time_norm, clock_skew
      print(source_spec.resolve, fetchers.iter_lines, time_norm.normalize, clock_skew.probe)
      "
      # Expect: 4 个函数对象打印（不是 ImportError）
      ```
      如果其中任何 import 失败 → tail_multi 未 ship 完，不能启动本任务。
- [ ] **0.3** Read tail_multi 的 ship commit（应该 1 个 commit，msg 形如
      `feat(log-aggregator): ship tail_multi v1.0 ...`）+ `.trellis/spec/
      backend/error-handling.md` 当前 40-49 区间内容，确认契约。
- [ ] **0.4** 确认 lib 中 `_infer_level` 函数对 correlate 可见性（design D2
      指出复用）。如果 tail_multi ship 时该函数仅 `_` 私有于 `filters.py`：
      - 选项 A：correlate 在自己内部 re-implement `_infer_level`（少量重复）
      - 选项 B：申请把 `_infer_level` 提升为 `lib.filters.infer_level`（公开）
      - design D2 倾向选项 B；但本 phase 决定时需读 tail_multi shipped 代码
        再定。若选 B，需要 1 个跨 child 的 minor commit 仅做 rename，**不**
        改语义。
- [x] **0.5** Spec slot：`.trellis/spec/backend/error-handling.md` 50-59
      已由 ssh-guarded `run_request` 占用，不能复用；本任务改用 90-99，
      并同步更新父/子 PRD 与 backend error-handling 规范。

## Phase 1 — Foundation (CLI shell + envelopes)

- [ ] **1.1** 用 v1.0 docstring 替换 v0.2 stub docstring（保留 `v1.0 surface`
      block；`See:` block 加 PRD/design 路径 + 父 prd.md X1-X7 引用）。
- [ ] **1.2** 保留 stub 的 `build_parser()`（6 flag 不动）。增加常量：
      ```python
      _EXIT_CODES = {None: 0, "anchor_not_found": 90, "window_invalid": 91,
                     "pair_unmatched": 92, "source_spec_invalid": 40,
                     "no_sources_resolved": 44, "_generic": 1, "_timeout": 124}
      _HUMAN_HINTS = {
          "anchor_not_found": "No line matched --anchor in --since window. "
                              "Try widening --since or simplifying the regex.",
          "window_invalid": "Window must be like '30s', '2m', '1h', '1d'.",
          "pair_unmatched": "--pair-mode needs ≥2 matches; only 1 found.",
          # ...
      }
      ```
- [ ] **1.3** `_emit_failure` + `_emit_envelope` per design D6/D7 实现。
      json 输出**不流式**（与 tail_multi 不同）：构造完整 envelope dict 后
      一次性 `json.dump(envelope, sys.stdout)`。
- [ ] **1.4** `main(argv) -> int` 占位骨架。

**Validation 1**:
```bash
python3 .../correlate.py --help  # 6 flag
python3 .../correlate.py --sources file://local/tmp/x.log --anchor "X" \
  --window "garbage" --json
# Expect: exit 91, failure_class=window_invalid
```

## Phase 2 — `_parse_window`

- [ ] **2.1** 实现 `_parse_window(s: str) -> float` + `WindowError` per D1。
- [ ] **2.2** main 中第一步调用（在 spec 解析之前，因为 cheap check）。

## Phase 3 — Lib 集成

- [ ] **3.1** 在 main 中按 design D1 顺序调用：
      `source_spec.resolve → clock_skew.probe`。每步异常映射到对应 failure_class
      + exit code。
- [ ] **3.2** 实现 `NormalizedLine` dataclass + `_infer_level_via_lib(raw)`
      helper（封装对 `lib.filters._infer_level` 或 inline impl 的调用，看
      Phase 0.4 选项）。

## Phase 4 — `_collect_all_lines`

- [ ] **4.1** 实现 `_collect_all_lines(sources, since, max_concurrent=8)
      -> tuple[list[NormalizedLine], list[FetcherFailure]]` per design D2。
- [ ] **4.2** 实现 `FetcherFailure` dataclass `(source_tag, error_msg)`。
- [ ] **4.3** 关键差异 vs tail_multi：**exhaust** iter_lines（不 follow，
      不流式），所有源跑完后才进入下一步。

**Validation 4**:
```python
# 单测 _collect_all_lines（mock fetcher）
@patch("correlate.fetchers.iter_lines")
def test_collect_mocked(mock_iter):
    mock_iter.side_effect = [iter([RawLine(...) for _ in range(3)]),
                              iter([RawLine(...) for _ in range(2)])]
    lines, failures = _collect_all_lines([src1, src2], since="5min")
    assert len(lines) == 5
    assert failures == []
```

## Phase 5 — `_find_anchor`

- [ ] **5.1** 实现 `Anchor` dataclass + `_find_anchor` + `PairUnmatchedError`
      per design D3。
- [ ] **5.2** main 中 try-except 处理：
      - `find_anchor` 返回 None → `_emit_failure("anchor_not_found", 90, ...)`
      - 抛 `PairUnmatchedError` → `_emit_failure("pair_unmatched", 92, ...)`

## Phase 6 — `_filter_window` + `_compute_hint`

- [ ] **6.1** 实现 `_filter_window` per design D4。
- [ ] **6.2** 实现 `_compute_hint` per design D5。

## Phase 7 — 三段输出

- [ ] **7.1** 实现 `_emit_three_sections_human(writer, anchor, window_lines,
      hint, summary, color)`：
      - 三个分割线 `=== Anchor ===` 等字符级与 design D6 / references 一致
      - anchor 行尾加 `← anchor` marker
      - hint 行加 `↑ earliest critical (hint)` marker（如有）
      - color：anchor 红、critical 红、其他默认
- [ ] **7.2** 实现 `_emit_envelope_json` per design D6 JSON 节。
- [ ] **7.3** main 装配：
      ```python
      anchor = _find_anchor(all_lines, args.anchor, args.pair_mode)
      window_lines = _filter_window(all_lines, anchor, window_s)
      hint = _compute_hint(window_lines, anchor)
      summary = {"sources_with_hits": ..., "events_in_window": len(window_lines),
                 "anchor_severity": ...}
      if args.json:
          _emit_envelope_json(...)
      else:
          _emit_three_sections_human(...)
      return 0
      ```

**Validation 7**:
```bash
# Human mode 三段输出
echo "..." > /tmp/test.log  # 构造 3 行含 anchor
python3 .../correlate.py --sources "file://local/tmp/test.log?ts_regex=...&ts_format=..." \
  --anchor "MATCHME" --window 5s --since 1h
# Expect: stdout 含 `=== Anchor ===` / `=== Within window ===` / `=== Summary ===` 三段
```

## Phase 8 — Unit tests (~25)

- [ ] **8.1** 新建 `tests/test_correlate_unit.py`。
- [ ] **8.2** _parse_window tests（4 个）。
- [ ] **8.3** _find_anchor tests（5 个），mock all_lines 列表。
- [ ] **8.4** _filter_window tests（3 个），边界条件。
- [ ] **8.5** _compute_hint tests（3 个）。
- [ ] **8.6** 三段输出格式 tests（4 个），用 capsys / regex 断言 layout。
- [ ] **8.7** CLI / main tests（6 个），含 mock lib 调用：
      ```python
      @patch("correlate.fetchers.iter_lines")
      @patch("correlate.clock_skew.probe")
      def test_anchor_not_found(mock_probe, mock_iter, tmp_path):
          mock_probe.return_value = {}
          mock_iter.return_value = iter([])  # no lines
          # ... run subprocess and assert exit 90
      ```

**Validation 8**:
```bash
pytest tests/test_correlate_unit.py -v
# Expect ~25 PASS
```

## Phase 9 — Integration tests

- [ ] **9.1** 新建 `tests/test_correlate_integration.py`，2 个 `@pytest.mark.live_ssh`
      测试 per design D8。
- [ ] **9.2** `test_correlate_remote_pai_journal` 注意 fallback：如果 pai
      上 30min 无 'Accepted' 记录 → `pytest.skip`，不算 FAIL。
- [ ] **9.3** `test_correlate_local_fixture_pair_mode` 用 tmp_path 构造 fixture，
      验证 pair-mode duration_ms 精确等于构造数据。
- [ ] **9.4** 跑通：
      ```bash
      pytest tests/test_correlate_integration.py -m live_ssh -v
      ```

## Phase 10 — SKILL.md / references 同步

- [ ] **10.1** Re-read SKILL.md decision tree `correlate.py` 段
      (`SKILL.md:35-41`)。若实际默认值（如 `--window 30s`）与文档一致 → 无改动。
- [ ] **10.2** Re-read `references/correlation_patterns.md` 5 类节。前 3 类
      （trace-id / time-window / span pair）描述与本实现一致 → 无改动。第 4-5
      类（causality-by-symptom / cross-host diff）属"组合工作流"描述，本任务
      不实现 → 文档不动。
- [ ] **10.3** 若 ship 时发现 layout 与 references 字符级偏差 → **优先改实现**
      使其与 references 一致（references 是 SOT）。除非 references 本身有
      bug → 在 design.md "Open questions" 记下，并改 references。

## Phase 11 — Quality gate + spec update

- [ ] **11.1** 全套：
      ```bash
      uv run ruff check plugins/log-aggregator/
      uv run mypy plugins/log-aggregator/skills/log-aggregator/scripts/
      uv run pytest tests/test_correlate_unit.py tests/test_correlate_integration.py -v
      uv run pytest tests/  # full suite — 包括 tail_multi
      uv run pytest tests/ -m live_ssh -v
      ```
- [ ] **11.2** `git restore uv.lock`。
- [ ] **11.3** 编辑 `.trellis/spec/backend/error-handling.md`：插入：
      ```markdown
      | `90`–`99` | `correlate` per-script failure classes. `90`
      `anchor_not_found`, `91` `window_invalid`, `92` `pair_unmatched`.
      Introduced by `correlate` v1.0 (see
      `.trellis/tasks/05-25-log-aggregator-correlate/prd.md` R8). |
      ```
      在「Do not invent new exit codes」段加入 `90`–`99`。

## Phase 12 — Commit & wrap-up

- [ ] **12.1** git status 确认 commit 范围：
      - `plugins/log-aggregator/skills/log-aggregator/scripts/correlate.py`（重写）
      - `tests/test_correlate_unit.py`（新）
      - `tests/test_correlate_integration.py`（新）
      - `.trellis/spec/backend/error-handling.md`（追加 90-99 段）
      - `lib/*.py`（若 Phase 0.4 选项 B —— _infer_level rename 公开，加这一项）
      - **不** commit：`.trellis/tasks/**` / `uv.lock`
- [ ] **12.2** Commit message：
      ```
      feat(log-aggregator): ship correlate v1.0 anchor-and-window correlation

      - regex anchor search across all sources, returns earliest match
      - window expansion (s/m/h/d suffix), within-window line sort
      - three-section output (Anchor / Within window / Summary)
      - likely_root_cause_hint: earliest critical event in window
      - --pair-mode for span start/end duration
      - 3 per-script failure classes / exit codes 90-99
      - Reuses lib/ shipped by tail_multi (no lib changes in this commit
        unless _infer_level rename, see Phase 0.4)
      - ~25 unit + 2 live_ssh integration tests
      - spec update: error-handling.md gains 90-99 range

      Refs: .trellis/tasks/05-25-log-aggregator-correlate/
      Parent: .trellis/tasks/05-25-log-aggregator-v1/ (Phase 2)
      ```
- [ ] **12.3** `task.py finish` 清 current → `task.py archive ...`。
- [ ] **12.4** 更新父 prd.md Notes（与祖父 prd.md）。

## Rollback points

- Phase 0.2 lib import 失败 → 立即停，回到 tail_multi child 看 ship 状态。
- Phase 4 发现 lib.fetchers 接口与 design 假设不符 → 评估是否升级父 prd
  改 lib 接口（不是单边在 correlate 适配）。
- Phase 7 输出 layout 与 references 字符级偏差 → 改实现，不改 references
  （除非真有 bug）。

## Open follow-ups（写入父 prd.md Notes）

- 若 hint 算法在真实数据上**经常不命中真因**（critical event 不一定是根因，
  常是 cascade 的中间环节）→ 评估 v1.1 是否加 "earliest non-anchor source"
  作为 secondary hint。
- 若 user 反馈 want 多 anchor → 评估 v1.1 接受 `--anchor regex1 --anchor regex2`
  + AND/OR 模式。
