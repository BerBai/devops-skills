# devops-skills

> 面向 [Claude Code](https://claude.com/claude-code) 的开源 DevOps 与跨设备调试技能集。

[English](./README.md) | 简体中文

八个可组合的 plugin，覆盖完整 DevOps 面 —— 传输、审计、主机调试、Kubernetes、Docker、日志、IaC、CI/CD。

| Plugin | 层 | 角色 |
|---|---|---|
| **`ssh-core`** | 传输 | 快车道 —— 长连接守护进程、集群广播、端口隧道、服务器间直传 |
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
- **v0.2 默认只读诊断**。新增 5 个 plugin（k8s/docker/log/iac/cicd）只做检查；写操作走 `ssh-guarded`（带审计）或人工 CLI。
- **`<host>` 作为第一参**。所有 v0.2 诊断脚本第一参都是 host alias；`local` 走本机，否则通过 `ssh-core` 转发。
- **两层文档**。`SKILL.md` 用于分诊（决策树 + 速查），深度内容下沉 `references/*.md` 按需加载。
- **`subprocess.run(list, ...)`，永不传 shell 字符串**。无例外。
- **JSON 输出契约**。每个脚本支持 `--json` 并输出 `{success, exit_code, stdout, stderr, data}`，能链式组合无需自然语言解析。

## 状态

`v0.2.0` —— 8 个 plugin 全部 scaffold 完成。Manifests、SKILL.md、references、Python 脚本桩（全部通过 `--help` smoke）就位。真实实现留到 v1.0，见 [`CONTRIBUTING.zh-CN.md`](./CONTRIBUTING.zh-CN.md) 的路线图。

## License

MIT，详见 [`LICENSE`](./LICENSE)。
