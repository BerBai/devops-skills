# Design — grep_across_sources v1.0

## Architecture overview

```
CLI args ───▶ build_parser() ──▶ args

                  │
                  ▼
         ┌─────────────────────────────┐
         │ _validate_pattern(args.pat) │
         │  → re.Pattern | None        │
         │  失败 → exit 180            │
         └────────┬────────────────────┘
                  │
                  ▼
         ┌─────────────────────────────┐
         │ lib.source_spec.resolve     │
         │  失败 40 / 空集 44          │
         └────────┬────────────────────┘
                  │
                  ▼
         ┌─────────────────────────────┐
         │ ThreadPoolExecutor(8):      │
         │   each source:              │
         │     fetchers.iter_lines     │
         │     for line in iter:       │
         │       if pat: if pat.search │
         │         hits[src] += 1      │
         │         samples[src].push   │
         │         (if --format=raw|json)
         │       else (heat-map):      │
         │         lines[src] += 1     │
         │ collect partial failures    │
         └────────┬────────────────────┘
                  │
                  ▼
         ┌─────────────────────────────┐
         │ dispatch by --format:       │
         │   summary → _emit_summary   │
         │   raw     → _emit_raw       │
         │   json    → _emit_json      │
         └─────────────────────────────┘
```

最简单的 child：一次性查询、无 reorder、无关联、无 filter。

## Module structure

```
plugins/log-aggregator/skills/log-aggregator/scripts/
└── grep_across_sources.py        # ~280 LOC，本任务 owner
```

内部分块：
- `build_parser` / `main` / `_emit_failure` / `_validate_pattern`
- `_HitGroup` dataclass（hits / samples / first_seen）
- `_run_grep_per_source(src, pat, since, mode_heatmap) -> _HitGroup`
- `_emit_summary(per_source, total, is_heatmap, writer)`
- `_emit_raw(per_source, writer)`
- `_emit_json(per_source, total, is_heatmap, writer, failed_fetchers)`

## D1 — `_validate_pattern`

```python
def _validate_pattern(s: str | None) -> re.Pattern | None:
    """None/empty string → heat-map mode (return None).
    Valid regex → compiled Pattern.
    Invalid → raise PatternError."""
    if not s:
        return None
    try:
        return re.compile(s)
    except re.error as e:
        raise PatternError(f"invalid regex {s!r}: {e}")


class PatternError(ValueError):
    pass
```

## D2 — `_HitGroup` 与 `_run_grep_per_source`

```python
@dataclass
class _HitGroup:
    source_tag: str
    hits: int = 0
    samples: list[tuple[str, str]] = field(default_factory=list)  # [(ts, body), ...]
    first_seen: str | None = None  # iso, heat-map 模式用


def _run_grep_per_source(src: ResolvedSource, pattern: re.Pattern | None,
                         since: str, max_samples: int) -> _HitGroup:
    """Fetch lines from one source, run pattern.search per line.
    Heat-map mode (pattern=None): count all lines."""
    group = _HitGroup(source_tag=src.source_tag)
    for raw in fetchers.iter_lines(src, since=since, follow=False, timeout=120):
        if pattern is None:
            # Heat-map mode
            group.hits += 1
            if group.first_seen is None:
                group.first_seen = raw.raw_ts or "?"
        else:
            if pattern.search(raw.raw_body):
                group.hits += 1
                if len(group.samples) < max_samples:
                    group.samples.append((raw.raw_ts or "?", raw.raw_body))
                if group.first_seen is None:
                    group.first_seen = raw.raw_ts or "?"
    return group


# main：并发 + 收集 partial failures
def _collect_groups(sources, pattern, since, max_samples, max_concurrent=8
                    ) -> tuple[list[_HitGroup], list[FetcherFailure]]:
    groups, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        future_map = {pool.submit(_run_grep_per_source, s, pattern, since,
                                  max_samples): s for s in sources}
        for f in concurrent.futures.as_completed(future_map):
            src = future_map[f]
            try:
                groups.append(f.result())
            except fetchers.FetcherError as e:
                failures.append(FetcherFailure(src.source_tag, str(e)))
                groups.append(_HitGroup(source_tag=src.source_tag))  # 0 hits 占位
    return groups, failures
```

## D3 — Output: summary 格式

```python
def _emit_summary(groups: list[_HitGroup], is_heatmap: bool,
                  writer, total: int):
    sorted_groups = sorted(groups, key=lambda g: (-g.hits, g.source_tag))
    src_col_width = max(20, max(len(g.source_tag) for g in sorted_groups) + 2)
    hits_label = "Lines" if is_heatmap else "Hits"
    sample_label = "First seen" if is_heatmap else "Sample (first match)"

    print(f"{'Source':<{src_col_width}}{hits_label:>8}   {sample_label}",
          file=writer)
    print("─" * (src_col_width + 12 + len(sample_label)), file=writer)
    for g in sorted_groups:
        sample = ""
        if is_heatmap:
            sample = g.first_seen or "(no data)"
        elif g.samples:
            ts, body = g.samples[0]
            sample = f"[{ts}] {body}"
            if len(sample) > 80:
                sample = sample[:77] + "..."
        print(f"{g.source_tag:<{src_col_width}}{g.hits:>8}   {sample}",
              file=writer)
    print("─" * (src_col_width + 12 + len(sample_label)), file=writer)
    obj_label = "lines" if is_heatmap else "hits"
    print(f"Total: {len(groups)} sources, {total} {obj_label}", file=writer)
```

## D4 — Output: raw 与 json 格式

```python
def _emit_raw(groups: list[_HitGroup], writer):
    """Flatten all matched lines. `--max-samples` does not cap raw/json,
    only the summary-table sample column."""
    for g in groups:
        for ts, body in g.events:
            print(f"[{g.source_tag} {ts}] {body}", file=writer)


def _emit_json(groups, failures, is_heatmap, writer):
    # Stream NDJSON events
    for g in groups:
        for ts, body in g.events:
            event = {"source": g.source_tag, "time": ts, "raw": body}
            print(json.dumps(event, ensure_ascii=False), file=writer)
    # Trailing envelope summary
    per_source = {g.source_tag: g.hits for g in groups}
    total_key = "total_lines" if is_heatmap else "total_hits"
    envelope = {
        "success": True, "exit_code": 0, "stdout": "", "stderr": "",
        "data": {
            total_key: sum(g.hits for g in groups),
            "per_source": per_source,
            "sources": [g.source_tag for g in groups],
            "is_heatmap": is_heatmap,
            "failed_fetchers": [{"source": f.source_tag, "error": f.error_msg}
                                for f in failures],
            **({"failure_class": "partial_fetch"} if failures else {}),
        }
    }
    print(json.dumps(envelope, ensure_ascii=False), file=writer)
```

**`--format raw/json` 行为说明**：与 grep -r 类似，输出全部命中事件。
`--max-samples` 只限制 summary table 的首条/样例存储，不限制 raw/json 事件流。

## D5 — Failure class & exit codes

| Exit code | failure_class | 触发 |
|---|---|---|
| 0 | (none) | 成功（含 partial_fetch warning） |
| 1 | generic | 未预期异常 |
| 40 | source_spec_invalid | 透传 |
| 44 | no_sources_resolved | 透传 |
| 180 | pattern_invalid | `_validate_pattern` 失败 |
| 181-189 | reserved | 未来扩展 |
| 124 | (透传) | ssh timeout |

## D6 — Testing strategy

### Unit (~20) - `tests/test_grep_across_sources_unit.py`

| Group | Count | What |
|---|---|---|
| _validate_pattern | 3 | empty/None → heat-map / 合法 regex / 非法 regex |
| Summary 格式 | 4 | 排序、宽度对齐、sample 截断 80、`--max-samples=1`、0 命中行 |
| Raw 格式 | 3 | 多源合并、空命中、samples 与 hits 数对齐 |
| JSON 格式 | 3 | NDJSON 流式、envelope total_hits / total_lines / per_source 字段 |
| Heat-map 模式 | 3 | --pattern 空 → mode 切换 / summary 标题 "Lines" / envelope total_lines |
| CLI / main | 4 | args bad / pattern invalid 180 / partial_fetcher exit 0 + failed_fetchers / sources 空 |

### Integration (~2 live_ssh)

```python
@pytest.mark.live_ssh
def test_grep_remote_pai_journal_pattern():
    cmd = [sys.executable, str(SCRIPT_PATH),
           "--sources", "journal://pai/sshd",
           "--pattern", "sshd",
           "--since", "30min",
           "--format", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    lines = out.stdout.strip().split("\n")
    *events, summary_line = lines
    env = json.loads(summary_line)
    assert env["success"] is True
    assert env["data"]["total_hits"] >= 1  # sshd 行总有的
    assert "pai/sshd" in env["data"]["per_source"]


@pytest.mark.live_ssh  # local fixture
def test_grep_local_fixture_heatmap(tmp_path):
    log1 = tmp_path / "a.log"; log1.write_text("\n".join(f"l{i}" for i in range(10)))
    log2 = tmp_path / "b.log"; log2.write_text("\n".join(f"l{i}" for i in range(100)))
    cmd = [sys.executable, str(SCRIPT_PATH),
           "--sources", f"file://local{log1}", f"file://local{log2}",
           "--format", "json"]  # 无 --pattern → heat-map
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    lines = out.stdout.strip().split("\n")
    env = json.loads(lines[-1])
    assert env["data"]["is_heatmap"] is True
    assert env["data"]["total_lines"] == 110
    assert env["data"]["per_source"][f"local{log2}"] == 100
```

## Open questions

- **Q1**: `--max-samples` 应否限制 raw/json？v1.0 不限制；raw/json 输出
  全部事件，`--max-samples` 只限制 summary 样例列。
- **Q2**: heat-map 排序应否提供 `--sort source` 选项？v1.0 不加，按 hits
  降序。
- **Q3**: 多 pattern 是否值得？v1.0 不加，用户 `pattern1|pattern2`。
