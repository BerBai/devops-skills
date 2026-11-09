#!/usr/bin/env python3
"""audit.py - summarize, prune and rotate the ssh-core audit trail (DV-12/13).

Every script can append JSONL records to ``$TMPDIR/ssh-core/<md5>.jsonl``
(``--audit-log`` or ``SSH_CORE_AUDIT=on``), but until now nothing read them
back: the trail grew forever and the only way to answer "which host failed
most in the last day" was to write a throwaway ``jq`` pipeline.

This script is that pipeline, with the skill's usual contract:

``summarize``
    Aggregate the records of the last ``--since`` window into per-host rows:
    calls, failures, failure classes, slowest command, most recent error. The
    answer to "which host is the problem" is the first row.
``prune``
    Delete records older than ``--older-than`` (default 30d), rewriting each
    file atomically. ``--dry-run`` reports what would go.
``rotate``
    Force the size-based rotation that :func:`lib.append_audit` performs on
    write (``--max-bytes``), for hosts that have not written since the policy
    changed.
``list``
    Show the files, their size and their record count, without parsing every
    record unless asked.

Usage::

    python scripts/audit.py summarize --since 24h [--host a,b] [--json]
    python scripts/audit.py prune --older-than 30d [--dry-run] [--verbose]
    python scripts/audit.py rotate [--max-bytes 10MB] [--keep 3] [--dry-run]
    python scripts/audit.py list [--all] [--no-count] [--json]

``--since`` / ``--older-than`` accept ``30s``, ``15m``, ``24h``, ``7d``,
``2w`` — deliberately not ``1y``: a bare year is ambiguous (365d? 12w?) and a
window that large is a sign the caller wanted ``--all``.

Nothing here ever prints a password: the records never contain one, and the
alias is the only host identifier they carry.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402
    AUDIT_KEEP,
    AUDIT_MAX_BYTES,
    append_audit,
    audit_dir,
    audit_log_path,
    audit_policy,
    emit,
    json_result,
)
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUDIT_BAD_SINCE,
    EXIT_AUDIT_NO_RECORDS,
    EXIT_INVALID_ARGUMENTS,
    EXIT_OK,
    exit_code_for,
)

#: ``--since`` / ``--older-than`` accept ``30s``, ``15m``, ``24h``, ``7d``, ``2w``.
_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

#: ``--max-bytes`` accepts a plain byte count or a ``K``/``M``/``G`` suffix.
_SIZE_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[kKmMgG]?)[bB]?$")
_SIZE_MULTIPLIER = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def _as_json(args: argparse.Namespace) -> bool:
    """True when ``--json`` was given, before *or* after the verb (FA-12).

    The flag is declared with ``default=argparse.SUPPRESS`` so a subparser
    cannot overwrite a value parsed by the top-level parser; that also means
    the attribute may not exist at all when the caller never passed it.
    """
    return bool(getattr(args, "json", False))


@dataclass
class AuditRecord:
    """One parsed JSONL line plus the file it came from."""

    ts: datetime | None
    alias: str
    raw: dict[str, Any]
    path: Path


@dataclass
class HostSummary:
    """Per-host aggregate built by ``summarize``."""

    alias: str
    calls: int = 0
    failures: int = 0
    failure_classes: dict[str, int] = field(default_factory=dict)
    routes: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    elapsed_total: float = 0.0
    slowest: tuple[float, str] | None = None
    last_error: str | None = None
    last_seen: datetime | None = None

    @property
    def failure_rate(self) -> float:
        return (self.failures / self.calls) if self.calls else 0.0

    @property
    def avg_elapsed_s(self) -> float:
        return (self.elapsed_total / self.calls) if self.calls else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "calls": self.calls,
            "failures": self.failures,
            "failure_rate": round(self.failure_rate, 4),
            "failure_classes": dict(
                sorted(self.failure_classes.items(), key=lambda kv: -kv[1])
            ),
            "routes": dict(sorted(self.routes.items(), key=lambda kv: -kv[1])),
            "bytes": self.bytes,
            "avg_elapsed_s": round(self.avg_elapsed_s, 3),
            "slowest_command": self.slowest[1] if self.slowest else None,
            "slowest_s": round(self.slowest[0], 3) if self.slowest else None,
            "last_error": self.last_error,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


def parse_duration(value: str) -> timedelta:
    """Parse ``30s`` / ``15m`` / ``24h`` / ``7d`` / ``2w`` into a timedelta."""
    text = (value or "").strip().lower()
    match = _DURATION_RE.match(text)
    if not match:
        raise ValueError(
            f"cannot parse duration {value!r}; use forms like 45m, 24h, 7d, 2w"
        )
    return timedelta(seconds=int(match.group("value")) * _UNIT_SECONDS[match.group("unit")])


def parse_size(value: str) -> int:
    """Parse ``1048576`` / ``512K`` / ``10MB`` / ``1G`` into bytes."""
    text = (value or "").strip()
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"cannot parse size {value!r}; use bytes or 512K/10MB/1G")
    return int(match.group("value")) * _SIZE_MULTIPLIER[match.group("unit").lower()]


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Python 3.7's fromisoformat cannot parse a trailing "Z".
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iter_records(paths: Iterable[Path]) -> Iterable[AuditRecord]:
    """Yield every parseable record from ``paths``; skip damaged lines.

    A partly-written line (killed process) or a hand-edited file must not
    abort the report: the whole point is to look at an audit trail after
    something went wrong, so damaged lines are skipped and counted by the
    caller via :func:`load_records`.
    """
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            alias = raw.get("host") or raw.get("alias")
            yield AuditRecord(
                ts=_parse_ts(raw.get("ts")),
                alias=str(alias) if alias else path.stem,
                raw=raw,
                path=path,
            )


def audit_files(include_rotated: bool = False) -> list[Path]:
    """Every ``*.jsonl`` in the audit dir (and optionally ``.1``/``.2``/``.3``)."""
    base = audit_dir()
    files = sorted(base.glob("*.jsonl"))
    if include_rotated:
        files += sorted(base.glob("*.jsonl.*"))
    return [p for p in files if p.is_file()]


def load_records(since: timedelta | None = None) -> tuple[list[AuditRecord], int]:
    """Load records, optionally filtered to the last ``since``.

    Returns ``(kept, skipped)`` where ``skipped`` counts both unparseable
    lines (a half-written record after a kill, or a hand-edited file) and
    records with no usable timestamp. Both are reported rather than silently
    dropped, because "the audit trail is missing rows" is itself a finding.
    """
    cutoff = datetime.now(timezone.utc) - since if since is not None else None
    kept: list[AuditRecord] = []
    skipped = 0
    for path in audit_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            if not isinstance(raw, dict):
                skipped += 1
                continue
            alias = raw.get("host") or raw.get("alias")
            record = AuditRecord(
                ts=_parse_ts(raw.get("ts")),
                alias=str(alias) if alias else path.stem,
                raw=raw,
                path=path,
            )
            if record.ts is None:
                skipped += 1
                continue
            if cutoff is not None and record.ts < cutoff:
                continue
            kept.append(record)
    return kept, skipped


def summarize(records: list[AuditRecord]) -> list[HostSummary]:
    """Fold records into one :class:`HostSummary` per alias, worst first."""
    by_host: dict[str, HostSummary] = {}
    for record in records:
        summary = by_host.setdefault(record.alias, HostSummary(alias=record.alias))
        summary.calls += 1
        raw = record.raw
        route = raw.get("route")
        if isinstance(route, str):
            summary.routes[route] = summary.routes.get(route, 0) + 1
        success = raw.get("success")
        failure_class = raw.get("failure_class")
        if success is False:
            summary.failures += 1
            if isinstance(failure_class, str) and failure_class:
                summary.failure_classes[failure_class] = (
                    summary.failure_classes.get(failure_class, 0) + 1
                )
            summary.last_error = (
                f"{failure_class or 'unknown'}: {raw.get('command') or raw.get('direction') or ''}"
            ).strip()
        size = raw.get("bytes") or raw.get("output_bytes") or 0
        if isinstance(size, (int, float)):
            summary.bytes += int(size)
        elapsed = raw.get("elapsed_s")
        if isinstance(elapsed, (int, float)):
            summary.elapsed_total += float(elapsed)
            command = str(raw.get("command") or raw.get("direction") or "")
            if summary.slowest is None or float(elapsed) > summary.slowest[0]:
                summary.slowest = (float(elapsed), command)
        if record.ts and (summary.last_seen is None or record.ts > summary.last_seen):
            summary.last_seen = record.ts
    return sorted(
        by_host.values(),
        key=lambda s: (-s.failure_rate, -s.failures, -s.calls, s.alias),
    )


def _format_summary_human(rows: list[HostSummary], window: str) -> str:
    if not rows:
        return f"(no audit records in the last {window})\n"
    header = (
        f"{'alias':<24} {'calls':>5} {'fail':>5} {'rate':>6} "
        f"{'avg_s':>7} {'last_error'}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        last = row.last_error or ""
        if len(last) > 48:
            last = last[:45] + "..."
        lines.append(
            f"{row.alias:<24} {row.calls:>5} {row.failures:>5} "
            f"{row.failure_rate * 100:>5.1f}% {row.avg_elapsed_s:>7.2f} {last}"
        )
    worst = rows[0]
    lines.append("")
    if worst.failures:
        lines.append(
            f"worst: {worst.alias} - {worst.failures}/{worst.calls} calls failed "
            f"({worst.failure_rate * 100:.1f}%)"
        )
    else:
        lines.append("worst: none - every recorded call succeeded")
    return "\n".join(lines) + "\n"


def _cmd_summarize(args: argparse.Namespace) -> int:
    try:
        since = parse_duration(args.since)
    except ValueError as exc:
        result = json_result(
            success=False,
            exit_code=EXIT_AUDIT_BAD_SINCE,
            stderr=f"audit.py: {exc}\n",
            data={"failure_class": "audit_bad_since"},
        )
        emit(result, as_json=_as_json(args))
        return int(result["exit_code"])

    records, skipped = load_records(since)
    if args.host:
        wanted = {h.strip() for h in args.host.split(",") if h.strip()}
        records = [r for r in records if r.alias in wanted]
    rows = summarize(records)
    if not rows:
        result = json_result(
            success=False,
            exit_code=EXIT_AUDIT_NO_RECORDS,
            stderr=(
                f"audit.py: no records in {audit_dir()} for the last {args.since}"
                f"{' matching ' + args.host if args.host else ''}\n"
            ),
            data={
                "failure_class": "audit_no_records",
                "since": args.since,
                "audit_dir": str(audit_dir()),
                "records": 0,
                "skipped_lines": skipped,
                "hosts": [],
            },
        )
        emit(result, as_json=_as_json(args))
        return int(result["exit_code"])

    payload = {
        "since": args.since,
        "since_seconds": int(since.total_seconds()),
        "audit_dir": str(audit_dir()),
        "records": len(records),
        "skipped_lines": skipped,
        "hosts": [row.as_dict() for row in rows],
        "worst_host": rows[0].alias if rows else None,
        "failure_class": None,
    }
    stdout = _format_summary_human(rows, args.since)
    emit(
        json_result(success=True, exit_code=EXIT_OK, stdout=stdout, data=payload),
        as_json=_as_json(args),
    )
    return EXIT_OK


def _rewrite_without_old(path: Path, cutoff: datetime, dry_run: bool) -> tuple[int, int]:
    """Drop records older than ``cutoff`` from ``path``; returns (kept, dropped)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return (0, 0)
    kept: list[str] = []
    dropped = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            dropped += 1
            continue
        ts = _parse_ts(raw.get("ts")) if isinstance(raw, dict) else None
        if ts is None or ts < cutoff:
            dropped += 1
            continue
        kept.append(stripped)
    if not dry_run:
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        try:
            tmp.write_text(
                ("\n".join(kept) + "\n") if kept else "", encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            return (len(kept), 0)
    return (len(kept), dropped)


def _cmd_prune(args: argparse.Namespace) -> int:
    try:
        older = parse_duration(args.older_than)
    except ValueError as exc:
        result = json_result(
            success=False,
            exit_code=EXIT_AUDIT_BAD_SINCE,
            stderr=f"audit.py: {exc}\n",
            data={"failure_class": "audit_bad_since"},
        )
        emit(result, as_json=_as_json(args))
        return int(result["exit_code"])

    cutoff = datetime.now(timezone.utc) - older
    files = audit_files(include_rotated=True)
    details: list[dict[str, Any]] = []
    total_dropped = 0
    for path in files:
        kept, dropped = _rewrite_without_old(path, cutoff, args.dry_run)
        total_dropped += dropped
        if dropped or args.verbose:
            details.append({
                "path": str(path),
                "kept": kept,
                "dropped": dropped,
            })
    payload = {
        "older_than": args.older_than,
        "cutoff": cutoff.isoformat(),
        "dry_run": bool(args.dry_run),
        "files": details,
        "dropped": total_dropped,
        "failure_class": None,
    }
    verb = "would drop" if args.dry_run else "dropped"
    stdout = (
        f"audit prune: {verb} {total_dropped} record(s) older than "
        f"{args.older_than} across {len(files)} file(s)\n"
    )
    emit(
        json_result(success=True, exit_code=EXIT_OK, stdout=stdout, data=payload),
        as_json=_as_json(args),
    )
    return EXIT_OK


def _cmd_rotate(args: argparse.Namespace) -> int:
    try:
        max_bytes = parse_size(args.max_bytes) if args.max_bytes else audit_policy()[0]
        keep = args.keep if args.keep is not None else AUDIT_KEEP
    except ValueError as exc:
        result = json_result(
            success=False,
            exit_code=EXIT_INVALID_ARGUMENTS,
            stderr=f"audit.py: {exc}\n",
            data={"failure_class": "invalid_arguments"},
        )
        emit(result, as_json=_as_json(args))
        return int(result["exit_code"])

    from lib import _rotate_audit  # noqa: PLC2701 — the one rotation primitive

    rotated: list[dict[str, Any]] = []
    for path in audit_files():
        try:
            before = path.stat().st_size
        except OSError:
            continue
        if before < max_bytes:
            continue
        if args.dry_run:
            rotated.append({"path": str(path), "bytes": before})
            continue
        _rotate_audit(path, max_bytes=0, keep=keep)  # 0 forces the rename
        rotated.append({"path": str(path), "bytes": before})
    payload = {
        "max_bytes": max_bytes,
        "keep": keep,
        "dry_run": bool(args.dry_run),
        "rotated": rotated,
        "failure_class": None,
    }
    stdout = (
        f"audit rotate: {'would rotate' if args.dry_run else 'rotated'} "
        f"{len(rotated)} file(s) past {max_bytes} bytes\n"
    )
    emit(
        json_result(success=True, exit_code=EXIT_OK, stdout=stdout, data=payload),
        as_json=_as_json(args),
    )
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    files = audit_files(include_rotated=bool(args.all))
    rows: list[dict[str, Any]] = []
    for path in files:
        try:
            size = path.stat().st_size
            mtime = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            continue
        count = 0
        if not args.no_count:
            count = sum(1 for _ in iter_records([path]))
        rows.append({
            "path": str(path),
            "bytes": size,
            "records": count,
            "modified": mtime,
        })
    payload = {
        "audit_dir": str(audit_dir()),
        "policy": {"max_bytes": audit_policy()[0], "keep": audit_policy()[1]},
        "files": rows,
        "total_bytes": sum(r["bytes"] for r in rows),
        "failure_class": None,
    }
    lines = [f"audit dir: {audit_dir()}", ""]
    for row in rows:
        lines.append(
            f"  {row['bytes']:>10}  {row['records']:>6} rec  {row['path']}"
        )
    if not rows:
        lines.append("  (no audit files yet: pass --audit-log to any script)")
    lines.append("")
    lines.append(
        f"  total {payload['total_bytes']} bytes across {len(rows)} file(s); "
        f"policy: rotate at {audit_policy()[0]} bytes, keep {audit_policy()[1]}"
    )
    emit(
        json_result(
            success=True, exit_code=EXIT_OK,
            stdout="\n".join(lines) + "\n", data=payload,
        ),
        as_json=_as_json(args),
    )
    return EXIT_OK


def _demo_record(alias: str, index: int) -> dict[str, Any]:
    """A synthetic record used only by ``--self-test`` (never written by normal runs)."""
    return {
        "host": alias,
        "route": "exec",
        "command": f"demo-{index}",
        "success": index % 3 != 0,
        "exit_code": 0 if index % 3 != 0 else 104,
        "failure_class": None if index % 3 != 0 else "remote_error",
        "elapsed_s": 0.1 * index,
        "bytes": 10,
    }


def _cmd_self_test(args: argparse.Namespace) -> int:
    """Write a handful of records then summarize them (used by the tests).

    The alias's existing trail is cleared first: the check asserts exact
    counts, and an accumulated file from a previous run would make it flaky.
    """
    alias = args.self_test
    _clear_alias(alias)
    for index in range(1, 7):
        append_audit(alias, _demo_record(alias, index))
    records, _ = load_records(timedelta(hours=1))
    rows = summarize([r for r in records if r.alias == alias])
    ok = bool(rows) and rows[0].calls == 6 and rows[0].failures == 2
    payload = {
        "alias": alias,
        "rows": [row.as_dict() for row in rows],
        "path": str(audit_log_path(alias)),
        "failure_class": None if ok else "verify_failed",
    }
    emit(
        json_result(success=ok, exit_code=EXIT_OK if ok else 9, data=payload),
        as_json=_as_json(args),
    )
    return EXIT_OK if ok else 9


def _clear_alias(alias: str) -> None:
    """Remove an alias's audit file and its rotated generations."""
    path = audit_log_path(alias)
    for candidate in [path, *[path.with_name(f"{path.name}.{i}") for i in range(1, 8)]]:
        try:
            candidate.unlink()
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    # ``--json`` must work before *or* after the verb, like every other
    # ssh-core script, so each subparser inherits the same flags.
    #
    # FA-12: ``default=argparse.SUPPRESS`` is what makes "before the verb"
    # work. Without it the subparser's own ``--json`` default (False) overwrote
    # the top-level True that was already parsed, so `audit.py --json list`
    # printed the human table while `audit.py list --json` printed JSON.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--version", "-V", action="store_true",
                        default=argparse.SUPPRESS)

    p = SSHArgumentParser(
        prog="audit.py",
        parents=[common],
        description=(
            "Summarize, prune and rotate the ssh-core audit trail in "
            "$TMPDIR/ssh-core. Answers 'which host failed most in the last "
            "24h' without a throwaway jq pipeline."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    summarize_p = sub.add_parser(
        "summarize", parents=[common],
        help="Aggregate records into a per-host table",
    )
    summarize_p.add_argument(
        "--since", default="24h",
        help="Window to include: 30m, 24h, 7d, 2w (default: 24h)",
    )
    summarize_p.add_argument(
        "--host", default=None, help="Only these aliases (comma-separated)"
    )

    prune_p = sub.add_parser(
        "prune", parents=[common], help="Delete records older than a window"
    )
    prune_p.add_argument(
        "--older-than", default="30d",
        help="Retention window: 7d, 30d, 12w (default: 30d)",
    )
    prune_p.add_argument("--dry-run", action="store_true")
    prune_p.add_argument("--verbose", action="store_true")

    rotate_p = sub.add_parser(
        "rotate", parents=[common], help="Force size-based rotation now"
    )
    rotate_p.add_argument(
        "--max-bytes", default=None,
        help=f"Rotate files larger than this (default: {AUDIT_MAX_BYTES} bytes)",
    )
    rotate_p.add_argument(
        "--keep", type=int, default=None,
        help=f"Generations to keep (default: {AUDIT_KEEP})",
    )
    rotate_p.add_argument("--dry-run", action="store_true")

    list_p = sub.add_parser(
        "list", parents=[common], help="List audit files with size and counts"
    )
    list_p.add_argument("--all", action="store_true", help="Include rotated files")
    list_p.add_argument("--no-count", action="store_true", help="Skip record counting")

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("audit.py", argv)
    if early is not None:
        return early

    # ``--self-test <alias>`` is a hidden verb: write demo records, summarize.
    if "--self-test" in argv:
        index = argv.index("--self-test")
        alias = argv[index + 1] if index + 1 < len(argv) else "self-test"
        args = argparse.Namespace(json="--json" in argv, self_test=alias)
        return _cmd_self_test(args)

    args = build_parser().parse_args(argv)
    handlers = {
        "summarize": _cmd_summarize,
        "prune": _cmd_prune,
        "rotate": _cmd_rotate,
        "list": _cmd_list,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        result = json_result(
            success=False,
            exit_code=EXIT_INVALID_ARGUMENTS,
            stderr=f"audit.py: unknown command {args.cmd!r}\n",
            data={"failure_class": "invalid_arguments"},
        )
        emit(result, as_json=getattr(args, "json", False))
        return int(result["exit_code"])
    return handler(args)


if __name__ == "__main__":
    sys.exit(guarded_main("audit.py", main))
