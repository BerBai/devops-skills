# Implementation — grep_across_sources v1.0

> 前置条件：
> 1. 父任务与本任务均 `planning`。
> 2. **必须**在 `05-25-log-aggregator-tail-multi` `completed` 后启动（复用 lib）。
> 3. 推荐在 `05-25-log-aggregator-correlate` `completed` 后启动（避免 lib
>    接口分歧；如果 correlate 揭示 lib 需要调整，本任务可以受益）。
> 4. 等 `05-24-devops-skills-v1-rollout` Phase 1（ssh-core）收尾 + e2e 验证
>    通过后再 `task.py start`。

## Phase 0 — Pre-flight

- [ ] **0.1** Re-read `prd.md` (R1-R11) + `design.md` (D1-D6)。
- [ ] **0.2** Verify lib ready：
      ```bash
      python3 -c "
      from plugins.log_aggregator... import source_spec, fetchers
      print(source_spec.resolve, fetchers.iter_lines, fetchers.FetcherError)
      "
      ```
- [ ] **0.3** Re-read tail_multi ship commit + correlate ship commit
      （如果 correlate 已 ship）确认 lib 接口稳定。
- [x] **0.4** Spec slot：`.trellis/spec/backend/error-handling.md` 60-69 已被
      `ssh-guarded exec_detached` 占用；本 child 改用 180-189，避免冲突。

## Phase 1 — Foundation

- [ ] **1.1** 用 v1.0 docstring 替换 stub docstring；保留 `v1.0 surface` block。
- [ ] **1.2** 保留 build_parser 6 flag。增加：
      ```python
      _EXIT_CODES = {None: 0, "pattern_invalid": 180,
                     "source_spec_invalid": 40, "no_sources_resolved": 44,
                     "_generic": 1, "_timeout": 124}
      _HUMAN_HINTS = {
          "pattern_invalid": "regex failed to compile; check syntax",
          # ...
      }
      ```
- [ ] **1.3** `_emit_failure` 实现（与 correlate 类似，但更简）。
- [ ] **1.4** main 占位骨架。

## Phase 2 — `_validate_pattern`

- [ ] **2.1** 实现 `_validate_pattern(s) -> re.Pattern | None` + `PatternError`
      per design D1。
- [ ] **2.2** main 中调用：
      ```python
      try:
          pattern = _validate_pattern(args.pattern)
      except PatternError as e:
          return _emit_failure("pattern_invalid", 180, str(e), args)
      ```

## Phase 3 — Source 解析 + 并发抓取

- [ ] **3.1** main 调 `source_spec.resolve`，标准错误处理。
- [ ] **3.2** 实现 `_HitGroup` dataclass + `_run_grep_per_source` per design D2。
- [ ] **3.3** 实现 `_collect_groups(sources, pattern, since, max_samples,
      max_concurrent=8)`。
- [ ] **3.4** 实现 `FetcherFailure` dataclass `(source_tag, error_msg)`
      （或直接 import / 复用 correlate 的若已 ship）。

**Validation 3**:
```python
# 单测：mock fetcher 返回固定行，跑 _run_grep_per_source
@patch("grep_across_sources.fetchers.iter_lines")
def test_run_grep(mock_iter):
    mock_iter.return_value = iter([
        RawLine(..., raw_body="ERROR foo", raw_ts="ts1", fetched_at=0, fields={}),
        RawLine(..., raw_body="OK bar", raw_ts="ts2", fetched_at=0, fields={}),
        RawLine(..., raw_body="ERROR baz", raw_ts="ts3", fetched_at=0, fields={}),
    ])
    g = _run_grep_per_source(src, re.compile("ERROR"), since="5min", max_samples=5)
    assert g.hits == 2
    assert g.samples == [("ts1", "ERROR foo"), ("ts3", "ERROR baz")]
```

## Phase 4 — Three output formats

- [ ] **4.1** 实现 `_emit_summary(groups, is_heatmap, writer, total)` per D3。
- [ ] **4.2** 实现 `_emit_raw(groups, writer)` per D4。
- [ ] **4.3** 实现 `_emit_json(groups, failures, is_heatmap, writer)` per D4。
- [ ] **4.4** main dispatch：
      ```python
      fmt = "json" if args.json else args.format
      is_heatmap = pattern is None
      total = sum(g.hits for g in groups)
      if fmt == "summary":
          _emit_summary(groups, is_heatmap, sys.stdout, total)
      elif fmt == "raw":
          _emit_raw(groups, sys.stdout)
      elif fmt == "json":
          _emit_json(groups, failures, is_heatmap, sys.stdout)
      return 0
      ```

**Validation 4**:
```bash
# 本地 file fixture，三种 format 各跑一次
echo -e "ERROR a\nOK b\nERROR c" > /tmp/g.log
python3 .../grep_across_sources.py --sources file://local/tmp/g.log \
  --pattern ERROR --format summary
# Expect: 表头 + "file://local/tmp/g.log  2  [...] ERROR a" + Total: 1 sources, 2 hits

python3 .../grep_across_sources.py --sources file://local/tmp/g.log \
  --pattern ERROR --format raw
# Expect: 2 行 "[file://local/tmp/g.log ?] ERROR a" / "... ERROR c"

python3 .../grep_across_sources.py --sources file://local/tmp/g.log \
  --pattern ERROR --json
# Expect: 2 行 NDJSON + 1 行 envelope, envelope total_hits=2
```

## Phase 5 — Unit tests (~20)

- [ ] **5.1** 新建 `tests/test_grep_across_sources_unit.py`。
- [ ] **5.2** `_validate_pattern` tests（3 个）。
- [ ] **5.3** Summary 格式 tests（4 个）—— 用 io.StringIO writer 捕获，regex
      断言 layout。
- [ ] **5.4** Raw 格式 tests（3 个）。
- [ ] **5.5** JSON 格式 tests（3 个）—— 解析 NDJSON 行，断言 envelope shape。
- [ ] **5.6** Heat-map 模式 tests（3 个）。
- [ ] **5.7** CLI / main tests（4 个）。

**Validation 5**:
```bash
pytest tests/test_grep_across_sources_unit.py -v  # ~20 PASS
```

## Phase 6 — Integration tests

- [ ] **6.1** 新建 `tests/test_grep_across_sources_integration.py`，2 个
      `@pytest.mark.live_ssh` per design D6。
- [ ] **6.2** `test_grep_remote_pai_journal_pattern` 注意：journal `sshd`
      行总是有的（机器启动以来），不需要 skip。
- [ ] **6.3** `test_grep_local_fixture_heatmap` 用 tmp_path 写 110 行。
- [ ] **6.4** 跑通：
      ```bash
      pytest tests/test_grep_across_sources_integration.py -m live_ssh -v
      ```

## Phase 7 — SKILL.md / references 同步

- [ ] **7.1** Re-read SKILL.md `grep_across_sources.py` 段（`:42-45` decision
      tree + `:103-108` quick reference）。预期与 v1.0 实现一致 → 无改动。
- [ ] **7.2** 若 ship 时发现 default 值偏差（如 `--max-samples` 从 5 改了）
      → 同步 quick reference。
- [x] **7.3** raw/json 模式不做 sample cap；实现对齐 PRD R5/R6，
      `--max-samples` 仅限制 summary 样例列。

## Phase 8 — Quality gate + spec update

- [ ] **8.1** 全套：
      ```bash
      uv run ruff check plugins/log-aggregator/
      uv run mypy plugins/log-aggregator/skills/log-aggregator/scripts/
      uv run pytest tests/test_grep_across_sources_unit.py \
                    tests/test_grep_across_sources_integration.py -v
      uv run pytest tests/  # full
      uv run pytest tests/ -m live_ssh -v
      ```
- [ ] **8.2** `git restore uv.lock`.
- [ ] **8.3** 编辑 `.trellis/spec/backend/error-handling.md`：在 50-59 行
      之后插入：
      ```markdown
      | `180`–`189` | `grep_across_sources` per-script failure classes. `180`
      `pattern_invalid`. Introduced by `grep_across_sources` v1.0 (see
      `.trellis/tasks/05-25-log-aggregator-grep-across-sources/prd.md` R8). |
      ```
      在「Do not invent new exit codes」段加入 `180`–`189`。

## Phase 9 — Commit & wrap-up

- [ ] **9.1** Commit 范围：
      - `plugins/log-aggregator/skills/log-aggregator/scripts/grep_across_sources.py`（重写）
      - `tests/test_grep_across_sources_unit.py`（新）
      - `tests/test_grep_across_sources_integration.py`（新）
      - `.trellis/spec/backend/error-handling.md`（追加 180-189 段）
      - `plugins/log-aggregator/skills/log-aggregator/SKILL.md`（仅在 Phase 7
        发现需调整时）
      - **不** commit：`.trellis/tasks/**` / `uv.lock`
- [ ] **9.2** Commit message：
      ```
      feat(log-aggregator): ship grep_across_sources v1.0 N-source pattern hunt

      - regex match across N sources with hit-count + sample summary
      - three formats: summary (table sorted by hits) / raw / json (NDJSON)
      - heat-map mode when --pattern empty: per-source line volume
      - 1 per-script failure class / exit code 180
      - Reuses lib/{source_spec,fetchers} shipped by tail_multi (no lib changes)
      - ~20 unit + 2 live_ssh integration tests
      - spec update: error-handling.md gains 180-189 range
      - Final v1.0 ship for log-aggregator plugin; e2e validation task next

      Refs: .trellis/tasks/05-25-log-aggregator-grep-across-sources/
      Parent: .trellis/tasks/05-25-log-aggregator-v1/ (Phase 2 — complete)
      ```
- [ ] **9.3** `task.py finish` → `task.py archive ...`。
- [ ] **9.4** 更新父 prd.md Notes（Phase 2 完成）+ 祖父 prd.md Notes
      （+1 stub ship；如果是 23 个的最后一个，标记 100%）。
- [ ] **9.5** 父 task `05-25-log-aggregator-v1` 全部 3 child 完成 →
      `task.py archive 05-25-log-aggregator-v1`。
- [ ] **9.6** **开新任务**（不在本 child 范围）：
      `task.py create "e2e validate log-aggregator on pai" --parent 05-24-devops-skills-v1-rollout`
      参考 `05-24-e2e-validate-skills-on-pai` 做 e2e 验证 → 完成后才算
      log-aggregator plugin 真正收尾（per 父 prd AC）。

## Rollback points

- Phase 0.2 lib import 失败 → 停，回 tail_multi。
- Phase 3.3 _collect_groups 在真机上 ThreadPoolExecutor 异常未捕获 → 加
  catch（不让单源失败炸全局）。
- Phase 6 live_ssh 测试失败 → 不 mark completed，root cause 修完再 ship。

## Open follow-ups（写入父 prd.md Notes）

- raw/json 模式按 PRD 输出全部匹配事件；`--max-samples` 仅限制 summary 样例。
- heat-map 模式是否需要排序选项？（v1.0 按 hits 降序唯一；user 反馈决定 v1.1）
