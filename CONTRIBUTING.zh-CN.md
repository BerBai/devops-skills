# 为 devops-skills 贡献代码

[English](./CONTRIBUTING.md) | 简体中文

感谢你考虑参与贡献。本文档讲清楚仓库结构、已发布的 V1 状态，以及当前 V1.1 路线图。

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

每个 plugin **自我封闭** —— 它的 `skills/<name>/scripts/` 只在有明确文档说明的情况下才跨边界。诊断 plugin（`remote-debug` / `k8s-debug` / `docker-quick` / `log-aggregator` / `iac-state` / `cicd-debug`）依赖 `ssh-core` 提供远端命令执行，但**不**直接 import 其 Python 模块；而是 shell out 调用用户已安装的 `ssh-core` CLI。`ssh-guarded` 同样如此。

## Skill 编写约定

1. **`SKILL.md` 服务于分诊，不是深度。** 决策树、硬性检查点、一段话的命令速查。其余内容下沉到 `references/`。
2. **`plugin.json` 里的 `description` 决定 Claude Code 是否选中本 skill。** 用动词、用具体关键词开头；避免 "tool" / "helper" 这类泛词。中英文双语触发词都欢迎 —— 多语用户受益。
3. **硬性检查点（hard checkpoints）。** 如果某个 workflow 有不可逆副作用，把检查点序列写进 `SKILL.md` 并拒绝偏离。
4. **JSON 输出契约。** 每个脚本必须支持 `--json` 并输出 `{"success": bool, "exit_code": int, "stdout": "...", "stderr": "...", "data": {...}}`。这是让 Claude Code 能链式调用而无需解析自然语言的关键。
5. **`subprocess.run(list, ...)`，永不传 shell 字符串。** 不要例外，哪怕输入"可信"。测试用例里专门塞了 `;`、`&&` 和 `` ` ``。
6. **skill 输出不要 emoji**（除非用户显式要求）。Skill markdown 同样保持无 emoji。

## V1 状态

按已接受的 rollout 口径，V1 已完成：

- 8 个 plugin 的 V1 CLI surface 均已发布。
- 每个脚本都支持 `--help`；每个运行型脚本都支持 JSON 输出契约。
- 已发布脚本具备 unit 覆盖；依赖环境的 integration 测试保持 skip-safe。
- `ssh_daemon.py` 依据 ADR-001 A2 有意延后到 V1.1。V1 通过 `ssh_execute.py` 使用原生 OpenSSH subprocess；没有已发布的 V1 代码调用 daemon stub。

V1 脚本清单：

| Plugin | V1 状态 |
|---|---|
| `ssh-core` | `ssh_execute`、`ssh_cluster`、`ssh_tunnel`、`ssh_server_transfer`、`ssh_config_manager`、`ssh_upload`、`ssh_download`、`deploy_pubkey` 已发布；`ssh_daemon.py` 延后到 V1.1。 |
| `ssh-guarded` | request drafters、`run_request`、`exec_detached`、`scan_software`、`redact_check` 已发布。 |
| `remote-debug` | `diagnose_host`、`tail_log`、`port_check`、`compare_across_hosts` 已发布。 |
| `k8s-debug` | `check_namespace`、`diagnose_pod`、`cluster_health`、`helm_status` 已发布。 |
| `docker-quick` | `inspect_container`、`image_audit`、`compose_status` 已发布。 |
| `log-aggregator` | `tail_multi`、`correlate`、`grep_across_sources` 已发布。 |
| `iac-state` | `state_inspect`、`drift_check`、`module_validate` 已发布。 |
| `cicd-debug` | `pipeline_analyzer`、`runner_check`、`secret_scope_audit` 已发布。 |

## V1.1 路线图

优先推进能解锁多个后续能力的传输层基础工作：

1. `ssh-core` 连接复用：先评估 OpenSSH ControlMaster，再决定 `ssh_daemon.py` 是保留为自定义 daemon，还是变成兼容包装。
2. `ssh_server_transfer.py`：实现真正的 `direct` / `auto` / `hybrid` 模式，以及 `--use-rsync`。
3. `ssh_tunnel.py`：实现 `--idle-timeout`，并在连接复用决策后重新评估 Windows 支持。
4. 长时间日志 follow：改进 `tail_multi.py`、`tail_log.py` 等 follow / reconnect 工作流。
5. 脱敏强化：多行 private key 缓冲、默认 catalog 导出、可选 regex timeout 防护。
6. 插件级增强：CI secret-scope 深层风险标记、request upload-root 可配置，以及其他已记录的 V1.1 follow-up。

## 环境准备

本项目使用 [uv](https://github.com/astral-sh/uv) 管理依赖。clone 之后执行：

```bash
uv sync --all-extras
```

这会创建 `.venv/`，并装好 `pytest`、`ruff`、`mypy` —— 也就是下面测试和 lint 章节假定能直接在 `PATH` 里找到的工具。

## 测试

```bash
pytest tests/
```

CI 最低要求：

1. JSON-lint 每个 `marketplace.json` 和 `plugin.json`
2. 检查每个 `SKILL.md` 是否含必备字段（`description`、`When to use`、`Workflow`）
3. 对每个脚本 smoke 测 `--help`（所有脚本必须支持）
