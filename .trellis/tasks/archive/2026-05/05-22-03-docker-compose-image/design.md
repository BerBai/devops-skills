# Design: docker-quick compose_status + image_audit v1.0

> 技术决策:host 路由分叉(继承 inspect_container)、docker compose ps 双格式解析、history JSON Lines 流式聚合、waste pattern 子串集、severity 评分、findings 生成、失败传播。两脚本共享 inline helpers,但**不**共享业务函数。

---

## 1. host 路由分叉

继承 `inspect_container.py` 的双工模式,**新增** `cwd` 参数以支持 `docker compose` 必须在项目目录下运行的约束。

```
                       ┌─ local ──── _run_local(argv_list, timeout, cwd=...)
                       │              subprocess.run(["docker", ...], cwd=cwd)
                       │              shell=False
host  ─── _route ─────┤
                       │
                       └─ alias ──── _run_via_ssh_execute(host, cmd_string, ...)
                                      python ssh_execute.py <host> "<bash cmd>" --json
                                      parse the envelope
```

**为什么 local cwd 而不是 `cd && ...`**:
- argv 列表传 `cwd=` 给 `subprocess.run` 干净、无 shell 解释。
- `cd /no/such && docker compose ps` 报错信息混在 stderr 不易归因。

**为什么 remote 仍用 `cd ... && ...`**:
- ssh_execute 接口是 `host + 单条 command string`,没有 `cwd` 参数。
- 远端透过 `shlex.quote(project_dir)` 保护路径中的特殊字符。
- 远端 docker compose 命令是否真在 project_dir 下跑,由远端 shell 自己处理。

image_audit 无 cwd 概念(docker history 不依赖 cwd),沿用原版 `_run_local(argv, timeout)`。

---

## 2. compose_status:docker compose ps 输出双格式

`docker compose ps --format json` 在不同 compose 子版本下输出形态有别:

- **JSON Lines**(docker compose v2.21+):每行一个 service JSON 对象。
- **JSON Array**(更早期):整个 stdout 是 `[ {...}, {...} ]`。

解析策略:
```
text = stdout.strip()
if text.startswith("["):
    services = json.loads(text)                  # array path
else:
    services = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        services.append(json.loads(line))         # lines path
```

**字段稳定性**:两种格式下每个服务 dict 至少含 `Service` / `Name`(container 真名)/ `State` / `Health` / `ExitCode`。**不**假设 `Status` 字段(它是 human-readable 字符串,无法解析)。`Health` 可能缺失(无 healthcheck 配置),按 `""` 处理。

**为什么不传 `--format json --quiet`**:`--quiet` 只输出 container ID,丢失 service 名映射,不可用。

---

## 3. compose_status:per-service inspect 扩散

list 调用拿到 service 数组后:

```
for svc in services:
    if svc.state == "running" and svc.health != "unhealthy":
        # ok 路径 — 直接进 services 输出,不二次调用
        continue
    inspect_env = _run_inspect_one_container(host, svc.Name, ...)
    if not inspect_env.success:
        svc["inspect_error"] = True
        svc["inspect_stderr"] = inspect_env.stderr
        continue
    parsed = json.loads(inspect_env.stdout)
    svc["restart_count"] = parsed.get("RestartCount", 0)
    svc["health_log"] = ...
```

**为什么不在 ok 路径也 inspect**:N 次远端调用对大栈昂贵;ok 服务我们已有 ps 输出的 State / Health,够评分了。

**`--service NAME` 过滤**:在 list 解析完后,过滤 `services = [s for s in services if s.Service == NAME]`;若空,`failure_class = "service_not_found"`、`exit 1`,findings 含 `kind="service_not_found"`。

---

## 4. image_audit:docker history JSON Lines 解析

`docker history --no-trunc --format "{{json .}}"` 输出严格 JSON Lines(一行一 layer,**不**是数组)。每行字段:

```
{
  "ID": "<missing>" or sha256,
  "CreatedAt": "2024-01-15T10:23:45Z",
  "CreatedBy": "/bin/sh -c apt-get install -y curl && rm -rf /var/lib/apt/lists",
  "Size": "12.3MB" or "0B",
  "Comment": ""
}
```

**注意**:`Size` 字段是 **human-readable 字符串**(如 `"12.3MB"`),**不**是字节数。必须解析:

```
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

def _parse_size(s: str) -> int:
    if not s:
        return 0
    s = s.strip()
    for suffix, mul in _SIZE_UNITS.items():
        if s.upper().endswith(suffix):
            num = s[:-len(suffix)].strip()
            try:
                return int(float(num) * mul)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0
```

也支持 `--format "{{.Size}}"` 加上 `--human=false` 的可能性,但 v1.0 **不**用 `--human=false`(并非所有 docker 版本支持该 flag),坚持文本解析。

---

## 5. image_audit:waste pattern 子串集

```
_WASTE_PATTERNS = [
    # (kind, must_have_substring, must_NOT_have_substring)
    ("apt_cache_left",
     "apt-get install",
     "rm -rf /var/lib/apt/lists"),
    ("pip_cache_left",
     "pip install",
     "--no-cache-dir"),
    ("npm_cache_left",
     "npm install",
     "npm cache clean"),
]
```

对每 layer 的 `CreatedBy` 字段:case-sensitive 子串匹配。命中即追加 finding(同一 layer 可多次命中)。

**为什么不正则**:正则容易过拟合(`apt-get install` vs `DEBIAN_FRONTEND=noninteractive apt-get -y install`);子串足够稳。

**为什么 case-sensitive**:Dockerfile 惯例就是小写;case-insensitive 反而可能误捕 `RUN APT-GET ...` 罕见拼写。

**`large_layer`** finding 独立于 waste:layer `Size > threshold_mb * MB` 即标。Size 解析见 §4。

---

## 6. severity 评分规则总表

| Domain | 信号 | 等级 | finding kind |
|---|---|---|---|
| compose state | 任一服务 `Health == "unhealthy"` | crit | `unhealthy` |
| compose state | 任一服务 `State == "restarting"` | warn | `restart_loop` |
| compose state | 任一服务 `RestartCount > 5` | warn | `restart_loop` |
| compose state | 任一服务 `State == "exited" AND ExitCode != 0` | warn | `exited_nonzero` |
| compose state | `services_total == 0` | warn | `empty_stack` |
| compose state | 所有 `State == "running"` 且无 health 告警 | ok | (无 finding) |
| compose state | `--service NAME` 但 `NAME` 不在 ps 中 | warn | `service_not_found` |
| image state | 存在 layer `Size > threshold_mb * MB` | warn | `large_layer` |
| image state | layer `CreatedBy` 命中 apt waste | warn | `apt_cache_left` |
| image state | layer `CreatedBy` 命中 pip waste | warn | `pip_cache_left` |
| image state | layer `CreatedBy` 命中 npm waste | warn | `npm_cache_left` |
| image state | `Config.User` 为空/"root"/"0" | info | `running_as_root` |
| image state | 以上都没有 | ok | (无 finding) |

**聚合规则**:`summary.state = max(severity)`,其中 `crit > warn > ok`,`info` 不参与聚合(只作为 finding 出现)。

`config` / `logs` 两个 domain 字段在两脚本均**不存在**(`inspect_container` 才有);compose_status / image_audit 的 summary 只有一个 `state` 字段加上 domain-specific 计数字段(`services_total` / `services_running` / `total_size_mb` / `layer_count` / `large_layers`)。

---

## 7. failure_class 总表

| failure_class | 触发条件 | 影响 |
|---|---|---|
| `remote_error` | docker 命令返回非 0 / 镜像或容器不存在 / cwd 不存在 | success=false, exit 1 |
| `precondition` | ssh_execute.py 文件不存在(remote 路径) | success=false, exit 1 |
| `timeout` | _run_local 或 ssh_execute 子进程超时 | success=false, exit 124 |
| `parse_error` | docker stdout 既非 JSON Lines 也非数组 | success=false, exit 1 |
| `ssh_execute_broken` | ssh_execute stdout 不是合法 envelope JSON | success=false, exit 1 |
| `service_not_found`(compose 独有) | `--service NAME` 在 ps 输出中无匹配 | success=false, exit 1 |
| `inspect_unavailable`(image 独有) | history 成功但 inspect 失败 | success=false, exit 1,但 layers 仍可用 |

**partial-data 原则**:list 成功 + per-service inspect 部分失败,顶层仍 `success=true`(failure 在 per-service `inspect_error` 标记)。history 成功 + image inspect 失败,顶层 `success=false` 但 `data.layers` 仍嵌入。

---

## 8. 函数清单

### compose_status.py

```
# helpers (inline)
def _envelope(...) -> dict
def _emit(result, as_json) -> None
def _ssh_execute_path() -> Path

# runners
def _run_local(argv, timeout, cwd=None) -> dict     # adds cwd vs inspect_container
def _run_via_ssh_execute(ssh_exec, host, cmd, timeout, connect_timeout) -> dict

# per-call workers
def _list_services(host, project_dir, connect_timeout, *, ssh_exec_override=None) -> dict
def _inspect_container(host, container_name, connect_timeout, *, ssh_exec_override=None) -> dict

# parsers
def _parse_ps_output(stdout: str) -> tuple[list[dict] | None, str | None]
    # returns (services, parse_error)

# scoring
def _score_services(services, service_filter) -> tuple[str, list[dict]]

# formatting
def _format_human(data) -> str

# argparse
def build_parser() -> ArgumentParser
def main(argv=None) -> int
```

### image_audit.py

```
# helpers (inline, same shape)
def _envelope(...) -> dict
def _emit(result, as_json) -> None
def _ssh_execute_path() -> Path

# runners
def _run_local(argv, timeout) -> dict               # no cwd
def _run_via_ssh_execute(ssh_exec, host, cmd, timeout, connect_timeout) -> dict

# per-call workers
def _history_image(host, image, runtime, connect_timeout, *, ssh_exec_override=None) -> dict
def _inspect_image(host, image, runtime, connect_timeout, *, ssh_exec_override=None) -> dict

# parsers
def _parse_size(s: str) -> int
def _parse_history_output(stdout: str) -> tuple[list[dict] | None, str | None]
def _parse_inspect_output(stdout: str) -> tuple[dict | None, str | None]

# scoring
def _score_layers(layers, threshold_bytes) -> tuple[str, list[dict]]
def _score_user(inspect_blob) -> list[dict]                                 # info-only

# formatting
def _format_human(data) -> str

# argparse
def build_parser() -> ArgumentParser
def main(argv=None) -> int
```

---

## 9. 失败传播伪码 — compose_status

```
list_env = _list_services(host, project_dir, ...)
if not list_env.success:
    return envelope(False, exit_code, failure_class=list_env.data.failure_class, ...)

services, parse_err = _parse_ps_output(list_env.stdout)
if services is None:
    return envelope(False, EXIT_FAIL, failure_class="parse_error", ...)

if service_filter and not any(s.Service == service_filter for s in services):
    return envelope(False, EXIT_FAIL, failure_class="service_not_found",
                    findings=[{kind:"service_not_found", value:service_filter}], ...)

for svc in services:
    if needs_inspect(svc):
        env = _inspect_container(host, svc.Name, ...)
        if env.success:
            parsed, _ = _parse_inspect_output(env.stdout)
            svc.restart_count = parsed.get("RestartCount", 0)
            svc.health_log = ...
        else:
            svc.inspect_error = True
            svc.inspect_stderr = env.stderr

state, findings = _score_services(services, service_filter)
overall_success = state != "crit"
overall_exit = EXIT_OK if overall_success else EXIT_FAIL
```

## 10. 失败传播伪码 — image_audit

```
hist_env = _history_image(host, image, runtime, ...)
if not hist_env.success:
    return envelope(False, exit_code, failure_class=hist_env.data.failure_class, ...)

layers, parse_err = _parse_history_output(hist_env.stdout)
if layers is None:
    return envelope(False, EXIT_FAIL, failure_class="parse_error", ...)

inspect_env = _inspect_image(host, image, runtime, ...)
if inspect_env.success:
    inspect_blob, _ = _parse_inspect_output(inspect_env.stdout)
else:
    inspect_blob = None
    inspect_failure_class = "inspect_unavailable"

state, layer_findings = _score_layers(layers, threshold_bytes)
user_findings = _score_user(inspect_blob) if inspect_blob else []
findings = sorted(layer_findings + user_findings, key=lambda f: rank(f.severity))

failure_class = None
if inspect_blob is None:
    failure_class = "inspect_unavailable"
overall_success = state != "crit" and failure_class is None  # state never crit in v1.0
overall_exit = EXIT_OK if overall_success else EXIT_FAIL
```

---

## 11. argparse 终态

### compose_status.py

| flag/positional | type | default | 说明 |
|---|---|---|---|
| `host` | str | (req) | `local` 或 ssh-core alias |
| `project_dir` | str | (req) | Compose 项目目录(本地 cwd 或远端目录) |
| `--service NAME` | str | None | 仅检查该服务(过滤 list 输出) |
| `--connect-timeout N` | int | 8 | SSH ConnectTimeout(remote 才生效) |
| `--json` | flag | False | 输出 JSON envelope |

### image_audit.py

| flag/positional | type | default | 说明 |
|---|---|---|---|
| `host` | str | (req) | `local` 或 ssh-core alias |
| `image` | str | (req) | 如 `myorg/api:1.2.3` |
| `--threshold-mb N` | int | 200 | layer 大小告警阈值(MB) |
| `--runtime docker\|podman` | choice | `docker` | 容器运行时 |
| `--connect-timeout N` | int | 8 | SSH ConnectTimeout(remote 才生效) |
| `--json` | flag | False | 输出 JSON envelope |

---

## 12. 测试矩阵

### compose_status unit(估 30-35 tests)
- `_envelope` / `_emit` 形状(envelope=3 tests,emit json/human=4)
- `_parse_ps_output` 双格式:JSON Lines 单服务、JSON Lines 多服务、JSON Array、空 stdout、非 JSON(8)
- `_score_services` 各阈值:全 ok、含 unhealthy → crit、含 restarting → warn、含 restart_count>5 → warn、含 exited!=0 → warn、empty → warn、service_filter miss → warn finding(7)
- `_list_services` local 路由(monkeypatch subprocess.run,verify argv + cwd)(3)
- `_list_services` remote 路由(monkeypatch subprocess.run on ssh_execute,verify shell-out command string)(3)
- `_inspect_container` per-service(local/remote)(2)
- `main()` happy path、ssh_execute 缺失、parse_error、service_not_found、partial inspect_error、--service 命中 ok、--service 命中 crit、--service miss(8)
- argparse 缺参 exit 2(1)

### image_audit unit(估 25-30 tests)
- `_envelope` / `_emit`(2)
- `_parse_size` 各单位:`12B` / `12KB` / `12.3MB` / `1GB` / `"0B"` / 空字符串 / 无单位数字 / 非数字 / 奇怪格式(9)
- `_parse_history_output` JSON Lines:多 layer、单 layer、空、非 JSON(4)
- `_parse_inspect_output`:happy、非 dict、非 JSON(3)
- `_score_layers` 各阈值:全 ok、含 large_layer、含 apt_cache_left、含 pip_cache_left、含 npm_cache_left、多 waste 同 layer(6)
- `_score_user` info-only:root / "0" / "" / "nobody"(4)
- `_history_image` + `_inspect_image` local/remote 路由(4)
- `main()` happy、镜像不存在(remote_error)、history 成功 inspect 失败(inspect_unavailable)、--runtime podman、--threshold-mb 自定义、ssh_execute 缺失(6)
- argparse 缺参(1)

### 集成(各 1-2 tests)
- compose_status:`tmp_path` 写最小 `compose.yml`(只声明 1 个 busybox 服务 sleep 60),`docker compose up -d`,跑 compose_status local → 验证 `services_total >= 1`、`summary.state in {"ok","warn"}`;teardown 用 `docker compose down`。
- image_audit:`busybox:latest` 应已被 inspect_container 的集成测试 pull 过;直接跑 `image_audit local busybox:latest --threshold-mb 1` → 验证 history JSON Lines parse 成功、layers 不为空。

总新增 ≈ 60-70 unit tests + 2-4 integration tests。

---

## 13. 不引入的复杂度

- **不**抽公共 `_run_local` / `_run_via_ssh_execute` 到共享 lib(沿 CONTRIBUTING.md L29 inline 模式);两脚本各自有自己的副本,接受微量代码重复。
- **不**做 podman compose 兼容(R9):`compose_status` 不暴露 `--runtime`。
- **不**做 multi-arch manifest 拆解。
- **不**实际拉镜像扫文件系统(只看 `docker history` 命令文本)。
- **不**实现并行 per-service inspect(N 次串行;v1.1 再优化)。
- **不**做 compose.yml 静态解析(端口冲突 / depends_on cycle / healthcheck 命令缺失探测都留 v1.1)。
- **不**给两脚本加 `--tail` / `--logs` 类 flag(对应需求请用 inspect_container)。

## 14. 与第三刀的差异

| 维度 | inspect_container | compose_status | image_audit |
|---|---|---|---|
| 远端调用数 | 固定 2(inspect + logs) | 动态 1+N(list + per-service inspect) | 固定 2(history + inspect) |
| 评分 domain | 3(state/config/logs) | 1(state) | 1(state) |
| 主要 parse 难点 | docker inspect JSON object | ps 双格式 | history JSON Lines + Size 字符串 |
| 是否支持 podman | 是 | 否 | 是 |
| 部分失败语义 | logs 失败仍嵌 inspect | per-svc inspect 失败标 inspect_error | inspect 失败仍嵌 layers |
| cwd 参数 | 不需要 | 需要(local 模式) | 不需要 |
