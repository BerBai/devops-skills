---
name: log-aggregator
description: Multi-source log aggregation and correlation. Use when the user needs to follow logs across multiple hosts/containers/Pods simultaneously, correlate events across sources by time/trace-id/request-id, or hunt for the same error pattern across N machines. Aggregates from journalctl, docker logs, kubectl logs, plain files, and remote tails via ssh-core.
---

# log-aggregator

多源 log 聚合 + 关联分析。**只读**：从你能想到的任意一组源（journalctl / docker logs / kubectl logs / 远端文件 / 本地文件）同时拉日志，按时间/trace-id/request-id 关联起来。**不**做日志写入、清理、轮转。

## When to use

触发场景：
- "看下 web-1 / web-2 / web-3 的 nginx 日志，最近 10 分钟" → 多主机 tail
- "查这个 trace-id 在 4 个服务都经过了什么" → 跨源关联
- "API 报 5xx，同时看 nginx / app / db logs" → 时间窗对齐
- "所有 K8s namespace 里搜 OOMKilled" → 跨命名空间 grep
- "journalctl + docker logs + 应用 log 一起看" → 异构源合并

Skip when:
- 只看一个源 / 一台主机的 log → `remote-debug/tail_log.py` 更轻量
- 要做长期收集 / 持久化 → 你需要的是 Loki / Elastic / OpenSearch，不是本 plugin
- 要解析结构化日志做指标 → Prometheus + node-exporter / OTel collector

## Decision tree

```
日志问题？
│
├─ 多源同时看（live）─────── tail_multi.py
│                              --sources <source-spec>...
│                              --follow
│                              → 行前缀 [host/source] 区分源
│
├─ 跨源关联（找因果链）─── correlate.py
│                              --sources <source-spec>...
│                              --window 30s
│                              --anchor "<regex|trace-id|request-id>"
│                              → 以 anchor 为时间锚，前后 window 内各源的事件
│
└─ N 源一并搜索 ────────── grep_across_sources.py
                              --sources <source-spec>...
                              --pattern "<regex>"
                              --since 1h
                              → 每源命中行数 + 样例
```

## Source spec 语法

为统一描述异构源，本 plugin 用 URL-like spec：

| Spec | 含义 |
|---|---|
| `journal://<host>/<unit>` | `journalctl -u <unit>` 在 host 上 |
| `journal://<host>` | 全 system journal |
| `docker://<host>/<container>` | `docker logs <container>` |
| `kube://<host>/<namespace>/<pod>` | `kubectl logs -n ns pod` |
| `kube://<host>/<namespace>?label=<sel>` | `kubectl logs -n ns -l <sel> --all-containers` |
| `file://<host>/<path>` | 普通文件 `tail` / `cat` |

`<host>` 为 `local` 时直接执行；否则经 ssh-core。

例子：
```
tail_multi.py \
  --sources journal://web-1/nginx \
            journal://web-2/nginx \
            journal://web-3/nginx \
            kube://local/payment/payment-svc-* \
            docker://builder/redis \
  --follow --since 10m
```

## Output: prefix 与机器可读

人模式（默认）每行前缀 `[<source-tag> <time>]`：
```
[web-1/nginx 14:02:01] GET /api/charge 200 12ms
[web-2/nginx 14:02:01] GET /api/charge 200 14ms
[web-3/nginx 14:02:02] GET /api/charge 500 8ms      ← 看这里
[payment-svc 14:02:02] FATAL connection refused: pg://db:5432
```

`--json` 模式产 NDJSON，每行一个 event：
```json
{"source": "web-3/nginx", "time": "2026-05-14T14:02:02Z",
 "level": "info", "raw": "GET /api/charge 500 8ms", "fields": {...}}
```

## Quick reference

```bash
# 三台 web 同步看 nginx
python scripts/tail_multi.py \
  --sources journal://web-{1,2,3}/nginx \
  --follow --since 10m

# 跨源关联 trace-id
python scripts/correlate.py \
  --sources journal://web-1/nginx kube://local/payment/payment-svc-* docker://db-1/postgres \
  --anchor "trace_id=abc123" \
  --window 30s

# 跨集群 grep "OOMKilled"
python scripts/grep_across_sources.py \
  --sources kube://prod-cluster/all/?label=app \
  --pattern "OOMKilled|137" \
  --since 1h \
  --json
```

## References

- `references/sources.md` —— 每种 source 类型的具体抓取命令 + 边界情况
- `references/correlation_patterns.md` —— trace-id / request-id / time-window / span 类型的关联思路
- `references/noise_reduction.md` —— 怎么过滤健康检查 / heartbeat / debug 噪音

## What this skill is *not*

- 不是日志存储 / 索引引擎（不是 Loki / ELK / Splunk）
- 不做实时告警（不订阅 stream 触发动作）
- 不解析二进制 / 结构化协议日志（除非源本身吐 JSON）
- 不替你写 alert 规则
