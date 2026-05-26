"""time_norm — normalize per-scheme line timestamps to UTC datetimes.

Public surface:
    normalize(line: RawLine) -> tuple[datetime, str] | None

Returns ``(utc_datetime, raw_body)`` on success; ``None`` when the line's
timestamp could not be parsed. Callers fall back to ``RawLine.fetched_at``
in that case (precision degrades to "moment we received the line").

Per-scheme rules (mirrors references/sources.md "时间归一化" table):

    journal:  __REALTIME_TIMESTAMP microseconds  -> UTC
    docker:   RFC3339 (line prefix)              -> UTC
    kube:     RFC3339 (via --timestamps)         -> UTC
    file:     user-supplied ts_regex + ts_format -> UTC (treated as UTC)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .fetchers import RawLine


def normalize(line: RawLine) -> tuple[datetime, str] | None:
    scheme = line.source.scheme
    if scheme == "journal":
        return _norm_journal(line)
    if scheme == "docker":
        return _norm_docker(line)
    if scheme == "kube":
        return _norm_kube(line)
    if scheme == "file":
        return _norm_file(line)
    return None


def _norm_journal(line: RawLine) -> tuple[datetime, str] | None:
    if not line.raw_ts:
        return None
    try:
        us = int(line.raw_ts)
        return (
            datetime.fromtimestamp(us / 1e6, tz=timezone.utc),
            line.raw_body,
        )
    except (ValueError, OverflowError, OSError):
        return None


def _norm_docker(line: RawLine) -> tuple[datetime, str] | None:
    """RFC3339: '2026-05-25T14:02:01.123Z' or '...+08:00'."""
    if not line.raw_ts:
        return None
    try:
        # datetime.fromisoformat in 3.11+ tolerates trailing 'Z'; we
        # explicitly translate for 3.10 compatibility too.
        dt = datetime.fromisoformat(line.raw_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc), line.raw_body


def _norm_kube(line: RawLine) -> tuple[datetime, str] | None:
    # kubectl --timestamps prints the same RFC3339 shape as docker.
    return _norm_docker(line)


def _norm_file(line: RawLine) -> tuple[datetime, str] | None:
    """Use the user-supplied ts_regex + ts_format from the source query.

    Without both, we can't parse the line's own timestamp — return None so
    the caller falls back to fetched_at.
    """
    ts_regex = line.source.query.get("ts_regex")
    ts_format = line.source.query.get("ts_format")
    if not (ts_regex and ts_format):
        return None
    try:
        m = re.search(ts_regex, line.raw_body)
    except re.error:
        return None
    if not m:
        return None
    try:
        # group(0) when no capture group; group(1) when user supplied one.
        token = m.group(1) if m.groups() else m.group(0)
        dt = datetime.strptime(token, ts_format)
    except (ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        # v1.0: file logs in unknown tz are assumed UTC. v1.1 may add
        # a ?tz=America/Los_Angeles query field; see PRD R4 / D4.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc), line.raw_body


__all__ = ["normalize"]
