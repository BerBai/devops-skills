# Pod lifecycle — when does Kubernetes get stuck?

K8s 的 Pod 状态机比看起来复杂。每一步都可能卡住，原因不一样。本文按生命周期阶段拆解。

```
Pending → ContainerCreating → Running → Terminating
   ↑          ↑                   ↑          ↑
 调度卡       存储/网络卡         应用问题   清理卡
```

## 阶段 1: Pending

调度器还没把 Pod 绑定到节点。**详细见 `common_issues.md#pending`**。

最常见原因（几乎涵盖 90%）：

| 原因 | 一行排查 |
|---|---|
| 资源不够 | `kubectl describe pod <pod>` Events 末段 |
| 没匹配节点（selector / taint） | `kubectl get nodes --show-labels` + 比对 |
| PVC pending | `kubectl get pvc -n <ns>` |
| ResourceQuota 阻挡 | `kubectl describe quota -n <ns>` |

**关键命令**：
```bash
kubectl get pod <pod> -n <ns> -o wide        # 看 NODE 列是否空
kubectl describe pod <pod> -n <ns> | tail -30
```

## 阶段 2: ContainerCreating

Pod 已被调度到节点，节点上的 kubelet 正在准备容器（拉镜像、挂卷、配网络）。卡这里通常是节点侧问题：

| 卡点 | 表征 | 排查 |
|---|---|---|
| 拉镜像 | Events 里 `Pulling image` 一直没 `Successfully pulled` | `crictl pull <image>` 在节点上手动 |
| 挂卷失败 | Events `FailedMount` / `Unable to attach or mount` | 看 PV / PVC / StorageClass / CSI driver |
| CNI 失败 | Events `failed to setup network` | `journalctl -u kubelet` + CNI plugin 日志 |
| Secret 挂载错 | `MountVolume.SetUp failed for volume "..." : secret "..." not found` | `kubectl get secret -n <ns>` |
| 镜像层超大 | 长时间无进展但无报错 | 看节点 `du -sh /var/lib/containerd` |

**关键命令**：
```bash
kubectl describe pod <pod> -n <ns>           # Events 段是金矿
ssh <node> "journalctl -u kubelet --since 5min | grep <pod>"
ssh <node> "crictl ps -a | grep <pod>"
ssh <node> "crictl logs <container-id>"
```

## 阶段 3: Running

应用启动且不退出。这里"卡住"的形态多种多样：

### 3a. Running 但 Ready=false

readiness probe 没通过。

```bash
kubectl describe pod <pod> -n <ns> | grep -A5 "Readiness"
kubectl logs <pod> -n <ns> --tail=50
```

常见原因：
- readinessProbe 路径 / 端口写错
- 应用启动慢，readinessProbe 太早开始
- 应用依赖（DB / 配置中心）没准备好

**修复方向**：调大 `initialDelaySeconds`，或拆出 `startupProbe`。

### 3b. Running 但有 restart

容器死过又被 restart policy 拉起。

```bash
kubectl get pod <pod> -n <ns>                # RESTARTS 列
kubectl logs <pod> -n <ns> --previous        # 死前日志
kubectl describe pod <pod> -n <ns> | grep -A10 "Last State"
```

→ 进入 `common_issues.md#crashloopbackoff` 流程。

### 3c. Running 性能异常（不重启但行为不对）

不属于"卡"，是应用层问题。本 plugin 给不出结构化诊断，可看：
- `kubectl top pod <pod>` —— CPU / mem 使用
- `kubectl exec` 进 Pod 用应用自己的 profiler
- 业务 log

## 阶段 4: Terminating

Pod 被删除时进入。正常应在 `terminationGracePeriodSeconds`（默认 30s）内消失。

**卡 Terminating 的典型原因**：

| 原因 | 排查 | 修复 |
|---|---|---|
| finalizer 没释放 | `kubectl get pod <pod> -o yaml \| grep finalizers` | 找到对应控制器 / 手动 `kubectl patch pod <pod> -p '{"metadata":{"finalizers":[]}}' --type=merge` |
| 进程不响应 SIGTERM | `kubectl exec` 看进程是否还在 | 应用要 handle SIGTERM；或调小 graceperiod |
| volume detach 失败 | Events 里 `Multi-Attach error` 等 | 看 PV 状态、CSI driver |
| 节点 NotReady | 节点本身挂了 | `kubectl get nodes` |

**强制删除**（慎用，可能留下脏状态）：
```bash
kubectl delete pod <pod> -n <ns> --grace-period=0 --force
```

## 完整的 Reason 字典

`kubectl describe pod` 的 Events 段里你会看到一堆 `Reason`，记几个最常见的：

| Reason | 含义 |
|---|---|
| `Scheduled` | 已被分配到节点（→ ContainerCreating） |
| `Pulling` / `Pulled` | 镜像拉取中 / 完成 |
| `Created` | 容器对象创建好 |
| `Started` | 容器进程启动 |
| `Killing` | kubelet 主动杀（probe failed / preempt / shutdown） |
| `BackOff` | 容器死了，等指数退避后再试 |
| `Unhealthy` | probe 失败（liveness / readiness） |
| `FailedScheduling` | 调度失败（Pending 阶段） |
| `FailedMount` | 卷挂载失败（ContainerCreating 阶段） |
| `Evicted` | 节点压力下被 kubelet 驱逐 |
| `NodeNotReady` | 节点失联 |
| `Preempted` | 高优先级 Pod 抢占了资源 |
