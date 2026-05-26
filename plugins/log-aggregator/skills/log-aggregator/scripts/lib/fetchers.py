"""fetchers — per-scheme line iterators backed by streaming subprocess.

Public surface:
    RawLine(source, raw_body, raw_ts, fields, fetched_at)
    FetcherError(host, argv, returncode, stderr_tail)
    iter_lines(source: ResolvedSource, *, since, follow, timeout)
        -> Iterator[RawLine]

Local sources (``host == "local"``) shell out to subprocess.Popen directly.
Remote sources run native ``ssh`` via subprocess.Popen with the same
``-o BatchMode=yes -o ConnectTimeout=N`` knobs ssh_execute.py uses.

Design drift from prd R10 / design D3:
    PRD R10 says "shell out to ssh_execute.py" for remote. ssh_execute uses
    ``subprocess.run`` which is fully buffered — incompatible with the
    line-by-line streaming this fetcher needs for ``--follow`` (and for the
    reorder buffer to do any cross-source ordering on burst-mode reads).
    We therefore go direct to ``ssh`` via subprocess.Popen, which still
    honors ADR-001 D1 ("subprocess + native ssh, never import paramiko").
    The non-streaming clock_skew probe continues to use ssh_execute.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

from .source_spec import ResolvedSource

# Cap how much remote-stderr we surface to the failure_class envelope.
_STDERR_TAIL_BYTES = 2048


@dataclass(frozen=True)
class RawLine:
    """One unparsed log line + fetcher-side metadata.

    Attributes:
        source: The ResolvedSource that produced this line.
        raw_body: Log line body (fetcher prefixes such as docker's leading
            RFC3339 timestamp are already stripped).
        raw_ts: Per-line timestamp string as the fetcher saw it
            (microsecond int string for journal, RFC3339 for docker/kube,
            None for file when no ts_regex is in scope).
        fields: Per-line structured fields (journal `-o json` populates
            this fully; other schemes leave it empty).
        fetched_at: time.time() at the moment the line arrived locally;
            the time-normalizer falls back to this when raw_ts is missing
            or unparseable.
    """

    source: ResolvedSource
    raw_body: str
    raw_ts: str | None
    fields: dict[str, str]
    fetched_at: float


@dataclass
class FetcherError(Exception):
    """Raised when an underlying fetch process died unexpectedly.

    Carries enough context for the partial_fetch envelope to surface a
    useful per-fetcher failure to the caller.
    """

    host: str
    argv: list[str]
    returncode: int
    stderr_tail: str = ""

    def __str__(self) -> str:  # pragma: no cover — repr only
        return (
            f"FetcherError(host={self.host!r}, rc={self.returncode}, "
            f"argv={self.argv!r}, stderr_tail={self.stderr_tail!r})"
        )


def iter_lines(
    source: ResolvedSource, *, since: str, follow: bool, timeout: int
) -> Iterator[RawLine]:
    """Dispatch to the appropriate fetcher by source.scheme."""
    dispatch = {
        "journal": _journal_fetcher,
        "docker": _docker_fetcher,
        "kube": _kube_fetcher,
        "file": _file_fetcher,
    }
    fn = dispatch.get(source.scheme)
    if fn is None:
        raise FetcherError(
            host=source.host, argv=[], returncode=2,
            stderr_tail=f"no fetcher for scheme {source.scheme!r}",
        )
    return fn(source, since=since, follow=follow, timeout=timeout)


# ---------------------------------------------------------------------------
# Per-scheme fetchers
# ---------------------------------------------------------------------------


def _journal_fetcher(
    src: ResolvedSource, *, since: str, follow: bool, timeout: int
) -> Iterator[RawLine]:
    since_epoch = _parse_since_to_epoch(since)
    cmd = ["journalctl", "-o", "json", "--no-pager"]
    if src.payload:
        cmd += ["-u", src.payload]
    cmd += ["--since", f"@{since_epoch}"]
    if follow:
        cmd.append("-f")

    for raw in _run_cmd(src.host, cmd, timeout=timeout):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # journalctl can emit non-JSON prelude on some builds; skip.
            continue
        raw_ts = obj.get("__REALTIME_TIMESTAMP")
        body = obj.get("MESSAGE", "")
        if isinstance(body, list):
            # MESSAGE can be a byte-array list on some journald builds.
            try:
                body = bytes(body).decode("utf-8", errors="replace")
            except (TypeError, ValueError):
                body = str(body)
        yield RawLine(
            source=src,
            raw_body=str(body),
            raw_ts=str(raw_ts) if raw_ts is not None else None,
            fields={k: str(v) for k, v in obj.items() if isinstance(v, (str, int, float))},
            fetched_at=time.time(),
        )


def _docker_fetcher(
    src: ResolvedSource, *, since: str, follow: bool, timeout: int
) -> Iterator[RawLine]:
    cmd = ["docker", "logs", "--timestamps", "--since", since]
    if follow:
        cmd.append("--follow")
    cmd.append(src.payload)
    for line in _run_cmd(src.host, cmd, timeout=timeout):
        if not line:
            continue
        ts, _, body = line.partition(" ")
        yield RawLine(src, body, ts or None, {}, time.time())


def _kube_fetcher(
    src: ResolvedSource, *, since: str, follow: bool, timeout: int
) -> Iterator[RawLine]:
    ns, _, pod_pattern = src.payload.partition("/")
    label = src.query.get("label")
    previous = src.query.get("previous", "").lower() in {"1", "true", "yes"}

    if "*" in pod_pattern or label or not pod_pattern:
        pods = _enumerate_kube_pods(src.host, ns, pod_pattern, label, timeout)
    else:
        pods = [pod_pattern]

    if not pods:
        return  # nothing to follow; caller will see 0 events from this src

    for pod in pods:
        cmd = [
            "kubectl", "logs", "-n", ns, pod,
            "--timestamps", "--since", since,
            "--all-containers", "--prefix",
        ]
        if follow:
            cmd.append("--follow")
        if previous:
            cmd.append("--previous")
        for line in _run_cmd(src.host, cmd, timeout=timeout):
            if not line:
                continue
            ts, _, body = line.partition(" ")
            yield RawLine(src, body, ts or None, {}, time.time())


def _enumerate_kube_pods(
    host: str, ns: str, pattern: str, label: str | None, timeout: int
) -> list[str]:
    cmd = ["kubectl", "get", "pods", "-n", ns, "-o", "name"]
    if label:
        cmd += ["-l", label]
    pods = list(_run_cmd(host, cmd, timeout=min(timeout, 15)))
    pods = [p.removeprefix("pod/") for p in pods if p.strip()]
    if pattern and "*" in pattern:
        pods = fnmatch.filter(pods, pattern)
    return pods


def _file_fetcher(
    src: ResolvedSource, *, since: str, follow: bool, timeout: int
) -> Iterator[RawLine]:
    # `tail` cannot filter by time, so we either tail-N (non-follow) or
    # tail -F (follow); the drain loop trims by --since after normalize.
    cmd = ["tail", "-F", src.payload] if follow else ["tail", "-n", "10000", src.payload]
    for line in _run_cmd(src.host, cmd, timeout=timeout):
        yield RawLine(src, line, None, {}, time.time())


# ---------------------------------------------------------------------------
# Transport — streaming subprocess for both local and remote
# ---------------------------------------------------------------------------


def _run_cmd(host: str, argv: list[str], *, timeout: int) -> Iterator[str]:
    """Stream stdout lines from `argv` running on `host`.

    Local route:  subprocess.Popen(argv, ...)  (no shell).
    Remote route: subprocess.Popen(['ssh', '-o BatchMode=yes', ..., host,
                                     <shlex.joined cmd>], ...)

    On non-zero exit (other than SIGTERM from our own ``finally`` cleanup),
    raises FetcherError so the parent ThreadPoolExecutor can record this
    fetcher as failed and the envelope can surface partial_fetch.
    """
    if host == "local":
        full_argv = list(argv)
    else:
        # Native ssh takes a single command string; quote argv so the
        # remote shell sees it as one command.
        remote_cmd = shlex.join(argv)
        full_argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            host,
            remote_cmd,
        ]

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
            full_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
    except FileNotFoundError as exc:
        raise FetcherError(host, full_argv, 127, stderr_tail=str(exc)) from None

    deadline = time.time() + max(timeout, 1)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
            if time.time() > deadline:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)

        rc = proc.returncode
        # 0 / None / SIGTERM/SIGKILL from our own cleanup are not failures.
        clean = {0, None, -signal.SIGTERM, -signal.SIGKILL}
        if rc not in clean:
            stderr_tail = ""
            if proc.stderr is not None:
                with contextlib.suppress(OSError, ValueError):
                    stderr_tail = proc.stderr.read()[-_STDERR_TAIL_BYTES:]
            raise FetcherError(host, full_argv, rc, stderr_tail=stderr_tail)


# ---------------------------------------------------------------------------
# --since parser
# ---------------------------------------------------------------------------


def _parse_since_to_epoch(since: str) -> int:
    """Parse a since spec into an absolute unix epoch (int seconds).

    Accepted forms:
        * ``"5min"`` / ``"5m"`` / ``"30s"`` / ``"2h"`` / ``"1d"``  (duration)
        * ``"5 min ago"`` / ``"30 seconds ago"``                   (duration)
        * RFC3339 / ISO 8601                                       (absolute)
        * bare integer                                             (epoch)

    Defaults to "now - 5min" if parsing fails entirely (matches the CLI
    default and protects journalctl ``@<epoch>`` from a NaN argument).
    """
    s = since.strip().lower()
    now = time.time()

    # 1. Bare epoch
    if s.isdigit():
        return int(s)

    # 2. Duration shorthand: <N>[unit]  or  "<N> <unit> ago"
    units = {"s": 1, "sec": 1, "second": 1, "seconds": 1,
             "m": 60, "min": 60, "minute": 60, "minutes": 60,
             "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
             "d": 86400, "day": 86400, "days": 86400}
    s_no_ago = s.replace("ago", "").strip()

    # try "Nunit" (no whitespace) first, then "N unit"
    m = re.match(r"^(\d+)\s*([a-z]+)$", s_no_ago)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit in units:
            return int(now - n * units[unit])

    # 3. ISO 8601
    try:
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        pass

    # 4. fall back to "5 min ago"
    return int(now - 300)


__all__ = [
    "RawLine",
    "FetcherError",
    "iter_lines",
]
