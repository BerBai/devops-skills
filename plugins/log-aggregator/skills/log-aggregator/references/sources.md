# Source types — every supported log source

每种 source spec 翻译成什么命令、限制是什么、时间戳怎么解析。

`<N>` 就是 `tail_multi.py --lines N`（默认 **10000**）。它只影响**非 follow**
的快照抓取：给了 `--lines` 时映射到 `file://` 的 `tail -n N`、`docker://` 的
`docker logs --tail N`、`kube://` 的 `kubectl logs --tail=N`；`--follow` 是无界流，
`journal://` 没有「最后 N 行」的语义（用 `--since` 限流），两者都忽略它。
`grep_across_sources.py` / `correlate.py` 不暴露该参数，仍按各自的默认抓取（远端
`file://` 为 `tail -n 10000`，docker/kube 不加 `--tail`）。

## journal://<host>/<unit?>

```bash
# 单 unit
journalctl -u <unit> -o json --since "<since>" --no-pager
# 全 journal
journalctl -o json --since "<since>" --no-pager
```

**优点**：journalctl 输出已经是结构化的（`-o json` 给字段）。systemd unit 名稳定。

**坑**：
- `--since` 接受 `"10 min ago"` / `"2026-05-14 14:00"` / Unix timestamp。我们统一传 `"@<epoch>"` 避免本地化问题。
- root 才能看 system journal；非 root 用户只能看自己的 user journal。
- 远端跑 `journalctl -f` 会被 ssh-core daemon 的 idle timeout 截断，长时间 follow 需要心跳。
- **没有 `--lines` 对应物**：journalctl 不支持「最后 N 行」，只能用 `--since` 缩小范围。

**字段**（`-o json`）：
- `__REALTIME_TIMESTAMP` —— 微秒级时间戳
- `_SYSTEMD_UNIT` —— 服务名
- `MESSAGE` —— 日志原文
- `PRIORITY` —— syslog 等级（0–7）
- `_HOSTNAME`、`_PID`、`_COMM`、`_EXE`

## docker://<host>/<container>

```bash
docker logs --timestamps --tail <N> [--follow] [--since <since>] <container>
```

`<N>` = `tail_multi --lines`（默认 10000）。非 follow 时总会带上 `--tail`：不带时
`docker logs` 会倾倒容器全部历史，而不是"最近一段"。`grep_across_sources` /
`correlate` 不传该参数，因此它们不加 `--tail`（行为与 GAP-007 之前一致）。

**优点**：每行带 RFC3339 时间戳。

**坑**：
- 容器是 logging driver = `json-file` 时才有完整历史。其他 driver（`journald`/`syslog`/`fluentd`/`none`）拿不到或行为不同：
  ```bash
  docker inspect <c> --format '{{.HostConfig.LogConfig.Type}}'
  ```
  driver = `none` → `docker logs` 返回空，需要看真正的去处。
- 容器死掉后 `docker logs` 还能看，只要容器对象没被 `docker rm`。
- 多行 stack trace 默认按字节流，无 multi-line 合并。本 plugin 给每行加 source tag 时不合并，输出端可以选择重组。

## kube://<host>/<namespace>/<pod>

```bash
kubectl logs -n <ns> <pod> [-c <container>] [--tail=<N>] [--since <since>] [--follow] [--previous]
```

`<N>` = `tail_multi --lines`（默认 10000），非 follow 时以 `--tail=<N>` 透传。

`pod` 字段支持：
- 精确名：`payment-svc-abc123`
- glob：`payment-svc-*` → 我们先 `kubectl get pods` 列出再合并
- `?label=app=foo` → 走 `-l` 选择

**坑**：
- `--previous`（拉上一轮死前的）很关键，CrashLoop 场景里没有它就只能看当前刚启动那点日志。
- multi-container Pod 必须指定 `-c <container>` 或加 `--all-containers --prefix`。
- `kubectl logs` 本身不带绝对时间戳 —— 用 `--timestamps`。
- 远端 `kubectl logs -f` 也受 ssh-core idle timeout 影响。

## file://<host>/<path>

```bash
tail -n <N> <path>                # 不带时间戳
tail -F <path>                    # --follow
```

`<N>` = `tail_multi --lines`（默认 10000）。`<host>` 为 `local` 时不调用 `tail`：用
Python 读文件末尾（同样是最后 N 行），因此 Windows 上也能用。`grep_across_sources` /
`correlate` 不传该参数，走默认的 10000。

**优点**：最简单。

**坑**：
- 没时间戳的日志只能靠 line order 排序，跨源关联会丢精度。我们在 source spec 后允许 `?ts_regex=<regex>&ts_format=<strftime>`，让用户告诉我们怎么从行里提取时间。
- `source_spec` 会先做 brace expansion，所以 query 里的正则不要直接写 `{4}` 这类量词；它会被当成 brace 模板改写。用 `[0-9][0-9][0-9][0-9]`，或在未来 URL-encoding 支持前避免花括号量词。
- 日志轮转期间（`logrotate`）`tail -F` 行为比 `tail -f` 好，但 v1.0 实现要小心新文件 inode 变化。
- 远端文件如果在 NFS 上，`tail -f` 不稳。

## "all" / glob 模式

为减少打字，spec 允许 brace expansion 风格的扩展：
- `journal://web-{1,2,3}/nginx` → 3 个源
- `kube://local/payment/payment-svc-*` → glob 展开后多源

实现层面：spec 解析后立刻 enumerate 出每个具体 source，然后并发抓取。

## 时间归一化

所有源最终统一到 ISO 8601 UTC（`Z` 后缀）。各源的转换：

| Source | 原始 | 转 UTC |
|---|---|---|
| journal | `__REALTIME_TIMESTAMP` 微秒 | `datetime.fromtimestamp(us/1e6, tz=UTC)` |
| docker | RFC3339 `2026-05-14T14:02:02.123Z` | 已是 UTC |
| kube | `--timestamps` 给 RFC3339 | 已是 UTC |
| file | 用户提供的 ts_regex/format | 在用户机器时区解析 → 转 UTC |

跨源排序按归一化后的时间。**注意**：远端机器与本地的时钟可能不同步，本 plugin 在 `tail_multi.py` 启动时会先并发 ping 每个远端 host 抓 `date -u +%s.%N`，记录 skew，输出时加 `[clock_skew_ms=...]` 元数据，便于诊断"为什么这条 X 看起来比 Y 早，但其实是 X 主机时钟慢"。

## 性能注意

多源并发拉，**每源独立 SSH 连接** —— 借力 ssh-core 的 ControlMaster 或 daemon 模式以减少重连开销。N=5 源以下问题不大；N=20+ 时建议加 `--max-concurrent 8` 限制。
