# devops-skills

> 面向 [Claude Code](https://claude.com/claude-code) 的开源 DevOps 与跨设备调试技能集。

[English](./README.md) | 简体中文

八个可组合的 plugin，覆盖完整 DevOps 面 —— 传输、审计、主机调试、Kubernetes、Docker、日志、IaC、CI/CD。

| Plugin | 层 | 角色 |
|---|---|---|
| **`ssh-core`** | 传输 | 快车道 —— 字节安全命令执行、密钥/密码认证、交互会话、集群广播、端口隧道、多后端传输、sudo、重试与 SSH config CRUD |
| **`ssh-guarded`** | 安全 | 慢车道 —— request/execute 两阶段写、默认脱敏输出、workdir 沙箱 |
| **`remote-debug`** | 主机调试 | Playbook —— 症状决策树、应急响应、Linux 与网络诊断 |
| **`k8s-debug`** | K8s | Pod/Deployment/Service 诊断、CrashLoopBackOff/OOMKilled、Helm release 健康 |
| **`docker-quick`** | 容器 | 容器检查、退出码分析、镜像审计、Compose stack 健康 |
| **`log-aggregator`** | 日志 | 跨主机/容器/Pod 多源 tail + 按 trace-id / 时间窗关联 |
| **`iac-state`** | IaC | Terraform/OpenTofu state、漂移检测、module 审计 |
| **`cicd-debug`** | CI/CD | GitHub Actions / GitLab CI 流水线分析、runner 健康、secret 作用域审计 |

## 安装

```bash
# 在 Claude Code 内
/plugin marketplace add https://github.com/BerBai/devops-skills
/plugin install ssh-core@devops-skills
/plugin install ssh-guarded@devops-skills
/plugin install remote-debug@devops-skills
/plugin install k8s-debug@devops-skills
/plugin install docker-quick@devops-skills
/plugin install log-aggregator@devops-skills
/plugin install iac-state@devops-skills
/plugin install cicd-debug@devops-skills
```

可以只装其中任意子集。`ssh-core` 是其他 plugin 的唯一硬依赖（用于远端命令执行）。

## 设计原则

- **一个问题域一个 plugin**。窄而明确的 `description` 让 Claude Code 的 skill 匹配器更精准。
- **`ssh-core` 是远程连接子功能**。传输、认证、会话、隧道、文件传输与 `~/.ssh/config` 只在这里实现；其他 plugin 通过 shell out 复用它（ADR-005）。
- **默认只读诊断**。诊断类 plugin 默认只做检查；写操作走 `ssh-guarded`（带审计）或人工 CLI。
- **`<host>` 作为第一参**。每个诊断脚本第一参是 host；`local` 走本机，否则通过 `ssh-core` 转发。省略即视为 `local`。
- **两层文档**。`SKILL.md` 用于分诊（决策树 + 速查），深度内容下沉 `references/*.md` 按需加载。
- **`subprocess.run(list, ...)`，永不传 shell 字符串**。无例外。
- **JSON 输出契约**。每个脚本支持 `--json` 并输出 `{success, exit_code, stdout, stderr, data}`，能链式组合无需自然语言解析。`success` 表示动作已完成，而非"请求已派发"。
- **每个脚本都有 `--version`**。所有公开脚本都支持 `--version` / `-V`，输出来自该插件 `plugin.json` 的 `<script> (<plugin> <x.y.z>)`，"装的是哪一版"一条命令即可确认。
- **统一的 partial-fetch 语义**。任何一个源读不到，整次调用即判失败（`success: false` + 非 0 退出码），但已取到的数据仍放在 `data` 里返回 —— 绝不伪装成空结果的"成功"。被平台检查或 `--health-check` 主动跳过的主机不算失败：它列在 `data.skipped_hosts`，整次调用仍是 `success: true`。

## ssh-core 在哪里

诊断插件不靠猜：各 plugin 内的 `scripts/_ssh_core_locator.py` 按顺序解析。

1. `$DEVOPS_SSH_CORE_SCRIPTS` —— `.../ssh-core/scripts` 目录
2. `$DEVOPS_SSH_CORE_DIR` —— `.../ssh-core` skill 根目录
3. 仓库布局（调用方 plugin 旁边的 `plugins/ssh-core/...`）
4. 已安装的 agent 根目录（`~/.claude/plugins/`、`~/.dsh/skills/`、`~/.agents/skills/`、`~/.codex/skills/`）

把插件装到互不相邻的目录时，设置第一个变量即可；报错信息会告诉你它找过哪些路径。

## 文档

- [`docs/README.md`](./docs/README.md) —— 索引，以及 CI 实际会跑的本地命令
- [`docs/architecture.md`](./docs/architecture.md) —— 分层图、失败类、状态文件位置
- [`docs/offline-install.md`](./docs/offline-install.md) —— 离线包与安装器
- [`CHANGELOG.md`](./CHANGELOG.md) —— 发布历史与版本真源规则
- [`CLAIM-LEDGER.md`](./CLAIM-LEDGER.md) —— 每条承重断言及其证据
- [`SECURITY.md`](./SECURITY.md) —— 支持版本、报告方式、凭据模型

## 状态

8 个 plugin surface 均已实现，版本统一为 `1.0.0`。`ssh-core` `1.0.0`
包含独立版 1.5.0 功能集（[ADR-004](./docs/adr/ADR-004-ssh-core-1.5.0-sync.md)、
[ADR-005](./docs/adr/ADR-005-ssh-core-boundary.md)）：`doctor.py` 能力探针、
`ssh_slice.py` 窗口读日志、`run.cmd`/`run.sh` 启动器、`--output-file`、
`--script-file`、`--allow-exit` 与 `user_required` 失败类。其他 plugin 仍只通过
CLI + JSON 与它交互。Tags：`v0.2.0`（2026-05-21）是首个公开 **scaffold** 版本；`v1.0.0` 打在
本次发布上。版本真源规则见 [`CHANGELOG.md`](./CHANGELOG.md)。

路线图见 [`CONTRIBUTING.zh-CN.md`](./CONTRIBUTING.zh-CN.md)。

## License

MIT，详见 [`LICENSE`](./LICENSE)。
