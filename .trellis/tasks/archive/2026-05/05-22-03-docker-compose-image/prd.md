# docker-quick compose_status + image_audit v1.0

## Goal

把 `docker-quick/compose_status.py` 与 `docker-quick/image_audit.py` 从 v0.2 stub 实化为可用的 v1.0 脚本,作为 `docker-quick` plugin 的**收官刀**:加上前刀 `inspect_container.py`,本 plugin v1.0 三件套齐全(单容器 / Compose 栈 / 镜像)。同时验证 **docker 工具链 shell-out 模式对 Compose 子命令与 docker history JSON 输出仍成立**。

涉及脚本(本刀 2 个):
- `plugins/docker-quick/skills/docker-quick/scripts/compose_status.py`
- `plugins/docker-quick/skills/docker-quick/scripts/image_audit.py`

## Background

- `docker-quick` v0.2 落地 3 个 stub。v1.0 第三刀已实化 `inspect_container.py`;本刀实化剩下两个。
- SKILL.md L46-48 定义的 host 路由模型已被 `inspect_container.py` 验证可行:`local` 直跑、其它 alias 透过 `ssh-core/ssh_execute.py`。本刀沿用,**不**重新决策。
- SKILL.md L82-101 output contract 与前刀一致;本刀必须保持 envelope 形状一致以便 SKILL 决策树消费方一致(R1)。
- v1.0 已积累的 inline helpers 模式(`_envelope` / `_emit` / `_ssh_execute_path`)在 `port_check` / `tail_log` / `compare_across_hosts` / `inspect_container` 共 4 个脚本验证稳定;本刀沿用、不抽公共 lib(沿 CONTRIBUTING.md L29)。
- `docker compose ps --format json` 与 `docker history --no-trunc --format "{{json .}}"` 是新引入工具链 surface,本刀首次验证它们的 JSON shape 在 docker / podman 下都可消费。podman 的 `compose` 子命令存在但行为有差异,v1.0 **限制 compose_status 为 docker**(`--runtime` flag 仅在 image_audit 提供)。

## Requirements

### R1. JSON 输出契约
- `--json` 时输出严格 `{success, exit_code, stdout, stderr, data}` 信封(参 [error-handling.md](../../spec/backend/error-handling.md))。
- `compose_status.py` `data` 顶层:`host` / `project_dir` / `service` / `summary` / `services` / `findings` / `raw`。
- `image_audit.py` `data` 顶层:`host` / `image` / `runtime` / `threshold_mb` / `summary` / `layers` / `findings` / `raw`。
- `summary` 在 compose 下:`{state: ok|warn|crit, services_total, services_running}`;在 image 下:`{state: ok|warn|crit, total_size_mb, layer_count, large_layers}`。
- `findings`:`list[{severity, kind, value, hint}]`,severity ∈ `{info, warn, crit}`。
- `exit_code` 只允许 `0 / 1 / 2 / 124`。

### R2. v0.2 argparse 向后兼容
- 保留 v0.2 stub 的 positional 与 flag:
  - compose_status:`host` / `project_dir` / `--service` / `--json`。
  - image_audit:`host` / `image` / `--threshold-mb` / `--json`。
- 允许**新加** flag,不允许**删** / 改名。

### R3. host 路由
- `host == "local"`:`subprocess.run(["docker", ...], shell=False)`;compose 子命令同样用 argv 列表(`["docker", "compose", "ps", "--format", "json", "--all"]`,并通过 `cwd=<project_dir>` 切目录,而非 `cd && ...`)。
- `host != "local"`:构造完整命令字符串,透过 `ssh-core/.../ssh_execute.py <host> "<cmd>" --json` shell-out 调用;远端只能用 `cd <quoted_dir> && docker compose ...` 组合(因 ssh_execute 不传 `cwd`)。
- `project_dir` 在远端拼装时透过 `shlex.quote` 保护。
- shell-out only(CONTRIBUTING.md L29)— 不 import ssh-core lib。
- `local` 路径**不**透过 ssh_execute。

### R4. 远端命令组合 — compose_status

每次调用最多发 1 + N 次 docker 调用(N = not-running/unhealthy 服务数):

1. **list**:`docker compose ps --format json --all` 在 `<project_dir>` 下跑。
   - 返回 JSON Lines(每服务一行)或一个 JSON 数组(取决于 docker compose v2 子版本)。两种格式都要能解析。
   - `--all` 必须加,确保 exited / stopped 服务也出现。
2. **per-service inspect**:对 `state != "running"` 或 `health == "unhealthy"` 的服务,额外发一次 `docker inspect --format "{{json .}}" <container_id>`,取 State / Health / RestartCount。

若 `--service NAME` 指定:只对该服务做 #2;若该 service 不在 `ps` 输出中,envelope `failure_class = "service_not_found"`。

### R5. 远端命令组合 — image_audit

每次调用最多 2 次 docker 调用:

1. **history**:`docker history --no-trunc --format "{{json .}}" <image>` → JSON Lines,每 layer 一行,含 `Size` / `CreatedBy` / `CreatedAt` / `Comment`。
2. **inspect**:`docker inspect --format "{{json .}}" <image>` → 单 blob,取 `Config.User` / `Architecture` / `Size`。

不实际拉镜像扫文件系统;不并行(总两次,串行即可)。`image` 字符串不做归一化(不要把 `nginx` 自动改成 `docker.io/library/nginx:latest`)。

### R6. 严重度评分 — compose_status

- `state`:任一服务 `Health.Status == "unhealthy"` → `crit`;任一服务 `State == "restarting"` 或 `RestartCount > 5` 或 `State == "exited" with ExitCode != 0` → `warn`;所有 `state == "running"` 且无 health 告警 → `ok`;`services_total == 0` → `warn`(空栈)。
- summary 不分多个 domain;`summary.state` 是聚合 max。

### R7. 严重度评分 — image_audit

- `state`:存在 layer `Size > threshold_mb * 1024 * 1024`(默认 200MB)→ `warn`;layer CreatedBy 匹配 waste pattern(R8)→ `warn`;`Config.User` 为空 / "root" / "0" → info finding,不升级 summary;以上都没有 → `ok`。
- v1.0 不引入 `crit` 等级(没有 image 级别的不可恢复信号)。

### R8. findings 列表

compose_status kind 枚举:
- `unhealthy`:value=service_name + health log 摘要(crit)。
- `restart_loop`:value=service_name + restart_count(warn)。
- `exited_nonzero`:value=service_name + exit_code(warn)。
- `service_not_found`:value=requested_service(warn,只在 `--service` 时出现)。
- `empty_stack`:value=null(warn,services_total==0)。

image_audit kind 枚举:
- `large_layer`:value=layer 摘要 `{index, size_mb, cmd_head}`(warn,size > threshold)。
- `apt_cache_left`:value=layer index(warn,layer cmd 含 `apt-get install` 但无 `rm -rf /var/lib/apt/lists`)。
- `pip_cache_left`:value=layer index(warn,layer cmd 含 `pip install` 但无 `--no-cache-dir`)。
- `npm_cache_left`:value=layer index(warn,layer cmd 含 `npm install` 但无 `npm cache clean`)。
- `running_as_root`:value=user_string 或 `(default root)`(info)。

waste pattern 是文本子串扫描(不要正则),case-sensitive 命中即标。

### R9. runtime
- `image_audit.py` 提供 `--runtime docker|podman`(默认 `docker`),与 inspect_container 一致。
- `compose_status.py` v1.0 **不**提供 `--runtime` —— `docker compose` 与 `podman-compose` 命令不兼容;限制 docker 一种。文档明示 podman 在 v1.0 不支持 compose。

### R10. 失败语义
- compose_status list 失败(目录不存在 / `docker compose` 命令不存在):envelope `success=false`,`data.failure_class = "remote_error"`(远端 cd 失败 / docker 缺失)或 `"precondition"`(ssh_execute 自身缺失)。**不**做 per-service 调用。
- compose_status per-service inspect 部分失败:不影响顶层 `success`(只要 list 成功);失败的 service 在 `data.services[i]` 上标 `inspect_error: true`,顶层 `failure_class` 不设。
- image_audit history 失败(镜像不存在):envelope `success=false`,`data.failure_class = "remote_error"`(local 模式下 docker 报 No such image)或 `"precondition"`(ssh_execute 自身缺失)。
- image_audit inspect 失败但 history 成功:`data.failure_class = "inspect_unavailable"`,`data.layers` 仍嵌进;summary 用 history 数据评分,不评 USER。

### R11. 测试
- Unit tests(mock `subprocess.run`):覆盖 — host 路由(local/remote)、parser 容错(空 stdout、JSON 数组 vs JSON Lines)、severity 评分各阈值、findings 生成、failure_class 6 个分支、JSON envelope 形状、argv 错误。
- 集成 tests:`@pytest.mark.live_ssh` 沿用 marker;`compose_status` 用 `tmp_path` 真实生成最小 `compose.yml` 跑 local route smoke;`image_audit` 用 `busybox:latest` 跑 local route smoke(沿 `inspect_container` 的本机 docker 依赖检测模式,本机有 docker 就跑,否则模块级 skip)。
- 测试目标数:本刀新增 ≈ 50-70 unit tests,总数应达到 ≈ 340-360,默认 `pytest -m "not live_ssh"` 全绿。

### R12. 质量门
- `ruff check <touched>` 0 violations。
- 全仓 `ruff check` 不退步(当前 baseline:8 errors,见第三刀)。
- `pytest -m "not live_ssh"` 默认套件全绿。
- 不修改 SKILL.md(除非示例命令格式失配)。
- 不修改 ssh-core / ssh_execute / remote-debug / inspect_container 任何代码。

## Acceptance Criteria

**compose_status:**

- [x] AC1. `compose_status.py local <dir> --json`:本地 `subprocess.run(["docker", "compose", ...], cwd=<dir>)` 直跑,不走 ssh_execute。
- [x] AC2. `compose_status.py pai <dir> --json`:透过 `python ssh_execute.py pai "cd ... && docker compose ps --format json --all" --json` shell-out。
- [x] AC3. `compose_status.py local /no/such --json`:envelope `success=false`、`data.failure_class="remote_error"`,**不**抛异常。
- [x] AC4. `compose_status.py local <dir> --json`:全部服务 `running` 时 `data.summary.state == "ok"`、`data.findings` 不含 crit/warn。
- [x] AC5. `compose_status.py local <dir> --json`:存在 `unhealthy` 服务时 `data.summary.state == "crit"`,findings 含 `kind="unhealthy"`。
- [x] AC6. `compose_status.py local <dir> --json`:存在 `restarting` 或 `restart_count > 5` 服务时 `data.summary.state == "warn"`。
- [x] AC7. `compose_status.py local <dir> --service foo --json` 且 `foo` 不在 ps 输出中:`data.failure_class="service_not_found"`,exit 1,findings 含 `kind="service_not_found"`。
- [x] AC8. compose ps stdout 同时支持 JSON Lines(docker v2+)与 JSON 数组(早期版本)两种格式。
- [x] AC9. per-service inspect 失败不影响顶层 success;失败 service 在 `data.services[i]` 标 `inspect_error: true`。

**image_audit:**

- [x] AC10. `image_audit.py local <image> --json`:本地直跑 `docker history` + `docker inspect`,不走 ssh_execute。
- [x] AC11. `image_audit.py pai <image> --json`:两次 docker 调用透过 ssh_execute shell-out。
- [x] AC12. `image_audit.py local nonexistent:tag --json`:envelope `success=false`、`data.failure_class="remote_error"`。
- [x] AC13. `image_audit.py local <image> --threshold-mb 50`:layer `Size > 50 * 1024 * 1024` 时 `data.summary.state == "warn"`,findings 含 `kind="large_layer"`。
- [x] AC14. layer CreatedBy 含 `apt-get install` 但不含 `rm -rf /var/lib/apt/lists`:findings 含 `kind="apt_cache_left"`,summary `warn`。
- [x] AC15. layer CreatedBy 含 `pip install` 但不含 `--no-cache-dir`:findings 含 `kind="pip_cache_left"`,summary `warn`。
- [x] AC16. `Config.User` 为空/"root"/"0":findings 含 `kind="running_as_root"` 但 severity `info`,**不**升级 summary。
- [x] AC17. `--runtime podman`:本地与远端命令都用 `podman` 而非 `docker`。
- [x] AC18. image_audit inspect 失败但 history 成功:`failure_class="inspect_unavailable"`,`data.layers` 仍嵌入,根据 layers 评 summary。

**共同:**

- [x] AC19. `compose_status.py` / `image_audit.py` 不传 host / 第二参 → argparse 自动 exit 2。
- [x] AC20. 两脚本不 `--json` 时输出 human-readable 摘要到 stdout,无 prefix。
- [x] AC21. `grep -R "import lib" plugins/docker-quick/` 仍返回 0(inline helpers 约定)。
- [x] AC22. `ruff check plugins/docker-quick/skills/docker-quick/scripts/compose_status.py plugins/docker-quick/skills/docker-quick/scripts/image_audit.py tests/test_compose_status_*.py tests/test_image_audit_*.py` 0 violations。
- [x] AC23. `pytest -m "not live_ssh"` 全绿(实际 399 tests,优于预估 340-360)。
- [x] AC24. 顶层 envelope 形状匹配 SKILL.md L82-101 contract(`data.summary` / `data.findings` / `data.raw` 等字段存在)。
- [x] AC25. v0.2 argparse 命令名 / positional 数不变;`--help` 仍展示 v0.2 描述的 surface(可加新 flag)。

## Non-Goals

- 不实现 `compose.yml` 静态解析(depends_on cycles / volume drift / healthcheck cmd missing 等都留 v1.1)。
- 不实现 端口冲突探测(需要远端 `ss -tlnp`,留 v1.1)。
- 不并行 per-service inspect(N 次串行;v1.0 优先正确性,v1.1 再加 ThreadPoolExecutor)。
- 不实现 podman-compose 兼容(`compose_status` 限 docker)。
- 不实际拉镜像扫文件系统(`image_audit` 不做 `docker save | tar -tvf`)。
- 不集成 trivy/grype 类安全扫描器。
- 不实现 multi-arch 镜像的 manifest 拆分。
- 不抽公共 lib(每 plugin 自带 inline helpers)。
- 不修改 SKILL.md(除非示例失效)。
- 不跑 live remote integration(暂无测试机);本机有 docker 跑 local route smoke,无 docker 模块级 skip。

## Notes

- 设计细节(docker compose ps 双格式解析、waste pattern 子串集、severity 阈值、failure 传播)见 `design.md`。
- 执行顺序、checklist、验证命令、commit 拆分见 `implement.md`。
- 任何打破 R2 的修改必须先回到本 PRD 加 ADR。
