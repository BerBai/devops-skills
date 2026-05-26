#!/usr/bin/env python3
"""tail_multi.py — multi-source log tail with unified prefix (v1.0).

Public surface (locked at v0.2 stub — do NOT add flags without a PRD bump)::

    tail_multi.py --sources <spec> [<spec>...]
        [--follow] [--since <duration>]
        [--min-level trace|debug|info|warn|error|fatal]
        [--suppress] [--aggressive-suppress]
        [--max-concurrent N]
        [--no-default-filters]
        [--json]

Source specs:
    journal://<host>/<unit?>
    docker://<host>/<container>
    kube://<host>/<ns>/<pod-or-glob>[?label=<sel>][?previous=true]
    file://<host>/<path>[?ts_regex=<r>&ts_format=<f>]

Behavior:
    1. Resolve brace/glob specs to concrete sources (lib.source_spec).
    2. Probe each remote host's clock once for skew metadata
       (lib.clock_skew).
    3. Fan out one fetcher per source via ThreadPoolExecutor; each fetcher
       streams stdout lines from local subprocess or remote `ssh`
       (lib.fetchers).
    4. Pipe lines through (a) time normalization (lib.time_norm) and
       (b) noise/level/suppress filters (lib.filters).
    5. Cross-source reorder buffer (256 lines / 2s flush) restores
       approximate chronological order before output.
    6. Human-mode prefix ``[<source_tag> HH:MM:SS.fff] <body>``; ``--json``
       mode emits NDJSON events + a final envelope summary line.

Per-script failure_class namespace (exit codes 40-49):
    40  source_spec_invalid
    41  fetcher_failed       (every fetcher failed)
    42  clock_skew_failed    (reserved — v1.0 warns only, does not trigger)
    43  normalize_failed     (>50% of lines failed time normalization)
    44  no_sources_resolved  (brace/glob expansion produced 0 sources)
    45  partial_fetch        (>=1 fetcher failed; warning, exit 0)
    46-49  reserved
    124  ssh idle/connect timeout (propagated)

See:
    .trellis/tasks/05-25-log-aggregator-tail-multi/prd.md      — R1-R12
    .trellis/tasks/05-25-log-aggregator-tail-multi/design.md   — D1-D10
    .trellis/spec/backend/adr-001-ssh-execute.md               — transport
    plugins/log-aggregator/skills/log-aggregator/references/   — sources,
                                                               noise rules
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import json
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make ``lib`` importable when run directly: ``python tail_multi.py ...``.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (  # noqa: E402
    clock_skew,
    fetchers,
    filters,
    source_spec,
    time_norm,
)
from lib.fetchers import FetcherError, RawLine  # noqa: E402
from lib.source_spec import ResolvedSource, SpecError  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXIT_CODES: dict[str | None, int] = {
    None: 0,
    "source_spec_invalid": 40,
    "fetcher_failed": 41,
    "clock_skew_failed": 42,
    "normalize_failed": 43,
    "no_sources_resolved": 44,
    "partial_fetch": 0,  # warning class — exit 0, paired failure_class
    "_generic": 1,
    "_timeout": 124,
}

_HUMAN_HINTS: dict[str, str] = {
    "source_spec_invalid": "Check source spec syntax: <scheme>://<host>/<payload>[?k=v]",
    "fetcher_failed": "All fetchers died; see data.failed_fetchers for per-source detail.",
    "clock_skew_failed": "Clock-skew probe failed for all remote hosts.",
    "normalize_failed": ">50% of lines could not be time-normalized.",
    "no_sources_resolved": "Brace/glob expansion produced 0 sources; check --sources.",
    "partial_fetch": "One or more fetchers died; remaining sources continued.",
}

_REORDER_MAX_PENDING = 256
_REORDER_FLUSH_S = 2.0
_LINE_QUEUE_MAX = 4096
_DRAIN_POLL_S = 0.25

_HIGHLIGHT_PATTERNS = (
    "error", "err ", "fatal", "panic", "oom", "crash", "killed",
    "timeout", "refused", "reset by peer", "broken pipe",
    "denied", "forbidden", "unauthorized",
    "out of memory", "disk full", "no space left",
)


# ---------------------------------------------------------------------------
# CLI parser (v0.2 surface, do not extend)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tail_multi.py")
    p.add_argument(
        "--sources", nargs="+", required=True,
        help="One or more source specs (see references/sources.md)",
    )
    p.add_argument("--follow", action="store_true")
    p.add_argument(
        "--since", default="5min",
        help="Lookback duration or ISO timestamp (default: 5min)",
    )
    p.add_argument(
        "--min-level",
        choices=["trace", "debug", "info", "warn", "error", "fatal"],
        default="trace",
    )
    p.add_argument(
        "--suppress", action="store_true",
        help="Suppress repeated lines (syslog-style)",
    )
    p.add_argument("--aggressive-suppress", action="store_true")
    p.add_argument("--max-concurrent", type=int, default=8)
    p.add_argument(
        "--no-default-filters", action="store_true",
        help="Disable built-in noise deny-list",
    )
    p.add_argument("--json", action="store_true")
    return p


# ---------------------------------------------------------------------------
# Internal data classes
# ---------------------------------------------------------------------------


@dataclass
class FetchedLine:
    """One line, post time-normalization and level inference."""

    raw: RawLine
    norm_time: datetime
    body: str
    level: str

    @property
    def source_tag(self) -> str:
        return self.raw.source.source_tag


# Sentinel pushed by fetcher workers when they exhaust their source.
class _Sentinel:
    __slots__ = ("source_tag",)

    def __init__(self, source_tag: str) -> None:
        self.source_tag = source_tag


# ---------------------------------------------------------------------------
# Reorder buffer (lives in tail_multi.py per design D4: correlate/grep have
# different ordering needs and don't share this).
# ---------------------------------------------------------------------------


class ReorderBuffer:
    """Bounded chronological buffer that flushes once entries age out.

    Entries are kept sorted by norm_time. ``pop_ready()`` returns lines
    that are safe to emit because either the buffer is full or the oldest
    entry is older than ``flush_after_s``.
    """

    def __init__(
        self,
        max_pending: int = _REORDER_MAX_PENDING,
        flush_after_s: float = _REORDER_FLUSH_S,
    ) -> None:
        self._buf: list[tuple[float, int, FetchedLine]] = []
        self._counter = 0  # monotonic tiebreaker so equal times are stable
        self._max = max_pending
        self._flush_s = flush_after_s

    def push(self, line: FetchedLine) -> None:
        ts = line.norm_time.timestamp()
        self._counter += 1
        bisect.insort(self._buf, (ts, self._counter, line))

    def pop_ready(self) -> list[FetchedLine]:
        out: list[FetchedLine] = []
        threshold = datetime.now(tz=timezone.utc).timestamp() - self._flush_s
        while self._buf and (
            len(self._buf) > self._max or self._buf[0][0] < threshold
        ):
            out.append(self._buf.pop(0)[2])
        return out

    def drain(self) -> list[FetchedLine]:
        out = [item[2] for item in self._buf]
        self._buf.clear()
        return out

    def __len__(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------


class OutputWriter:
    """Owns stdout. Human mode = prefixed text; --json = NDJSON events."""

    def __init__(self, *, json_mode: bool) -> None:
        self.json_mode = json_mode
        self.use_color = (
            not json_mode
            and sys.stdout.isatty()
            and not os.environ.get("NO_COLOR")
        )
        self._events_emitted = 0
        self._lock = threading.Lock()

    @property
    def events_emitted(self) -> int:
        return self._events_emitted

    def banner(self, meta: dict[str, Any]) -> None:
        if self.json_mode:
            return  # human-only; JSON mode keeps stdout pure NDJSON
        sources = meta.get("sources", [])
        skew = meta.get("clock_skew_ms", {})
        skew_parts = []
        for host, ms in skew.items():
            if ms is None:
                skew_parts.append(f"{host}:probe_failed")
            else:
                skew_parts.append(f"{host}:{ms:+d}ms")
        skew_str = ", ".join(skew_parts) if skew_parts else "n/a"
        sys.stderr.write(
            f"# tail_multi: {len(sources)} sources, clock_skew "
            f"({skew_str})\n"
        )

    def emit(self, line: FetchedLine) -> None:
        with self._lock:
            self._events_emitted += 1
            if self.json_mode:
                ev = {
                    "source": line.source_tag,
                    "time": line.norm_time.astimezone(timezone.utc).isoformat()
                            .replace("+00:00", "Z"),
                    "level": line.level,
                    "raw": line.body,
                    "fields": line.raw.fields,
                }
                sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            else:
                ts_short = line.norm_time.astimezone(timezone.utc).strftime(
                    "%H:%M:%S.%f"
                )[:-3]
                prefix = f"[{line.source_tag} {ts_short}]"
                body = line.body
                if self.use_color and _should_highlight(body):
                    body = f"\033[31m{body}\033[0m"  # red
                sys.stdout.write(f"{prefix} {body}\n")
                sys.stdout.flush()

    def summary(self, envelope: dict[str, Any]) -> None:
        """Emit the final envelope summary line.

        - JSON mode: write the envelope as a final NDJSON line (no
          ``source`` key, which is how callers distinguish event from
          summary).
        - Human mode: write a separator + short table to stderr so stdout
          stays a clean stream of lines.
        """
        if self.json_mode:
            sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return
        data = envelope.get("data", {})
        sources = data.get("sources", [])
        failed = data.get("failed_fetchers", [])
        sys.stderr.write(
            "─" * 60 + "\n"
            f"# sources={len(sources)} events={data.get('events_emitted', 0)} "
            f"failed_fetchers={len(failed)} "
            f"elapsed={data.get('elapsed_s', 0)}s\n"
        )
        if failed:
            for tag, err in failed:
                sys.stderr.write(f"#   failed: {tag} — {err}\n")


def _should_highlight(body: str) -> bool:
    lower = body.lower()
    return any(kw in lower for kw in _HIGHLIGHT_PATTERNS)


# ---------------------------------------------------------------------------
# Fetcher worker
# ---------------------------------------------------------------------------


def _run_fetcher(
    src: ResolvedSource,
    args: argparse.Namespace,
    line_q: queue.Queue[RawLine | _Sentinel],
    stop_event: threading.Event,
) -> None:
    """Push fetched RawLines onto ``line_q`` until the source is exhausted.

    On FetcherError or any other exception this function lets the
    exception escape — the ThreadPoolExecutor records it on the future
    and the main thread maps it to ``partial_fetch``.
    """
    timeout = 120 if not args.follow else 600
    try:
        for raw in fetchers.iter_lines(
            src, since=args.since, follow=args.follow, timeout=timeout,
        ):
            if stop_event.is_set():
                break
            line_q.put(raw)
    finally:
        line_q.put(_Sentinel(src.source_tag))


# ---------------------------------------------------------------------------
# Drain loop
# ---------------------------------------------------------------------------


def _drain_loop(
    line_q: queue.Queue[RawLine | _Sentinel],
    n_sources: int,
    pipeline: filters.FilterPipeline,
    reorder: ReorderBuffer,
    writer: OutputWriter,
    stop_event: threading.Event,
    counters: dict[str, int],
) -> None:
    """Pull from the queue, normalize / filter / reorder / emit."""
    sentinels_seen = 0
    while sentinels_seen < n_sources and not stop_event.is_set():
        try:
            item = line_q.get(timeout=_DRAIN_POLL_S)
        except queue.Empty:
            for ready in reorder.pop_ready():
                writer.emit(ready)
            continue

        if isinstance(item, _Sentinel):
            sentinels_seen += 1
            continue

        counters["lines_in"] += 1

        norm = time_norm.normalize(item)
        if norm is None:
            # Only count as a normalize failure when the source was
            # *expected* to provide timestamps: journal/docker/kube always
            # carry one; file only when the user opted in via ts_regex.
            scheme = item.source.scheme
            has_ts_intent = scheme in {"journal", "docker", "kube"} or (
                scheme == "file" and item.source.query.get("ts_regex")
            )
            if has_ts_intent:
                counters["normalize_failed"] += 1
            dt = datetime.fromtimestamp(item.fetched_at, tz=timezone.utc)
            body = item.raw_body
        else:
            dt, body = norm

        level = filters.infer_level(item.source.scheme, item.fields, body)
        fline = FetchedLine(raw=item, norm_time=dt, body=body, level=level)

        if pipeline.should_drop(fline):
            counters["dropped"] += 1
            continue

        reorder.push(fline)
        for ready in reorder.pop_ready():
            writer.emit(ready)

    # Final flush: emit anything still buffered.
    for ready in reorder.drain():
        writer.emit(ready)


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def _build_envelope(
    *,
    success: bool,
    exit_code: int,
    stderr: str,
    sources: list[ResolvedSource],
    skew: dict[str, int | None],
    failure_class: str | None,
    failed_fetchers: list[tuple[str, str]],
    events_emitted: int,
    counters: dict[str, int],
    elapsed_s: float,
    terminated_by: str | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": stderr,
        "data": {
            "sources": [s.source_tag for s in sources],
            "clock_skew_ms": skew,
            "events_emitted": events_emitted,
            "failure_class": failure_class,
            "failed_fetchers": [
                {"source": tag, "error": err} for tag, err in failed_fetchers
            ],
            "lines_in": counters.get("lines_in", 0),
            "lines_dropped": counters.get("dropped", 0),
            "normalize_failed_count": counters.get("normalize_failed", 0),
            "elapsed_s": round(elapsed_s, 3),
            "terminated_by": terminated_by,
        },
    }


def _emit_failure(
    fclass: str, exit_code: int, stderr_msg: str, args: argparse.Namespace
) -> int:
    envelope = {
        "success": False,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": stderr_msg + ("\n" if not stderr_msg.endswith("\n") else ""),
        "data": {
            "sources": args.sources,
            "failure_class": fclass,
            "events_emitted": 0,
            "failed_fetchers": [],
        },
    }
    if args.json:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(str(envelope["stderr"]))
        hint = _HUMAN_HINTS.get(fclass)
        if hint:
            sys.stderr.write(f"hint: {hint}\n")
    return exit_code


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 — single-entry orchestration
    args = build_parser().parse_args(argv)
    start_t = time.monotonic()

    # 1. spec resolution ----------------------------------------------------
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

    # 2. clock skew probe (best-effort) -------------------------------------
    remote_hosts = [s.host for s in sources if s.host != "local"]
    skew = clock_skew.probe(remote_hosts, timeout=5.0)

    writer = OutputWriter(json_mode=args.json)
    writer.banner({"sources": sources, "clock_skew_ms": skew})

    # 3. filter pipeline ----------------------------------------------------
    pipeline = filters.build_pipeline(
        no_default=args.no_default_filters,
        min_level=args.min_level,
        suppress=args.suppress,
        aggressive=args.aggressive_suppress,
        user_overrides=filters.load_user_overrides(),
    )

    reorder = ReorderBuffer()
    line_q: queue.Queue[RawLine | _Sentinel] = queue.Queue(maxsize=_LINE_QUEUE_MAX)
    stop_event = threading.Event()
    counters: dict[str, int] = {"lines_in": 0, "dropped": 0, "normalize_failed": 0}

    # 4. signal handlers (graceful shutdown) --------------------------------
    def _signal(_sig: int, _frm: Any) -> None:
        stop_event.set()

    old_int = signal.signal(signal.SIGINT, _signal)
    old_term = signal.signal(signal.SIGTERM, _signal)

    failed_fetchers: list[tuple[str, str]] = []
    terminated_by: str | None = None

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, args.max_concurrent),
            thread_name_prefix="fetcher",
        ) as pool:
            futures = {
                pool.submit(_run_fetcher, src, args, line_q, stop_event): src
                for src in sources
            }
            try:
                _drain_loop(
                    line_q, len(sources), pipeline, reorder, writer,
                    stop_event, counters,
                )
            except KeyboardInterrupt:
                stop_event.set()
                terminated_by = "interrupted"

            # Collect per-source failures (excluding our own stop_event cancels).
            for fut, src in futures.items():
                fut_exc = fut.exception()
                if fut_exc is None:
                    continue
                if stop_event.is_set() and isinstance(fut_exc, KeyboardInterrupt):
                    continue
                failed_fetchers.append((src.source_tag, _format_fetch_error(fut_exc)))
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    if stop_event.is_set() and terminated_by is None:
        terminated_by = "signal"

    # 5. failure_class resolution ------------------------------------------
    failure_class: str | None = None
    exit_code = 0
    n_failed = len(failed_fetchers)
    if n_failed == len(sources) and n_failed > 0:
        failure_class = "fetcher_failed"
        exit_code = _EXIT_CODES["fetcher_failed"]
    elif n_failed > 0:
        failure_class = "partial_fetch"
        exit_code = _EXIT_CODES["partial_fetch"]
    elif counters["lines_in"] > 0 and (
        counters["normalize_failed"] * 2 > counters["lines_in"]
    ):
        failure_class = "normalize_failed"
        exit_code = _EXIT_CODES["normalize_failed"]

    envelope = _build_envelope(
        success=(exit_code == 0),
        exit_code=exit_code,
        stderr="",
        sources=sources,
        skew=skew,
        failure_class=failure_class,
        failed_fetchers=failed_fetchers,
        events_emitted=writer.events_emitted,
        counters=counters,
        elapsed_s=time.monotonic() - start_t,
        terminated_by=terminated_by,
    )
    writer.summary(envelope)
    return exit_code


def _format_fetch_error(exc: BaseException) -> str:
    if isinstance(exc, FetcherError):
        tail = exc.stderr_tail.strip().splitlines()[-1] if exc.stderr_tail else ""
        return f"rc={exc.returncode} {tail}".strip()
    return f"{type(exc).__name__}: {exc}".strip()


if __name__ == "__main__":
    sys.exit(main())
