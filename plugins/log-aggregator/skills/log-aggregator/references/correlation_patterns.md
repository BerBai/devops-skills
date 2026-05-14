# Correlation patterns — how to chain events across sources

跨源关联的几种典型形态。`correlate.py` 内置识别这些，但当输入异常时你也需要知道原理。

## 1. Trace-ID / Request-ID 关联（最强）

最理想的场景。一次请求穿过 N 个服务，每个服务的日志都打了同一个 `trace_id`：

```
[edge   ] trace_id=abc123 method=POST /api/charge
[api    ] trace_id=abc123 user=42 amount=100
[payment] trace_id=abc123 stripe_call=ch_xxx
[db     ] trace_id=abc123 insert charges duration=2ms
[api    ] trace_id=abc123 response=200 elapsed=187ms
```

`correlate.py --anchor "trace_id=abc123"` 把所有源里命中 `abc123` 的行按时间排序输出。

**前提**：所有服务都打了 trace_id。如果没有 —— 这才是真问题，本 plugin 关联不了，需要先补全 instrument。

## 2. Time-window 锚定

没 trace_id，但有"现象时刻"。比如 nginx 在 14:02:02 报 500，想看上下游：

```
correlate.py \
  --sources journal://web-3/nginx kube://local/payment/* docker://db-1/postgres \
  --anchor "GET /api/charge.*500" \
  --window 30s
```

工作流：
1. 在指定 sources 里找 anchor 匹配的行 → 记录时间 T
2. 拉所有源在 [T - window, T + window] 区间的所有行
3. 按时间统一排序输出

**前提**：各源时钟相对同步（clock skew < window）。`tail_multi.py` 的 skew 检测在此特别有用。

## 3. Span 模式（嵌套）

OTel / Jaeger 风格的 span_id + parent_span_id。本 plugin 不实现完整 span tree，但识别两个常见模式：

- **共同 trace_id 内的 span 链** → 上一节即可
- **span_id 出现两次（开始/结束）** → 算 duration

```
correlate.py --anchor "span_id=s7" --pair-mode
→ 输出 s7 第一次出现 + 最后一次出现 + 中间 elapsed
```

## 4. Causality by symptom（最弱也最常用）

没 trace_id，只有"症状"。例如：
- "5xx 增加 = root cause？" → 时间序列上 anchor "status=5\d\d"，window 60s 看其他源
- "Pod restart 突增" → anchor "container died" 然后 window 看 dmesg / k8s events

```
grep_across_sources.py --pattern "5\d{2}" --since 5m --sources <all>
correlate.py --sources <all> --anchor "<first-hit>" --window 60s
```

## 5. Cross-host 比较

"为什么 web-3 报错，web-1/web-2 不报？" 这不是关联，是 diff：

```bash
# 同时间窗的三主机比较
grep_across_sources.py --pattern "ERROR" --since 5m \
  --sources journal://web-{1,2,3}/myapp \
  --format summary
# 输出每源命中行数 + 各自的几个样例
```

异常 web-3 的样例就是要看的入口；web-1/web-2 的样例帮你确认"正常机器在干什么"。

## 关联结果如何呈现

`correlate.py` 输出三段：

```
=== Anchor ===
[web-3/nginx 14:02:02.034Z] GET /api/charge 500 8ms

=== Within window (30s) ===
[web-3/nginx     14:02:01.987Z] GET /api/charge ... (start)
[payment-svc-x   14:02:01.991Z] received charge req
[payment-svc-x   14:02:02.012Z] FATAL connection refused: pg://db:5432
[db-1/postgres   14:02:01.500Z] FATAL: too many clients already
                                ↑ 这条比 anchor 早 500ms，可能就是因
[web-3/nginx     14:02:02.034Z] GET /api/charge 500 8ms ← anchor

=== Summary ===
sources_with_hits: 4
anchor_severity: error
likely_root_cause_hint:
  earliest_critical_event in window: db-1/postgres FATAL ...
```

**hint 字段**只是启发式，不是结论。把"最早的 FATAL/ERROR/CRITICAL"作为可能根因展示，让 Claude / 用户判断。

## 常见坑

- **服务器时钟漂移** → 关联出错的"假因果"。tail_multi 启动时先校时。
- **日志缓冲未刷** → 应用真实发生时间和写入 log 时间差 100ms 以上常见。Java/Python 默认 line buffer。修法：让应用刷 stdout 即写（PYTHONUNBUFFERED=1 / `System.out.flush()`）。
- **K8s logs 历史** → Pod 重启后 `kubectl logs` 默认只看当前容器；要历史用 `--previous` 或外部 Loki。
- **trace_id 没传过去** → 90% 的关联失败原因。本 plugin 不能修这个，但能在 hint 里提醒"这个源没有 trace_id 字段，关联可能丢失链路"。
