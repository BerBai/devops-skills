# Implementation — tail_multi v1.0

> 前置条件：父任务 `05-25-log-aggregator-v1` 与本任务均 `planning`；本 plan 等
> `05-24-devops-skills-v1-rollout` Phase 1（ssh-core）全部 ship 完成 + e2e
> validate 通过后，再 `task.py start 05-25-log-aggregator-tail-multi` 触发
> in_progress。

## Phase 0 — Pre-flight

- [ ] **0.1** Re-read `prd.md` (R1-R12 locked, 9 flag CLI surface, 4 scheme spec,
      40-49 exit codes) and `design.md` (architecture diagram, D1-D10 lib
      module layout). Confirm parent `prd.md` X1-X7 cross-cutting decisions
      still hold (check git log for any update since this plan was written).
- [ ] **0.2** Read `.trellis/spec/backend/error-handling.md` 当前内容。
      确认 40-49 区间未被占用（截 2026-05-25 仅 0/1/2/4-9/10-15/20-29/124
      被占）。本任务 ship 同 commit 内追加 40-49 段（Phase 11.3）。
- [ ] **0.3** Confirm `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py`
      CLI 形态：`ssh_execute.py <alias> [--json] [--timeout N] -- <cmd argv>`。
      读 ssh_execute.py docstring + 一个调用方（如 deploy_pubkey.py / ssh_cluster.py）
      确认 exact argv shape。如发现 stub 文档与现实有 drift，design.md D3
      `_run_cmd` 的 ssh_argv 构造需要同步调整。
- [ ] **0.4** Confirm Phase 1 (ssh-core) 真的收尾了。运行
      `python3 ./.trellis/scripts/task.py list 2>&1 | grep ssh-core` 确认
      所有 `ssh-core-*` child 都 `completed` 或 `archived`。如果 Phase 1
      未收尾 → **不要 start 本任务**，回到父 prd.md X2-X5 与 user 重新协商。
- [ ] **0.5** Read 已 ship 的参考脚本作 shape 借鉴：
      - `ssh_cluster.py` — N 个远端并发 ssh_execute 的模式（fan-out / collect）
      - `ssh_server_transfer.py` — failure_class namespace + envelope 的实现模板
      - `deploy_pubkey.py` — _HUMAN_HINTS 表 + _EXIT_CODES dict 写法

## Phase 1 — Foundation (CLI shell + envelopes)

- [ ] **1.1** 在 `plugins/log-aggregator/skills/log-aggregator/scripts/tail_multi.py`
      用 v1.0 docstring 替换 v0.2 stub docstring（保留 `v1.0 surface:` block；
      `See:` block 增加 PRD/design 路径 + ADR-001 引用 + 父 prd.md X1-X7 引用）。
- [ ] **1.2** 保留 v0.2 stub 的 `build_parser()`（9 flag 不动）。
      增加 `_EXIT_CODES` / `_HUMAN_HINTS` 常量：
      ```python
      _EXIT_CODES = {None: 0, "source_spec_invalid": 40,
                     "fetcher_failed": 41, "clock_skew_failed": 42,
                     "normalize_failed": 43, "no_sources_resolved": 44,
                     "partial_fetch": 0,  # warning, exit 0
                     "_generic": 1, "_timeout": 124}
      _HUMAN_HINTS = {
          "source_spec_invalid": "Check source spec syntax: <scheme>://<host>/<payload>",
          "no_sources_resolved": "Brace/glob 展开后 0 个有效源；检查 --sources",
          # ... 6 类全列
      }
      ```
- [ ] **1.3** 实现 `_emit_failure(fclass: str, exit_code: int, stderr_msg: str, args)`：
      构造 envelope `{success: False, exit_code, stdout: "", stderr: stderr_msg,
      data: {failure_class: fclass, sources: args.sources, ...}}`；按 args.json
      决定写 stdout (NDJSON) 还是 stderr (human + hint)。
- [ ] **1.4** 实现 `_emit_envelope_summary(writer, sources, skew, fclass=None,
      failed_fetchers=None, events_emitted=0, elapsed=0.0) -> int`：
      success envelope 或 partial_fetch warning。
- [ ] **1.5** `main(argv) -> int`：占位骨架，先 stub 出
      ```python
      args = build_parser().parse_args(argv)
      # TODO Phase 2-7
      return 0
      ```

**Validation 1**:
```bash
python3 plugins/log-aggregator/skills/log-aggregator/scripts/tail_multi.py --help
# Expect 9 flag 全部列出（与 v0.2 stub 一致）

python3 plugins/log-aggregator/skills/log-aggregator/scripts/tail_multi.py \
  --sources "INVALID" --json
# Expect 此刻还会 exit 0（TODO），但 envelope 写出 succes:True 即可。下个 phase 实现
# spec 校验后再回填。
```

## Phase 2 — `lib/source_spec.py`

- [ ] **2.1** 创建 `plugins/log-aggregator/skills/log-aggregator/scripts/lib/__init__.py`
      (空) 与 `lib/source_spec.py`。
- [ ] **2.2** 实现 `ResolvedSource` dataclass(frozen=True) 6 字段（design D2）。
- [ ] **2.3** 实现 `SpecError(ValueError)` 与 `_BRACE_RE` / `_SPEC_RE` 常量。
- [ ] **2.4** 实现 `_expand_brace(spec) -> list[str]` 递归展开。
      边界：无 brace 直接返回单元素 list；空 brace `{}` 视为空展开（保留原 spec
      但不再展开）；嵌套 brace 通过递归处理。
- [ ] **2.5** 实现 `_parse_one(spec) -> ResolvedSource`：4 scheme 各自校验
      （per design D2），构造 `source_tag` 用于输出前缀。
- [ ] **2.6** 实现 `resolve(specs) -> list[ResolvedSource]` 主入口：先 brace
      展开，再 parse；去重（同 `original_spec` 去重）。
- [ ] **2.7** 接入 `main()`：在 build_parser 后立刻 try-except 调用
      `source_spec.resolve(args.sources)`，捕获 SpecError 走 _emit_failure(
      "source_spec_invalid", 40, ...)；空集走 ("no_sources_resolved", 44, ...)。

**Validation 2**:
```bash
python3 .../tail_multi.py --sources "journal://web-{1,2,3}/nginx" --json
# Expect: 仍 exit 0 但 envelope 写出 data.sources 长度 3，source_tag = "web-N/nginx"
python3 .../tail_multi.py --sources "BAD" --json
# Expect: exit 40, failure_class=source_spec_invalid
```

## Phase 3 — `lib/clock_skew.py`

- [ ] **3.1** 实现 `_SSH_EXECUTE_PATH` 模块常量：`Path(__file__).resolve().
      parents[3] / "ssh-core/skills/ssh-core/scripts/ssh_execute.py"`。
      ⚠ 该路径假设 plugins/ 目录布局；ship 前用 `_SSH_EXECUTE_PATH.exists()`
      做 sanity check。
- [ ] **3.2** 实现 `_probe_one(host, timeout)` 与 `probe(remote_hosts, timeout)`
      per design D5。
- [ ] **3.3** 接入 `main()`：spec 解析后立刻 probe，结果存 `skew` 局部变量，
      后续传给 writer.banner / envelope.data。

**Validation 3**:
```bash
python3 .../tail_multi.py --sources "journal://pai/sshd" --json --since 1min
# Expect: 仍 exit 0，envelope data.clock_skew_ms 含 {"pai": <int|null>}
```

## Phase 4 — `lib/fetchers.py`

- [ ] **4.1** 实现 `RawLine` dataclass(frozen=True) 5 字段（design D3）。
- [ ] **4.2** 实现 `FetcherError(Exception)` + `_run_cmd(host, argv, timeout) ->
      Iterator[str]` per design D3：本地 vs 远端 dispatch，subprocess.Popen
      + readline + finally terminate。
- [ ] **4.3** 实现 `_journal_fetcher` per design D3，含 `_parse_since_to_epoch
      (since: str) -> int`（支持 "5min" / "1h" / ISO timestamp / unix epoch）。
- [ ] **4.4** 实现 `_docker_fetcher`。
- [ ] **4.5** 实现 `_kube_fetcher` 与 `_enumerate_kube_pods(host, ns, pattern,
      label)`。glob 展开用 `fnmatch.filter`。
- [ ] **4.6** 实现 `_file_fetcher`（tail -F / tail -n N）。
- [ ] **4.7** 实现 `iter_lines(source, since, follow, timeout)` dispatch。

**Validation 4**:
```bash
# 本地 file fetcher 烟测
echo "line 1" > /tmp/foo.log; echo "line 2" >> /tmp/foo.log
python3 -c "
from plugins.log_aggregator... import fetchers, source_spec
src = source_spec.resolve(['file://local/tmp/foo.log'])[0]
for line in fetchers.iter_lines(src, since='5min', follow=False, timeout=5):
    print(line.raw_body)
"
# Expect: line 1 / line 2
```

## Phase 5 — `lib/time_norm.py` + reorder buffer

- [ ] **5.1** 实现 `lib/time_norm.py` 4 个 `_norm_<scheme>` 函数 + `normalize`
      dispatch per design D4。
- [ ] **5.2** 在 `tail_multi.py` 内（不抽到 lib）实现 `ReorderBuffer` 类
      per design D4。原因：只有 tail_multi 用，correlate / grep_across_sources
      自己的输出策略不一样（correlate 一次性输出排序后窗口，无需 reorder；
      grep 不要求顺序）。
- [ ] **5.3** 实现 `FetchedLine` 包装类（main script-level）：
      ```python
      @dataclass
      class FetchedLine:
          raw: RawLine
          norm_time: datetime
          body: str
          level: str  # "trace"|"debug"|"info"|"warn"|"error"|"fatal"
      ```

**Validation 5**:
```python
# 在 ipython 验证 normalize
from time_norm import normalize, RawLine, ResolvedSource
src = ResolvedSource("journal", "local", "sshd", {}, "...", "local/sshd")
rl = RawLine(src, "test msg", "1716580921000000", {"MESSAGE":"test msg"}, 0)
dt, body = normalize(rl)
print(dt.isoformat())  # 2024-05-24T...
```

## Phase 6 — `lib/filters.py`

- [ ] **6.1** 实现 `_DEFAULT_NOISE_PATTERNS` 常量：12 条 regex，每条加注释
      引用 `references/noise_reduction.md` 的对应小节。
- [ ] **6.2** 实现 `_LEVEL_ORDER` dict 与 `_infer_level(scheme, fields, body)`
      函数（journal 用 `PRIORITY` 字段；其他用 regex `\b(TRACE|DEBUG|INFO|
      WARN|ERROR|FATAL|PANIC)\b`，nginx 用 status code 5xx → error）。
- [ ] **6.3** 实现 `_normalize_key(body, aggressive) -> str`：
      - 默认：去掉 ISO timestamp / `[PID:...]` / IPv4 / `trace_id=...` /
        `request_id=...` / 数字 timestamp
      - aggressive：再把所有数字 → `<N>`、所有 hex → `<HEX>`
- [ ] **6.4** 实现 `_SuppressEntry` dataclass 与 `FilterPipeline` per design D6。
- [ ] **6.5** 实现 `load_user_overrides() -> tuple[list[Pattern], list[Pattern]]`：
      读 `~/.devops/log_filters.json`（不存在则返回空对）。
- [ ] **6.6** 实现 `build_pipeline(...) -> FilterPipeline` 入口函数。

**Validation 6**:
```python
from filters import build_pipeline
fp = build_pipeline(no_default=False, min_level="trace", suppress=True,
                    aggressive=False, user_overrides=([], []))
# 验证 healthcheck 行被默认 noise 丢
class FL: body = "GET /healthz 200 1ms"; level="info"
assert fp.should_drop(FL())
```

## Phase 7 — main() 主流程 + drain loop

- [ ] **7.1** 实现 `_run_fetcher(src, args, line_q) -> None`：把 fetcher
      迭代器结果 push 到 line_q；遇到 FetcherError 让 future 异常抛出（主线
      程收集为 partial_fetch）。
- [ ] **7.2** 实现 `_drain_loop(line_q, futures, filter_pipeline, reorder, writer)`
      per design D7。注意 Empty exception 触发 reorder buffer flush 检查。
- [ ] **7.3** 实现 `OutputWriter` 类（main script-level，~80 LOC）：
      - `banner(meta: dict)` — 人模式写头部摘要；json 模式 no-op
      - `emit(line: FetchedLine)` — 人/json 模式各自写 stdout
      - `summary(envelope: dict)` — 写最后一行（人模式分割线 + 表；json
        模式 envelope NDJSON 行）
      - 含 ANSI color helper（NO_COLOR / not isatty 时 disable）
- [ ] **7.4** 装配 `main()`：
      ```python
      args -> resolve sources -> probe skew -> banner ->
      build filter pipeline -> open reorder buffer -> open line_q ->
      ThreadPoolExecutor with workers per source ->
      _drain_loop -> collect futures exceptions ->
      _emit_envelope_summary
      ```
      Ctrl-C → cancel futures + drain reorder buffer + emit envelope (exit 0)。
- [ ] **7.5** SIGTERM 处理：`signal.signal(SIGTERM, _graceful_shutdown)`，
      触发与 Ctrl-C 同等路径。

**Validation 7**:
```bash
# 本地 file → 走完整流水线
echo "line 1" > /tmp/x.log; echo "line 2" >> /tmp/x.log
python3 .../tail_multi.py --sources "file://local/tmp/x.log" --json
# Expect: 2 个 NDJSON event + 1 行 envelope summary，summary.success=True，
# data.events_emitted=2

# 远端 journal（在 pai 跑）
python3 .../tail_multi.py --sources "journal://pai/sshd" --since 5min --json
# Expect: 多个 event + envelope，summary.clock_skew_ms 含 pai 字段
```

## Phase 8 — Unit tests (~30)

- [ ] **8.1** 新建 `tests/test_tail_multi_unit.py`。固定测试 dispatch：
      ```python
      SCRIPT_PATH = (Path(__file__).resolve().parents[1] / "plugins"
                     / "log-aggregator" / "skills" / "log-aggregator"
                     / "scripts" / "tail_multi.py")
      ```
- [ ] **8.2** Source_spec tests（12 个）— per design D10：
      ```python
      def test_journal_spec_with_unit():
          rs = source_spec.resolve(["journal://web-1/nginx"])
          assert len(rs) == 1
          assert rs[0].scheme == "journal"
          assert rs[0].host == "web-1"
          assert rs[0].payload == "nginx"
          assert rs[0].source_tag == "web-1/nginx"
      # ... 11 more
      ```
- [ ] **8.3** Time_norm tests（8 个）：journal μs / docker RFC3339 /
      kube RFC3339 / file ts_regex+ts_format，各正常 + 异常 fallback。
- [ ] **8.4** Filters tests（8 个）：noise pattern 命中 / level 阈值 /
      suppress 速率 5次 / keep_always 优先级。
- [ ] **8.5** Clock_skew tests（3 个）：mock `subprocess.check_output` 返回
      `"1716580921.123\n"`，断言 `_probe_one` 计算 skew_ms；空 host list；
      全部 mock 抛异常 → 全 None。
- [ ] **8.6** CLI / main tests（5 个）：
      - `--help` exit 0 + stdout 含 9 个 flag
      - `--sources BAD_SPEC` → exit 40, json envelope failure_class
      - `--sources file://local/<tmpfile>` → 行数与 envelope events_emitted 一致
      - Ctrl-C 触发（用 `signal.alarm` 模拟）→ envelope success=True, exit 0
      - 1 失败 fetcher（mock raise FetcherError）+ 1 成功 → exit 0,
        failure_class=partial_fetch

**Validation 8**:
```bash
pytest tests/test_tail_multi_unit.py -v
# Expect: ~30 PASS, 0 FAIL
```

## Phase 9 — Integration tests (`-m live_ssh`)

- [ ] **9.1** 新建 `tests/test_tail_multi_integration.py`，pytest marker
      `@pytest.mark.live_ssh`。
- [ ] **9.2** 实现 2 个测试 per design D10：
      - `test_journal_remote_pai` — `journal://pai/sshd --since 10min --json`，
        20s timeout，断言 events_emitted ≥ 0、clock_skew_ms 含 pai。
      - `test_file_local_fixture` — tmp_path 写 50 行 → `file://local{path}`，
        断言 events_emitted == 50。
- [ ] **9.3** 跑测试确认通过：
      ```bash
      pytest tests/test_tail_multi_integration.py -m live_ssh -v
      # Expect: 2 PASS
      ```

## Phase 10 — SKILL.md / references 同步

- [ ] **10.1** Re-read `plugins/log-aggregator/skills/log-aggregator/SKILL.md`
      确认与 v1.0 实现一致。预期需要的微调：
      - SKILL.md frontmatter 描述：当前已是 v1.0 描述，**无需改**
      - Quick reference `tail_multi.py` 块 (line 92-95)：确认默认 `--since 5min`
        与实现一致
      - Decision tree 描述：当前已是 v1.0 描述，无需改
- [ ] **10.2** Re-read `references/sources.md` 4 个 scheme 节，逐节对比实现：
      - 远端命令 argv 完全一致 ✓
      - 字段名（`__REALTIME_TIMESTAMP` 等）一致 ✓
      - 坑（idle timeout / `--previous`）描述与实现行为一致 ✓
      - 如有 drift，编辑对应节。
- [ ] **10.3** Re-read `references/noise_reduction.md` 默认 deny-list 节，
      对比 `_DEFAULT_NOISE_PATTERNS`。每条 regex 都要在 references 里有对应
      项；若实现微调（如加了新 pattern），同步追加。

## Phase 11 — Quality gate + spec update

- [ ] **11.1** Lint / type-check / 全套测试：
      ```bash
      cd /Users/ber/Project/devops-skills
      uv run ruff check plugins/log-aggregator/
      uv run ruff format --check plugins/log-aggregator/
      uv run mypy plugins/log-aggregator/skills/log-aggregator/scripts/
      uv run pytest tests/test_tail_multi_unit.py tests/test_tail_multi_integration.py -v
      uv run pytest tests/  # 全套套件
      uv run pytest tests/ -m live_ssh -v  # 全套 live
      ```
      预期：全绿。
- [ ] **11.2** `git restore uv.lock`（per 父任务 prd Note：`uv run` 触发
      bcrypt 等依赖升级，commit 前隔离）。
- [ ] **11.3** 编辑 `.trellis/spec/backend/error-handling.md`：在 exit code
      表内 `20-29` 行之后插入新行：
      ```markdown
      | `40`–`49` | `tail_multi` per-script failure classes. `40`
      `source_spec_invalid`, `41` `fetcher_failed`, `42`
      `clock_skew_failed` (reserved, v1.0 warn-only), `43`
      `normalize_failed`, `44` `no_sources_resolved`, `45`
      `partial_fetch` (warning: exit 0, paired data.failure_class).
      Introduced by `tail_multi` v1.0 (see
      `.trellis/tasks/05-25-log-aggregator-tail-multi/prd.md` R9). |
      ```
      同时在「Do not invent new exit codes」段的 list 里加入 `40`–`49`。

## Phase 12 — Commit & wrap-up

- [ ] **12.1** `git status` 确认 commit 范围：
      - `plugins/log-aggregator/skills/log-aggregator/scripts/tail_multi.py`（重写）
      - `plugins/log-aggregator/skills/log-aggregator/scripts/lib/__init__.py`（新）
      - `plugins/log-aggregator/skills/log-aggregator/scripts/lib/source_spec.py`（新）
      - `plugins/log-aggregator/skills/log-aggregator/scripts/lib/fetchers.py`（新）
      - `plugins/log-aggregator/skills/log-aggregator/scripts/lib/time_norm.py`（新）
      - `plugins/log-aggregator/skills/log-aggregator/scripts/lib/clock_skew.py`（新）
      - `plugins/log-aggregator/skills/log-aggregator/scripts/lib/filters.py`（新）
      - `tests/test_tail_multi_unit.py`（新）
      - `tests/test_tail_multi_integration.py`（新）
      - `.trellis/spec/backend/error-handling.md`（追加 40-49 段）
      - `plugins/log-aggregator/skills/log-aggregator/SKILL.md`（仅在 10.1
        发现需调整时）
      - `plugins/log-aggregator/skills/log-aggregator/references/sources.md`
        / `noise_reduction.md`（仅在 10.2/10.3 发现需调整时）
      **Excluded**: `.trellis/tasks/**`（任务工作区，per CONTRIBUTING/父 prd 惯例
      不入主仓 commit）；`uv.lock`（11.2 已 restore）。
- [ ] **12.2** Commit message（参 ssh-server-transfer commit `a502afb` 风格）：
      ```
      feat(log-aggregator): ship tail_multi v1.0 multi-source live tail

      - 4 fetchers (journal/docker/kube/file) + ssh-core shell-out transport
      - source_spec parser with brace expansion + 4 scheme validation
      - clock-skew probe at startup, surfaced in envelope
      - 3-stage filter pipeline (noise/level/suppress + user overrides)
      - reorder buffer for cross-source time ordering (256/2s)
      - 6 per-script failure classes / exit codes 40-49
      - lib/ shared with correlate + grep_across_sources (next children)
      - ~30 unit + 2 live_ssh integration tests
      - spec update: error-handling.md gains 40-49 range

      Refs: .trellis/tasks/05-25-log-aggregator-tail-multi/
      Parent: .trellis/tasks/05-25-log-aggregator-v1/ (Phase 2)
      ```
- [ ] **12.3** `task.py finish` 清 current → `task.py archive
      05-25-log-aggregator-tail-multi` 归档。
- [ ] **12.4** 在父 prd.md `05-25-log-aggregator-v1/prd.md` 的 Notes 节追加：
      ```markdown
      - 2026-XX-XX **tail_multi v1.0 shipped** — commit `<short-sha>`.
        N files / X insertions / Y deletions. ~30 unit + 2 live_ssh = ~32
        new PASS. Aggregate suite: <default> default + <live> live = <total>
        PASS. ruff clean, mypy clean. Phase 2 进度: 1/3. Key decisions:
        <if any>. Doc sync: <list>.
      ```
- [ ] **12.5** 在祖父 prd.md `05-24-devops-skills-v1-rollout/prd.md` Notes
      节追加 1 行 mirror，告知整体进度 +1。

## Rollback points

- 任何 phase 中断 → `git stash` + 任务 status 改回 `in_progress`，**不**
  archive。
- 如 Phase 9 live_ssh 测试在 pai 跑挂 → 先按 root cause 修，禁止 mark 任务
  completed 直到 live test 全绿（per 项目惯例「禁止只有 mock 单测、没有 live
  integration」）。
- 如 Phase 10 发现 SKILL.md / references 与设计存在重大不一致（不是微调可
  解决） → 升级到父 prd.md 修订（不在 child task 里改跨脚本契约）。

## Open follow-ups（不阻塞 ship；写入父 prd Notes）

- correlate / grep_across_sources 复用 `lib/` 时若发现接口不便，回到本
  child 评估是否 refactor lib（v1.1 改动需 PR review，不是 v1.0 ship 内）。
- `--follow` 远端被 ssh idle timeout 截断的体验是否需要在文档加 warning
  banner（vs envelope 内的 `data.terminated_by`）。
