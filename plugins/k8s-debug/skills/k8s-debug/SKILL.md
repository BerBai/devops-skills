---
name: k8s-debug
description: Kubernetes diagnostic playbook. Use when the user reports Pod/Deployment/Service/Namespace issues — CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending pods, failed Helm releases, broken Ingress, or unhealthy nodes. Read-only diagnosis via kubectl/helm; remote hosts go through ssh-core.
---

# k8s-debug

K8s 层的应用诊断 playbook。**只读**：跑 `kubectl describe / logs / get / events`、Helm `status`、节点健康指标。**不**做 `apply / scale / delete / rollout` —— 那些写操作请用 `ssh-guarded` 或人工 `kubectl`。

## When to use

触发场景：
- 用户提到 K8s / kubectl / Pod / Deployment / Service / Namespace / Ingress / Helm
- 看到症状：CrashLoopBackOff / OOMKilled / ImagePullBackOff / Pending / Evicted / ContainerCreating 卡住
- "为什么 Pod 不起来" / "Pod 跑着跑着就死" / "节点 NotReady"
- Helm release 状态异常

Skip when:
- 用户问的是远端**主机本身**（disk/load/process/network） → 用 `remote-debug`
- 用户问的是**容器**而不是 Pod（裸 docker，没有 K8s 上下文）→ 用 `docker-quick`
- 用户要执行 `kubectl apply / scale / rollout` → 这些是写操作，本 plugin 不做

## Decision tree

```
Got a K8s symptom?
│
├─ 命名空间整体不健康？
│   └─ check_namespace.py <host> <namespace>
│        → Pod 总数、运行/失败/Pending 分布、failed events、最近重启
│
├─ 某个 Pod 状态异常？
│   └─ diagnose_pod.py <host> <namespace> <pod>
│        → kubectl describe + events + last container logs + restart count
│
├─ 集群级问题（节点 NotReady / API 慢 / 控制面）？
│   └─ cluster_health.py <host>
│        → nodes / control-plane pods / kube-system events
│
└─ Helm release 状态？
    └─ helm_status.py <host> <namespace> <release>
         → helm status + 关联资源 + 失败 hook
```

## Host model

第一参 `<host>` 是 ssh-core 的 alias（含 `local`）：
- `local` → `subprocess.run(["kubectl", ...])` 直接跳本机的 `kubectl`，用本机 `KUBECONFIG`
- 其他 alias → 通过 `ssh-core/ssh_execute.py <alias> "kubectl ..."` 在远端跑

这意味着你可以在你本机调试集群（如平常那样），也可以让"远端跳板机"代为执行（在跳板机上有 `KUBECONFIG`、但你本机没有的常见场景）。

## Quick reference

```bash
# 命名空间健康
python scripts/check_namespace.py local production --json
python scripts/check_namespace.py bastion-prod production --since 30min

# 单 Pod 诊断
python scripts/diagnose_pod.py local production payment-svc-abc123
python scripts/diagnose_pod.py bastion-prod production worker-7

# 集群级
python scripts/cluster_health.py local --json

# Helm
python scripts/helm_status.py local production stripe-checker
```

## Output contract

所有脚本支持 `--json`，统一输出：

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "<human-readable summary>",
  "stderr": "",
  "data": {
    "host": "local",
    "namespace": "production",
    "summary": { "pods": "warn", "events": "ok", "deployments": "ok" },
    "findings": [
      {"severity": "warn", "kind": "Pod",
       "name": "payment-svc-abc123",
       "phase": "CrashLoopBackOff", "restart_count": 12,
       "hint": "last termination: OOMKilled, see references/common_issues.md#oomkilled"}
    ],
    "raw": { "..." : "..." }
  }
}
```

## References

- `references/common_issues.md` —— 症状 → 原因 → 诊断 → 修复 推荐顺序
- `references/pod_lifecycle.md` —— Pending / ContainerCreating / Running / Terminating 各阶段卡住的处理
- `references/networking.md` —— Service / Ingress / NetworkPolicy / DNS 排查

## What this skill is *not*

- 不替你 deploy / scale / apply / restart —— 写操作请用 `ssh-guarded` 提交 request
- 不是 K8s 安装/升级工具
- 不监控（订阅事件流）—— 一次性快照诊断
