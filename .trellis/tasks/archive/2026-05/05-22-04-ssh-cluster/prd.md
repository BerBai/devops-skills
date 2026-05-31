# ssh-core ssh_cluster v1.0

## Goal

把 `ssh-core/ssh_cluster.py` 从 v0.2 stub 实化为可用的并发广播脚本,作为 ssh-core plugin 的**第二刀**(继 `ssh_execute` 之后)。本刀同时验证 **ssh-core 内部多脚本协作模式**:`ssh_cluster` 透过 subprocess shell-out 调用同 plugin 内的 `ssh_execute.py`,**不**用 Python import(尽管同 plugin 允许),保持每脚本独立可测、failure_class 一致。

涉及脚本(本刀 1 个):
- `plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py`

## Background

- ssh-core v0.2 落地 9 个 stub。`ssh_execute` 已在 v1.0 第一刀实化;`ssh_cluster` 是第二刀。
- v0.2 stub 已定义接口:`ssh_cluster.py "<cmd>" --hosts a,b,c --tags x --environment y --parallel --max-workers N --health-check --fail-fast --timeout T --json`。本刀必须保持 argparse 向后兼容(R2)。
- v0.2 stub `unimplemented` 内置在 `scripts/lib/__init__.py`。**本刀第一步是去掉 unimplemented 调用,改成真实实现**。
- v1.0 第二/三/四刀建立的**子进程 shell-out ssh_execute** 模式(`tail_log` / `compare_across_hosts` / `inspect_container` / `compose_status` / `image_audit`)已在 5 个脚本上验证稳定;本刀沿用,且因为是 ssh-core 内,**可以**直接用相对路径 `Path(__file__).parent / "ssh_execute.py"`(不需要 parents[4] 跨 plugin 路径)。
- v1.0 `tail_log` 已实现多 host ThreadPoolExecutor(max 8 workers)模式;本刀复用拓扑思路,新增 `--fail-fast` / `--health-check` 两条语义。
- SKILL.md 提到 `--tags` / `--environment` 是 ssh-core config metadata 概念,但 ssh_config_manager 自身在 v1.0 仍是 stub;本刀**显式不实现** tags/environment 过滤(留 ssh_config_manager 那刀),用 SUPPRESS no-op + stderr 警告做向后兼容。

## Requirements

### R1. JSON 输出契约
- `--json` 时输出严格 `{success, exit_code, stdout, stderr, data}` 信封(参 [error-handling.md](../../spec/backend/error-handling.md))。
- `data` 顶层至少含:`command` / `hosts` / `summary` / `results` / `failure_class`(可为 null)。
- `summary`:`{total, ok, fail, skipped, elapsed_ms}`。
- `results`:`dict[alias, per_host_envelope]`,每 host envelope 至少含 `success` / `exit_code` / `stdout` / `stderr` / `elapsed_ms` / `failure_class`。
- `exit_code` 只允许 `0 / 1 / 2 / 124`。

### R2. v0.2 argparse 向后兼容
- 保留 v0.2 stub 所有 flag:`command` positional;`--hosts` / `--tags` / `--environment` / `--parallel` / `--max-workers` / `--health-check` / `--fail-fast` / `--timeout` / `--json`。
- 允许新加 flag(`--connect-timeout` 等),不允许删 / 改名。
- `--tags` / `--environment` 在 v1.0 是 **SUPPRESS no-op**:若用户传入,argparse 仍接受但脚本立刻 stderr 警告并按"未实现"处理(若同时传了 `--hosts` 仍按 `--hosts` 走;若没传 `--hosts` 则 exit 2)。

### R3. Target resolution
- v1.0 **唯一**支持的目标来源是 `--hosts a,b,c`(逗号分隔,空白容忍)。
- `--hosts` 为空 / 缺失 / 只有空白:argparse exit 2(显式比隐式好;不允许"广播全部 config")。
- `--hosts` 内重复 alias 去重保留首次顺序。
- `--hosts pai,local`:支持 `local` alias —— 透过 `ssh_execute` 自己的 local 路由(沿用,无须 ssh_cluster 额外特例)。
- 不解析 `~/.ssh/config` metadata(留 ssh_config_manager 自己的刀)。

### R4. 并发拓扑
- `--parallel`(默认开)走 ThreadPoolExecutor;`--max-workers N` 控并发数(默认 8,与 sshd 默认 MaxStartups 一致)。
- `--parallel=False`(假设支持反向 `--no-parallel`)或 `--max-workers 1` 等价于串行。
- 每 worker submit 一个 `python3 ssh_execute.py <alias> "<cmd>" --json --timeout T --connect-timeout T` 子进程,解析返回 envelope。
- worker 超时墙:`subprocess.run(... timeout=args.timeout + args.connect_timeout + 5)`,超时 → 该 host envelope `failure_class=timeout`、`exit_code=124`,**不**取消其它正在跑的。

### R5. fail-fast 语义
- `--fail-fast`:首个 host envelope `success=false` 出现时,**停止 submit 新任务**;已 submit 但未完成的继续跑(不杀)。未提交的 host envelope 标 `skipped: true`,`failure_class="skipped_fail_fast"`,出现在 `data.results` 中。
- 未传 `--fail-fast`:全部跑完才聚合。
- top-level `success`:无 fail-fast 时,任一 host 失败 → false;有 fail-fast 时,任一非 skip host 失败 → false。

### R6. health-check 语义
- `--health-check`:正式 command 提交前,先对每 host **并发**跑 `ssh_execute.py <alias> "true" --json --timeout 5 --connect-timeout T`。
- 失败 host:envelope `failure_class="health_check_failed"`、不再跑实际 command,出现在 `data.results` 但 `success=false`、`skipped=true`。
- 通过 host 进入正式 broadcast。
- 不传 `--health-check`:跳过 probe 阶段。

### R7. 与 ssh_execute 的关系
- ssh_cluster **永远**通过 subprocess shell-out 调用 `ssh_execute.py`,**不** Python import(虽然同 plugin 允许)。理由:
  - 避免 argparse / sys.exit 冲突;
  - 让 ssh_execute 的 failure_class / connect-timeout / 噪音过滤等行为天然继承;
  - 让 ssh_cluster 与 5 个跨 plugin 调 ssh_execute 的脚本架构对称。
- ssh_execute 路径:`Path(__file__).resolve().parent / "ssh_execute.py"`(同目录,**不**用 parents[4])。

### R8. failure_class 枚举
- `target_resolution_failed`(top-level):`--hosts` 空或仅传 tags/env 时。
- `ssh_execute_missing`(top-level):ssh_execute.py 文件不存在(异常情况)。
- `partial_failure`(top-level):至少一个 host 失败但不是全失败。
- `all_hosts_failed`(top-level):全部 host 失败。
- per-host failure_class 直接由 ssh_execute envelope 透传(network / auth / remote_error / timeout / ssh_execute_broken),加上 ssh_cluster 自己引入的:
  - `health_check_failed`:health-check probe 失败。
  - `skipped_fail_fast`:fail-fast 触发后未启动。
  - `timeout`:worker 自身超时(超过 ssh_execute 子进程墙)。

### R9. 输出格式
- `--json`:顶层 envelope + `data.results` dict。
- 非 `--json`:human-readable 表格 — 每 host 一行 `[ok|FAIL] alias elapsed exit_code stdout_head` + 尾部 summary。
- per-host stdout / stderr 在 envelope `data.results[alias].stdout/stderr` 全量保留,human 模式只截前 N 字符。

### R10. 测试
- Unit tests(monkeypatch subprocess.run):覆盖 — target resolution(空 hosts / 重复 / 空白容忍)、tags-only 拒绝、ThreadPoolExecutor 并发 mock、health-check 通过 / 失败混合、fail-fast 中止、超时、JSON envelope shape、argv 错误。
- 集成 tests(`@pytest.mark.live_ssh`):
  - 用 `--hosts pai,local "hostname"` 双 host 并发 → 验证 results dict 含两个 alias 且 stdout 各异。
  - 用 `--hosts pai,no-such-alias-xyz "true"` 部分失败 → 验证 top success=false + partial_failure。
  - 用 `--health-check --hosts pai,local "ls /"` 通过 → 验证两 host 都跑了 command。
- 测试目标:本刀新增 ≈ 35-45 unit + 3 integration tests。

### R11. 质量门
- `ruff check <touched>` 0 violations。
- 全仓 `ruff check` 不退步(当前 baseline:8 errors)。
- `pytest -m "not live_ssh"` 默认套件全绿。
- 不修改 SKILL.md(除非示例失效)。
- 不修改 ssh_execute / remote-debug / docker-quick 任何代码。
- 不重写 `lib/__init__.py`(只读消费 json_result / emit / filter_ssh_noise / classify_failure)。

## Acceptance Criteria

**target resolution:**

- [x] AC1. `ssh_cluster.py "uptime" --hosts pai --json`:单 host,正常通过 ssh_execute 跑,`data.results.pai.success=true`。
- [x] AC2. `ssh_cluster.py "uptime" --hosts pai,local --json`:双 host,results 含两 alias。
- [x] AC3. `ssh_cluster.py "uptime" --json`(无 --hosts):argparse exit 2(命令行错误)。
- [x] AC4. `ssh_cluster.py "uptime" --hosts " pai , local " --json`:空白容忍,results 仍 2 个 alias。
- [x] AC5. `ssh_cluster.py "uptime" --hosts pai,pai,local --json`:去重,results 含 2 个 alias。
- [x] AC6. `ssh_cluster.py "uptime" --tags web --json` 但无 `--hosts`:exit 2,stderr 含"--tags not implemented in v1.0"。

**并发 & 拓扑:**

- [x] AC7. `--max-workers 4 --hosts a,b,c,d,e,f,g,h`:8 host 并发 ≤4 worker(用 mock 验证 submit 数 ≤ 4 同时活跃)。
- [x] AC8. `--max-workers 1`:串行,results 顺序与 --hosts 顺序一致(unit test 验)。
- [x] AC9. 各 host envelope 包含 `elapsed_ms`(int)。

**fail-fast & health-check:**

- [x] AC10. `--fail-fast --hosts a,b,c`(mock:a fail):b/c 中尚未 submit 的标 `skipped: true / failure_class=skipped_fail_fast`。
- [x] AC11. `--health-check --hosts a,b`(mock:a probe fail):a 标 `failure_class=health_check_failed`、不跑实际 command;b 通过。
- [x] AC12. 无 `--health-check` 时,probe 阶段被跳过(unit test 验 ssh_execute 只被调用 N 次,而不是 2N)。

**失败聚合:**

- [x] AC13. 全成功:`data.failure_class=null`、顶层 success=true、exit 0。
- [x] AC14. 部分失败:`data.failure_class="partial_failure"`、顶层 success=false、exit 1。
- [x] AC15. 全失败:`data.failure_class="all_hosts_failed"`、顶层 success=false、exit 1。
- [x] AC16. ssh_execute.py 文件不存在:`data.failure_class="ssh_execute_missing"`、exit 1,不抛异常。
- [x] AC17. 单 host worker 超时:该 host envelope `failure_class=timeout`、`exit_code=124`;其它 host 不受影响。

**输出:**

- [x] AC18. `--json` 输出严格符合 R1 schema(每字段都在)。
- [x] AC19. 不 `--json` 时输出 human 表格到 stdout(每 host 一行),summary 一行。

**质量门:**

- [x] AC20. `ruff check plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py tests/test_ssh_cluster_*.py` 0 violations。
- [x] AC21. `pytest -m "not live_ssh"` 全绿(实际 443 tests = 399 + 44)。
- [x] AC22. v0.2 argparse 命令名 / positional 数 / 已有 flag 不变;`--help` 仍展示 v0.2 surface。
- [x] AC23. `grep -R "^from ssh_execute" plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py` 返回 0(确认不 import ssh_execute 而是 shell-out)。

## Non-Goals

- 不解析 `~/.ssh/config` metadata(`--tags` / `--environment` v1.0 不实现)。
- 不集成 `ssh_config_manager` 来枚举 fleet(留 ssh_config_manager 自己的 v1.0 刀)。
- 不实现 streaming output(per-host stdout 全跑完才聚合;不做实时打印)。
- 不实现 retry(失败 host 不重试)。
- 不实现 `--exclude-hosts`(留 v1.1)。
- 不实现 `--script-file`(执行远端脚本文件,留 v1.1)。
- 不重写 `scripts/lib/__init__.py`(只消费现有 helper)。
- 不修改 ssh_execute(若它有 bug,另外起刀)。
- 不修改 SKILL.md。

## Notes

- 设计细节(线程池拓扑、fail-fast 时序、health-check 阶段、failure_class 表)见 `design.md`。
- 执行顺序、checklist、validation cmds、commit 拆分见 `implement.md`。
- 任何打破 R2 的修改必须先回到本 PRD 加 ADR。
