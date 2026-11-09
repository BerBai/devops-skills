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
│                              --lines 10000      (快照抓多少行)
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
| `docker://<host>/<container>` | `docker logs --tail <N> <container>` |
| `kube://<host>/<namespace>/<pod>` | `kubectl logs -n ns --tail=<N> pod` |
| `kube://<host>/<namespace>?label=<sel>` | `kubectl logs -n ns -l <sel> --all-containers --tail=<N>` |
| `file://<host>/<path>` | 普通文件 `tail -n <N>` / `cat` |

`<N>` 就是 `tail_multi.py --lines N`（默认 10000）。

`<host>` 为 `local` 时**不经过外部命令**：`file://` 用 Python 直接读文件（Windows 上没有 `tail`），`journal://` / `docker://` / `kube://` 仍需要对应二进制。远端（任意 alias）行为不变，仍是 `tail` / `journalctl` / `docker logs` / `kubectl logs`。

Windows 上 `file://local/C:/...` 可用：`source_spec` 补的前导 `/` 在带盘符的路径上会被去掉。

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

### 部分源抓取失败（三个脚本统一的规则）

任一源抓取失败 → 信封按失败上报，**但已经抓到的数据照样放在 `data` 里**：

```json
{"success": false, "exit_code": 41, "stdout": "", "stderr": "",
 "data": {"failure_class": "partial_fetch",
          "failed_fetchers": [{"source": "lab-240/sshd", "error": "rc=255 ..."}],
          "...": "成功源的数据照常在这里"}}
```

| 字段 | 值 |
|---|---|
| `success` | `false` |
| `exit_code` | `41` |
| `data.failure_class` | `"partial_fetch"` |
| `data.failed_fetchers` | `[{"source": ..., "error": ...}, ...]`，每个失败源一条 |

`tail_multi.py` / `grep_across_sources.py` / `correlate.py` 三者**完全一致**，编排层
只需 `if not success` 或看 exit code，不必逐脚本记住不同语义。规则只在一处实现：
`scripts/lib/envelope.py`。

要点：

- 全部源都失败与部分失败是**同一个** `failure_class`（`partial_fetch`），不是两个。
  `fetcher_failed` 是 v1.0 的旧名字，仍被当作别名接受，但不再被发出。
- 人模式（`tail_multi` 无 `--json`、`grep_across_sources --format summary|raw`、
  `correlate` 无 `--json`）不打印信封，**退出码同样为 41**，并在 stderr 列出失败源。
- `correlate` 在「一个源没抓到 + anchor 没找到」时也报 `partial_fetch` 而不是
  `anchor_not_found`（exit 90）：缺的那个源里可能就有 anchor，
  `data.summary.reason` 保留具体原因（`anchor_not_found` / `pair_unmatched`）。
- 其它失败类仍是各自的具体值（`source_spec_invalid` 40、`no_sources_resolved` 44、
  `pattern_invalid` 180、`anchor_not_found` 90、`window_invalid` 91、
  `pair_unmatched` 92、`normalize_failed` 43）。

## Quick reference

```bash
# 三台 web 同步看 nginx
python scripts/tail_multi.py \
  --sources journal://web-{1,2,3}/nginx \
  --follow --since 10m

# 只想先看最后 20 行（file / docker / kube 都认）
python scripts/tail_multi.py \
  --sources file://web-1/var/log/nginx/error.log docker://db-1/postgres \
  --lines 20 --since 1h

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

`tail_multi.py --lines N`（默认 10000）只影响非 follow 的快照抓取：
`file://` 映射 `tail -n N`，`docker://` 映射 `docker logs --tail N`，
`kube://` 映射 `kubectl logs --tail=N`。`--follow` 是无界流，`journal://` 没有
「最后 N 行」的概念（请用 `--since`），两者忽略该参数。`correlate.py` /
`grep_across_sources.py` 不暴露该参数，行为保持不变。

## References

- `references/sources.md` —— 每种 source 类型的具体抓取命令 + 边界情况
- `references/correlation_patterns.md` —— trace-id / request-id / time-window / span 类型的关联思路
- `references/noise_reduction.md` —— 怎么过滤健康检查 / heartbeat / debug 噪音

## What this skill is *not*

- 不是日志存储 / 索引引擎（不是 Loki / ELK / Splunk）
- 不做实时告警（不订阅 stream 触发动作）
- 不解析二进制 / 结构化协议日志（除非源本身吐 JSON）
- 不替你写 alert 规则
