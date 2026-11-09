"""fetchers — per-scheme line iterators backed by streaming subprocess.

Public surface:
    RawLine(source, raw_body, raw_ts, fields, fetched_at)
    FetcherError(host, argv, returncode, stderr_tail)
    DEFAULT_LINES = 10000
    iter_lines(source: ResolvedSource, *, since, follow, timeout, lines=...)
        -> Iterator[RawLine]

``lines`` is the snapshot size a non-follow read asks the source for: it
becomes ``tail -n N`` for ``file://`` and ``--tail N`` for ``docker://`` /
``kube://``. ``journal://`` has no equivalent flag, and a follow stream is
unbounded by definition, so both ignore it. Leaving it ``None`` keeps each
fetcher's historical default (``grep_across_sources`` / ``correlate`` do).

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
import os
import re
import shlex
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

from .source_spec import ResolvedSource

# Cap how much remote-stderr we surface to the failure_class envelope.
_STDERR_TAIL_BYTES = 2048

#: Default snapshot size when a caller asks for "the last N lines" without
#: saying how many. ``tail_multi --lines`` defaults to this, so the remote
#: ``tail -n 10000`` the plugin has always issued stays the default.
DEFAULT_LINES = 10000

#: How long a process gets to exit on its own after stdout reaches EOF.
#: ``ssh`` has already printed everything the caller needs at that point, but
#: it still has to tear down the session and exit; killing it during that
#: window is what corrupted the return code on Windows (DEVOPS-BUG-004).
_EXIT_GRACE_S = 2.0

#: How long to wait for a process we decided to kill. Only reached when the
#: child ignores the signal, which for ``ssh`` means a wedged transport.
_KILL_WAIT_S = 2.0

#: sshd banners injected before the remote command runs. They arrive on stderr
#: on every login, so treating them as the failure reason hid the real error
#: (``tail: cannot open '...'``) behind "Authorized users only ...".
_BANNER_PATTERNS = (
    re.compile(r"^\s*authorized users only\b", re.IGNORECASE),
    re.compile(r"^\s*all activities may be monitored\b", re.IGNORECASE),
    re.compile(r"^\s*(last (failed )?login|welcome to)\b", re.IGNORECASE),
    re.compile(r"^\s*\*{3}.*\*{3}\s*$"),  # ***** WARNING / MOTD banners
)


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
    useful per-fetcher failure to the caller. ``stderr_tail`` is the remote
    command's own diagnostics with the sshd login banner stripped out
    (DEVOPS-BUG-004: the banner used to be the only thing anyone saw).
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


def _strip_login_banner(stderr: str) -> str:
    """Drop sshd's pre-command banner lines, keeping every genuine error.

    The banner ("Authorized users only. All activities may be monitored and
    reported.") is printed on *every* ssh login, so it must never be the thing
    a failure report shows. Blank lines are dropped too; if the whole stream
    was banner (the usual case for a clean remote exit that still fails), the
    result is empty and the caller falls back to just the return code.
    """
    kept: list[str] = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern.match(stripped) for pattern in _BANNER_PATTERNS):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept)


def _stderr_tail(stderr: str) -> str:
    """Banner-stripped stderr, capped to the tail the envelope may show."""
    cleaned = _strip_login_banner(stderr)
    if len(cleaned) <= _STDERR_TAIL_BYTES:
        return cleaned
    # Cut on a line boundary so a half-line cannot masquerade as the error.
    return cleaned[-_STDERR_TAIL_BYTES:].split("\n", 1)[-1]


def iter_lines(
    source: ResolvedSource,
    *,
    since: str,
    follow: bool,
    timeout: int,
    lines: int | None = None,
) -> Iterator[RawLine]:
    """Dispatch to the appropriate fetcher by source.scheme.

    ``lines`` bounds a non-follow snapshot (``tail_multi --lines``). It maps
    to ``tail -n N`` for ``file://`` and ``--tail N`` for ``docker://`` /
    ``kube://``; ``journal://`` has no equivalent knob and a ``--follow``
    stream is unbounded by definition, so both ignore it.

    ``None`` means "the caller did not ask for a snapshot size" and leaves
    each fetcher on its historical default. That keeps ``grep_across_sources``
    and ``correlate`` (which scan a whole window and never expose ``--lines``)
    byte-identical to their pre-GAP-007 behaviour.
    """
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
    return fn(
        source, since=since, follow=follow, timeout=timeout, lines=lines
    )


# ---------------------------------------------------------------------------
# Per-scheme fetchers
# ---------------------------------------------------------------------------


def _journal_fetcher(
    src: ResolvedSource,
    *,
    since: str,
    follow: bool,
    timeout: int,
    lines: int | None = None,
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
    src: ResolvedSource,
    *,
    since: str,
    follow: bool,
    timeout: int,
    lines: int | None = None,
) -> Iterator[RawLine]:
    cmd = ["docker", "logs", "--timestamps", "--since", since]
    if not follow and lines is not None:
        # `docker logs` is unbounded without --tail; honour --lines instead of
        # dumping the container's whole history.
        cmd += ["--tail", str(lines)]
    if follow:
        cmd.append("--follow")
    cmd.append(src.payload)
    for line in _run_cmd(src.host, cmd, timeout=timeout):
        if not line:
            continue
        ts, _, body = line.partition(" ")
        yield RawLine(src, body, ts or None, {}, time.time())


def _kube_fetcher(
    src: ResolvedSource,
    *,
    since: str,
    follow: bool,
    timeout: int,
    lines: int | None = None,
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
        if not follow and lines is not None:
            cmd.append(f"--tail={lines}")
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
    # str.removeprefix is 3.9+; the plugin floor is 3.8 (see ADR-006).
    pods = [p[4:] if p.startswith("pod/") else p for p in pods if p.strip()]
    if pattern and "*" in pattern:
        pods = fnmatch.filter(pods, pattern)
    return pods


def _file_fetcher(
    src: ResolvedSource,
    *,
    since: str,
    follow: bool,
    timeout: int,
    lines: int | None = None,
) -> Iterator[RawLine]:
    """Tail a file. ``local`` is read natively (LG-01).

    The old local path ran ``["tail", "-n", "10000", path]`` directly. On
    Windows that raises ``FileNotFoundError`` because there is no ``tail``, and
    the failure surfaced as ``rc=127 [WinError 2] 系统找不到指定的文件。`` — a
    message that names no binary and reads like a permissions problem. Reading
    the file with Python works on every platform and needs no external tool.
    """
    if src.host == "local":
        yield from _tail_local_file(
            src, follow=follow, timeout=timeout, lines=lines
        )
        return

    # `tail` cannot filter by time, so we either tail-N (non-follow) or
    # tail -F (follow); the drain loop trims by --since after normalize.
    count = DEFAULT_LINES if lines is None else lines
    cmd = (
        ["tail", "-F", src.payload]
        if follow
        else ["tail", "-n", str(count), src.payload]
    )
    for line in _run_cmd(src.host, cmd, timeout=timeout):
        yield RawLine(src, line, None, {}, time.time())


#: How much of the tail of a local file to read for a snapshot. The byte cap is
#: a guard against pathological single-line files; the line count is what the
#: caller actually asked for (``--lines``).
_LOCAL_TAIL_BYTES = 4 * 1024 * 1024


def _local_snapshot_lines(lines: int | None) -> int:
    """How many lines of a local file to yield for a non-follow snapshot."""
    if lines is None:
        return DEFAULT_LINES
    try:
        wanted = int(lines)
    except (TypeError, ValueError):
        return DEFAULT_LINES
    return wanted if wanted > 0 else DEFAULT_LINES


#: Poll interval for the local follow loop.
_LOCAL_FOLLOW_POLL_S = 0.4


def _local_path_payload(payload: str) -> str:
    """Return a filesystem path for a ``file://`` payload on *this* host.

    ``source_spec`` normalises every ``file://`` payload to start with ``/``
    (correct for a POSIX host). On Windows ``file://local/C:/Windows/win.ini``
    therefore arrives as ``/C:/Windows/win.ini``, and opening that yields
    ``WinError 123 (invalid path syntax)``. A drive-letter payload drops the
    leading slash; everything else is left exactly as written, so a POSIX
    caller on a POSIX host is unaffected.
    """
    if (
        os.name == "nt"
        and len(payload) > 3
        and payload[0] == "/"
        and payload[1].isalpha()
        and payload[2] == ":"
    ):
        return payload[1:]
    return payload


def _tail_local_file(
    src: ResolvedSource,
    *,
    follow: bool,
    timeout: int,
    lines: int | None = None,
) -> Iterator[RawLine]:
    """Read a local file: last ``lines`` lines, then optionally follow it."""
    from pathlib import Path

    target = Path(_local_path_payload(src.payload)).expanduser()
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            start = max(0, size - _LOCAL_TAIL_BYTES)
            handle.seek(start)
            blob = handle.read()
    except OSError as exc:
        raise FetcherError(
            src.host,
            ["open", str(target)],
            127,
            stderr_tail=f"cannot read {str(target)!r}: {exc}",
        ) from None

    text = blob.decode("utf-8", errors="replace")
    if start > 0:
        # Joined mid-line; drop the fragment so the first line is whole.
        _, sep, text = text.partition("\n")
        if not sep:
            text = ""
    snapshot = text.splitlines()
    # --lines bounds the snapshot exactly like `tail -n N` would. The byte cap
    # above only decides how far back the read starts, so a huge file is never
    # loaded whole; slicing here is what makes the count honest.
    wanted = _local_snapshot_lines(lines)
    if len(snapshot) > wanted:
        snapshot = snapshot[-wanted:]
    for line in snapshot:
        yield RawLine(src, line, None, {}, time.time())

    if not follow:
        return

    offset = size
    deadline = time.time() + max(timeout, 1)
    while time.time() < deadline:
        time.sleep(_LOCAL_FOLLOW_POLL_S)
        try:
            now_size = target.stat().st_size
        except OSError:
            continue
        if now_size < offset:  # truncated or rotated
            offset = 0
        if now_size == offset:
            continue
        try:
            with target.open("rb") as handle:
                handle.seek(offset)
                new = handle.read(_LOCAL_TAIL_BYTES)
        except OSError:
            continue
        offset += len(new)
        for line in new.decode("utf-8", errors="replace").splitlines():
            yield RawLine(src, line, None, {}, time.time())


# ---------------------------------------------------------------------------
# Transport — streaming subprocess for both local and remote
# ---------------------------------------------------------------------------


def _missing_binary_hint(argv: list[str], exc: BaseException) -> str:
    """Turn a spawn failure into something an operator can act on.

    ``str(FileNotFoundError)`` on Windows is ``[WinError 2] 系统找不到指定的文件。``
    — it names no binary and reads like a permissions problem. The command is
    the actionable half of the message, so put it in front (LG-01).
    """
    binary = argv[0] if argv else "<unknown>"
    return (
        f"cannot execute {binary!r} on this host "
        f"(argv={argv!r}): {exc}"
    )


def _read_stderr(proc: object) -> str:
    """Banner-stripped stderr of a process that has already exited."""
    stderr = getattr(proc, "stderr", None)
    if stderr is None:
        return ""
    with contextlib.suppress(OSError, ValueError):
        return _stderr_tail(stderr.read())
    return ""


def _run_cmd(host: str, argv: list[str], *, timeout: int) -> Iterator[str]:
    """Stream stdout lines from `argv` running on `host`.

    Local route:  subprocess.Popen(argv, ...)  (no shell).
    Remote route: subprocess.Popen(['ssh', '-o BatchMode=yes', ..., host,
                                     <shlex.joined cmd>], ...)

    On a non-zero exit the remote command really produced, raises FetcherError
    so the parent ThreadPoolExecutor can record this fetcher as failed and the
    envelope can surface partial_fetch.

    A process *we* killed is not a failure: it either overran ``timeout`` or
    the caller stopped consuming. That distinction used to be inferred from a
    POSIX signal number in the return code, which does not exist on Windows —
    ``Popen.terminate()`` there is ``TerminateProcess`` and leaves
    ``returncode == 1``, so every successful remote fetch was reported as
    ``rc=1 <login banner>`` (DEVOPS-BUG-004). It is now tracked explicitly.
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
            # Never decode with the ANSI code page: on a Chinese Windows the
            # exception text and any child diagnostics arrive as GBK, and a
            # UnicodeDecodeError inside the reader raises a bare
            # "Exception in thread" with an empty envelope.
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # line-buffered
        )
    except FileNotFoundError as exc:
        raise FetcherError(
            host, full_argv, 127, stderr_tail=_missing_binary_hint(full_argv, exc)
        ) from None

    deadline = time.time() + max(timeout, 1)
    stdout_eof = False
    killed_by_us = False
    abandoned = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
            if time.time() > deadline:
                break
        else:
            # The for-loop was not broken out of: stdout is at EOF.
            stdout_eof = True
    except GeneratorExit:
        # The caller stopped consuming (a bounded drain that already has what
        # it needs). Closing is not a fetch failure, and raising here would
        # surface as "generator ignored GeneratorExit" instead.
        abandoned = True
        raise
    finally:
        # 1. stdout at EOF means the child finished its work and only has
        #    teardown left (`ssh` closing the session). Give it time to exit
        #    on its own: killing it here is what corrupted the return code on
        #    Windows (DEVOPS-BUG-004). A stream we cut short instead — the
        #    caller stopped consuming, or `timeout` expired — is a
        #    long-running `tail -F` with nothing left to wait for, so it is
        #    killed without the grace period.
        if stdout_eof and proc.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_EXIT_GRACE_S)
        # 2. Still alive: kill it and remember that *we* did, instead of
        #    reverse-engineering a POSIX signal number out of the return code.
        if proc.poll() is None:
            killed_by_us = True
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_KILL_WAIT_S)

        rc = proc.returncode
        # A process we killed is an interrupted stream, not a fetch failure:
        # a `tail -F` that overran `timeout`, or the caller stopped consuming.
        # Anything else that ended non-zero is a real failure (`false`, a
        # missing file, `docker logs` on a container that does not exist), even
        # if we only got to see part of its stdout. ``abandoned`` must not
        # raise: the generator is already unwinding through GeneratorExit.
        if not abandoned and not killed_by_us and rc not in (0, None):
            raise FetcherError(
                host, full_argv, rc, stderr_tail=_read_stderr(proc)
            )


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
    "DEFAULT_LINES",
    "RawLine",
    "FetcherError",
    "iter_lines",
]
