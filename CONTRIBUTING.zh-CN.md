# 为 devops-skills 贡献代码

[English](./CONTRIBUTING.md) | 简体中文

感谢你考虑参与贡献。本文档讲清楚仓库结构，以及把项目从脚手架推到 v1.0 还要补哪些东西。

## 仓库布局

```
devops-skills/
├── .claude-plugin/marketplace.json   # marketplace 入口 —— 列出所有 plugin
├── plugins/
│   ├── ssh-core/                     # 传输层
│   │   ├── .claude-plugin/plugin.json
│   │   └── skills/ssh-core/
│   │       ├── SKILL.md              # workflow + 速查表（skill 入口）
│   │       ├── references/*.md       # 按需加载的深度参考
│   │       └── scripts/              # Claude Code 调用的 Python CLI
│   ├── ssh-guarded/                  # 安全层
│   ├── remote-debug/                 # 主机调试
│   ├── k8s-debug/                    # Kubernetes 诊断
│   ├── docker-quick/                 # Docker / 容器诊断
│   ├── log-aggregator/               # 多源日志聚合
│   ├── iac-state/                    # Terraform / OpenTofu state
│   └── cicd-debug/                   # GitHub Actions / GitLab CI
└── tests/                            # pytest 套件 + manifest linter
```

每个 plugin **自我封闭** —— 它的 `skills/<name>/scripts/` 只在有明确文档说明的情况下才跨边界。v0.2 诊断 plugin（`k8s-debug` / `docker-quick` / `log-aggregator` / `iac-state` / `cicd-debug`）依赖 `ssh-core` 提供远端命令执行，但**不**直接 import 其 Python 模块；而是 shell out 调用用户已安装的 `ssh-core` CLI。`ssh-guarded` 同样如此。

## Skill 编写约定

1. **`SKILL.md` 服务于分诊，不是深度。** 决策树、硬性检查点、一段话的命令速查。其余内容下沉到 `references/`。
2. **`plugin.json` 里的 `description` 决定 Claude Code 是否选中本 skill。** 用动词、用具体关键词开头；避免 "tool" / "helper" 这类泛词。中英文双语触发词都欢迎 —— 多语用户受益。
3. **硬性检查点（hard checkpoints）。** 如果某个 workflow 有不可逆副作用，把检查点序列写进 `SKILL.md` 并拒绝偏离。
4. **JSON 输出契约。** 每个脚本必须支持 `--json` 并输出 `{"success": bool, "exit_code": int, "stdout": "...", "stderr": "...", "data": {...}}`。这是让 Claude Code 能链式调用而无需解析自然语言的关键。
5. **`subprocess.run(list, ...)`，永不传 shell 字符串。** 不要例外，哪怕输入"可信"。测试用例里专门塞了 `;`、`&&` 和 `` ` ``。
6. **skill 输出不要 emoji**（除非用户显式要求）。Skill markdown 同样保持无 emoji。

## 从脚手架到 v1.0 的路线图

### `ssh-core`
- [ ] `ssh_daemon.py` —— 本地 TCP daemon、长度前缀 JSON 协议、60s 心跳、30 分钟空闲退出
- [ ] `ssh_execute.py` —— daemon 感知的入口；密钥认证走原生 ssh，密码认证降级 paramiko
- [ ] `ssh_tunnel.py` —— 端口转发守护进程，端口池 10000–20000，状态文件按 `md5(alias)` 命名
- [ ] `ssh_cluster.py` —— ThreadPoolExecutor 广播，支持 tag / environment 过滤
- [ ] `ssh_server_transfer.py` —— direct / stream / hybrid / auto 四种模式
- [ ] `ssh_config_manager.py` —— `~/.ssh/config` 的 CRUD（带注释行元数据）
- [ ] 所有传输路径 Windows 端强制 `MSYS_NO_PATHCONV=1`

### `ssh-guarded`
- [ ] `request_command.py` / `request_upload.py` / `request_mkdir.py` / `request_delete.py` —— 在 `reports/requests/` 下生成 JSON 请求工件
- [ ] `run_request.py` —— 审核后执行，无 `--execute` 时只校验
- [ ] `exec_detached.py` —— nohup + 本地 job 清单，含 `status` / `tail-log` 子命令
- [ ] `scan_software.py` —— 缓存 `python/cuda/gcc/cmake/kubectl/terraform/docker/...` 各主机的安装版本
- [ ] Redact 模块 —— 所有字符串输出路径默认脱敏

### `remote-debug`
- [ ] `diagnose_host.py` —— uptime / load / disk / mem / net / 僵尸进程检查，附严重度打分
- [ ] `tail_log.py` —— 多主机 log tail 带前缀
- [ ] `port_check.py` —— TCP 可达性矩阵
- [ ] `compare_across_hosts.py` —— N 台机器之间 diff 同名文件
- [ ] 症状 → 根因 参考（`common_issues.md`）
- [ ] SEV1–SEV4 应急响应参考

### `k8s-debug`
- [ ] `check_namespace.py` —— 命名空间健康快照（pods/events/deployments/services/PVCs）
- [ ] `diagnose_pod.py` —— 单 Pod 深挖（describe + logs + previous-logs + events）
- [ ] `cluster_health.py` —— nodes / control-plane / kube-system events
- [ ] `helm_status.py` —— release 状态 + 卡住 hook job + `--list-pending` 全集群
- [ ] Host 传输：支持 `local`（subprocess）和远端（经 ssh-core）

### `docker-quick`
- [ ] `inspect_container.py` —— state + config + health log + 近期 logs，含退出码分类
- [ ] `image_audit.py` —— 每层 size + 浪费检测（apt/pip/npm cache、.git 进镜像、root 用户）
- [ ] `compose_status.py` —— Compose 栈健康 + depends_on / healthcheck / 端口冲突检测

### `log-aggregator`
- [ ] `tail_multi.py` —— 多源 tail，含时间归一化 + 噪音过滤 + level 过滤 + 速率抑制
- [ ] `correlate.py` —— anchor + window 跨源关联，含 root-cause hint
- [ ] `grep_across_sources.py` —— N 源 pattern 搜索，summary / raw / json 三种输出
- [ ] Source spec 解析器（支持 `journal://`、`docker://`、`kube://`、`file://` 及 brace/glob 展开）
- [ ] 启动时时钟漂移探测；每源输出 `clock_skew_ms` 元数据

### `iac-state`
- [ ] `state_inspect.py` —— backend + size + lock 状态 + workspace 列表
- [ ] `drift_check.py` —— `plan -detailed-exitcode` 解析，逐资源分类（no-op/update/replace/destroy）
- [ ] `module_validate.py` —— module 审计 checklist，可选 `--target-version` 升级 diff
- [ ] 工具自动检测：有 `tofu` 用 `tofu`，否则 `terraform`

### `cicd-debug`
- [ ] `pipeline_analyzer.py` —— 单次 run 分析 + 与参照 run 的 diff + cache/actions/runner 摘要
- [ ] `runner_check.py` —— runner 池健康（online/offline/busy/labels）、队列深度、p95 等待时长
- [ ] `secret_scope_audit.py` —— secret 可见性图 + 风险标记（fork-PR 暴露、长效凭据等）
- [ ] Provider 鉴权检测：`gh auth status` / `glab auth status` 没登录直接 fail fast

## 测试

```bash
pytest tests/
```

CI 最低要求：

1. JSON-lint 每个 `marketplace.json` 和 `plugin.json`
2. 检查每个 `SKILL.md` 是否含必备字段（`description`、`When to use`、`Workflow`）
3. 对每个脚本 smoke 测 `--help`（所有脚本必须支持）
