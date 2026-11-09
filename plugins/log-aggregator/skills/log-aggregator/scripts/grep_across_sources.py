#!/usr/bin/env python3
# ruff: noqa: E402, I001
"""grep_across_sources.py - pattern hunt across many sources (v1.0).

Public surface (locked at v0.2 stub - do NOT add flags without a PRD bump)::

    grep_across_sources.py --sources <spec>... --pattern <regex>
        [--since 1h] [--format summary|raw|json]
        [--max-samples 5]

Behavior:
    1. Resolve source specs (lib.source_spec).
    2. Compile --pattern as a Python regex. Missing / empty means heat-map
       mode: count every fetched line per source.
    3. Fetch each source concurrently to exhaustion (lib.fetchers).
    4. Aggregate per-source hit/line counts, all matched events, and bounded
       summary samples.
    5. Emit summary table, raw matched lines, or NDJSON events plus a final
       JSON envelope summary.

Per-script failure_class namespace (exit codes 180-189):
    180  pattern_invalid
    181-189 reserved

Shared log-aggregator failure classes (table in ``lib/envelope.py``):
    40  source_spec_invalid
    41  partial_fetch           (any source failed to fetch — see SKILL.md)
    44  no_sources_resolved
    124 ssh idle/connect timeout (propagated)

See:
    .trellis/tasks/05-25-log-aggregator-grep-across-sources/prd.md
    .trellis/tasks/05-25-log-aggregator-grep-across-sources/design.md
    plugins/log-aggregator/skills/log-aggregator/references/sources.md
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make ``lib`` importable when run directly: ``python grep_across_sources.py ...``.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import envelope as envelope_lib, fetchers, source_spec  # type: ignore[import-not-found]  # noqa: E402
from lib.fetchers import FetcherError, RawLine  # type: ignore[import-not-found]  # noqa: E402
from lib.source_spec import ResolvedSource, SpecError  # type: ignore[import-not-found]  # noqa: E402
from _version import add_version_flag, handle_version

#: Shared with the other two CLIs (see lib/envelope.py); copied so a caller
#: cannot mutate the contract for its siblings.
_EXIT_CODES: dict[str | None, int] = dict(envelope_lib.EXIT_CODES)

_HUMAN_HINTS: dict[str, str] = {
    "source_spec_invalid": "Check source spec syntax: <scheme>://<host>/<payload>[?k=v].",
    "no_sources_resolved": "Brace/glob expansion produced 0 sources; check --sources.",
    "pattern_invalid": "Regex failed to compile; check --pattern syntax.",
    "partial_fetch": "One or more sources could not be fetched; the counts only "
                     "cover the ones that worked. See data.failed_fetchers.",
}

_SAMPLE_WIDTH = 80
_DEFAULT_TIMEOUT_S = 120
_DEFAULT_MAX_CONCURRENT = 8


class PatternError(ValueError):
    """Raised when ``--pattern`` is non-empty but cannot be compiled."""


@dataclass(frozen=True)
class FetcherFailure:
    source_tag: str
    error: str


@dataclass
class HitGroup:
    source_tag: str
    hits: int = 0
    events: list[tuple[str, str]] = field(default_factory=list)
    samples: list[tuple[str, str]] = field(default_factory=list)
    first_seen: str | None = None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grep_across_sources.py")
    p.add_argument("--sources", nargs="+", required=True)
    p.add_argument("--pattern", default=None, help="regex; empty = heat map mode")
    p.add_argument("--since", default="1h")
    p.add_argument("--format", choices=["summary", "raw", "json"], default="summary")
    p.add_argument("--max-samples", type=int, default=5)
    p.add_argument("--json", action="store_true", help="Equivalent to --format json")
    add_version_flag(p)
    return p


def _validate_pattern(value: str | None) -> re.Pattern[str] | None:
    if not value:
        return None
    try:
        return re.compile(value)
    except re.error as exc:
        raise PatternError(f"invalid regex {value!r}: {exc}") from None


def _run_grep_per_source(
    src: ResolvedSource,
    pattern: re.Pattern[str] | None,
    since: str,
    max_samples: int,
    *,
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> HitGroup:
    group = HitGroup(source_tag=src.source_tag)
    sample_limit = max(0, max_samples)
    for raw in fetchers.iter_lines(src, since=since, follow=False, timeout=timeout):
        if pattern is None:
            group.hits += 1
            line_time = _line_time(raw)
            group.events.append((line_time, raw.raw_body))
            if group.first_seen is None:
                group.first_seen = line_time
            if len(group.samples) < sample_limit:
                group.samples.append((line_time, raw.raw_body))
            continue

        if pattern.search(raw.raw_body):
            group.hits += 1
            line_time = _line_time(raw)
            group.events.append((line_time, raw.raw_body))
            if group.first_seen is None:
                group.first_seen = line_time
            if len(group.samples) < sample_limit:
                group.samples.append((line_time, raw.raw_body))
    return group


def _collect_groups(
    sources: list[ResolvedSource],
    pattern: re.Pattern[str] | None,
    since: str,
    max_samples: int,
    *,
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
) -> tuple[list[HitGroup], list[FetcherFailure]]:
    worker_count = max(1, min(max_concurrent, len(sources)))
    groups: list[HitGroup] = []
    failures: list[FetcherFailure] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {
            pool.submit(_run_grep_per_source, src, pattern, since, max_samples): src
            for src in sources
        }
        for future in concurrent.futures.as_completed(future_map):
            src = future_map[future]
            try:
                groups.append(future.result())
            except FetcherError as exc:
                failures.append(FetcherFailure(src.source_tag, _format_fetch_error(exc)))
                groups.append(HitGroup(source_tag=src.source_tag))

    return groups, failures


def _emit_summary(groups: list[HitGroup], is_heatmap: bool, writer: Any) -> None:
    sorted_groups = _sort_groups(groups)
    source_width = max([20, *(len(group.source_tag) + 2 for group in sorted_groups)])
    count_label = "Lines" if is_heatmap else "Hits"
    sample_label = "First seen" if is_heatmap else "Sample (first match)"
    total = sum(group.hits for group in sorted_groups)
    total_label = "lines" if is_heatmap else "hits"
    divider = "-" * (source_width + 11 + len(sample_label))

    print(f"{'Source':<{source_width}}{count_label:>8}   {sample_label}", file=writer)
    print(divider, file=writer)
    for group in sorted_groups:
        if is_heatmap:
            sample = group.first_seen or "(no data)"
        elif group.samples:
            ts, body = group.samples[0]
            sample = _truncate(f"[{_short_ts(ts)}] {body}", _SAMPLE_WIDTH)
        else:
            sample = ""
        print(f"{group.source_tag:<{source_width}}{group.hits:>8}   {sample}", file=writer)
    print(divider, file=writer)
    print(f"Total: {len(sorted_groups)} sources, {total} {total_label}", file=writer)


def _emit_raw(groups: list[HitGroup], writer: Any) -> None:
    for group in _sort_groups(groups):
        for ts, body in group.events:
            print(f"[{group.source_tag} {_short_ts(ts)}] {body}", file=writer)


def _emit_json(
    groups: list[HitGroup],
    failures: list[FetcherFailure],
    is_heatmap: bool,
    writer: Any,
    *,
    elapsed_s: float = 0.0,
) -> dict[str, Any]:
    sorted_groups = _sort_groups(groups)
    for group in sorted_groups:
        for ts, body in group.events:
            event = {"source": group.source_tag, "time": ts, "raw": body}
            print(json.dumps(event, ensure_ascii=False), file=writer)

    per_source = {group.source_tag: group.hits for group in sorted_groups}
    total_key = "total_lines" if is_heatmap else "total_hits"
    envelope = {
        "success": True,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "data": {
            total_key: sum(group.hits for group in sorted_groups),
            "per_source": per_source,
            "sources": [group.source_tag for group in sorted_groups],
            "is_heatmap": is_heatmap,
            "failed_fetchers": [],
            "failure_class": None,
            "elapsed_s": round(elapsed_s, 3),
        },
    }
    # Same partial-fetch rule as tail_multi / correlate: the counts above only
    # describe the sources that answered, so success must not read as "true".
    envelope_lib.apply_fetch_failures(envelope, failures)
    print(json.dumps(envelope, ensure_ascii=False), file=writer)
    return envelope


def _emit_failure(
    fclass: str,
    exit_code: int,
    stderr_msg: str,
    args: argparse.Namespace,
) -> int:
    stderr = stderr_msg + ("\n" if not stderr_msg.endswith("\n") else "")
    fmt = "json" if getattr(args, "json", False) else getattr(args, "format", "summary")
    envelope = {
        "success": False,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": stderr,
        "data": {
            "sources": getattr(args, "sources", []),
            "pattern": getattr(args, "pattern", None),
            "since": getattr(args, "since", None),
            "format": fmt,
            "failure_class": fclass,
            "failed_fetchers": [],
        },
    }
    if fmt == "json":
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(stderr)
        hint = _HUMAN_HINTS.get(fclass)
        if hint:
            sys.stderr.write(f"hint: {hint}\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    early = handle_version("grep_across_sources.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    start_t = time.monotonic()

    try:
        pattern = _validate_pattern(args.pattern)
    except PatternError as exc:
        return _emit_failure(
            "pattern_invalid",
            _EXIT_CODES["pattern_invalid"],
            f"pattern_invalid: {exc}",
            args,
        )

    try:
        sources = source_spec.resolve(args.sources)
    except SpecError as exc:
        return _emit_failure(
            "source_spec_invalid",
            _EXIT_CODES["source_spec_invalid"],
            f"source_spec_invalid: {exc}",
            args,
        )
    if not sources:
        return _emit_failure(
            "no_sources_resolved",
            _EXIT_CODES["no_sources_resolved"],
            "no_sources_resolved: spec expansion produced 0 sources",
            args,
        )

    groups, failures = _collect_groups(
        sources,
        pattern,
        args.since,
        args.max_samples,
    )
    fmt = "json" if args.json else args.format
    is_heatmap = pattern is None
    exit_code = 0
    if fmt == "summary":
        _emit_summary(groups, is_heatmap, sys.stdout)
    elif fmt == "raw":
        _emit_raw(groups, sys.stdout)
    else:
        envelope = _emit_json(
            groups, failures, is_heatmap, sys.stdout,
            elapsed_s=time.monotonic() - start_t,
        )
        exit_code = envelope["exit_code"]

    if fmt != "json" and failures:
        for failure in failures:
            sys.stderr.write(f"failed_fetcher: {failure.source_tag}: {failure.error}\n")
        # Human formats print no envelope, so the exit code is the only signal
        # that the counts are incomplete.
        exit_code = envelope_lib.exit_code_for(envelope_lib.PARTIAL_FETCH)
    return exit_code


def _sort_groups(groups: list[HitGroup]) -> list[HitGroup]:
    return sorted(groups, key=lambda group: (-group.hits, group.source_tag))


def _line_time(raw: RawLine) -> str:
    return raw.raw_ts or "?"


def _short_ts(value: str) -> str:
    if not value or value == "?":
        return "?"
    if "T" in value:
        tail = value.split("T", 1)[1]
        return tail.replace("+00:00", "Z")
    return value


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


def _format_fetch_error(exc: BaseException) -> str:
    return envelope_lib.format_fetch_error(exc)


if __name__ == "__main__":
    sys.exit(main())
