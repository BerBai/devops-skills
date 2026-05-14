---
name: docker-quick
description: Docker / container diagnostic playbook. Use when the user reports container issues — Exited 137, OOM, restart loops, image bloat, layer cache misses, Compose service unhealthy, port already in use, mount failures. Read-only inspect via docker/podman/compose; remote hosts go through ssh-core.
---

# docker-quick

容器层快速诊断。**只读**：跑 `docker inspect / logs / stats / ps`、镜像层分析、Compose 状态检查。**不**做 `run / restart / rm / build / push` —— 写操作请用 `ssh-guarded` 或人工。

## When to use

触发场景：
- 用户提到 docker / podman / docker-compose / container / image / Dockerfile / 容器
- 看到症状：Exited (137 / 139 / 1 / 255)、Restarting、unhealthy、port already in use、no space left on device
- 镜像太大、构建太慢、layer cache 不命中、push 慢
- Compose 起不来 / 某个 service unhealthy

Skip when:
- 容器在 **K8s** 里跑（Pod / kubectl） → 用 `k8s-debug`
- 问题是宿主机本身（disk / load / 网络） → 用 `remote-debug`
- 用户要 `docker run / restart / rm / build` 等写操作 → 走 `ssh-guarded`

## Decision tree

```
容器症状？
│
├─ 容器没起来 / 起来就死 ───── inspect_container.py <host> <name|id>
│                              → State + ExitCode + Health + RestartCount
│                              → Mounts / Env / Cmd / Entrypoint 三件套验真
│                              → 最后 N 行 logs
│
├─ 镜像太大 / 构建慢 ──────── image_audit.py <host> <image>
│                              → 每 layer size + cmd + age
│                              → 重复 layer / 可合并 step
│                              → 安全提示（root / 大缓存 / apt cache 未清）
│
└─ Compose 整个 stack 异常 ─── compose_status.py <host> <project-dir>
                                → 每 service 状态 + health + 端口冲突
                                → depends_on 链路 + healthcheck 失败定位
                                → 网络 / volume 名字解析
```

## Host model

第一参 `<host>` 同 `k8s-debug` 约定：
- `local` → 直接 `subprocess.run(["docker", ...])`
- 其他 alias → 通过 `ssh-core/ssh_execute.py <alias> "docker ..."`

`docker context use` 也算"远端"，但本 plugin 不强依赖它 —— 我们走 ssh-core 的明确路径，便于审计。

## Quick reference

```bash
# 单容器诊断
python scripts/inspect_container.py local payment-svc --json
python scripts/inspect_container.py builder worker-7 --tail 100

# 镜像审计
python scripts/image_audit.py local myorg/api:1.2.3
python scripts/image_audit.py local myorg/api:1.2.3 --threshold-mb 200

# Compose
python scripts/compose_status.py local ~/projects/payment --json
python scripts/compose_status.py builder /srv/stack
```

## Common signals & where to look

| 症状 | 决策树入口 | 主要线索 |
|---|---|---|
| `Exited (137)` | `inspect_container` | OOMKilled —— 看 `State.OOMKilled` + 宿主机 dmesg |
| `Exited (139)` | `inspect_container` | SIGSEGV —— 应用 crash，看 logs |
| `Exited (1)` | `inspect_container` | 应用主动退出 —— 看 logs 最后几行 |
| `Restarting (N) X seconds ago` | `inspect_container` | restart_count 增长 + last_logs 提示根因 |
| `unhealthy` | `inspect_container` 或 `compose_status` | `State.Health.Log` 段最近 5 次 probe 输出 |
| `bind: address already in use` | `inspect_container` | 同宿主机 `ss -tlnp \| grep <port>` 找占用 |
| `no space left on device` | `remote-debug` 上做 host 层诊断 | 不是容器本身问题 |
| 镜像层超大 | `image_audit` | 单 layer > 200MB 通常可优化 |

## Output contract

所有脚本支持 `--json`，与 v0.1 既有 plugin 一致：

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "data": {
    "host": "local",
    "target": "payment-svc",
    "summary": {"state": "warn", "config": "ok", "logs": "warn"},
    "findings": [
      {"severity": "warn", "kind": "exit_code", "value": 137,
       "hint": "OOMKilled — see references/container_issues.md#oomkilled-exit-137"}
    ],
    "raw": {"...": "..."}
  }
}
```

## References

- `references/container_issues.md` —— 退出码、Restarting 模式、health probe 失败
- `references/image_optimization.md` —— layer 合并、`.dockerignore`、multi-stage、BuildKit cache mount
- `references/compose_debug.md` —— depends_on / healthcheck / network / volume 排查

## What this skill is *not*

- 不重启 / 重建容器 —— 走 ssh-guarded
- 不做镜像安全扫描（trivy / grype 类）—— 留 v0.3
- 不替代 docker stats 实时监控 —— 只做一次快照诊断
