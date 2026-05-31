# Design: ssh-core ssh_cluster v1.0

> 技术决策:target resolution、ThreadPoolExecutor 拓扑、ssh_execute shell-out、fail-fast 时序、health-check 阶段、failure_class 总表。

---

## 1. 总览数据流

```
 ssh_cluster "<cmd>" --hosts a,b,c --health-check --fail-fast
       │
       ▼
 ┌───────────────────────────────────────────────┐
 │ 1. parse_targets(args.hosts) -> [a, b, c]    │   R3
 │    - 拒空 / 拒 tags-only / 去重保序           │
 └───────────────────────────────────────────────┘
       │
       ▼
 ┌───────────────────────────────────────────────┐ optional
 │ 2. health-check phase (if --health-check)    │  R6
 │    并发跑 ssh_execute <h> "true"             │
 │    fail者标 health_check_failed,不进入下一阶段│
 └───────────────────────────────────────────────┘
       │
       ▼
 ┌───────────────────────────────────────────────┐
 │ 3. broadcast phase                           │  R4/R5
 │    ThreadPoolExecutor(max_workers)            │
 │    submit alive_hosts → ssh_execute <h> <cmd> │
 │    fail-fast: 首失败后 skip 未启动            │
 └───────────────────────────────────────────────┘
       │
       ▼
 ┌───────────────────────────────────────────────┐
 │ 4. aggregate                                 │  R8
 │    summary{total, ok, fail, skipped, elapsed}│
 │    top failure_class = partial / all / null  │
 └───────────────────────────────────────────────┘
```

每箭头都可能落 envelope;前一阶段失败导致后一阶段被 short-circuit。

---

## 2. Target resolution(R3)

```
parse_targets(hosts_arg, tags_arg, env_arg) -> tuple[list[str] | None, str | None]
    if tags_arg or env_arg:
        # v1.0 unimplemented; user warning, no resolution
        ...
    if not hosts_arg or not hosts_arg.strip():
        return None, "target_resolution_failed"
    aliases = []
    seen = set()
    for raw in hosts_arg.split(","):
        cleaned = raw.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        aliases.append(cleaned)
    if not aliases:
        return None, "target_resolution_failed"
    return aliases, None
```

argparse 不接受 `--tags` / `--environment` 同时无 `--hosts` → main 检测后 exit 2 + stderr 警告 (R3/AC6)。

---

## 3. ssh_execute shell-out(R7)

固定形态:

```
def _run_one(
    ssh_exec: Path,
    alias: str,
    command: str,
    timeout: int,
    connect_timeout: int,
) -> dict:
    argv = [sys.executable, str(ssh_exec), alias, command,
            "--json", "--timeout", str(timeout),
            "--connect-timeout", str(connect_timeout)]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout + connect_timeout + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _per_host_env(False, EXIT_TIMEOUT, alias,
                             elapsed_ms=int((time.monotonic()-started)*1000),
                             failure_class="timeout",
                             stderr=f"ssh_cluster: worker timed out after {timeout+connect_timeout+5}s")
    elapsed_ms = int((time.monotonic()-started)*1000)
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _per_host_env(False, proc.returncode or EXIT_FAIL, alias,
                             elapsed_ms=elapsed_ms,
                             failure_class="ssh_execute_broken",
                             stdout=proc.stdout, stderr=proc.stderr)
    # ssh_execute envelope passes failure_class via env.data; promote it
    fc = (env.get("data") or {}).get("failure_class")
    return _per_host_env(env.get("success", False), env.get("exit_code", 1),
                         alias, elapsed_ms=elapsed_ms,
                         stdout=env.get("stdout",""), stderr=env.get("stderr",""),
                         failure_class=fc)
```

**关键**:
- ssh_execute 路径用 `Path(__file__).resolve().parent / "ssh_execute.py"`(同目录,**不**用 parents[4])。
- per-host envelope schema 固定 6 字段:`alias / success / exit_code / stdout / stderr / elapsed_ms / failure_class`。

**为什么不 Python import**:见 R7 — 让 fail-cycle 与 envelope 行为天然继承,避免内部 sys.exit。

---

## 4. ThreadPoolExecutor 拓扑(R4)

```
def _broadcast(
    ssh_exec, aliases, command, timeout, connect_timeout, max_workers, fail_fast,
) -> dict[str, dict]:
    results = {}
    if max_workers <= 1:
        # 串行路径(便于稳定测试 + AC8)
        for alias in aliases:
            env = _run_one(ssh_exec, alias, command, timeout, connect_timeout)
            results[alias] = env
            if fail_fast and not env["success"]:
                # 标后续为 skipped
                for skip in aliases[aliases.index(alias)+1:]:
                    results[skip] = _per_host_env(
                        False, EXIT_FAIL, skip, elapsed_ms=0,
                        failure_class="skipped_fail_fast",
                    )
                break
        return results

    # 并发路径
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for alias in aliases:
            futures[alias] = pool.submit(
                _run_one, ssh_exec, alias, command, timeout, connect_timeout
            )

        if not fail_fast:
            for alias in aliases:
                results[alias] = futures[alias].result()
            return results

        # fail-fast 路径:as_completed,首失败时 cancel 未启动 futures
        completed = set()
        first_fail_seen = False
        for fut in as_completed(futures.values()):
            alias = next(a for a, f in futures.items() if f is fut)
            env = fut.result()
            results[alias] = env
            completed.add(alias)
            if not env["success"]:
                first_fail_seen = True
                break

        if first_fail_seen:
            # cancel 尚未启动的,标 skipped
            for a, f in futures.items():
                if a in completed:
                    continue
                if f.cancel():
                    results[a] = _per_host_env(
                        False, EXIT_FAIL, a, elapsed_ms=0,
                        failure_class="skipped_fail_fast",
                    )
                else:
                    # 已经在跑了,等结果
                    results[a] = f.result()
        else:
            # broke out without fail -> shouldn't happen, defensive
            for a, f in futures.items():
                if a not in completed:
                    results[a] = f.result()
    return results
```

**关键决策**:
- `max_workers=1` 串行路径是显式分支(测试更稳;`AC8` 需保序)。
- 并发路径用 `as_completed` 检测首个失败;`f.cancel()` 对**未启动**的 future 有效,**已启动**的 future 不能 cancel,等它跑完。
- 因为 cancel 行为是 "best-effort",AC10 验证标的是"尚未 submit"的子集 — 在串行路径很好验,并发路径用 mock 控制启动时序。

---

## 5. health-check 阶段(R6)

```
def _health_check(
    ssh_exec, aliases, connect_timeout, max_workers,
) -> tuple[list[str], dict[str, dict]]:
    """Probe each host with `true`. Returns (alive_aliases, dead_envelopes)."""
    HEALTH_TIMEOUT = 5
    alive: list[str] = []
    dead: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            a: pool.submit(_run_one, ssh_exec, a, "true",
                           HEALTH_TIMEOUT, connect_timeout)
            for a in aliases
        }
        for alias, fut in futures.items():
            env = fut.result()
            if env["success"]:
                alive.append(alias)
            else:
                # override failure_class to be specific to this phase
                env["failure_class"] = "health_check_failed"
                env["skipped"] = True
                dead[alias] = env
    return alive, dead
```

**关键**:health-check 的 timeout 短于业务 command (5s);用同样的 ssh_execute shell-out,语义一致;**保序** alive_aliases(为了后续 fail-fast 串行测试)。

---

## 6. failure_class 总表(R8)

| failure_class | 触发 | top / per-host |
|---|---|---|
| `target_resolution_failed` | --hosts 空 / 仅 tags/env | top |
| `ssh_execute_missing` | ssh_execute.py 文件不存在 | top |
| `partial_failure` | 至少一个失败但不全失败 | top |
| `all_hosts_failed` | 全部 host 失败 | top |
| `health_check_failed` | probe true 失败 | per-host(set skipped=true) |
| `skipped_fail_fast` | fail-fast 触发,尚未 submit | per-host(set skipped=true) |
| `timeout` | worker 子进程超时 | per-host |
| `ssh_execute_broken` | ssh_execute stdout 非 JSON | per-host |
| network / auth / remote_error | ssh_execute 透传 | per-host(passthrough) |

聚合规则:

```
def _classify_top(per_host_results: dict[str, dict]) -> tuple[bool, str | None]:
    total = len(per_host_results)
    ok = sum(1 for e in per_host_results.values() if e["success"])
    fail = total - ok
    if fail == 0:
        return True, None
    if ok == 0:
        return False, "all_hosts_failed"
    return False, "partial_failure"
```

---

## 7. envelope schema(R1)

```jsonc
{
  "success": true,
  "exit_code": 0,
  "stdout": "<human-friendly summary>",
  "stderr": "",
  "data": {
    "command": "uptime",
    "hosts": ["pai", "local"],            // resolved aliases, in order
    "summary": {
      "total": 2,
      "ok": 2,
      "fail": 0,
      "skipped": 0,
      "elapsed_ms": 1245
    },
    "results": {
      "pai":   {"alias":"pai",   "success":true, "exit_code":0,
                "stdout":"...", "stderr":"", "elapsed_ms":612,
                "failure_class": null, "skipped": false},
      "local": {"alias":"local", "success":true, "exit_code":0,
                "stdout":"...", "stderr":"", "elapsed_ms":18,
                "failure_class": null, "skipped": false}
    },
    "failure_class": null,                // top-level
    "fail_fast": false,
    "health_check": false
  }
}
```

per-host envelope 字段固定 8 个,top 字段固定 7 个(含可空 failure_class)。

---

## 8. argparse 终态

| flag / positional | type | default | 说明 |
|---|---|---|---|
| `command` | str (req) | — | 远端命令字符串(透传给 ssh_execute) |
| `--hosts` | str | None | 逗号分隔 alias 列表(v1.0 唯一目标来源) |
| `--tags` | str | None | **SUPPRESS no-op**;传 → stderr 警告 |
| `--environment` | str | None | **SUPPRESS no-op**;传 → stderr 警告 |
| `--parallel` | flag | True(默认开)| 沿用 v0.2;`--no-parallel` 切串行(等价 --max-workers 1) |
| `--max-workers` | int | 8 | 线程池规模 |
| `--health-check` | flag | False | 先 probe `true` |
| `--fail-fast` | flag | False | 首失败后停 submit |
| `--timeout` | int | 120 | 业务 command 超时 |
| `--connect-timeout` | int | 8 | SSH ConnectTimeout(透给 ssh_execute) |
| `--json` | flag | False | 输出 JSON envelope |

**注意**:v0.2 stub 用 `--parallel` 作 store_true(默认 False)。本刀**改为默认 True**(因为常用),并加 `--no-parallel` 反向 flag。这是 v0.2 没明确语义的扩展;**不算 R2 违反**(action 行为变,默认更安全)。

—— 改主意:为绝对保险,**保留 v0.2 store_true**(默认 False),但**额外**支持 `--max-workers > 1` 自动并发(更直觉)。串行只发生在 `--max-workers 1`,不依赖 `--parallel` flag。`--parallel` flag 在 v1.0 保留但是 no-op(因为 max-workers 已经表达并发度)。

最终决策:**`--parallel` 在 v1.0 是 no-op,实际并发度由 `--max-workers` 决定**;若 user 显式传 `--parallel` 不警告(它本来意图就是 "enable concurrent")。

---

## 9. 函数清单

```
# helpers (consume from lib/__init__.py)
from lib import json_result, emit, filter_ssh_noise, classify_failure

# discovery
def _ssh_execute_path() -> Path     # same-dir, NOT parents[4]

# target resolution
def _parse_targets(hosts_arg, tags_arg, env_arg) -> tuple[list[str] | None, str | None]
def _validate_targets(aliases) -> bool

# per-host envelope construction
def _per_host_env(success, exit_code, alias, *, elapsed_ms, stdout="", stderr="",
                  failure_class=None, skipped=False) -> dict

# worker
def _run_one(ssh_exec, alias, command, timeout, connect_timeout) -> dict

# orchestration
def _health_check(ssh_exec, aliases, connect_timeout, max_workers) -> tuple[list[str], dict]
def _broadcast(ssh_exec, aliases, command, timeout, connect_timeout, max_workers, fail_fast) -> dict[str, dict]

# aggregation
def _classify_top(results) -> tuple[bool, str | None]
def _summarize(results, started_at) -> dict

# formatting
def _format_human(data) -> str

# argparse + main
def build_parser() -> ArgumentParser
def main(argv=None) -> int
```

---

## 10. main() 失败传播伪码

```
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    started_at = time.monotonic()

    # 1. tags/env warning
    if args.tags or args.environment:
        sys.stderr.write("ssh_cluster: --tags / --environment not implemented in v1.0; use --hosts\n")
        if not args.hosts:
            return _emit_top_fail(args, "target_resolution_failed",
                                  "ssh_cluster: --hosts required (tags/env unsupported in v1.0)")

    # 2. target resolution
    aliases, err = _parse_targets(args.hosts, args.tags, args.environment)
    if aliases is None:
        return _emit_top_fail(args, err, "ssh_cluster: --hosts cannot be empty")

    # 3. ssh_execute discovery
    ssh_exec = _ssh_execute_path()
    if not ssh_exec.exists():
        return _emit_top_fail(args, "ssh_execute_missing",
                              f"ssh_cluster: ssh_execute.py not found at {ssh_exec}")

    # 4. health-check (optional)
    dead: dict[str, dict] = {}
    if args.health_check:
        aliases, dead = _health_check(ssh_exec, aliases, args.connect_timeout, args.max_workers)

    # 5. broadcast
    results = _broadcast(ssh_exec, aliases, args.command, args.timeout,
                         args.connect_timeout, args.max_workers, args.fail_fast)

    # 6. merge dead probes back into results
    for alias, env in dead.items():
        results[alias] = env

    # 7. aggregate
    success, fc = _classify_top(results)
    summary = _summarize(results, started_at)
    data = {
        "command": args.command,
        "hosts": list(results.keys()),
        "summary": summary,
        "results": results,
        "failure_class": fc,
        "fail_fast": args.fail_fast,
        "health_check": args.health_check,
    }
    code = EXIT_OK if success else EXIT_FAIL
    out = _format_human(data) if not args.json else ""
    emit(json_result(success, code, stdout=out, data=data), args.json)
    return code
```

---

## 11. 测试矩阵

### Unit(估 35-45 tests)

- `_parse_targets`:happy 单 / 多 / 重复去重 / 空白容忍 / 空 / None / 仅 tags+env(8)
- `_per_host_env` 形状(2)
- `_run_one` mocks:happy / timeout / json broken / ssh_execute stderr propagation(4)
- `_broadcast` 并发 mock:max_workers=1 串行保序 / max_workers=4 N>4 hosts / fail-fast skip 未启动 / 全失败 / 全成功 / partial(7)
- `_health_check` mocks:全通过 / 部分失败 / 全失败(3)
- `_classify_top`:全 ok / partial / all_fail / 空(4)
- `_summarize` 计数正确(2)
- `main()` E2E:happy / no --hosts → exit 2 / tags-only → exit 2 / --max-workers 1 串行 / fail-fast + 部分失败 / health-check 混合 / ssh_execute missing(8-10)
- argparse defaults(2)

### 集成(估 3 tests)

- `--hosts pai,local "hostname"`:两 host 并发 → results 含 pai/local,stdout 各异
- `--hosts pai,no-such-alias "true"`:partial_failure
- `--health-check --hosts pai,local "ls /"`:两 host 都跑

---

## 12. 不引入的复杂度

- 不解析 ~/.ssh/config metadata(v1.0 受限,留 ssh_config_manager 那刀)。
- 不做 retry。
- 不做 streaming stdout(每 host 全跑完才聚合)。
- 不做 cancel-running-future(只 cancel-未启动)。
- 不解析 ssh_execute stderr 的 known_hosts noise(由 ssh_execute 自己 filter,我们透传)。

## 13. 与已实施脚本的差异

| 维度 | tail_log 多 host | ssh_cluster | inspect_container |
|---|---|---|---|
| 多 host | 用 ThreadPoolExecutor + as_completed | 用 ThreadPoolExecutor + 串行/并发分叉 | 单 host |
| fail-fast | 无 | 有 | 不适用 |
| health-check | 无 | 有 | 不适用 |
| ssh_execute 调用方式 | subprocess | subprocess | subprocess |
| ssh_execute 路径策略 | parents[4] 跨 plugin | parents[0] 同目录 | parents[4] |
| inline helpers | 是(跨 plugin) | 否(用同 plugin lib) | 是(跨 plugin) |
| failure_class 引入 | 4 | 7(继承 + 3 新) | 5 |
