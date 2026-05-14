# Runner debug — self-hosted runners, labels, concurrency

GitHub Actions 与 GitLab CI 的 runner 模型不同，但常见坑相似。本文按 provider 分两段。

---

## GitHub Actions Runners

### 三种来源
- **GitHub-hosted**：标 `ubuntu-latest` / `windows-latest` / `macos-latest`。无配置即用，每月 free minutes 配额。
- **Self-hosted (classic)**：你自己装 runner agent，挂 `[self-hosted, <labels>...]`。
- **ARC (Actions Runner Controller)**：在 K8s 上跑，按需启停，ephemeral，推荐生产用法。

### 注册流程（self-hosted classic）

```bash
# 1. repo / org / enterprise 三层之一拿 token
gh api repos/<owner>/<repo>/actions/runners/registration-token --jq .token

# 2. 在 runner host：
./config.sh --url https://github.com/<owner>/<repo> --token <token> \
  --labels "self-hosted,Linux,X64,gpu" \
  --ephemeral                # 推荐：跑完一个 job 就退出

# 3. 装成 systemd：
sudo ./svc.sh install
sudo ./svc.sh start

# 4. 校验
gh api orgs/<org>/actions/runners --jq '.runners[] | {name, status, busy}'
```

### 常见状态判读

| status | busy | 含义 |
|---|---|---|
| `online` | false | 健康，等活 |
| `online` | true | 正在跑 job |
| `offline` | * | 失联（agent 挂、网断、机器关） |

**没有 `online + busy` 的某 label = 该 label 的 job 必排队**。

### Label 匹配语义

```yaml
runs-on: [self-hosted, gpu, x64]
```

要求 runner **同时**挂 `self-hosted` + `gpu` + `x64` 三个 label，且**两组都满足**才能接。

最常见误解：以为 `runs-on: gpu` 就够。实际还要 `self-hosted` 才能筛掉 GitHub-hosted。

### Ephemeral runner

非 ephemeral runner 跑完 job 不退出，下次接同样的活会**继承上一个 job 的状态**：
- `/tmp` 里旧文件
- 环境变量
- docker 拉过的镜像（这点是优点）
- workspace 目录（潜在 secret 残留）

强烈建议 ephemeral：每个 job 在干净环境跑，安全且可重复。

```bash
./config.sh ... --ephemeral
```

配合 ARC，K8s 自动起新 Pod 接活，跑完 Pod 销毁。

### 排查活没人接

```bash
# 1. 看 queued 的活
gh run list --status queued --limit 20
# 2. 看 runner 池
gh api orgs/<org>/actions/runners --jq '.runners[] | {name, status, busy, labels: [.labels[].name]}'
# 3. label 匹配？
gh api repos/<owner>/<repo>/actions/runs/<id>/jobs --jq '.jobs[] | {name, runs_on: .labels}'
# 4. concurrency 限制？
gh api repos/<owner>/<repo>/actions/permissions --jq .
```

### Runner 日志位置

```bash
# 默认安装路径
ls ~/actions-runner/_diag/
# Runner 主进程
tail -100 ~/actions-runner/_diag/Runner_*.log
# Worker（每个 job 一个）
tail -100 ~/actions-runner/_diag/Worker_*.log
```

---

## GitLab CI Runners

### 三种 executor

- **shared runner**：GitLab.com 提供（私有部署可关），有月度 minute 配额
- **specific runner**：注册到单个 project
- **group runner**：注册到 group，组内所有 project 共享

每种 runner 选一个 `executor`：

| Executor | 适合 | 坑 |
|---|---|---|
| `shell` | 简单脚本 | 共享 host 环境，安全风险 |
| `docker` | 大多场景 | 镜像拉取、docker socket 处理 |
| `docker+machine` | autoscale 到 EC2 等 | docker-machine 已弃维护，迁 fleeting |
| `kubernetes` | K8s 集群运行 | 推荐生产 |
| `instance` | bare-metal 注册的特定机 | 类似 GH self-hosted |

### 注册流程

```bash
# 1. 拿 registration token（project / group / instance settings）
# 2. 在 runner host
sudo gitlab-runner register \
  --url https://gitlab.com \
  --registration-token <token> \
  --description "ci-runner-1" \
  --executor docker \
  --docker-image "alpine:latest" \
  --tag-list "linux,docker"

# 3. 启动
sudo systemctl enable --now gitlab-runner
```

### Tags 匹配（GitLab 的"label"）

```yaml
job_x:
  tags: [linux, docker]
```

GitLab 用 **tag**（不是 label）。逻辑 AND：runner 必须**同时**挂所有 tag 才被选中。

### Concurrency

`/etc/gitlab-runner/config.toml` 顶层：
```toml
concurrent = 4              # 这台 host 同时跑几个 job
check_interval = 0          # poll GitLab 间隔
```

每个 `[[runners]]` 段：
```toml
limit = 2                   # 这个 runner 同时跑几个
```

实际并发 = min(concurrent, sum(limit))。

### 排查队列

```bash
glab api projects/<project-id>/runners                    # 注册到 project 的 runner
glab api projects/<project-id>/pipelines/<id>/jobs       # job 状态
glab ci view --pipeline-id <id>                          # 可视化
# Runner 端
ssh <runner-host> "sudo gitlab-runner status"
ssh <runner-host> "sudo gitlab-runner verify"
ssh <runner-host> "sudo journalctl -u gitlab-runner --since '10 min ago'"
```

### Runner 日志

```bash
sudo journalctl -u gitlab-runner -f
sudo cat /var/log/gitlab-runner.log     # 视安装方式
```

---

## 通用建议

- **ephemeral 永远更好**。状态污染调试比性能差更难
- **监控 queue depth**：queued > N min 就告警
- **runner 数 < CI 需求** 时，开发体验糟糕；> 需求 ×3 时浪费钱。p95 队列时间 < 30s 是合理目标
- **不要在 runner 上存任何持久 secret**：通过 CI 注入，job 结束随 ephemeral 销毁
- **Linux runner 优先**：macOS / Windows runner 既贵又慢，仅平台特性 job 用
