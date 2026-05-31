# Implement: docker-quick inspect_container v1.0

> 执行计划:阶段顺序、checklist、验证命令、review gate、rollback。

---

## 阶段顺序

```
A. inspect_container.py 实现       (~250 行代码 + 60 行测试常量)
B. test_inspect_container_unit.py  (~30 tests)
C. test_inspect_container_integration.py  (写文件,不跑)
D. 质量门 + commit + push + archive
```

单 commit 收尾(脚本范围小)。

---

## 阶段 A — 脚本实现

### A.1 文件骨架(顶到底)

- [ ] docstring 顶部含 v1.0 surface、行为、exit codes、References。
- [ ] 常量集中(`LOG_TAIL_DEFAULT`、`RESTART_WARN_THRESHOLD`、`INSPECT_TIMEOUT_S`、`LOGS_TIMEOUT_S`、`HEALTH_LOG_VALUE_MAX_CHARS`、`LOG_KEYWORDS`、severity / failure_class 字符串、exit codes)。
- [ ] 内联 `_envelope` / `_emit` / `_ssh_execute_path` helpers(从 diagnose_host 复制风格)。
- [ ] `_route(host)` / `_run_local(argv)` / `_run_via_ssh_execute(host, cmd_string)` 两路径。
- [ ] `_inspect_one(host, name, runtime, connect_timeout) -> dict`(返回 envelope-like)。
- [ ] `_logs_one(host, name, tail, runtime, connect_timeout) -> dict`(同)。
- [ ] `_extract_inspect(raw_json: dict) -> dict`(精简版数据)。
- [ ] `_score_state(inspect) -> tuple[str, list[finding]]`。
- [ ] `_score_config(inspect) -> tuple[str, list[finding]]`。
- [ ] `_score_logs(log_text) -> tuple[str, list[finding]]`。
- [ ] `_severity_rank(sev) -> int`。
- [ ] `_format_human(data) -> str`(给非 `--json` 路径)。
- [ ] `build_parser` + `main` + `if __name__ == "__main__"`。

### A.2 关键决策点(参 design.md)

- [ ] inspect 命令用 `--format "{{json .}}"`(单对象,不是 array)。
- [ ] local route argv 列表;`shell=False`。
- [ ] remote route 构造命令字符串,`shlex.quote(name)`。
- [ ] inspect JSON parse 失败 → `failure_class="parse_error"`,raw 保留前 1KB stdout。
- [ ] logs 失败但 inspect 成功 → `failure_class="logs_unavailable"`,inspect 数据嵌进 `data.raw.inspect`。
- [ ] `success` 只在 `summary.state == "crit"` 或任何 failure_class 设置时是 false。
- [ ] `exit_code` 0 / 1 / 124(124 透传自 ssh_execute);不引入新 code。

### A.3 反向手测

- [ ] `python <script>.py --help` 显示 v0.2 surface + 新 `--runtime` / `--connect-timeout`。
- [ ] `python <script>.py local nonexistent-ctr --json | jq` 解析成功,`success=false`、`failure_class=remote_error`(本机无 docker 时同样路径,只是 stderr 含 "docker: command not found")。
- [ ] `python <script>.py --runtime podman local foo --json` 不挂(argv 走 podman)。

### A.4 Gate

- [ ] `ruff check plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py` 0 violations。
- [ ] ssh_execute path discovery(`Path(__file__).resolve().parents[4]` 数法同 diagnose_host)正确。

---

## 阶段 B — Unit tests

### B.1 文件框架

- [ ] `tests/test_inspect_container_unit.py`,header 复用 v1.0 第二刀模式(REPO_ROOT / SCRIPTS_DIR / sys.path.insert / import inspect_container)。
- [ ] 常量定义(EXIT_OK / EXIT_FAIL / EXIT_ARGS / EXIT_TIMEOUT / RESTART_THRESHOLD)。
- [ ] `_envelope_json` / `_completed` / `_mock_inspect_payload` helpers。

### B.2 测试组(目标 ~30 个)

- [ ] `_extract_inspect`:happy path、缺 State、缺 Health、Mounts 缺失。
- [ ] `_score_state`:ok / OOMKilled-crit / exit137-crit / exit139-crit / unhealthy-crit / restarting-warn / restart_count>5-warn / exited-warn / exited-with-zero-exit-ok。
- [ ] `_score_config`:user="" / user="root" / user="0" / user="1000" / user="1000:1000"。
- [ ] `_score_logs`:含 panic / fatal / OOM / Killed / stack trace → warn;干净 → ok;空字符串 → ok。
- [ ] `_severity_rank`:crit < warn < info < unknown。
- [ ] local route:`_inspect_one("local", "foo", "docker", 8)` mock 返回 inspect JSON,验证 argv 是 `[runtime, "inspect", "--format", "{{json .}}", "foo"]`。
- [ ] local route runtime podman:argv 第一项是 podman。
- [ ] remote route:`_inspect_one("pai", "foo", "docker", 8)` mock subprocess.run 返回 ssh_execute envelope,验证 argv shape(走 ssh_execute.py)。
- [ ] remote route name 含特殊字符:`shlex.quote` 应用,远端命令字符串里 name 是单 token。
- [ ] logs 调用:argv shape + tail 值正确;runtime podman 时换名。
- [ ] inspect 失败:容器不存在 mock(remote exit 1 stderr "No such object") → envelope failure_class="remote_error"。
- [ ] inspect JSON 不是 JSON:envelope failure_class="parse_error",raw 保留 stdout。
- [ ] logs 失败但 inspect 成功:envelope failure_class="logs_unavailable",data.raw.inspect 完整。
- [ ] ssh_execute 不存在(remote only mock 路径不在):envelope failure_class="precondition"。
- [ ] ssh_execute 超时:envelope exit_code=124、failure_class="timeout"。
- [ ] argparse:缺 host / name → exit 2(argparse 自动)。
- [ ] argparse:`--runtime invalid` → exit 2(choices)。
- [ ] envelope shape:顶层 5 字段 + data 含 host/target/runtime/summary/findings/raw。
- [ ] --json 输出严格 JSON;无 --json 输出人类摘要(含 "[state=...]" 或类似)。
- [ ] summary.state=crit → 顶层 success=false、exit_code=1。
- [ ] summary.state=warn → 顶层 success=true、exit_code=0。

### B.3 Gate

- [ ] `pytest tests/test_inspect_container_unit.py -v` 全绿。
- [ ] `ruff check tests/test_inspect_container_unit.py` 0。

---

## 阶段 C — Integration tests(写文件,不跑)

### C.1 文件框架

- [ ] `tests/test_inspect_container_integration.py`,`pytestmark = pytest.mark.live_ssh`。
- [ ] module 级 skip 条件:`shutil.which("docker") is None` → `pytest.skip(...)`。
- [ ] 不需要 ssh-agent / pai(local route 已经能验证大部分)。
- [ ] 用 `_run_envelope` helper(同 v1.0 第二刀模式)。

### C.2 测试用例

- [ ] `test_inspect_local_nonexistent_returns_failure_envelope`:`local foo-does-not-exist` → success=false + failure_class=remote_error。
- [ ] `test_inspect_local_busybox_running_smoke`:本机起一个 busybox `sleep 60`,inspect 它,断言 summary.state in {ok, warn}。**用 fixture 起容器,teardown 拆**(避免污染本机)。

### C.3 Gate

- [ ] `ruff check tests/test_inspect_container_integration.py` 0。
- [ ] `pytest -m live_ssh tests/test_inspect_container_integration.py --collect-only` 能正常 collect(不要求跑成功)。

---

## 阶段 D — 收尾

### D.1 Quality

- [ ] `pytest -m "not live_ssh"` 全套绿(应该 ~270 个)。
- [ ] `ruff check <3 touched files>` 0。
- [ ] `ruff check`(全仓 baseline)≤ 8 errors(不退步)。
- [ ] `grep -R "import lib" plugins/docker-quick/` 空。
- [ ] PRD AC 16 项全部 [x]。

### D.2 Commit + push

```bash
git add plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py \
        tests/test_inspect_container_unit.py \
        tests/test_inspect_container_integration.py
git commit -m "feat(docker-quick): ship inspect_container v1.0 with shell-out to docker"
git push origin main
```

### D.3 Archive

```bash
python3 ./.trellis/scripts/task.py archive 05-22-02-docker-inspect-container
```

### D.4 报告

向用户回报:
- 1 commit, push 成功
- N unit tests 全绿
- 0 ruff violations
- v1.0 第三刀 docker-quick 模式建立完成
- 自然下一站:其它 2 个 docker-quick stub / k8s-debug / log-aggregator / ssh-cluster

---

## 验证命令(一键)

```bash
# 单元
uv run pytest -m "not live_ssh" tests/test_inspect_container_unit.py -v

# 整套(回归)
uv run pytest -m "not live_ssh" 2>&1 | tail -5

# Lint touched
uv run ruff check \
  plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py \
  tests/test_inspect_container_unit.py \
  tests/test_inspect_container_integration.py

# Lint baseline
uv run ruff check 2>&1 | tail -3

# 反向手测
python3 plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py --help
python3 plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py local nonexistent --json | jq .data.failure_class
```

---

## Rollback

| 出问题阶段 | 操作 |
|---|---|
| A | `git restore plugins/docker-quick/.../inspect_container.py` |
| B / C | `git restore tests/test_inspect_container_*.py` |
| 已 push 后 bug | `feat(...): hotfix ...` 新 commit;不 force-push |

---

## Review gates(自检)

每个阶段进入下一阶段前:

1. ✅ 单元测试相关组全绿(A 后 manual `--help`;B 后 unit 全绿)
2. ✅ ruff 触碰文件 0
3. ✅ envelope 形状手测 1 次(jq)
4. ✅ 决策点遵循 design.md
