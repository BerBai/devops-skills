# docker-quick inspect_container v1.0

## Goal

把 `docker-quick/inspect_container.py` 从 v0.2 stub 实化为可用的单容器诊断脚本,作为 `docker-quick` plugin 的 v1.0 第一刀。本刀同时验证 **shell-out 模式对 docker 工具链是否成立**(remote-debug 验证了 ssh / nc / tail / journalctl / cat / dpkg/rpm;docker 是新工具链)。

涉及脚本(本刀只 1 个):
- `plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py`

## Background

- `docker-quick` v0.2 落地 3 个 stub:`inspect_container` / `compose_status` / `image_audit`。本刀只做 `inspect_container`,作为 plugin 模式建立。
- v0.2 SKILL.md L46-48 已确立 host 模型:`local` → 直 `subprocess.run(["docker", ...])`;其它 alias → 透过 `ssh-core/ssh_execute.py`。**不**走 ssh_execute 的 `local` 路由,因为本地直跑无握手成本、无 metachar 拒绝问题。
- SKILL.md L82-101 已确立 output contract:顶层 `data` 含 `summary`(per-domain ok/warn/crit)+ `findings`(severity/kind/value/hint)+ `raw`。本刀的 envelope 必须匹配这个 schema(R1)。
- v1.0 第一刀 + 第二刀(remote-debug 4 脚本)已建立 `_envelope` / `_emit` / `_ssh_execute_path` inline 模式。本刀沿用。

## Requirements

### R1. JSON 输出契约
- `--json` 时输出严格 `{success, exit_code, stdout, stderr, data}` 信封(参 [error-handling.md](../../spec/backend/error-handling.md))。
- `data` 顶层字段至少含 `host` / `target` / `summary` / `findings` / `raw`(匹配 SKILL.md L82-101 contract)。
- `data.summary`:`{state, config, logs}` 每域 `"ok" | "warn" | "crit"`。
- `data.findings`:`list[{severity, kind, value, hint}]`,severity ∈ `{info, warn, crit}`。
- `exit_code` 只允许 `0 / 1 / 2 / 124`。

### R2. v0.2 argparse 向后兼容
- 保留 v0.2 stub 的 positional `host` / `name`,以及 `--tail` / `--json`。
- 允许**新加** flag(`--runtime` / `--connect-timeout` 等),不允许**删** / 改名。

### R3. host 路由
- `host == "local"`:`subprocess.run(["docker", "inspect", "--format", "{{json .}}", name], shell=False)` 等。argv 列表,绝不 `shell=True`。
- `host != "local"`:构造 docker 命令字符串,透过 `ssh-core/.../ssh_execute.py <host> "<docker cmd>" --json` 调用,解析 envelope。
- shell-out only(CONTRIBUTING.md L29)— 不 import ssh-core lib。
- `local` 路径**不**透过 ssh_execute(避免握手成本 + ADR-001 D7 metachar 拒绝;docker argv 本就无 metachar)。

### R4. 远端命令组合
本脚本对每个目标容器最多发 3 次 docker 调用:

1. **inspect**:`docker inspect --format "{{json .}}" <name>` → 单行 JSON,parse 出 State / Config / NetworkSettings / Mounts / Health。
2. **logs**:`docker logs --tail N <name>` → 文本,本地不再解析,作为 `raw.logs`。
3. **healthcheck history**(可选):若 inspect 显示有 healthcheck,从 inspect 输出里直接取 `State.Health.Log[]`(已经包含在 #1 里,无需第二次远端调用)。

### R5. 严重度评分
- `state`: `Status == "running"` 且 `RestartCount <= 5` → `ok`;`Status == "restarting"` 或 `RestartCount > 5` → `warn`;`OOMKilled == true` 或 `ExitCode in {137, 139}` 或 `Health.Status == "unhealthy"` → `crit`;其它 `exited` → `warn`。
- `config`: 默认 `ok`;若 `User` 为空/root 且镜像不在白名单内 → `warn`(只 info findings,不升级 summary);v1.0 简化:`config` 永远 `ok`,findings 里给 info hint。
- `logs`: 文本扫描含 `panic`/`fatal`/`OOM`/`error` 关键词时 → `warn`;否则 `ok`。**不** crit(避免脚本对未知 logs 过激)。

### R6. findings 列表
每条 `{severity, kind, value, hint}`,kind 可枚举(便于消费方过滤):
- `exit_code`:value=137/139/etc., hint 指向 `references/container_issues.md#oomkilled-exit-137` 等。
- `restart_count`:value=N。
- `oomkilled`:value=true。
- `health_unhealthy`:value=最近 1 条 health log。
- `running_as_root`:value=user_string(info)。
- `log_keyword`:value=匹配到的关键词列表(warn)。

### R7. runtime
- 默认 `docker`;`--runtime podman` 切换。
- v1.0 **不**实现 podman 自适应检测(避免远端额外探测),`--runtime` 是显式 flag。

### R8. 失败语义
- inspect 失败(容器不存在 / 远端无 docker):envelope `success=false`,`data.failure_class = "remote_error"`(或 `"precondition"` 若是 ssh_execute 自身缺失)。logs 调用跳过。
- logs 失败:envelope `success=false`,但 inspect 数据仍嵌进 `data.raw.inspect`;`data.failure_class = "logs_unavailable"`。
- ssh_execute 不存在(`local` 模式无影响,因为 local 直跑):envelope `data.failure_class = "precondition"`。

### R9. 测试
- Unit tests:`monkeypatch subprocess.run` 覆盖 — local route 直跑 docker、remote route shell-out ssh_execute、inspect happy/fail、logs happy/fail、severity 评分各阈值、findings 生成、SUPPRESS 兼容(无新 SUPPRESS)、JSON envelope 形状、argv 错误。
- 集成 tests:`@pytest.mark.live_ssh` 沿用 marker。若本机有 docker 跑 1-2 个 `local` route smoke;若无 docker,模块级 skip。本任务**暂不**跑 live(无测试机),只写文件留作以后验证。

### R10. 质量门
- `ruff check <touched>` 0 violations。
- 全仓 `ruff check` 不退步(当前 baseline:8 errors,见 v1.0 第二刀)。
- `pytest -m "not live_ssh"` 默认套件全绿。
- 不修改 SKILL.md(除非示例失效)。
- 不修改 ssh-core / ssh_execute / remote-debug 任何代码。

## Acceptance Criteria

- [x] AC1. `inspect_container.py local <name> --json` 在本地通过 `subprocess.run(["docker", "inspect", ...])` 直接拿数据,**不**走 ssh_execute。
- [x] AC2. `inspect_container.py pai myctr --json` 透过 `python ssh_execute.py pai "docker inspect ..." --json` shell-out,解析返回的 envelope。
- [x] AC3. `inspect_container.py local <name> --json` 当容器不存在,envelope `success=false`、`data.failure_class="remote_error"`,**不**抛异常。
- [x] AC4. `inspect_container.py local running-ctr --json` 返回 `data.summary.state == "ok"`,`data.findings` 不含 crit 项。
- [x] AC5. `inspect_container.py local oom-ctr --json`(mock OOMKilled=true 或 ExitCode=137):`data.summary.state == "crit"`,findings 含 `kind="oomkilled"` 或 `kind="exit_code", value=137`。
- [x] AC6. `inspect_container.py local restart-ctr --json`(mock RestartCount=10):`data.summary.state == "warn"`,findings 含 `kind="restart_count", value=10`。
- [x] AC7. `inspect_container.py local unhealthy-ctr --json`(mock Health.Status="unhealthy"):`data.summary.state == "crit"`,findings 含 `kind="health_unhealthy"`,值含 health log 摘要。
- [x] AC8. `inspect_container.py local with-error-logs --json --tail 50`:logs 含 "fatal" 时 `data.summary.logs == "warn"`,findings 含 `kind="log_keyword"`。
- [x] AC9. `--runtime podman` 时,本地 / 远端命令都用 `podman` 而非 `docker`。
- [x] AC10. `inspect_container.py` 不传 `host` / `name` → argparse 自动 exit 2。
- [x] AC11. `inspect_container.py local <name>`(不 `--json`)输出 human-readable 摘要到 stdout,无 prefix。
- [x] AC12. `grep -R "import lib" plugins/docker-quick/` 返回 0(沿用 inline helpers 约定)。
- [x] AC13. `ruff check plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py tests/test_inspect_container_*.py` 0 violations。
- [x] AC14. `pytest -m "not live_ssh" tests/test_inspect_container_unit.py` 全绿(52/52)。
- [x] AC15. 顶层 envelope 形状匹配 SKILL.md L82-101 contract(`data.summary` / `data.findings` / `data.raw` 等字段存在)。
- [x] AC16. v0.2 argparse 命令名 / positional 数不变;`python inspect_container.py --help` 仍展示 v0.2 描述的 surface(可加新 flag)。

## Non-Goals

- 不实现 `compose_status.py` / `image_audit.py`(留 v1.0 后续刀)。
- 不实现 podman 自适应探测(`--runtime` 显式)。
- 不实现 docker context 集成(v1.0 走显式 ssh-core)。
- 不抽公共 lib(每 plugin 自带 inline helpers)。
- 不修改 SKILL.md(除非示例失效)。
- 不实现 healthcheck 的二次远端调用(`State.Health.Log` 已含在 inspect 输出中)。
- 不实现 mem/CPU stats(留 v1.1,需要 `docker stats --no-stream` 第二次调用)。
- 不跑 live integration(暂无测试机;只写 mock unit 覆盖)。

## Notes

- 设计细节(docker JSON parse、severity 阈值、failure 传播)见 `design.md`。
- 执行顺序、checklist、验证命令见 `implement.md`。
- 任何打破 R2 的修改必须先回到本 PRD 加 ADR。
