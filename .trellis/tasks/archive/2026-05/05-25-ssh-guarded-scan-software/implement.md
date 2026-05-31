# Implementation — scan_software v1.0

> 前置条件：父任务 `05-24-devops-skills-v1-rollout` Phase 2 启动决议本任务
> 与 `05-25-log-aggregator-tail-multi` 并行 dispatch（user 决议
> 2026-05-26）。本任务**不依赖** ssh-guarded request-pipeline 的 lib
> （PRD R5 + parent prd Notes 已确认 §C1–C4 / §C8–C9 / §C11 与本任务无关）。
> ship 顺序：本任务可以独立先 ship（与 tail-multi 互不阻塞）。

## Phase 0 — Pre-flight

- [ ] **0.1** Re-read `prd.md`（D1-D9 锁定、CLI 6 flag、catalog schema、
      75-79 + 79 `tool_probe_corrupted` 命名空间）和 `design.md`（15 节）。
      确认父 `prd.md` X-section 与 cross_cutting §C5/§C7/§C10 仍有效
      （git log 比对，本 PRD 写成后是否被改）。
- [ ] **0.2** 读 `.trellis/spec/backend/error-handling.md` 当前内容；
      确认 75-79 区间未被占（截 2026-05-26：0/1/2/4-9/10-15/20-29/30-39/124
      已占；40-49 / 50-59 / 60-69 / 70-74 是 Phase 2 sibling 区间）。本任务
      ship 同 commit 内追加 75-79 段（Phase 12.3）。
- [ ] **0.3** Confirm `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py`
      CLI shape：`ssh_execute.py <alias> <command> [--json] [--timeout N]`。
      读 ssh_execute.py docstring + 一个调用方（deploy_pubkey.py 或
      ssh_cluster.py）确认 exact argv shape。`--json` 时它返回
      `{success, exit_code, stdout, stderr, data}` envelope，本任务 parse
      其 `stdout`（含 NDJSON）。如发现 shape drift → 在 design.md §11
      D9 同步修订（不重新协商，仅记录修订）。
- [ ] **0.4** 读已 ship 的参考脚本作 shape 借鉴：
      - `ssh_config_manager.py` — `_HUMAN_HINTS` dict + 6 类 failure_class
        模板（per-script namespace 第 1 个先例）
      - `deploy_pubkey.py` — `_EXIT_CODES` dict + 8 类 failure_class + ssh
        子进程超时双轨（第 2 先例）
      - `ssh_server_transfer.py` — 12 类 failure_class + envelope 包装函数
        + envelope→stdout/stderr 分发（第 3 先例）
- [ ] **0.5** 检 `~/.devops/cache/` 目录在本机是否已存在（其他脚本可能已
      创建）；用 `Path.mkdir(parents=True, exist_ok=True)` 兜底，不假设
      预存在。

## Phase 1 — CLI shell + envelope foundation (~40 LOC)

- [ ] **1.1** 在 `plugins/ssh-guarded/skills/ssh-guarded/scripts/scan_software.py`
      用 v1.0 docstring 替换 v0.2 stub docstring（保留 `v1.0 surface:` block；
      `See:` block 增加 prd/design 路径 + ADR-001 引用 + parent prd 引用）。
- [ ] **1.2** 保留 v0.2 stub 的 `build_parser()` argparse，按 design §2
      追加 `--no-refresh-stale` flag（共 6 flag：`alias` 必填 positional +
      `--name`/`--has` 互斥 + `--refresh` + `--no-refresh-stale` +
      `--catalog` + `--json`）。
- [ ] **1.3** 增加常量：
      ```python
      _EXIT_CODES = {
          None: 0,
          "catalog_load_failed": 75,
          "cache_write_failed": 76,
          "cache_corrupt": 77,
          "ssh_batch_failed": 78,
          "tool_probe_corrupted": 79,
          "_generic": 1, "_timeout": 124,
      }
      _HUMAN_HINTS = {
          "catalog_load_failed": "Check --catalog path or ~/.devops/software_catalog.json schema",
          "cache_write_failed": "~/.devops/cache/<alias>/ permission or disk full?",
          "cache_corrupt": "Delete the cache and retry with --refresh",
          "ssh_batch_failed": "ssh_execute returned non-zero; check alias / network",
          "tool_probe_corrupted": "Remote shell emitted malformed NDJSON; retry with --refresh",
      }
      _DEVOPS_HOME = Path(os.path.expanduser("~/.devops"))
      _CACHE_DIR = _DEVOPS_HOME / "cache"
      _USER_CATALOG_DEFAULT = _DEVOPS_HOME / "software_catalog.json"
      _SSH_EXECUTE = Path(__file__).resolve().parents[3] / "ssh-core/skills/ssh-core/scripts/ssh_execute.py"
      _TTL_SECONDS = 7 * 24 * 3600  # design §8 D6
      ```
- [ ] **1.4** 实现 `_emit_envelope(args, *, success, exit_code, data, stderr_msg="",
      failure_class=None)`：构造 envelope dict，按 `args.json` 决定
      stdout NDJSON 还是 human stderr。借鉴 ssh_server_transfer 的写法。
- [ ] **1.5** 实现 `_emit_failure(args, failure_class, data=None, extra_stderr="")`：
      `_emit_envelope` 的薄壳，固定 `success=False` + `exit_code=_EXIT_CODES[failure_class]`
      + `data["failure_class"] = failure_class`，stderr 含 `_HUMAN_HINTS[failure_class]`。
- [ ] **1.6** `main(argv)` 占位骨架：
      ```python
      args = build_parser().parse_args(argv)
      try:
          catalog = _load_catalog(args.catalog)
      except CatalogLoadError as e:
          return _emit_failure(args, "catalog_load_failed",
                               data={"path": str(e.path)}, extra_stderr=str(e))
      if args.has:
          return _dispatch_has(args, catalog)
      if args.name:
          return _dispatch_name(args, catalog)
      return _dispatch_full(args, catalog)
      ```

## Phase 2 — Default catalog (~100 LOC literal)

- [ ] **2.1** 实现 `_DEFAULT_CATALOG: list[dict]`，按 design §3 表格 33
      个 entry 一行一 dict 字面量。category 分组顺序：lang(11) → build(5)
      → gpu(2) → containers(5) → iac(6) → shell(5) → xilinx(3)。
- [ ] **2.2** Xilinx 3 个 entry 用 `scan_method="xilinx_dir"` +
      `xilinx_subdir`（值: `"Vivado"` / `"Vitis"` / `"Vivado_HLS"`）取代
      `probe_cmd / version_cmd / version_regex` 字段。
- [ ] **2.3** Catalog 加 `name → entry` dict 索引 `_DEFAULT_BY_NAME`，避免
      下游循环查询。
- [ ] **2.4** Unit test 卡 catalog literal：
      `test_default_catalog_has_33_entries` + `test_default_catalog_categories`
      （按 design §3 表头数字断言）+ per-category 子断言。

## Phase 3 — Catalog loading + user override (~50 LOC)

- [ ] **3.1** 实现 `CatalogLoadError(Exception)`：`__init__(self, message, path)`。
- [ ] **3.2** 实现 `_load_catalog(arg_path: str | None) -> dict[str, dict]`：
      按 design §4 D2 加载顺序：
      1. base = copy of `_DEFAULT_BY_NAME`
      2. 决定 user catalog path：`arg_path` 显式 → 必须存在（不存在抛
         CatalogLoadError）；`arg_path` None → `_USER_CATALOG_DEFAULT`
         存在则用之，缺失则静默 skip
      3. 加载 user catalog → JSON parse 失败抛 CatalogLoadError
      4. schema 校验：必须 `{"version": 1, "patterns": [...]}` 或 `[...]`
         直接是 entry 列表（兼容两种 shape；design §4 默认前者，但 stub
         docstring 没明示，本任务两种都支持，design.md §4 同步修订）
      5. 同名整体替换（非 merge）；缺字段 → CatalogLoadError
- [ ] **3.3** Unit test：3.2 的 5 个分支各自正/负 case；schema 校验
      （version 缺失 / patterns 非 list / entry 缺 name）。

## Phase 4 — Remote shell builder + base64 transport (~80 LOC)

- [ ] **4.1** 实现 `_build_remote_script(catalog: dict[str, dict]) -> str`：
      按 design §5 D3 emit POSIX sh heredoc，30 个 `command -v` block +
      Xilinx 的 `scan_xilinx` function block。
- [ ] **4.2** Heredoc 内的 `emit_present` / `emit_absent` 函数遵循 design
      §5 第 119-122 行格式；version_raw 须经 `head -c 4000 | tr '\n' ' ' |
      sed 's/"/\\\\"/g'` 三步处理避免 NDJSON 损坏。
- [ ] **4.3** 实现 `_run_remote_scan(alias: str, script: str, timeout: int)
      -> tuple[str, int, str]`：
      ```python
      payload_b64 = base64.b64encode(script.encode()).decode()
      remote_cmd  = f"echo {payload_b64} | base64 -d | bash"
      proc = subprocess.run(
          [sys.executable, str(_SSH_EXECUTE), alias, remote_cmd,
           "--json", "--timeout", str(timeout)],
          capture_output=True, text=True, timeout=timeout + 10,
      )
      env = json.loads(proc.stdout)
      return env["stdout"], env["exit_code"], env.get("stderr", "")
      ```
- [ ] **4.4** Subprocess 超时 `timeout + 10`（design §11 倒数第 3 行）；
      `subprocess.TimeoutExpired` → 透传 `_timeout` (124)。
- [ ] **4.5** ssh_execute envelope `exit_code != 0` → `ssh_batch_failed` (78)。
- [ ] **4.6** Unit test：mock subprocess，断言 argv 形态 + base64 解码后
      shell 内容包含 `kubectl` block + Xilinx block；超时路径 → 124；
      envelope.exit_code=255 → 78。

## Phase 5 — NDJSON parse + regex extract (~40 LOC)

- [ ] **5.1** 实现 `_parse_ndjson(ndjson_text: str, catalog: dict[str, dict])
      -> tuple[dict[str, dict], list[str]]`：
      返回 `(tools_dict, corrupted_lines)`；每行 `json.loads` 失败 →
      append 到 corrupted_lines。
- [ ] **5.2** 实现 `_extract_version(name: str, version_raw: str, catalog:
      dict[str, dict]) -> str`：按 catalog[name] 的 `version_regex` 匹配；
      Xilinx 类（`scan_method=xilinx_dir`）直接取 path 末段：
      `version_raw.rstrip('/').split('/')[-1]`。
- [ ] **5.3** 每条 tool record 注入 `scan_method` + `category` 字段（从
      catalog 复制）。
- [ ] **5.4** corrupted_lines 非空 → 即使部分入 cache，envelope.success=
      False + failure_class=tool_probe_corrupted（79）；其余 tool 仍正常
      入 cache（design §10 D8 末段）。
- [ ] **5.5** Unit test：正常 NDJSON 30 行解析 + Xilinx 3 行（path 末段
      提取）+ 1 损坏行（仍入剩余 + 报 79）。

## Phase 6 — Cache I/O (~50 LOC)

- [ ] **6.1** 实现 `_cache_path(alias: str) -> Path`：
      `_CACHE_DIR / alias / "software.json"`。
- [ ] **6.2** 实现 `_cache_read(alias) -> dict | None`：
      文件不存在 → None；JSON parse 失败或 schema 不符 → CacheCorruptError
      （exit 77，由 caller 处理）；schema 校验 version==1 + tools 是 dict。
- [ ] **6.3** 实现 `_cache_write(alias, payload)`：原子写
      ```python
      target = _cache_path(alias)
      target.parent.mkdir(parents=True, exist_ok=True)
      tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
      with open(tmp, "w") as f:
          json.dump(payload, f, indent=2)
          f.flush()
          os.fsync(f.fileno())
      os.replace(tmp, target)
      ```
      OSError → `cache_write_failed` (76)。
- [ ] **6.4** `is_stale(payload) -> bool`：
      `(now - parse_iso8601(payload["scanned_at"])) > _TTL_SECONDS`。
- [ ] **6.5** Unit test：cache miss 返 None / cache corrupt 抛 77 /
      atomic write tmp→replace（断言 tmp 名字含 pid，最终 target 内容正确）
      / fresh 返 False / stale 返 True / `_cache_write` 抛 OSError → 76。

## Phase 7 — Three invocation modes (~80 LOC)

- [ ] **7.1** 实现 `_dispatch_full(args, catalog) -> int`：
      按 design §8 D6 决策矩阵；
      读 cache → 决定是否 rescan → scan → 写 cache → emit envelope。
      stale-fallback 路径 exit 0 + `data.cache_stale=true` + stderr warn。
- [ ] **7.2** 实现 `_dispatch_name(args, catalog) -> int`：
      `args.name` 不在 catalog → exit 1 + stderr `unknown tool '<name>'`
      （区别于 `--has` 的 exit 2，因为 `--name` 是查询而非判定）。
      cache miss / stale 触发 full scan（复用 7.1 内部），完后 lookup
      返回单条。
- [ ] **7.3** 实现 `_dispatch_has(args, catalog) -> int`：
      `args.has` 不在 catalog → exit 2 + stderr `unknown tool '<name>'`
      （区别于 absent 的 exit 1）；cache miss / stale 触发 full scan；
      present → exit 0；absent → exit 1。
      默认非 --json 时静默；--json 时 stdout 一行 envelope，`data.tools.<X>.present`。
- [ ] **7.4** Summary table 渲染（非 json 默认输出，full 模式）：
      按 category 分组，每组 `<category>: N present / M absent`，行内
      列出 present 的 `<name>:<version>` 紧凑形式（design §12 D10 输出节
      "non-json summary table"）。**纯 stdlib，不引 rich**。

## Phase 8 — Wire main() + smoke (~20 LOC)

- [ ] **8.1** 把 7.1 / 7.2 / 7.3 接入 `main()` 的 dispatch（Phase 1.6
      已留 placeholder）。
- [ ] **8.2** 添加 `if __name__ == "__main__": sys.exit(main())`。
- [ ] **8.3** Smoke：`python scan_software.py --help` exit 0，列出 6 flag。
- [ ] **8.4** Smoke：`python scan_software.py local --json`（用 mock
      catalog 走 `/dev/null` user catalog；mock subprocess.run 返回 empty
      NDJSON）→ 不崩，envelope shape 合法。

## Phase 9 — Unit tests (~25 tests, `tests/test_scan_software_unit.py`)

- [ ] **9.1** Phase 2/3/4/5/6/7 已散布的 unit test 集中到一个文件。覆盖
      design §12 D10 的 unit 清单（默认 catalog × 5、catalog override × 3、
      cache I/O × 4、TTL × 3、refresh × 1、--has × 3、--name × 2、远端 × 3、
      Xilinx × 1、输出 × 1 = 26）。
- [ ] **9.2** 全程 mock `subprocess.run`（patch `scan_software.subprocess.run`
      或顶层模块）。注意 ssh_execute 实际是子进程，但我们 mock 顶层
      subprocess.run，不需要 spawn 真 ssh。
- [ ] **9.3** Fixture：`tmp_path` 配 `monkeypatch.setattr(scan_software,
      "_DEVOPS_HOME", tmp_path)` 隔离真实 `~/.devops`，避免误写本机。
      （借鉴 ssh_config_manager unit 测试的隔离手法）
- [ ] **9.4** Parametrized regex test（design §3 末尾要求）：每个非
      Xilinx entry × 一组示例 version_raw → 期望 captured version。
- [ ] **9.5** 跑 `pytest tests/test_scan_software_unit.py -v` 全绿。

## Phase 10 — Integration tests (~2 tests, `tests/test_scan_software_integration.py`)

- [ ] **10.1** Marker `live_ssh`，真连 pai（CLAUDE.md 第 1 条已确认本机
      网段时可用）。
- [ ] **10.2** `test_full_scan_on_pai`：
      1. 清掉 `~/.devops/cache/pai/software.json` （tmp_path 不行，因为
         脚本读固定路径；用 `monkeypatch` 改 `_CACHE_DIR` 到 tmp_path 也
         行，但 integration 倾向真路径，需 cleanup `addfinalizer`）。
      2. `python scan_software.py pai --json` exit 0。
      3. envelope.success=True；data.tools 含 33 个 name；至少 `bash`/`git`
         present=True 且 version 非空。
      4. 文件 `~/.devops/cache/pai/software.json` 真实写入并可 reload。
- [ ] **10.3** `test_has_subcommand_on_pai`：
      1. `python scan_software.py pai --has bash` exit 0（present）。
      2. `python scan_software.py pai --has totally-fake-tool-${uuid4}.hex[:8]`
         → exit 2（不在 catalog）。
      3. 注入用户 catalog（写 tmp catalog 含 `totally-fake-tool` entry +
         probe_cmd `command -v totally-fake-tool`）→
         `python scan_software.py pai --catalog <tmp> --has totally-fake-tool`
         → exit 1（在 catalog 但远端 absent）。
- [ ] **10.4** 跑 `pytest tests/test_scan_software_integration.py -m live_ssh -v`
      全绿。
- [ ] **10.5** **本机网段检查**：CLAUDE.md 第 2 条明示本机 IP 不在
      192.168.2.0/24 网段时跳过验证，待后续验证才能 commit。如果本机
      ip 不在网段：integration 测试**跳过**（pytest skipif 或直接 console
      跳过），但仍允许进入 Phase 12 commit gate（commit 包含 unit + integ
      代码，integ 留待用户网段恢复后验证）。

## Phase 11 — Aggregate suite regression check

- [ ] **11.1** `pytest tests/` 全套 default 不破已 ship 测试（670+ PASS
      baseline，scan_software 加 ~26 → ~696+）。
- [ ] **11.2** `pytest tests/ -m live_ssh` 若本机网段允许 → 全绿
      （49 baseline + 2 新 = 51+）。
- [ ] **11.3** `ruff check plugins/ssh-guarded/ tests/test_scan_software_*.py`
      clean。
- [ ] **11.4** `mypy plugins/ssh-guarded/skills/ssh-guarded/scripts/scan_software.py`
      不引入新 warning。

## Phase 12 — Docs + commit gate

- [ ] **12.1** **SKILL.md sync** — `plugins/ssh-guarded/skills/ssh-guarded/SKILL.md`
      已 reference scan_software（约 line 150-162）；本任务 surface 与
      docstring + SKILL.md 一致，**只在发现偏差时**做最小同步（不大改）。
- [ ] **12.2** **references/ sync** — design §13 表示「不动 references/」。
      除非 implement 阶段发现 references 有错描，否则跳过。
- [ ] **12.3** **error-handling.md sync**（commit 内追加）：在
      `.trellis/spec/backend/error-handling.md` 末尾添加 75-79 段：
      ```markdown
      | `75`-`79` | `scan_software` per-script failure classes. `75` `catalog_load_failed`,
      `76` `cache_write_failed`, `77` `cache_corrupt`, `78` `ssh_batch_failed`,
      `79` `tool_probe_corrupted`. Introduced by `scan_software` v1.0
      (see `.trellis/tasks/05-25-ssh-guarded-scan-software/prd.md` D8 +
      design.md §10). ADR follow-up TBD. |
      ```
      **注意**：`.trellis/spec/backend/` 是个人 workspace 不入 commit
      （parent prd Notes line 84）。本 step 仅 *本机* 落地，不放 commit。
- [ ] **12.4** **tests/README.md sync** — 末尾「What's covered today」节
      追加：`scan_software` ~26 unit + 2 live_ssh，沿用 deploy_pubkey /
      ssh_server_transfer 惯例。
- [ ] **12.5** **uv.lock 隔离** — 跑 `uv run` 前先 `git status uv.lock`；
      若 uv 触发了无关升级 → commit 前 `git restore uv.lock` 隔离
      （parent prd Notes line 87）。
- [ ] **12.6** **Commit** — 沿用 ship commit 惯例，只 commit：
      - `plugins/ssh-guarded/skills/ssh-guarded/scripts/scan_software.py`
      - `tests/test_scan_software_unit.py`
      - `tests/test_scan_software_integration.py`
      - `tests/README.md`
      - 可选：`plugins/ssh-guarded/skills/ssh-guarded/SKILL.md`（仅 12.1
        发现偏差时）
      ```
      git add <above files>
      git commit -m "feat(ssh-guarded): ship scan_software v1.0 batch detection + cache"
      ```
- [ ] **12.7** **Trellis spec update** — 跑 `trellis-update-spec` 把本
      ship 的关键决策（catalog override "replace" 而非 merge / base64
      transport 决策 / TTL 7d + stale fallback exit 0 / per-script
      namespace 75-79）追加到 `.trellis/spec/backend/`（本机 workspace，
      不入 commit）。
- [ ] **12.8** **task.py finish + archive** — 本任务推到 `completed`，
      parent rollout `prd.md` Notes 节追加一行 ship 记录（commit hash +
      LOC + 新增测试数 + aggregate PASS baseline 变化）。

## Rollback points

- 任一 Phase 失败可独立 rollback：
  - Phase 1-2 失败：仅 scaffold 与 catalog literal，无 side effect，直接
    revert worktree
  - Phase 4 失败（ssh_execute 调用 shape drift）：回 0.3，与
    ssh_execute v1.0 owner 协商
  - Phase 6 失败（cache atomic write 在用户系统不工作）：fall back 到非
    原子写 + warning（小概率事件，仅在 NFS / FUSE 上）
  - Phase 10 失败（本机网段不在 192.168.2.0/24）：跳过 integration，
    Phase 12 commit 仍可走，integration 待网段恢复后再验

## Review gates

- **Gate A**（Phase 0-3 完后）：catalog literal 与 default 加载逻辑通过
  unit test → main session 确认与 design §3 / §4 1:1 对齐
- **Gate B**（Phase 4-7 完后）：远端 shell builder + 三模式 dispatch 全
  绿 → main session 抽检 NDJSON 解析 + Xilinx 路径取版本
- **Gate C**（Phase 11 完）：aggregate suite 全绿 + ruff clean + mypy 净
  → 进入 Phase 12 commit gate
- **Final gate**（Phase 12.6 前）：main session 人工 review diff，确认
  `uv.lock` 干净 + 只 commit scripts + tests + tests/README.md
