# Noise reduction — drop the boring lines so the signal pops

跨源多 tail 一开就是几千行/秒。我们做诊断不是读小说 —— 把噪音过掉信号才会浮上来。

## 默认丢什么

`tail_multi.py` 默认应用一个保守的 deny-list（可用 `--no-default-filters` 关）：

```
# Healthcheck / liveness / readiness probe（最大宗噪音）
\bGET /healthz?\b
\bGET /ready\b
\bGET /ping\b
\bkube-probe/[\d.]+
\bELB-HealthChecker/

# 静态资源
\bGET /favicon\.ico
\bGET /robots\.txt

# CORS preflight（基本无信息）
\bOPTIONS \b.*\b 204\b

# k8s 自身的 heartbeat / leader-elect
\bleader-elect\b
\belectionRunner\b

# systemd 启动正常事件
\bStarted \w+\.service\b
\bStopped \w+\.service\b
```

这些规则**每条都有理由**。不是审美，是统计上"出现频率高且对故障诊断信息量低"。

## 不要默认丢什么

诱人但有害的 deny：

- ❌ `WARN` —— 真问题前的早期信号常以 WARN 出现
- ❌ `^debug:` —— 看似是 debug 但应用可能用错 level
- ❌ "expected" 错误（"connection reset by peer" 等）—— 它们在某些故障里就是因
- ❌ 慢请求 / `elapsed > 1s` —— 慢就是问题
- ❌ 任何含 `5\d\d` / `panic` / `oom` / `FATAL` / `traceback` 的行

`correlate.py` 在 anchor 周围**强制不过滤** —— 不管 deny-list 怎么配，window 内的所有原始行都拉出来。

## 用户自定义

```
# ~/.devops/log_filters.json
{
  "version": 1,
  "drop": [
    "MyApp internal heartbeat: ok",
    "scheduler tick"
  ],
  "keep_always": [
    "TRACE_ID_MISSING",
    "PII"
  ]
}
```

`keep_always` 优先级最高 —— 命中 keep 的行永远不丢，即使匹配了 drop。

## 速率抑制

当某行**同样内容**短时间内大量出现，抑制：

```
[web-3/nginx 14:02:01] GET /api/charge 500 8ms (×1)
[web-3/nginx 14:02:01] GET /api/charge 500 8ms (×42 in last 1s, suppressed)
[web-3/nginx 14:02:02] FATAL connection refused: pg://db:5432
```

这是 syslog 的经典 trick（`message repeated N times`）。本 plugin 在 `tail_multi --suppress` 启用时自动启用。

抑制规则：
- 行被规范化（去掉时间戳、PID、IP、trace_id 等高基数字段）
- 同规范化行在 1s 内重复 ≥ 5 次 → 抑制
- 抑制期内每秒输出一行 `(×N suppressed)` 摘要
- 抑制以"规范化内容"为 key，**不会**把 trace_id 不同但模板相同的两条混为一谈（除非你用了 `--aggressive-suppress`）

## level 染色 / 排序

把 level 标准化为 6 个等级：
```
trace < debug < info < warn < error < fatal
```

各源原生的：
- syslog priority 0–7 → fatal/alert/crit/err/warn/notice/info/debug
- log4j / SLF4J → trace/debug/info/warn/error/fatal
- python logging → DEBUG/INFO/WARNING/ERROR/CRITICAL
- Go zerolog / zap → debug/info/warn/error/fatal/panic
- gunicorn / nginx → 用 status code 5xx 推断 error

`--min-level warn` 一次性把 info/debug/trace 全丢，保留 warn 及以上。诊断初期常用。

## 关键词高亮（不是过滤，是注意力）

非 `--json` 模式下，命中以下关键词的行**整行加亮**（终端 ANSI red/yellow）：
- `error|err|fatal|panic|crash|oom|killed`
- `timeout|refused|reset by peer|broken pipe`
- `denied|forbidden|unauthorized`
- `out of memory|disk full|no space left`

非诊断关键词不加亮。这是体感而非算法，目的是让人读屏时眼睛不累。

## 实战节奏

```
1. tail_multi --since 30m            # 拿原始体量印象
2. tail_multi --since 30m --suppress # 去重复噪音
3. tail_multi --since 30m --min-level warn  # 砍 info/debug
4. 看到可疑点 → correlate.py 锚住
5. 找到根因附近的源 → grep_across_sources 扩到整个 fleet 看是否普遍
```
