# Design: docker-quick inspect_container v1.0

> 技术决策:host 路由分叉、docker 命令选择、JSON parse、severity 评分、findings 生成、失败传播。

---

## 1. host 路由分叉

```
                       ┌─ local ──── _run_local(runtime, argv_list)
                       │              subprocess.run(["docker", "inspect", ...])
                       │              shell=False
host  ─── _route ─────┤
                       │
                       └─ alias ──── _run_via_ssh_execute(host, cmd_string)
                                      python ssh_execute.py <host> "<docker cmd>" --json
                                      parse the envelope
```

**为什么 local 不走 ssh_execute**:
- 握手成本 0。
- argv 列表,无 shell 解释,无需 D7 metachar 拒绝。
- docker CLI 输出是干净的 JSON / 文本,直接 capture_output。

**为什么 remote 走 ssh_execute**:
- 复用 envelope / failure_class / connect_timeout / noise filtering。
- 不重新实现 ssh subprocess 包装。

实现层面只有 `_inspect_one(host, name, runtime, ...)` 和 `_logs_one(host, name, tail, runtime, ...)` 两个 worker 各自封装路由分叉。

---

## 2. docker 命令选择

| 步骤 | local argv 列表 | remote shell 字符串 |
|---|---|---|
| inspect | `[runtime, "inspect", "--format", "{{json .}}", name]` | `<runtime> inspect --format '{{json .}}' <shlex.quote(name)>` |
| logs | `[runtime, "logs", "--tail", str(tail), name]` | `<runtime> logs --tail <tail> <shlex.quote(name)>` |

**`--format "{{json .}}"`**:让 docker 把整个 inspect 对象用单行 JSON 输出,免去 Python 端处理 docker 默认的 array-wrap + 多行格式。

> Alternative:`docker inspect <name>` 默认输出 `[ {...} ]`(array)。**否决**:多一层 unwrap,且如果同时传 2 个 name 会得到 2 元素 array,语义复杂。我们一次只 inspect 一个容器,`--format "{{json .}}"` 更干净。

**`shlex.quote(name)`**:防止容器名带空格 / 特殊字符。docker name 字符集本来很窄,这是 paranoid 防御。

---

## 3. inspect 输出 parse

docker `inspect --format "{{json .}}"` 输出 stdout 例:

```json
{
  "Id": "abc123...",
  "Name": "/payment-svc",
  "State": {
    "Status": "running",
    "Running": true,
    "Paused": false,
    "Restarting": false,
    "OOMKilled": false,
    "Dead": false,
    "Pid": 12345,
    "ExitCode": 0,
    "Error": "",
    "StartedAt": "2026-05-22T08:00:00Z",
    "FinishedAt": "0001-01-01T00:00:00Z",
    "Health": {
      "Status": "healthy",
      "FailingStreak": 0,
      "Log": [
        {"Start": "...", "End": "...", "ExitCode": 0, "Output": "..."}
      ]
    }
  },
  "RestartCount": 0,
  "Image": "sha256:...",
  "Config": {
    "User": "1000:1000",
    "Cmd": ["python", "app.py"],
    "Entrypoint": null,
    "Env": ["LANG=C", ...],
    "Image": "myorg/api:1.2.3"
  },
  "HostConfig": {...},
  "NetworkSettings": {...},
  "Mounts": [{"Source": "...", "Destination": "...", "Mode": "rw"}, ...]
}
```

**Parse 策略**:`json.loads(stdout)`。失败 → 把 stdout 截断到前 1KB 嵌进 `raw.inspect_raw`,envelope `success=false` + `failure_class="parse_error"`。

**所需字段**(`_extract_*` 小函数,每个返回 dict / 不抛):
- `State`: Status / Running / Restarting / OOMKilled / ExitCode / StartedAt / FinishedAt / Error。
- `State.Health`: Status / FailingStreak / Log(最近 N 条,默认 5)。
- `RestartCount`(顶层)。
- `Config.User` / `Config.Cmd` / `Config.Entrypoint` / `Config.Image` / `Config.Env`(只关键字段)。
- `Mounts`: 列表保留(用于 hint 但 v1.0 不打 finding)。

---

## 4. severity 评分函数

```python
def _score_state(inspect: dict) -> tuple[str, list[dict]]:
    """Return (severity, findings_for_this_domain)."""
    state = (inspect.get("State") or {})
    findings: list[dict] = []
    sev = "ok"

    # crit conditions (any one trips)
    if state.get("OOMKilled"):
        findings.append({"severity": "crit", "kind": "oomkilled", "value": True,
                         "hint": "references/container_issues.md#oomkilled-exit-137"})
        sev = "crit"
    exit_code = state.get("ExitCode") or 0
    if exit_code in (137, 139):
        findings.append({"severity": "crit", "kind": "exit_code", "value": exit_code,
                         "hint": f"references/container_issues.md#exit-{exit_code}"})
        sev = "crit"
    health = (state.get("Health") or {})
    if health.get("Status") == "unhealthy":
        last_log = (health.get("Log") or [{}])[-1].get("Output", "")[:200]
        findings.append({"severity": "crit", "kind": "health_unhealthy",
                         "value": last_log,
                         "hint": "references/container_issues.md#unhealthy"})
        sev = "crit"

    # warn conditions
    if sev != "crit":
        if state.get("Status") == "restarting":
            findings.append({"severity": "warn", "kind": "restarting", "value": True,
                             "hint": "references/container_issues.md#restart-loop"})
            sev = "warn"
        rc = inspect.get("RestartCount") or 0
        if rc > 5:
            findings.append({"severity": "warn", "kind": "restart_count", "value": rc,
                             "hint": "references/container_issues.md#restart-loop"})
            sev = "warn"
        if state.get("Status") == "exited" and exit_code != 0:
            findings.append({"severity": "warn", "kind": "exit_code", "value": exit_code,
                             "hint": f"references/container_issues.md#exit-{exit_code}"})
            sev = "warn"

    return sev, findings
```

阈值常量集中放在文件头(可改、易测):

```python
RESTART_WARN_THRESHOLD = 5
LOG_TAIL_DEFAULT = 100
INSPECT_TIMEOUT_S = 30
LOGS_TIMEOUT_S = 30
HEALTH_LOG_VALUE_MAX_CHARS = 200
```

---

## 5. config 评分(v1.0 简化)

```python
def _score_config(inspect: dict) -> tuple[str, list[dict]]:
    findings = []
    user = ((inspect.get("Config") or {}).get("User") or "").strip()
    # Empty / "root" / "0" all mean root.
    if user in {"", "root", "0", "0:0"}:
        findings.append({"severity": "info", "kind": "running_as_root",
                         "value": user or "(default root)",
                         "hint": "references/container_issues.md#root-user"})
    return "ok", findings  # v1.0: config never warns/crits
```

config 域永远 `ok`(info findings 不升级 summary)。这是设计简化(PRD R5)。

---

## 6. logs 扫描

```python
LOG_KEYWORDS = ("panic", "fatal", "OOM", "OutOfMemory", "Killed", "stack trace")

def _score_logs(log_text: str) -> tuple[str, list[dict]]:
    matched = []
    lower = log_text.lower()
    for kw in LOG_KEYWORDS:
        if kw.lower() in lower:
            matched.append(kw)
    if not matched:
        return "ok", []
    return "warn", [{
        "severity": "warn", "kind": "log_keyword",
        "value": matched,
        "hint": "references/container_issues.md#log-keywords",
    }]
```

**为什么不 crit**:logs 噪音大,关键词命中不等于运行有问题(`panic` 可能在 unit test 输出里)。crit 留给 state 域。

---

## 7. 顶层 summary 合并

```python
data["summary"] = {"state": state_sev, "config": cfg_sev, "logs": log_sev}
data["findings"] = sorted(state_f + cfg_f + log_f, key=lambda f: _severity_rank(f["severity"]))

def _severity_rank(sev: str) -> int:
    return {"crit": 0, "warn": 1, "info": 2}.get(sev, 3)
```

顶层 `success`:`state != "crit"`(crit 不算 success,但 envelope exit_code 仍 0,代表"脚本成功执行了诊断")。

> Alternative:`success = (state == "ok" and logs == "ok")`。**否决**:findings 已经表达严重度;`success` 在 v1.0 第一刀里只表示"诊断脚本本身跑通了",不代表"被诊断的容器是健康的"。这与 diagnose_host 一致。

待澄清:**`exit_code` 的语义**。我倾向 `0` if 诊断脚本跑通,`1` if state==crit。这和 diagnose_host 一致(critical → exit 1,让 CI / cron 能捕捉)。

---

## 8. 失败传播

| 情况 | envelope.success | envelope.exit_code | data.failure_class |
|---|---|---|---|
| 诊断正常,state==ok | true | 0 | null |
| 诊断正常,state==warn | true | 0 | null |
| 诊断正常,state==crit | false | 1 | null |
| 容器不存在 / docker 报错 | false | 1 | "remote_error" |
| logs 调用失败但 inspect 成功 | false | 1 | "logs_unavailable" |
| ssh_execute 不存在(remote only) | false | 1 | "precondition" |
| ssh_execute 自身超时 | false | 124 | "timeout" |
| docker 输出不是 JSON | false | 1 | "parse_error" |
| local route 但 docker 命令本身缺失 | false | 1 | "remote_error" (借用) |

`raw.inspect_raw` / `raw.inspect_json` / `raw.logs` 总是嵌进 `data.raw`,失败时是部分数据。

---

## 9. 数据形状

成功路径完整 envelope:

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "<human summary text>",
  "stderr": "",
  "data": {
    "host": "local",
    "target": "payment-svc",
    "runtime": "docker",
    "summary": {"state": "warn", "config": "ok", "logs": "ok"},
    "findings": [
      {"severity": "warn", "kind": "restart_count", "value": 7,
       "hint": "references/container_issues.md#restart-loop"}
    ],
    "raw": {
      "inspect": {
        "id": "abc123",
        "name": "/payment-svc",
        "state": {...},
        "config": {...},
        "restart_count": 7,
        "image": "...",
        "mounts": [...]
      },
      "logs": "<last N lines of logs text>",
      "logs_tail": 100
    }
  }
}
```

注意:`data.raw.inspect` 是**精简后**的版本(只含我们 extract 的字段);完整 inspect JSON 太长,不放。

---

## 10. argparse

```python
def build_parser():
    p = argparse.ArgumentParser(prog="inspect_container.py")
    p.add_argument("host", help="ssh-core alias, or 'local'")
    p.add_argument("name", help="container name or id (id prefix works)")
    p.add_argument("--tail", type=int, default=LOG_TAIL_DEFAULT)
    p.add_argument("--runtime", default="docker", choices=["docker", "podman"])
    p.add_argument("--connect-timeout", type=int, default=8)  # only used for remote
    p.add_argument("--json", action="store_true")
    return p
```

无 SUPPRESS flag(v0.2 stub 没有不实现的 flag)。

---

## 11. 测试策略

### 11.1 Unit(默认套)

每个测试用 `monkeypatch subprocess.run` 注入 docker / ssh_execute 输出。

| 用例分组 | 验证 |
|---|---|
| `_extract_*` helpers | 给定 mock inspect JSON,extract 字段正确 |
| `_score_state` | OOMKilled / exit 137 / unhealthy / restart_count / restarting / exited 各阈值 |
| `_score_config` | root / 1000:1000 / 空 → 期望 findings |
| `_score_logs` | 含 panic / fatal / OOM → warn;干净 logs → ok |
| `_severity_rank` 排序 | crit 先于 warn 先于 info |
| local route | mock `subprocess.run(["docker", ...])`,验证 argv shape + 路径 |
| remote route | mock ssh_execute envelope,验证 argv shape(走 ssh_execute) |
| `--runtime podman` | argv 里第一项是 "podman" |
| inspect 失败 | envelope success=false + failure_class="remote_error" |
| logs 失败但 inspect 成功 | failure_class="logs_unavailable",inspect 数据仍在 |
| inspect JSON parse 失败 | failure_class="parse_error" |
| ssh_execute 不存在(remote) | failure_class="precondition" |
| envelope 形状 | 顶层 5 字段 + data 内必含字段 |
| `--json` vs human stdout | --json 输出 JSON,无 --json 输出 readable 文本 |
| argparse 错误 | 缺 host / name → exit 2 |

### 11.2 Integration(`@pytest.mark.live_ssh`)

写文件但不强制跑:

| 用例 | 前置 |
|---|---|
| `inspect_container.py local <busybox-running> --json` | 本机有 docker + 跑一个 busybox |
| `inspect_container.py local <not-exist>` | 验证 envelope 不抛 |

module 级 skip 条件:`shutil.which("docker") is None`。

本任务暂**不**跑 live(无测试机),只把测试文件存好。

---

## 12. Rollback shape

单 commit:`feat(docker-quick): ship inspect_container v1.0`。回滚 `git restore plugins/docker-quick/.../inspect_container.py tests/test_inspect_container_*.py`。

---

## 13. 风险与对策

| 风险 | 对策 |
|---|---|
| docker 不同版本 inspect 字段名差异(`OOMKilled` vs `oomkilled`) | extract 用 `.get` 容忍缺失;字段名锁定 docker 官方文档大小写;podman 兼容**等遇到时再说**(留 v1.1 hotfix) |
| 容器 ID 同名冲突(短前缀匹配多个容器) | 不做去歧义;docker CLI 自己会报错,我们传递 envelope failure_class="remote_error" |
| logs 超大(几 GB) | 已有 ssh-core 10MB 流截断(SKILL.md);本脚本不再额外 cap |
| log 关键词误报(`panic` 出现在合法消息里) | 接受;v1.0 只给 warn(不 crit),findings 把命中关键词列出来给人看 |
| 本机无 docker | local route 失败时 envelope `failure_class="remote_error"`,clear stderr 提示 |
| podman inspect 输出 schema 微差 | v1.0 默认 docker;`--runtime podman` 时若 schema 不兼容,extract `.get` 返回空,severity 降级到 `ok` + 给 info finding "podman schema partial" |
