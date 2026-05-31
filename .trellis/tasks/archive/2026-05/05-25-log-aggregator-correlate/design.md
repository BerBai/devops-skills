# Design — correlate v1.0

## Architecture overview

```
CLI args ────────▶ build_parser() ──▶ args

                        │
                        ▼
              ┌────────────────────────────────┐
              │ window_seconds = _parse_window │
              │   (args.window)                │
              │  failure → exit 91             │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ lib.source_spec.resolve(specs) │
              │  失败 → exit 40                │
              │  空集 → exit 44                │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ lib.clock_skew.probe(hosts)    │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ collect_all_lines:             │
              │   ThreadPoolExecutor(8)        │
              │     each source:               │
              │       iter_lines(...).         │
              │         exhaust() → list       │
              │     all_lines = chain(...)     │
              │   normalize each → drop bad ts │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ find_anchor(all_lines,         │
              │   args.anchor, args.pair_mode) │
              │  None → exit 90                │
              │  pair_unmatched → exit 92      │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ filter window:                 │
              │   T = anchor.time              │
              │   keep all_lines if            │
              │     T-window ≤ ts ≤ T+window   │
              │ sort by ts                     │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ compute hint:                  │
              │   earliest critical event      │
              │     in window                  │
              └────────┬───────────────────────┘
                       │
                       ▼
              ┌────────────────────────────────┐
              │ writer.emit_three_sections     │
              │   (anchor, window_lines, hint) │
              └────────────────────────────────┘
```

**关键差异 vs tail_multi**：
- correlate 是**一次性查询**（非流式）：所有 fetcher 跑完、收集全部行后再
  搜索 anchor / 应用 window。没有 reorder buffer，没有 drain loop。
- 没有 filters：window 内强制保留所有行。
- 输出是**一次性 envelope**：human 三段 + json 单 object（不是 NDJSON）。

## Module structure

```
plugins/log-aggregator/skills/log-aggregator/scripts/
├── correlate.py            # ~350 LOC，本任务 owner
└── lib/                    # tail_multi child 已 ship，本任务 read-only
    ├── source_spec.py      # 复用 resolve()
    ├── fetchers.py         # 复用 iter_lines()
    ├── time_norm.py        # 复用 normalize()
    └── clock_skew.py       # 复用 probe()
```

correlate.py 内部模块（无 sub-package，全部一文件）：
- `build_parser()` / `main()` / `_emit_failure()` / `_emit_envelope()`
- `_parse_window(s) -> float` — 解析 `30s/2m/1h` 等
- `_collect_all_lines(sources, since, max_concurrent) -> list[NormalizedLine]`
- `_find_anchor(all_lines, regex, pair_mode) -> Anchor | tuple[Anchor, Anchor]`
- `_filter_window(all_lines, anchor_time, window_s) -> list[NormalizedLine]`
- `_compute_hint(window_lines, anchor) -> dict | None`
- `_emit_three_sections_human(...)`
- `_emit_envelope_json(...)`

## D1 — `_parse_window`

```python
_WINDOW_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")

def _parse_window(s: str) -> float:
    """Return seconds (float). Raises WindowError on bad input."""
    m = _WINDOW_RE.match(s.strip().lower())
    if not m:
        raise WindowError(f"window must be like '30s' / '2m' / '1h' / '1d', got: {s!r}")
    value = float(m.group(1))
    if value <= 0:
        raise WindowError(f"window must be positive, got: {s!r}")
    unit_s = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return value * unit_s


class WindowError(ValueError):
    pass
```

## D2 — `_collect_all_lines`

```python
@dataclass
class NormalizedLine:
    source_tag: str
    norm_time: datetime
    body: str
    level: str        # 从 lib.time_norm 复用 _infer_level
    fields: dict[str, str]


def _collect_all_lines(sources: list[ResolvedSource], since: str,
                       max_concurrent: int = 8
                       ) -> tuple[list[NormalizedLine], list[FetcherFailure]]:
    """Run each source's fetcher to exhaustion (no follow), normalize, return."""
    out: list[NormalizedLine] = []
    failures: list[FetcherFailure] = []

    def fetch_one(src):
        local_out = []
        try:
            for raw in fetchers.iter_lines(src, since=since, follow=False,
                                           timeout=120):
                norm = time_norm.normalize(raw)
                if norm is None:
                    # fetched_at fallback
                    norm_dt = datetime.fromtimestamp(raw.fetched_at, tz=timezone.utc)
                    body = raw.raw_body
                else:
                    norm_dt, body = norm
                local_out.append(NormalizedLine(
                    source_tag=src.source_tag,
                    norm_time=norm_dt,
                    body=body,
                    level=_infer_level_via_lib(raw),
                    fields=raw.fields,
                ))
        except fetchers.FetcherError as e:
            return local_out, FetcherFailure(src.source_tag, str(e))
        return local_out, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        results = list(pool.map(fetch_one, sources))

    for lines, fail in results:
        out.extend(lines)
        if fail is not None:
            failures.append(fail)
    return out, failures
```

**复用** `_infer_level` 函数：从 `lib.filters` 提取（tail_multi child 实现）。
若 tail_multi ship 时该函数仅在 filters 内部使用（非 public），correlate ship
时需在 lib 中提升其可见性。

## D3 — `_find_anchor`

```python
@dataclass
class Anchor:
    source_tag: str
    norm_time: datetime
    body: str
    level: str
    line_index: int  # 在 all_lines 排序后的位置（不必，留 debug）


def _find_anchor(all_lines: list[NormalizedLine], regex: re.Pattern,
                 pair_mode: bool) -> Anchor | tuple[Anchor, Anchor] | None:
    """
    Find first match in all_lines (sorted by norm_time).
    pair_mode: also find LAST match. Return (first, last) tuple if both found.
    """
    sorted_lines = sorted(all_lines, key=lambda l: l.norm_time)
    matches = [l for l in sorted_lines if regex.search(l.body)]
    if not matches:
        return None
    first = _to_anchor(matches[0])
    if not pair_mode:
        return first
    if len(matches) < 2:
        raise PairUnmatchedError(f"--pair-mode but only 1 match found")
    last = _to_anchor(matches[-1])
    return first, last


class PairUnmatchedError(Exception):
    pass


def _to_anchor(line: NormalizedLine) -> Anchor:
    return Anchor(source_tag=line.source_tag, norm_time=line.norm_time,
                  body=line.body, level=line.level, line_index=-1)
```

## D4 — `_filter_window`

```python
def _filter_window(all_lines: list[NormalizedLine],
                    anchor: Anchor | tuple[Anchor, Anchor],
                    window_s: float) -> list[NormalizedLine]:
    """Keep lines within ±window of anchor time (sorted by norm_time)."""
    if isinstance(anchor, tuple):
        first, last = anchor
        t_start = first.norm_time - timedelta(seconds=window_s)
        t_end = last.norm_time + timedelta(seconds=window_s)
    else:
        t_start = anchor.norm_time - timedelta(seconds=window_s)
        t_end = anchor.norm_time + timedelta(seconds=window_s)

    in_window = [l for l in all_lines if t_start <= l.norm_time <= t_end]
    return sorted(in_window, key=lambda l: l.norm_time)
```

## D5 — `_compute_hint`

```python
_CRITICAL_LEVELS = {"error", "fatal"}  # panic, crash 也归 fatal in _infer_level

def _compute_hint(window_lines: list[NormalizedLine],
                  anchor: Anchor | tuple[Anchor, Anchor]) -> dict | None:
    """Earliest critical event in window. Returns dict or None if no critical."""
    if isinstance(anchor, tuple):
        anchor_first = anchor[0]
    else:
        anchor_first = anchor

    criticals = [l for l in window_lines if l.level in _CRITICAL_LEVELS]
    if not criticals:
        return None
    earliest = min(criticals, key=lambda l: l.norm_time)
    return {
        "earliest_critical": {
            "source": earliest.source_tag,
            "time": earliest.norm_time.isoformat(),
            "level": earliest.level,
            "body": earliest.body[:200],
        },
        "anchor_severity": anchor_first.level,
        "is_anchor_critical": anchor_first.level in _CRITICAL_LEVELS,
    }
```

## D6 — 输出 layout

### Human mode

```
=== Anchor ===
[web-3/nginx 14:02:02.034Z] GET /api/charge 500 8ms

=== Within window (30s) ===
[web-3/nginx     14:02:01.987Z] GET /api/charge ... (start)
[payment-svc-x   14:02:01.991Z] received charge req
[db-1/postgres   14:02:01.500Z] FATAL: too many clients already
                                ↑ earliest critical (hint)
[payment-svc-x   14:02:02.012Z] FATAL connection refused: pg://db:5432
[web-3/nginx     14:02:02.034Z] GET /api/charge 500 8ms ← anchor

=== Summary ===
sources_with_hits: 3
events_in_window: 5
anchor_severity: error
likely_root_cause_hint:
  source: db-1/postgres
  time:   14:02:01.500Z
  body:   FATAL: too many clients already
```

**Pair-mode 变体**：
```
=== Anchors (pair) ===
[svc-x 14:02:01.991Z] span_id=s7 start
[svc-x 14:02:02.234Z] span_id=s7 end
duration: 243ms

=== Within window (30s each side) ===
...
```

字符级 layout 与 `references/correlation_patterns.md` 一致；不允许微调
（已是 SKILL.md 暴露给 Claude 的契约）。

### JSON mode (envelope, NOT NDJSON)

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "",
  "stderr": "",
  "data": {
    "anchor": {
      "source": "web-3/nginx",
      "time": "2026-05-25T14:02:02.034Z",
      "level": "error",
      "body": "GET /api/charge 500 8ms"
    },
    "pair": null,
    "window_seconds": 30,
    "window_lines": [
      {"source": "...", "time": "...", "level": "...", "body": "..."},
      ...
    ],
    "summary": {
      "sources_with_hits": 3,
      "events_in_window": 5,
      "anchor_severity": "error",
      "likely_root_cause_hint": {
        "earliest_critical": {...}
      }
    },
    "clock_skew_ms": {"web-3": 15, ...},
    "failed_fetchers": []
  }
}
```

`--pair-mode` 时 `data.anchor` 仍是 first，`data.pair` 是
`{"first": {...}, "last": {...}, "duration_ms": 243}`。

## D7 — Failure class & exit codes

| Exit code | failure_class | 触发 |
|---|---|---|
| 0 | (none) | 成功（含 partial_fetch warning） |
| 1 | generic | 未预期异常 |
| 40 | source_spec_invalid | 透传 lib.source_spec |
| 44 | no_sources_resolved | 透传 |
| 90 | anchor_not_found | 全部源扫描无 anchor 命中 |
| 91 | window_invalid | _parse_window 失败 |
| 92 | pair_unmatched | pair_mode 仅 1 命中 |
| 93-99 | reserved | 未来扩展 |
| 124 | (透传) | ssh timeout |

**warning 路径**：当 `≥1` fetcher 失败但 anchor 仍能找到 → success=true,
data.failure_class=null, data.failed_fetchers 列出失败源（参 tail_multi
设计 D8 的 partial_fetch warning，但 correlate 不用该 failure_class 名字，
仅放 failed_fetchers 列表）。理由：correlate 用户更关心是否找到 anchor，
而非 fetcher 是否全部跑通。

## D8 — Testing strategy

### Unit (~25) - `tests/test_correlate_unit.py`

| Group | Count | What |
|---|---|---|
| _parse_window | 4 | s/m/h/d 后缀 + edge case（0、负值、bad string） |
| _find_anchor | 5 | normal / 0命中 / 多源都命中取最早 / pair_mode / regex 含特殊字符 |
| _filter_window | 3 | edge: anchor at window boundary / pair_mode window 跨度 / 空 lines |
| _compute_hint | 3 | 有 critical / 无 critical / 多 critical 取最早 |
| 三段输出格式 | 4 | human 单 anchor / human pair / json envelope 字段 / empty window |
| CLI / main | 6 | --help / spec invalid / anchor_not_found / pair_unmatched / partial_fetcher 但 anchor 命中 / window_invalid |

Mock 策略：
- `fetchers.iter_lines` 用 `unittest.mock.patch` 替换为固定行迭代器
- `clock_skew.probe` 返回 `{}` 跳过
- 不触发真 SSH

### Integration (~2 live_ssh)

```python
@pytest.mark.live_ssh
def test_correlate_remote_pai_journal():
    cmd = [sys.executable, str(SCRIPT_PATH),
           "--sources", "journal://pai/sshd",
           "--anchor", "Accepted",
           "--window", "60s",
           "--since", "30min",
           "--json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode == 90:
        pytest.skip("no 'Accepted' login in last 30min on pai; expected on quiet host")
    assert out.returncode == 0
    env = json.loads(out.stdout)
    assert env["success"] is True
    assert env["data"]["anchor"]["source"].startswith("pai/")
    assert len(env["data"]["window_lines"]) >= 1


@pytest.mark.live_ssh  # local fixture, kept in same file
def test_correlate_local_fixture_pair_mode(tmp_path):
    log = tmp_path / "spans.log"
    lines = [
        "2026-05-25T14:02:01.000Z [svc-x] span_id=s7 start",
        "2026-05-25T14:02:01.100Z [svc-x] processing item 42",
        "2026-05-25T14:02:01.243Z [svc-x] span_id=s7 end",
    ]
    log.write_text("\n".join(lines))
    ts_regex = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
    ts_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    cmd = [sys.executable, str(SCRIPT_PATH),
           "--sources", f"file://local{log}?ts_regex={ts_regex}&ts_format={ts_format}",
           "--anchor", "span_id=s7",
           "--pair-mode",
           "--window", "5s",
           "--since", "5min",
           "--json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    env = json.loads(out.stdout)
    assert env["data"]["pair"]["duration_ms"] == 243
```

## Open questions

- **Q1**: anchor 在多源都命中时取最早 vs 用户指定优先源？v1.0 简单取最早，
  user 若想指定可以缩小 `--sources` 集合。
- **Q2**: window 跨过本地时钟与远端时钟漂移阈值时是否提示？v1.0 不专门
  提示（clock_skew_ms 已经在 envelope），文档说明。
- **Q3**: `--pair-mode` 时 first/last 跨越源（first 在 svc-x、last 在
  svc-y）是否合法？v1.0 **是合法的**——anchor 是 regex，不绑定源；这个行为
  允许跨服务 span 分析（如 trace_id 起止跨服务），与
  correlation_patterns.md "Span 模式" 兼容。
