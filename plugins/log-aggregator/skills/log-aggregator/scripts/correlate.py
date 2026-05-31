#!/usr/bin/env python3
# ruff: noqa: E402, I001
"""correlate.py — anchor-and-window correlation across sources (v1.0).

Public surface (locked at v0.2 stub — do NOT add flags without a PRD bump)::

    correlate.py --sources <spec>... --anchor <regex>
        [--window 30s] [--pair-mode] [--since 1h] [--json]

Behavior:
    1. Resolve specs to concrete log sources (lib.source_spec).
    2. Probe remote clock skew once (lib.clock_skew).
    3. Fetch all source lines in the --since search window (lib.fetchers).
    4. Normalize timestamps (lib.time_norm) and infer severity
       (lib.filters.infer_level).
    5. Find the earliest --anchor regex match, or earliest/latest in
       --pair-mode.
    6. Emit every line inside anchor ± --window, sorted by normalized time.
    7. Summarize the likely root-cause hint as the earliest critical event.

Per-script failure_class namespace (exit codes 90-99):
    90  anchor_not_found
    91  window_invalid
    92  pair_unmatched
    93-99 reserved

See:
    .trellis/tasks/05-25-log-aggregator-correlate/prd.md
    .trellis/tasks/05-25-log-aggregator-correlate/design.md
    plugins/log-aggregator/skills/log-aggregator/references/correlation_patterns.md
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Make ``lib`` importable when run directly: ``python correlate.py ...``.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import clock_skew, fetchers, filters, source_spec, time_norm  # type: ignore[import-not-found]  # noqa: E402
from lib.fetchers import FetcherError, RawLine  # type: ignore[import-not-found]  # noqa: E402
from lib.source_spec import ResolvedSource, SpecError  # type: ignore[import-not-found]  # noqa: E402

_EXIT_CODES: dict[str | None, int] = {
    None: 0,
    "anchor_not_found": 90,
    "window_invalid": 91,
    "pair_unmatched": 92,
    "source_spec_invalid": 40,
    "fetcher_failed": 41,
    "no_sources_resolved": 44,
    "_generic": 1,
    "_timeout": 124,
}

_HUMAN_HINTS: dict[str, str] = {
    "anchor_not_found": "No line matched --anchor in the --since window. Try widening --since.",
    "window_invalid": "Window must be like '30s', '2m', '1h', or '1d'.",
    "pair_unmatched": "--pair-mode needs at least 2 anchor matches.",
    "source_spec_invalid": "Check source spec syntax: <scheme>://<host>/<payload>[?k=v].",
    "no_sources_resolved": "Brace/glob expansion produced 0 sources; check --sources.",
}

_WINDOW_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
_CRITICAL_LEVELS = {"error", "fatal", "critical", "crit", "alert", "panic"}
_PAIR_MODE_MIN_MATCHES = 2


class WindowError(ValueError):
    """Raised when ``--window`` cannot be parsed."""


class PairUnmatchedError(ValueError):
    """Raised when ``--pair-mode`` has fewer than two anchor matches."""


@dataclass(frozen=True)
class NormalizedLine:
    raw: RawLine
    norm_time: datetime
    body: str
    level: str

    @property
    def source_tag(self) -> str:
        return self.raw.source.source_tag


@dataclass(frozen=True)
class FetcherFailure:
    source_tag: str
    error: str


@dataclass(frozen=True)
class Anchor:
    source_tag: str
    norm_time: datetime
    body: str
    level: str
    line_index: int


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="correlate.py")
    p.add_argument("--sources", nargs="+", required=True)
    p.add_argument("--anchor", required=True, help="regex matching the anchor line")
    p.add_argument("--window", default="30s", help="Window each side of anchor (e.g. 30s, 2m)")
    p.add_argument("--pair-mode", action="store_true", help="Anchor matches twice; emit duration")
    p.add_argument("--since", default="1h", help="How far back to search for the anchor")
    p.add_argument("--json", action="store_true")
    return p


def _parse_window(value: str) -> float:
    m = _WINDOW_RE.match(value.strip().lower())
    if not m:
        raise WindowError(f"window must be like '30s', '2m', '1h', or '1d', got {value!r}")
    amount = float(m.group(1))
    if amount <= 0:
        raise WindowError(f"window must be positive, got {value!r}")
    unit_s = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return amount * unit_s


def _collect_all_lines(
    sources: list[ResolvedSource],
    since: str,
    *,
    max_concurrent: int = 8,
    timeout: int = 120,
) -> tuple[list[NormalizedLine], list[FetcherFailure]]:
    """Fetch every source to exhaustion, normalize timestamps, keep all lines."""

    def fetch_one(src: ResolvedSource) -> tuple[list[NormalizedLine], FetcherFailure | None]:
        out: list[NormalizedLine] = []
        try:
            for raw in fetchers.iter_lines(src, since=since, follow=False, timeout=timeout):
                norm = time_norm.normalize(raw)
                if norm is None:
                    norm_time = datetime.fromtimestamp(raw.fetched_at, tz=timezone.utc)
                    body = raw.raw_body
                else:
                    norm_time, body = norm
                out.append(
                    NormalizedLine(
                        raw=raw,
                        norm_time=norm_time,
                        body=body,
                        level=filters.infer_level(raw.source.scheme, raw.fields, body),
                    )
                )
        except FetcherError as exc:
            return out, FetcherFailure(src.source_tag, _format_fetch_error(exc))
        return out, None

    worker_count = max(1, min(max_concurrent, len(sources)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(fetch_one, sources))

    lines: list[NormalizedLine] = []
    failures: list[FetcherFailure] = []
    for fetched, failure in results:
        lines.extend(fetched)
        if failure is not None:
            failures.append(failure)
    return lines, failures


def _find_anchor(
    all_lines: list[NormalizedLine],
    anchor_regex: re.Pattern[str],
    *,
    pair_mode: bool,
) -> Anchor | tuple[Anchor, Anchor] | None:
    sorted_lines = sorted(enumerate(all_lines), key=lambda item: item[1].norm_time)
    matches = [(idx, line) for idx, line in sorted_lines if anchor_regex.search(line.body)]
    if not matches:
        return None
    first = _to_anchor(matches[0][1], matches[0][0])
    if not pair_mode:
        return first
    if len(matches) < _PAIR_MODE_MIN_MATCHES:
        raise PairUnmatchedError("--pair-mode needs at least 2 anchor matches; only 1 found")
    last = _to_anchor(matches[-1][1], matches[-1][0])
    return first, last


def _filter_window(
    all_lines: list[NormalizedLine],
    anchor: Anchor | tuple[Anchor, Anchor],
    window_s: float,
) -> list[NormalizedLine]:
    if isinstance(anchor, tuple):
        start = anchor[0].norm_time - timedelta(seconds=window_s)
        end = anchor[1].norm_time + timedelta(seconds=window_s)
    else:
        start = anchor.norm_time - timedelta(seconds=window_s)
        end = anchor.norm_time + timedelta(seconds=window_s)
    return sorted(
        [line for line in all_lines if start <= line.norm_time <= end],
        key=lambda line: line.norm_time,
    )


def _compute_hint(window_lines: list[NormalizedLine]) -> dict[str, Any] | None:
    criticals = [line for line in window_lines if line.level in _CRITICAL_LEVELS]
    if not criticals:
        return None
    earliest = min(criticals, key=lambda line: line.norm_time)
    return {
        "source": earliest.source_tag,
        "time": _iso_z(earliest.norm_time),
        "level": _canonical_level(earliest.level),
        "body": earliest.body[:200],
    }


def _anchor_to_data(anchor: Anchor) -> dict[str, Any]:
    return {
        "source": anchor.source_tag,
        "time": _iso_z(anchor.norm_time),
        "level": _canonical_level(anchor.level),
        "body": anchor.body,
    }


def _line_to_data(line: NormalizedLine, anchor: Anchor | tuple[Anchor, Anchor]) -> dict[str, Any]:
    return {
        "source": line.source_tag,
        "time": _iso_z(line.norm_time),
        "level": _canonical_level(line.level),
        "body": line.body,
        "is_anchor": _is_anchor_line(line, anchor),
        "fields": line.raw.fields,
    }


def _build_success_envelope(
    *,
    anchor: Anchor | tuple[Anchor, Anchor],
    window_lines: list[NormalizedLine],
    window_s: float,
    sources: list[ResolvedSource],
    skew: dict[str, int | None],
    failed_fetchers: list[FetcherFailure],
    elapsed_s: float,
) -> dict[str, Any]:
    first_anchor = anchor[0] if isinstance(anchor, tuple) else anchor
    hint = _compute_hint(window_lines)
    source_hits = sorted({line.source_tag for line in window_lines})
    summary: dict[str, Any] = {
        "sources_with_hits": len(source_hits),
        "events_in_window": len(window_lines),
        "anchor_severity": _canonical_level(first_anchor.level),
        "likely_root_cause_hint": hint,
        "partial_fetch": bool(failed_fetchers),
    }
    if isinstance(anchor, tuple):
        summary["duration_ms"] = round(
            (anchor[1].norm_time - anchor[0].norm_time).total_seconds() * 1000,
            3,
        )

    return {
        "success": True,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "data": {
            "anchor": (
                {"first": _anchor_to_data(anchor[0]), "last": _anchor_to_data(anchor[1])}
                if isinstance(anchor, tuple)
                else _anchor_to_data(anchor)
            ),
            "window": {"seconds": window_s},
            "window_lines": [_line_to_data(line, anchor) for line in window_lines],
            "summary": summary,
            "sources": [src.source_tag for src in sources],
            "clock_skew_ms": skew,
            "failed_fetchers": [
                {"source": failure.source_tag, "error": failure.error}
                for failure in failed_fetchers
            ],
            "failure_class": None,
            "elapsed_s": round(elapsed_s, 3),
        },
    }


def _emit_three_sections_human(
    *,
    anchor: Anchor | tuple[Anchor, Anchor],
    window_lines: list[NormalizedLine],
    window_s: float,
    summary: dict[str, Any],
) -> None:
    sys.stdout.write("=== Anchor ===\n")
    if isinstance(anchor, tuple):
        sys.stdout.write(f"{_format_anchor(anchor[0])} ← anchor_start\n")
        sys.stdout.write(f"{_format_anchor(anchor[1])} ← anchor_end\n")
        sys.stdout.write(f"duration_ms: {summary['duration_ms']}\n")
    else:
        sys.stdout.write(f"{_format_anchor(anchor)}\n")

    sys.stdout.write(f"\n=== Within window ({_format_window(window_s)}) ===\n")
    hint = summary.get("likely_root_cause_hint")
    for line in window_lines:
        marker = ""
        if _is_anchor_line(line, anchor):
            marker = " ← anchor"
        elif hint and line.source_tag == hint["source"] and _iso_z(line.norm_time) == hint["time"]:
            marker = " ↑ earliest critical (hint)"
        sys.stdout.write(f"{_format_line(line)}{marker}\n")

    sys.stdout.write("\n=== Summary ===\n")
    sys.stdout.write(f"sources_with_hits: {summary['sources_with_hits']}\n")
    sys.stdout.write(f"anchor_severity: {summary['anchor_severity']}\n")
    if "duration_ms" in summary:
        sys.stdout.write(f"duration_ms: {summary['duration_ms']}\n")
    if hint:
        sys.stdout.write("likely_root_cause_hint:\n")
        sys.stdout.write(
            "  earliest_critical_event in window: "
            f"{hint['source']} {hint['level']} {hint['body'][:100]}\n"
        )


def _emit_failure(
    fclass: str,
    exit_code: int,
    stderr_msg: str,
    args: argparse.Namespace,
    *,
    failed_fetchers: list[FetcherFailure] | None = None,
) -> int:
    stderr = stderr_msg + ("\n" if not stderr_msg.endswith("\n") else "")
    envelope = {
        "success": False,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": stderr,
        "data": {
            "sources": args.sources,
            "anchor": getattr(args, "anchor", None),
            "window": getattr(args, "window", None),
            "since": getattr(args, "since", None),
            "pair_mode": getattr(args, "pair_mode", None),
            "failure_class": fclass,
            "failed_fetchers": [
                {"source": failure.source_tag, "error": failure.error}
                for failure in (failed_fetchers or [])
            ],
        },
    }
    if args.json:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(stderr)
        hint = _HUMAN_HINTS.get(fclass)
        if hint:
            sys.stderr.write(f"hint: {hint}\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0915 — CLI orchestration
    args = build_parser().parse_args(argv)
    start_t = time.monotonic()

    try:
        window_s = _parse_window(args.window)
    except WindowError as exc:
        return _emit_failure(
            "window_invalid", _EXIT_CODES["window_invalid"],
            f"window_invalid: {exc}", args,
        )

    try:
        anchor_re = re.compile(args.anchor)
    except re.error as exc:
        return _emit_failure(
            "anchor_not_found", _EXIT_CODES["anchor_not_found"],
            f"anchor_not_found: invalid --anchor regex: {exc}", args,
        )

    try:
        sources = source_spec.resolve(args.sources)
    except SpecError as exc:
        return _emit_failure(
            "source_spec_invalid", _EXIT_CODES["source_spec_invalid"],
            f"source_spec_invalid: {exc}", args,
        )
    if not sources:
        return _emit_failure(
            "no_sources_resolved", _EXIT_CODES["no_sources_resolved"],
            "no_sources_resolved: spec expansion produced 0 sources", args,
        )

    remote_hosts = [src.host for src in sources if src.host != "local"]
    skew = clock_skew.probe(remote_hosts, timeout=5.0)

    lines, failed_fetchers = _collect_all_lines(sources, args.since)

    try:
        anchor = _find_anchor(lines, anchor_re, pair_mode=args.pair_mode)
    except PairUnmatchedError as exc:
        return _emit_failure(
            "pair_unmatched", _EXIT_CODES["pair_unmatched"],
            f"pair_unmatched: {exc}", args, failed_fetchers=failed_fetchers,
        )
    if anchor is None:
        return _emit_failure(
            "anchor_not_found", _EXIT_CODES["anchor_not_found"],
            "anchor_not_found: no line matched --anchor in --since window",
            args,
            failed_fetchers=failed_fetchers,
        )

    window_lines = _filter_window(lines, anchor, window_s)
    envelope = _build_success_envelope(
        anchor=anchor,
        window_lines=window_lines,
        window_s=window_s,
        sources=sources,
        skew=skew,
        failed_fetchers=failed_fetchers,
        elapsed_s=time.monotonic() - start_t,
    )

    if args.json:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        _emit_three_sections_human(
            anchor=anchor,
            window_lines=window_lines,
            window_s=window_s,
            summary=envelope["data"]["summary"],
        )
        if failed_fetchers:
            for failure in failed_fetchers:
                sys.stderr.write(f"failed_fetcher: {failure.source_tag}: {failure.error}\n")
    return 0


def _to_anchor(line: NormalizedLine, idx: int) -> Anchor:
    return Anchor(
        source_tag=line.source_tag,
        norm_time=line.norm_time,
        body=line.body,
        level=line.level,
        line_index=idx,
    )


def _is_anchor_line(line: NormalizedLine, anchor: Anchor | tuple[Anchor, Anchor]) -> bool:
    anchors = anchor if isinstance(anchor, tuple) else (anchor,)
    return any(
        line.source_tag == item.source_tag
        and line.norm_time == item.norm_time
        and line.body == item.body
        for item in anchors
    )


def _format_anchor(anchor: Anchor) -> str:
    return f"[{anchor.source_tag} {_short_ts(anchor.norm_time)}] {anchor.body}"


def _format_line(line: NormalizedLine) -> str:
    return f"[{line.source_tag} {_short_ts(line.norm_time)}] {line.body}"


def _short_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_window(window_s: float) -> str:
    if float(window_s).is_integer():
        return f"{int(window_s)}s"
    return f"{window_s}s"


def _canonical_level(level: str) -> str:
    lower = (level or "info").lower()
    if lower in {"critical", "crit", "alert", "panic"}:
        return "fatal"
    if lower == "err":
        return "error"
    if lower == "warning":
        return "warn"
    return lower


def _format_fetch_error(exc: BaseException) -> str:
    if isinstance(exc, FetcherError):
        tail = exc.stderr_tail.strip().splitlines()[-1] if exc.stderr_tail else ""
        return f"rc={exc.returncode} {tail}".strip()
    return f"{type(exc).__name__}: {exc}".strip()


if __name__ == "__main__":
    sys.exit(main())
