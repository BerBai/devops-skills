# Design — tail_multi v1.0

## Architecture overview

```
CLI args ────────▶ build_parser() ──▶ args

                        │
                        ▼
              ┌────────────────────────────────┐
              │ lib.source_spec.resolve(specs) │
              │  → list[ResolvedSource]        │
              │  brace/glob 展开 + 字段拆解    │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ 空集 → exit 44                 │
              │       (no_sources_resolved)    │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ lib.clock_skew.probe(hosts)    │
              │  并发 ssh_execute date -u      │
              │  返回 dict[host, skew_ms]      │
              │  (5s 总 timeout)               │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ ThreadPoolExecutor(            │
              │   max_workers=--max-concurrent)│
              │                                │
              │ 每源一 worker：                │
              │   lib.fetchers.iter_lines(src) │
              │     → Iterator[RawLine]        │
              │ 推入 line_queue: Queue[Line]   │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ Main thread drain loop:        │
              │   while not (all_done && q.empty):  │
              │     line = q.get(timeout=0.5)  │
              │     line.time = time_norm(line)│
              │     if filters.should_drop(...): cont
              │     reorder_buffer.push(line)  │
              │     for ready in reorder_buffer.pop_ready():
              │       writer.emit(ready)       │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ writer.flush_envelope_summary  │
              │  (--json 模式追加 envelope 行) │
              └────────────────────────────────┘
```

单进程 + ThreadPoolExecutor。每个 fetcher worker 是阻塞 IO（subprocess.Popen
+ readline 循环），用线程而非 asyncio，避免引入 async 复杂度（与已 ship 的
9 个脚本同构）。

## Module structure

```
plugins/log-aggregator/skills/log-aggregator/scripts/
├── tail_multi.py                   # CLI shell, ~250 LOC
├── correlate.py                    # (this child 不动)
├── grep_across_sources.py          # (this child 不动)
└── lib/                            # 新建目录
    ├── __init__.py
    ├── source_spec.py              # D2，~120 LOC
    ├── fetchers.py                 # D3，~250 LOC（4 fetcher）
    ├── time_norm.py                # D4，~80 LOC
    ├── clock_skew.py               # D5，~60 LOC
    └── filters.py                  # D6，~140 LOC
```

总计 ~900 LOC 实现（tail_multi.py 250 + lib 5 × 平均 130 = 900）。
比 ssh_server_transfer 600 LOC 略大，原因是 4 个 fetcher 撑起 ~250 LOC。

## D1 — Top-level flow

`tail_multi.py` 仅做调度，无业务逻辑：

```python
# tail_multi.py
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        sources = source_spec.resolve(args.sources)
    except source_spec.SpecError as e:
        return _emit_failure("source_spec_invalid", 40, str(e), args)

    if not sources:
        return _emit_failure("no_sources_resolved", 44,
                             "specs resolved to 0 sources", args)

    skew = clock_skew.probe([s.host for s in sources if s.host != "local"],
                            timeout=5.0)

    writer = OutputWriter(json_mode=args.json, no_color=os.getenv("NO_COLOR"))
    writer.banner({"sources": [s.source_tag for s in sources],
                   "clock_skew_ms": skew})

    filter_pipeline = filters.build_pipeline(
        no_default=args.no_default_filters,
        min_level=args.min_level,
        suppress=args.suppress,
        aggressive=args.aggressive_suppress,
        user_overrides=filters.load_user_overrides(),
    )

    reorder = ReorderBuffer(max_pending=256, flush_after_s=2.0)
    line_q: queue.Queue[FetchedLine | _Sentinel] = queue.Queue(maxsize=4096)

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.max_concurrent) as pool:
        futures = [pool.submit(_run_fetcher, src, args, line_q)
                   for src in sources]

        try:
            _drain_loop(line_q, futures, filter_pipeline, reorder, writer)
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            return _emit_envelope(writer, sources, skew,
                                  reason="interrupted", exit_code=0)

    # 检查 fetcher 失败
    failed = [(s, f.exception()) for s, f in zip(sources, futures)
              if f.exception() is not None]
    fclass = "partial_fetch" if failed else None
    return _emit_envelope(writer, sources, skew, fclass=fclass,
                          failed_fetchers=[(s.source_tag, str(e)) for s, e in failed])
```

## D2 — Source spec parsing (`lib/source_spec.py`)

```python
@dataclass(frozen=True)
class ResolvedSource:
    scheme: str           # "journal" | "docker" | "kube" | "file"
    host: str             # "local" or remote alias (ssh-core target)
    payload: str          # journal: unit | docker: container
                          # kube: "<ns>/<pod-or-pattern>" | file: path
    query: dict[str, str] # parsed ?k=v, e.g. {"label": "app=foo", "ts_regex": "..."}
    original_spec: str    # 用户原文，便于错误信息
    source_tag: str       # 用于输出前缀: "web-1/nginx", "payment-svc-x" 等


class SpecError(ValueError):
    """Spec 字符串无法解析。"""


_BRACE_RE = re.compile(r"\{([^{}]+)\}")
_SPEC_RE = re.compile(r"^(?P<scheme>\w+)://(?P<rest>.+)$")


def resolve(specs: list[str]) -> list[ResolvedSource]:
    """Expand brace patterns then parse each into ResolvedSource."""
    expanded: list[str] = []
    for s in specs:
        expanded.extend(_expand_brace(s))
    return [_parse_one(s) for s in expanded]


def _expand_brace(spec: str) -> list[str]:
    """journal://web-{1,2,3}/nginx → ['journal://web-1/nginx', ...]
    支持单层 brace；多层用 itertools.product 解决。"""
    matches = _BRACE_RE.findall(spec)
    if not matches:
        return [spec]
    # 取第一个 brace，递归剩余
    m = _BRACE_RE.search(spec)
    options = m.group(1).split(",")
    rest = spec[:m.start()] + "{}" + spec[m.end():]
    out = []
    for opt in options:
        out.extend(_expand_brace(rest.format(opt)))
    return out


def _parse_one(spec: str) -> ResolvedSource:
    m = _SPEC_RE.match(spec)
    if not m:
        raise SpecError(f"not a scheme://... spec: {spec!r}")
    scheme = m.group("scheme")
    rest = m.group("rest")

    # 拆 ?query
    if "?" in rest:
        rest, qstr = rest.split("?", 1)
        query = dict(p.split("=", 1) for p in qstr.split("&"))
    else:
        query = {}

    parts = rest.split("/", 1)
    host = parts[0]
    payload = parts[1] if len(parts) > 1 else ""

    if scheme not in {"journal", "docker", "kube", "file"}:
        raise SpecError(f"unsupported scheme: {scheme!r}")

    # 各 scheme 特化校验
    if scheme == "journal":
        # unit 可空（全 system journal）
        source_tag = f"{host}/{payload or 'system'}"
    elif scheme == "docker":
        if not payload:
            raise SpecError(f"docker:// needs <container>: {spec!r}")
        source_tag = f"{host}/{payload}"
    elif scheme == "kube":
        # payload = "<ns>/<pod-or-glob>"，ns 必填，pod 可由 ?label= 替代
        if "/" not in payload and "label" not in query:
            raise SpecError(f"kube:// needs <ns>/<pod> or ?label=: {spec!r}")
        source_tag = payload.replace("/", "-") if payload else query["label"]
    elif scheme == "file":
        if not payload.startswith("/"):
            payload = "/" + payload  # tolerate file://host/var/log → /var/log
        source_tag = f"{host}{payload}"

    return ResolvedSource(scheme, host, payload, query, spec, source_tag)
```

**Brace 展开规则**：
- 嵌套 brace（`{a,b}-{1,2}` = 4 个）通过递归 `_expand_brace` 实现
- `{a..d}` range 模式**不支持** v1.0（仅 csv 列举），文档明确
- Brace 内的特殊字符（`,` 表分隔）不转义；要带逗号必须在 brace 外组织

**Glob 展开**（kube pod glob）：
- `resolve()` **不**展开 kube pod glob —— 那是 fetcher 阶段做（需要 `kubectl
  get pods` 一次 round-trip）
- spec 解析阶段，glob 保留在 `payload`，fetcher 第一步 enumerate

## D3 — Fetcher 接口与 4 类实现 (`lib/fetchers.py`)

### 通用接口

```python
@dataclass(frozen=True)
class RawLine:
    source: ResolvedSource
    raw_body: str          # 原始日志行（已去掉 fetcher 添加的时间戳前缀）
    raw_ts: str | None     # 行内时间戳字符串（未归一化），由 time_norm 处理
    fields: dict[str, str] # journal `-o json` 解出的字段；其他 scheme 为 {}
    fetched_at: float      # time.time()，fallback 用


def iter_lines(source: ResolvedSource, *, since: str, follow: bool,
               timeout: int) -> Iterator[RawLine]:
    """Dispatch to the right fetcher by scheme."""
    dispatch = {
        "journal": _journal_fetcher,
        "docker": _docker_fetcher,
        "kube": _kube_fetcher,
        "file": _file_fetcher,
    }
    return dispatch[source.scheme](source, since=since, follow=follow,
                                   timeout=timeout)
```

### `_journal_fetcher`

```python
def _journal_fetcher(src, *, since, follow, timeout):
    since_epoch = _parse_since_to_epoch(since)  # 5min → epoch
    cmd_remote = ["journalctl", "-o", "json", "--since", f"@{since_epoch}",
                  "--no-pager"]
    if src.payload:
        cmd_remote = cmd_remote[:1] + ["-u", src.payload] + cmd_remote[1:]
    if follow:
        cmd_remote.append("-f")

    for line in _run_cmd(src.host, cmd_remote, timeout=timeout):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_ts = d.get("__REALTIME_TIMESTAMP")  # μs string
        body = d.get("MESSAGE", "")
        yield RawLine(src, body, raw_ts, d, time.time())
```

### `_docker_fetcher`

```python
def _docker_fetcher(src, *, since, follow, timeout):
    cmd = ["docker", "logs", "--timestamps", "--since", since, src.payload]
    if follow:
        cmd.append("--follow")
    for line in _run_cmd(src.host, cmd, timeout=timeout):
        # RFC3339 timestamp prefix
        ts, _, body = line.partition(" ")
        yield RawLine(src, body, ts, {}, time.time())
```

### `_kube_fetcher`

```python
def _kube_fetcher(src, *, since, follow, timeout):
    ns, _, pod = src.payload.partition("/")

    # glob / label 展开
    if "*" in pod or "label" in src.query:
        pods = _enumerate_kube_pods(src.host, ns, pod, src.query.get("label"))
    else:
        pods = [pod]

    for p in pods:
        cmd = ["kubectl", "logs", "-n", ns, p,
               "--timestamps", "--since", since,
               "--all-containers", "--prefix"]
        if follow:
            cmd.append("--follow")
        if src.query.get("previous") == "true":
            cmd.append("--previous")

        for line in _run_cmd(src.host, cmd, timeout=timeout):
            ts, _, body = line.partition(" ")
            yield RawLine(src, body, ts, {}, time.time())


def _enumerate_kube_pods(host, ns, pattern, label):
    if label:
        cmd = ["kubectl", "get", "pods", "-n", ns, "-l", label, "-o", "name"]
    else:
        cmd = ["kubectl", "get", "pods", "-n", ns, "-o", "name"]
    pods = list(_run_cmd(host, cmd, timeout=10))
    pods = [p.removeprefix("pod/") for p in pods]
    if pattern and "*" in pattern:
        pods = fnmatch.filter(pods, pattern)
    return pods
```

### `_file_fetcher`

```python
def _file_fetcher(src, *, since, follow, timeout):
    # since 解释为 "tail -n <N>" + 启动后过滤时间窗，因为 tail 没法按时间
    if follow:
        cmd = ["tail", "-F", src.payload]
    else:
        cmd = ["tail", "-n", "10000", src.payload]
    for line in _run_cmd(src.host, cmd, timeout=timeout):
        yield RawLine(src, line, None, {}, time.time())  # ts 由 time_norm 提取
```

### `_run_cmd` — 本地 / 远端透明

```python
def _run_cmd(host: str, argv: list[str], *, timeout: int) -> Iterator[str]:
    """
    host == "local": subprocess.Popen 本机跑
    host != "local": shell out 到 ssh_execute.py
    返回 stdout 行迭代器（去除尾部 \n）。
    """
    if host == "local":
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    else:
        # ssh_execute 的 CLI 形态: ssh_execute.py <host> -- <cmd argv...>
        ssh_argv = [_SSH_EXECUTE_PATH, host, "--timeout", str(timeout), "--"] + argv
        proc = subprocess.Popen(ssh_argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)

    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in (0, None, -signal.SIGTERM, -signal.SIGKILL):
            # 非主动 terminate 导致的非 0 → 触发 fetcher_failed
            stderr_tail = proc.stderr.read()[-2048:] if proc.stderr else ""
            raise FetcherError(host, argv, proc.returncode, stderr_tail)
```

## D4 — Time normalization + reorder buffer (`lib/time_norm.py`)

```python
def normalize(line: RawLine) -> tuple[datetime, str] | None:
    """(iso_utc_datetime, raw_body) 或 None 表示无法解析（让 reorder 用 fetched_at）"""
    scheme = line.source.scheme
    if scheme == "journal":
        return _norm_journal(line)
    elif scheme == "docker":
        return _norm_docker(line)
    elif scheme == "kube":
        return _norm_kube(line)
    elif scheme == "file":
        return _norm_file(line)
    return None


def _norm_journal(line):
    if not line.raw_ts:
        return None
    try:
        us = int(line.raw_ts)
        return datetime.fromtimestamp(us / 1e6, tz=timezone.utc), line.raw_body
    except (ValueError, OverflowError):
        return None


def _norm_docker(line):
    """RFC3339: 2026-05-25T14:02:01.123Z 或 +08:00 形式"""
    try:
        dt = datetime.fromisoformat(line.raw_ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc), line.raw_body
    except (ValueError, AttributeError):
        return None


def _norm_kube(line):
    # kubectl --timestamps 同 docker 格式
    return _norm_docker(line)


def _norm_file(line):
    ts_regex = line.source.query.get("ts_regex")
    ts_format = line.source.query.get("ts_format")
    if not (ts_regex and ts_format):
        return None
    m = re.search(ts_regex, line.raw_body)
    if not m:
        return None
    try:
        # 用源主机时区解析 → 转 UTC。v1.0 假设源主机用本地容器/host 时区
        dt = datetime.strptime(m.group(0), ts_format)
        # 简化：默认按 UTC，如需源主机时区参考 `query.get("tz")` v1.1+
        return dt.replace(tzinfo=timezone.utc), line.raw_body
    except ValueError:
        return None
```

### ReorderBuffer

跨源时间排序不严格 —— 各 fetcher 异步推 line 进 queue，时间戳可能乱序到达。
ReorderBuffer 维护一个 256 行 deque（按 normalized time 插入排序），仅当
buffer 满或某行已经 buffer 满 `flush_after_s` 秒后 emit：

```python
class ReorderBuffer:
    def __init__(self, max_pending: int = 256, flush_after_s: float = 2.0):
        self._buf: list[tuple[datetime, FetchedLine]] = []  # sorted by ts
        self._max = max_pending
        self._flush_s = flush_after_s

    def push(self, line: FetchedLine):
        bisect.insort(self._buf, (line.norm_time, line),
                      key=lambda x: x[0])

    def pop_ready(self) -> list[FetchedLine]:
        """Return lines safe to emit now (oldest part of buffer).
        - if len > max_pending: emit excess
        - if oldest line is older than now - flush_after_s: emit until threshold
        """
        out = []
        now = datetime.now(tz=timezone.utc)
        threshold = now - timedelta(seconds=self._flush_s)
        while self._buf and (len(self._buf) > self._max
                              or self._buf[0][0] < threshold):
            out.append(self._buf.pop(0)[1])
        return out

    def drain(self) -> list[FetchedLine]:
        out = [l for _, l in self._buf]
        self._buf.clear()
        return out
```

**Trade-off**：reorder buffer 引入 ~2s 延迟（人模式 live tail 感觉略卡，但
能保证时间排序）。`--no-reorder` flag **不在** v1.0（保持 CLI surface 与
stub 一致），但内置 `flush_after_s=2.0` 可调（design 锁定值，未来 v1.1
可加 flag）。

## D5 — Clock skew probe (`lib/clock_skew.py`)

```python
def probe(remote_hosts: list[str], timeout: float = 5.0) -> dict[str, int | None]:
    """Concurrent ssh_execute date probe. Returns {host: skew_ms}.
    Positive skew_ms = remote clock is ahead of local.
    """
    if not remote_hosts:
        return {}

    results: dict[str, int | None] = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(remote_hosts))) as pool:
        future_map = {pool.submit(_probe_one, h, timeout): h
                      for h in set(remote_hosts)}
        for f in concurrent.futures.as_completed(future_map, timeout=timeout):
            host = future_map[f]
            try:
                results[host] = f.result()
            except Exception:
                results[host] = None
    return results


def _probe_one(host: str, timeout: float) -> int:
    t0 = time.time()
    cmd = [_SSH_EXECUTE_PATH, host, "--timeout", str(int(timeout)),
           "--", "date", "-u", "+%s.%N"]
    out = subprocess.check_output(cmd, text=True, timeout=timeout)
    t1 = time.time()
    remote_ts = float(out.strip())
    rtt_half = (t1 - t0) / 2
    local_at_remote_emit = t0 + rtt_half
    return int((remote_ts - local_at_remote_emit) * 1000)
```

**精度**：单次 probe 精度约 ±50ms（RTT 抖动），对跨源关联足够（typical
诊断窗 ≥30s）。v1.1 可加多次 probe + NTP-style 取最小 RTT。

## D6 — Filters (`lib/filters.py`)

```python
@dataclass
class FilterPipeline:
    noise_patterns: list[re.Pattern]
    user_drop: list[re.Pattern]
    user_keep_always: list[re.Pattern]
    min_level: str
    suppress: bool
    aggressive: bool
    _suppress_state: dict[str, _SuppressEntry] = field(default_factory=dict)

    def should_drop(self, line: FetchedLine) -> bool:
        # keep_always 最高优先
        if any(p.search(line.body) for p in self.user_keep_always):
            return False

        # noise + user_drop
        if any(p.search(line.body) for p in self.noise_patterns + self.user_drop):
            return True

        # level
        if _LEVEL_ORDER[line.level] < _LEVEL_ORDER[self.min_level]:
            return True

        # suppress（速率抑制）
        if self.suppress:
            key = _normalize_key(line.body, aggressive=self.aggressive)
            now = time.monotonic()
            entry = self._suppress_state.setdefault(key, _SuppressEntry(now, 0))
            if now - entry.first_seen < 1.0:
                entry.count += 1
                if entry.count > 5:
                    return True
            else:
                entry.first_seen = now
                entry.count = 1
        return False
```

**Level 推断**（per scheme）：
- journal: `PRIORITY` 字段（0-7）→ 6 级标准化
- docker / kube: 行内 `level=warn` / `WARN:` / `[WARNING]` regex 匹配
- file: 同 docker / kube；未识别 → `info`

**默认 noise deny-list** 内置在 `_DEFAULT_NOISE_PATTERNS`，复制自
`references/noise_reduction.md` "默认丢什么" 节（5 个 healthcheck +
2 个 static + 1 个 CORS + 2 个 k8s heartbeat + 2 个 systemd）。

## D7 — Concurrency model

```
Main thread
  ├─ build sources / probe skew
  ├─ ThreadPoolExecutor (--max-concurrent workers)
  │    ├─ Worker 1: iter_lines(src1) → push to line_q
  │    ├─ Worker 2: iter_lines(src2) → push to line_q
  │    └─ ...
  └─ Drain loop:
       while not (all_done and line_q.empty()):
         try: line = line_q.get(timeout=0.5)
         except Empty:
           # 检查 reorder buffer 是否有 ready lines
           emit_ready_from_buffer(); continue
         normalize line, run through filter pipeline, push to reorder buffer
         emit ready lines from reorder buffer
```

**线程安全**：
- `line_q` 是 `queue.Queue`（thread-safe）
- `ReorderBuffer` 仅主线程访问（worker push line_q，主线程 pop 后再 push
  reorder）
- `_suppress_state` 同上（仅主线程）
- subprocess.Popen 子进程归属各 worker thread；Ctrl-C 时主线程 cancel
  futures → 各 worker 在 `_run_cmd` finally 块 terminate proc

**资源回收**：所有 worker 走 ThreadPoolExecutor context manager 自动 join；
subprocess 走 `_run_cmd` finally 块。

## D8 — Failure class & exit codes

| Exit code | failure_class | 触发条件 |
|---|---|---|
| 0 | (none) | 成功，含 `partial_fetch` warning |
| 1 | generic | 未预期异常（兜底） |
| 40 | source_spec_invalid | `source_spec.SpecError` |
| 41 | fetcher_failed | 全部 fetcher 失败（partial 也算 success） |
| 42 | clock_skew_failed | reserved（v1.0 仅警告，未触发） |
| 43 | normalize_failed | normalize 失败行占比 > 50% |
| 44 | no_sources_resolved | spec 解析后空集 |
| 45 | partial_fetch | warning：≥1 fetcher 失败，其他 success（exit 0 + 设置 failure_class） |
| 46-49 | reserved | 未来扩展 |
| 124 | (透传 ssh_execute) | ssh 端 idle / connect timeout |

**注**：45 是 warning，不是 exit 45（沿用 scan_software cache_stale 模式）。

## D9 — Envelope shape

### Human mode (default)
```
[2026-05-25T14:02:01] tail_multi: 3 sources, clock_skew (web-2): -120ms
[web-1/nginx 14:02:01.234] GET /api/charge 200 12ms
[web-2/nginx 14:02:01.244] GET /api/charge 200 14ms (clock_skew -120ms)
[web-3/nginx 14:02:02.012] GET /api/charge 500 8ms   ← red
─────────────────────────────────────────────────────
sources_with_hits: 3, events_emitted: 1234, failed_fetchers: 0
elapsed: 10.4s
```

### `--json` mode (NDJSON + summary)
```jsonl
{"source":"web-1/nginx","time":"2026-05-25T14:02:01.234Z","level":"info","raw":"GET /api/charge 200 12ms","fields":{}}
{"source":"web-2/nginx","time":"2026-05-25T14:02:01.244Z","level":"info","raw":"GET /api/charge 200 14ms","fields":{}}
{"source":"web-3/nginx","time":"2026-05-25T14:02:02.012Z","level":"error","raw":"GET /api/charge 500 8ms","fields":{}}
{"success":true,"exit_code":0,"stdout":"","stderr":"","data":{"sources":["web-1/nginx","web-2/nginx","web-3/nginx"],"clock_skew_ms":{"web-2":-120,"web-3":15},"events_emitted":1234,"failure_class":null,"failed_fetchers":[]}}
```

CLI 调用方区分 event vs summary：**最后一行**是 summary，且 summary 没有
`source` 字段。

## D10 — Testing strategy

### Unit (~30) - `tests/test_tail_multi_unit.py`

| Group | Count | What |
|---|---|---|
| source_spec | 12 | 4 scheme × 2 normal + brace expansion 2 + glob/label 2 + error cases 4 |
| time_norm | 8 | 4 scheme × 2 (normal + failure fallback) |
| filters | 8 | noise patterns 2 + level 2 + suppress rate 2 + keep_always 2 |
| clock_skew | 3 | 2 hosts probe (mock subprocess) + 0 hosts + all-fail |
| CLI / main | 5 | args bad + spec empty + json envelope shape + Ctrl-C cleanup + partial_fetch |

Mock 策略：`_run_cmd` 用 `unittest.mock.patch` 替换为 `MagicMock` 返回固定
行序列；`ssh_execute.py` 调用全部 mock，单测**不触发**真 SSH。

### Integration (~2) - `tests/test_tail_multi_integration.py` (`-m live_ssh`)

```python
@pytest.mark.live_ssh
def test_journal_remote_pai():
    # 在 pai 跑 journal://pai/sshd 拉 10s
    cmd = [sys.executable, str(SCRIPT_PATH),
           "--sources", "journal://pai/sshd",
           "--since", "10min", "--json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    lines = out.stdout.strip().split("\n")
    *events, summary_line = lines
    summary = json.loads(summary_line)
    assert summary["success"] is True
    assert summary["data"]["events_emitted"] >= 0
    assert "pai" in summary["data"]["clock_skew_ms"]


@pytest.mark.live_ssh  # 即使是 local fixture 也跟 live 测试同一文件，便于 pytest 选
def test_file_local_fixture(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("\n".join(f"line {i}" for i in range(50)))
    cmd = [sys.executable, str(SCRIPT_PATH),
           "--sources", f"file://local{log}",
           "--json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    events = [l for l in out.stdout.strip().split("\n")
              if json.loads(l).get("source")]
    assert len(events) == 50
```

测试不依赖 docker daemon（v1.0 不写 docker/kube 的 integration test，留给
e2e validation child task）。

## Open questions（写完后由 user/main 决定，不阻塞 implement）

- **Q1**: `file://` source 的 `--follow` 在 logrotate 时 `tail -F` vs
  `tail -f`？v1.0 design 用 `tail -F`（follow by name），对 logrotate 友好
  但 inode 变化时多 1 行 message。若 ship 时发现真机问题，回归 `tail -f` +
  在文档说明。
- **Q2**: reorder buffer 的 256/2s 默认值是否需要调？v1.0 锁定，pre-flight
  在 pai 实测后微调。
- **Q3**: kube `--previous` 是否做成 query 字段？design 用
  `?previous=true`，与 stub 文档一致；v1.1 可考虑专 flag。
