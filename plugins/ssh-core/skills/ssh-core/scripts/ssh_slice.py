#!/usr/bin/env python3
"""ssh_slice.py - read a bounded slice of a remote file, with a cursor.

Why this exists (review D2/D5)
------------------------------
The only way to read a window of a remote log used to be to compose the remote
command by hand::

    ssh_execute.py host "sed -n '299700,302600p' /var/log/x.log | grep -v noise"

Three problems with that:

* the quoting goes through pwsh -> argv -> remote sh, so `$`, `"` and `'`
  needed two or three attempts before they survived (review D3);
* the 10 MB envelope cap silently dropped the tail of the window, and there
  was nothing in the answer that said *where* to resume (review D5);
* a 235 MB file could not be read in one call at all, and the workaround was
  an scp round trip.

``ssh_slice`` turns that into one verb with an explicit range and a cursor::

    python scripts/ssh_slice.py lab-231 /var/log/app.log --stat --json
    python scripts/ssh_slice.py lab-231 /var/log/app.log --lines 299700-302600 --json
    python scripts/ssh_slice.py lab-231 /var/log/app.log --lines 1-5000 --next-offset
    # data.range.next_offset = 5001, data.range.eof = false  -> read on
    python scripts/ssh_slice.py lab-231 /var/log/app.log --tail 200
    python scripts/ssh_slice.py lab-231 /var/log/app.log --grep 'ERROR|panic' --context 2
    python scripts/ssh_slice.py lab-231 /var/log/app.log --bytes 1048576-2097151

Every mode takes ``--max-bytes`` (default 4 MB, hard ceiling 64 MB) so the
answer always fits in an envelope, and every mode returns::

    data.range = {mode, start, end, next_offset, eof, bytes, lines, truncated}
    data.path, data.stat (with --stat), data.command (the exact remote argv)

**The sliced text is in the envelope** (BUG-005). ``data.content`` carries the
body and ``stdout`` carries the same bytes in ``--json`` mode, exactly like
``ssh_execute --json``: a JSON caller no longer has to fall back to parsing the
human output or to staging a file. The companions are::

    data.content, data.content_bytes, data.content_lines,
    data.content_sha256, data.content_truncated, data.content_hint

``content_truncated`` is true when the answer is *not* the whole window — the
byte/line budget cut it, the transport's 10 MB stream cap elided bytes, or
``--append`` dropped a half line so the file could be resumed. When it is true,
``content_hint`` names the escape hatch instead of leaving the caller to
wonder: ``--output-file`` (plus ``data.range.next_offset`` for the rest).

``--output-file PATH`` writes the same bytes to disk as well, so a caller that
really does want 200 MB can loop ``--lines`` with ``--next-offset`` and append.
``data.content`` and that file are byte-identical whenever a fresh
(non-``--append``) file is written: ``data.content_sha256`` and
``data.output_file.sha256`` are the same digest.

The remote command is built from a **fixed** set of POSIX tools (``sed``,
``head``, ``tail``, ``grep``, ``wc``) and the path is single-quoted, so there is
no quoting surface left for the caller to get wrong. Use ``--grep`` for pattern
matching; it is passed as one quoted argument, never interpolated by a shell.

Exit codes follow the shared table: 0 ok, 2 argv, 4 remote path missing, 7 the
remote tools are unavailable, 102/100/101 from ssh itself, 124 timeout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import CHILD_TEXT_KWARGS, MAX_STREAM_BYTES, msys_safe_env  # noqa: E402
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_INTERNAL,
    EXIT_INVALID_ARGUMENTS,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PARSE_ERROR,
)
from lib.version import VERSION_LINE, __version__  # noqa: E402

#: Default and maximum slice size. The 10 MB envelope cap still applies on the
#: transport side; asking for more than this is a sign the caller wants
#: ``--output-file`` plus a loop over ``--next-offset``.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
HARD_MAX_BYTES = 64 * 1024 * 1024

#: ``--lines A-B`` / ``A:B`` / ``A``.
_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-:.,]\s*(\d+)\s*$")
_SINGLE_RE = re.compile(r"^\s*(\d+)\s*$")


def quote_posix(value: str) -> str:
    """Single-quote for a POSIX shell (the only quoting this script emits)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _int(value: str, what: str) -> int:
    try:
        return int(value, 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be an integer, got {value!r}") from exc


@dataclass(frozen=True)
class SliceSpec:
    """The requested window, normalised."""

    mode: str                 # stat | head | tail | lines | bytes | grep
    start: int | None = None
    end: int | None = None
    pattern: str | None = None
    context: int = 0


def parse_spec(args: argparse.Namespace) -> SliceSpec:
    """Turn the mutually-exclusive flags into a :class:`SliceSpec`."""
    chosen = [
        name for name in ("stat", "head", "tail", "lines", "bytes", "grep")
        if getattr(args, name, None) not in (None, False)
    ]
    if not chosen:
        raise ValueError(
            "pick one of --stat, --head, --tail, --lines, --bytes, --grep"
        )
    if len(chosen) > 1:
        raise ValueError(
            "these are mutually exclusive: " + ", ".join("--" + c for c in chosen)
        )
    mode = chosen[0]
    if mode in ("head", "tail"):
        count = int(getattr(args, mode))
        if count < 0:
            raise ValueError(f"--{mode} needs a non-negative count")
        if mode == "head":
            return SliceSpec("head", start=1, end=count)
        return SliceSpec("tail", start=count)
    if mode == "lines":
        match = _RANGE_RE.match(str(args.lines))
        if match:
            start, end = int(match.group(1)), int(match.group(2))
        else:
            single = _SINGLE_RE.match(str(args.lines))
            if not single:
                raise ValueError(
                    f"--lines expects A-B (e.g. 200-400), got {args.lines!r}"
                )
            start = end = int(single.group(1))
        if start < 1 or end < start:
            raise ValueError("--lines needs 1 <= start <= end")
        return SliceSpec("lines", start=start, end=end)
    if mode == "bytes":
        match = _RANGE_RE.match(str(args.bytes))
        if match:
            start, end = int(match.group(1)), int(match.group(2))
        else:
            raise ValueError(
                f"--bytes expects A-B (e.g. 0-1048575), got {args.bytes!r}"
            )
        if start < 0 or end < start:
            raise ValueError("--bytes needs 0 <= start <= end")
        return SliceSpec("bytes", start=start, end=end)
    if mode == "grep":
        if not args.grep:
            raise ValueError("--grep needs a pattern")
        return SliceSpec("grep", pattern=args.grep, context=int(args.context))
    return SliceSpec("stat")


def build_remote_command(spec: SliceSpec, path: str, max_bytes: int,
                         max_lines: int) -> str:
    """The remote POSIX command for ``spec`` (path is single-quoted)."""
    q = quote_posix(path)
    cap = f"head -c {int(max_bytes)}"
    guard = (
        f"if [ ! -e {q} ]; then echo '__SSHCORE_NOFILE__' >&2; exit 44; fi; "
    )
    if spec.mode == "stat":
        return guard + f"wc -l -c < {q}"
    if spec.mode == "head":
        return guard + f"sed -n '1,{spec.end}p' -- {q} | {cap}"
    if spec.mode == "tail":
        return guard + (
            f"tail -n {spec.start} -- {q} | tail -c {int(max_bytes)}"
        )
    if spec.mode == "lines":
        return guard + f"sed -n '{spec.start},{spec.end}p' -- {q} | {cap}"
    if spec.mode == "bytes":
        count = spec.end - spec.start + 1
        return guard + (
            # `tail -c +N` is 1-based; fall back to dd where tail is old.
            f"tail -c +{spec.start + 1} -- {q} 2>/dev/null | head -c {int(min(count, max_bytes))}"
        )
    # grep: line numbers are part of the answer, so a caller can convert a hit
    # into a `--lines` window without a second search.
    ctx = f"-B {spec.context} -A {spec.context} " if spec.context else ""
    pattern = quote_posix(spec.pattern or "")
    return guard + (
        f"grep -n -E {ctx}-- {pattern} {q} | head -n {int(max_lines)} | {cap}"
    )


def _run_ssh_execute(host: str, command: str, args: argparse.Namespace,
                     timeout: int) -> tuple[dict[str, Any], int]:
    """Delegate the transport to ssh_execute.py (one auth path, one envelope)."""
    argv = [
        sys.executable, str(SCRIPTS_DIR / "ssh_execute.py"), host, command,
        "--json", "--timeout", str(timeout),
        "--auth", args.auth, "--password-env", args.password_env,
    ]
    if args.ssh_config:
        argv += ["--ssh-config", args.ssh_config]
    if args.user:
        argv += ["--user", args.user]
    if args.accept_new_hostkey:
        argv.append("--accept-new-hostkey")
    if args.no_redact:
        argv.append("--no-redact")
    for option in args.ssh_option or []:
        argv += ["-o", option]
    started = __import__("time").monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout + 30,
            check=False, stdin=subprocess.DEVNULL, env=msys_safe_env(),
            **CHILD_TEXT_KWARGS,
        )
    except subprocess.TimeoutExpired:
        return (
            {"success": False, "exit_code": 124, "stdout": "", "stderr": "timeout",
             "data": {"failure_class": "timeout"}},
            int((__import__("time").monotonic() - started) * 1000),
        )
    except (OSError, ValueError) as exc:
        return (
            {"success": False, "exit_code": EXIT_INTERNAL, "stdout": "",
             "stderr": str(exc), "data": {"failure_class": "internal"}},
            int((__import__("time").monotonic() - started) * 1000),
        )
    try:
        payload = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, TypeError):
        payload = {
            "success": False, "exit_code": proc.returncode,
            "stdout": proc.stdout or "", "stderr": proc.stderr or "",
            "data": {"failure_class": "ssh_execute_broken"},
        }
    return payload, int((__import__("time").monotonic() - started) * 1000)


def _range_from_output(
    spec: SliceSpec, text: str, max_bytes: int, max_lines: int
) -> dict[str, Any]:
    """Describe what came back and where a follow-up call should start.

    This is the review's D5 request: ``truncated`` alone told the caller that
    data was dropped but not *where to resume*. Every record therefore carries
    ``next_offset`` (in the mode's own unit: line for lines/head/tail/grep,
    byte for bytes) and ``eof``.
    """
    body_bytes = len(text.encode("utf-8", "replace"))
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    info: dict[str, Any] = {
        "mode": spec.mode,
        "bytes": body_bytes,
        "lines": lines,
        "truncated": body_bytes >= max_bytes or lines >= max_lines,
    }
    if spec.mode == "bytes":
        start = int(spec.start or 0)
        info["start"] = start
        info["end"] = start + body_bytes - 1 if body_bytes else start
        info["next_offset"] = info["end"] + 1
        requested = int(spec.end or 0) - start + 1
        # `eof` may only be true when the answer was *complete*: if the byte
        # budget cut it short there is more to read, whatever the range asked
        # for (review D5: "truncated" without a resume point is useless).
        info["eof"] = body_bytes < min(requested, max_bytes) and not info["truncated"]
        info["offset_unit"] = "byte"
        return info
    if spec.mode == "lines":
        start = int(spec.start or 1)
        requested = int(spec.end or 1) - start + 1
        emitted = len(text.splitlines())
        info["start"] = start
        if info["truncated"] and emitted:
            # The byte budget cuts mid-line, so the last line in the answer is
            # incomplete — `cat`ing it would duplicate the head of the next
            # window. Give the caller a clean line boundary to resume from and
            # say that one line was dropped.
            info["end"] = start + emitted - 2
            info["next_offset"] = start + emitted - 1
            info["partial_last_line"] = True
        else:
            info["end"] = start + emitted - 1 if emitted else start - 1
            info["next_offset"] = start + emitted
        # A short answer that was not cut by the budget means the range ran past
        # EOF. `truncated` (set above from the byte budget) is the separate
        # "the cap cut this short" flag — conflating them was the review's D5
        # complaint in miniature, and getting it backwards made the resumable
        # loop stop after one window.
        info["eof"] = emitted < requested and not info["truncated"]
        info["offset_unit"] = "line"
        info["requested_lines"] = requested
        return info
    if spec.mode == "head":
        emitted = len(text.splitlines())
        info["start"] = 1
        if info["truncated"] and emitted:
            info["end"] = emitted - 1
            info["next_offset"] = emitted
            info["partial_last_line"] = True
        else:
            info["end"] = emitted
            info["next_offset"] = emitted + 1
        # `head -N` reaching EOF is a complete answer, not a truncation: the
        # caller asked for the first N and the file has fewer. Compare the
        # request with what came back and nothing else.
        info["eof"] = emitted < int(spec.end or 0) and not info["truncated"]
        info["offset_unit"] = "line"
        return info
    if spec.mode == "tail":
        emitted = len(text.splitlines())
        info["start"] = None
        info["end"] = None
        info["next_offset"] = None
        # `tail -n N` reads the end of the file: nothing follows it. If the byte
        # budget cut the head off that tail, `truncated` already says so.
        info["eof"] = True
        info["offset_unit"] = "line"
        info["emitted_lines"] = emitted
        return info
    # grep / stat
    info["start"] = None
    info["end"] = None
    info["next_offset"] = None
    info["eof"] = not info["truncated"]
    info["offset_unit"] = "match"
    info["matches"] = len([line for line in text.splitlines() if line.strip()])
    return info


def _line_count(text: str) -> int:
    """Lines in ``text`` by the same rule ``data.range.lines`` uses."""
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def _content_fields(
    text: str, *, truncated: bool, hint: str | None
) -> dict[str, Any]:
    """The envelope's content block (BUG-005).

    ``ssh_slice --json`` used to ship metadata only, so every documented
    ``--json`` example returned no log body at all. The body now travels in
    ``data.content`` *and* ``stdout`` (the ssh_execute contract), with the
    counts a caller needs to trust it and an explicit ``content_truncated``
    flag so a cut answer can never look complete.
    """
    encoded = text.encode("utf-8", "replace")
    return {
        "content": text,
        "content_bytes": len(encoded),
        "content_lines": _line_count(text),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "content_truncated": bool(truncated),
        "content_hint": hint,
    }


def _truncation_hint(
    *, truncated: bool, max_bytes: int, dropped_partial_line: bool,
) -> str | None:
    """One sentence naming the escape hatch when the answer is incomplete.

    The review's complaint was silent truncation: ``truncated: true`` told the
    caller data was missing but not what to do about it. A window bigger than
    the 10 MB transport cap is the worst case because it is knowable *before*
    the call, so it gets its own wording — and an advisory even when this
    particular window happened to fit.
    """
    if not truncated:
        if max_bytes > MAX_STREAM_BYTES:
            return (
                f"--max-bytes {max_bytes} is above the {MAX_STREAM_BYTES} byte "
                "ssh_execute envelope cap: this window fit, but a larger one "
                "would be silently elided. Pass --output-file PATH when the "
                "window can exceed the cap."
            )
        return None
    if max_bytes > MAX_STREAM_BYTES:
        return (
            f"--max-bytes {max_bytes} exceeds the {MAX_STREAM_BYTES} byte "
            "ssh_execute envelope cap, so the tail of this window was elided. "
            "Pass --output-file PATH plus --append to write the whole window to "
            "disk, or keep --max-bytes at or below the cap and resume with "
            "data.range.next_offset."
        )
    if dropped_partial_line:
        return (
            "the last line was cut by --max-bytes and --append dropped it to "
            "keep the output file resumable; data.range.next_offset resumes at "
            "that line. Use --output-file plus --append to rebuild the file."
        )
    return (
        f"the window was cut at the {max_bytes} byte budget. Pass --output-file "
        "PATH to write the slice to disk, then resume with "
        "data.range.next_offset, or raise --max-bytes."
    )


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_slice.py",
        description=(
            "Read a bounded slice of a remote file with an explicit range and a "
            "resumable cursor. No hand-written sed/grep, no quoting to escape, "
            "no 10 MB surprise: the body comes back in data.content (and "
            "stdout) and data.range.next_offset resumes it."
        ),
    )
    p.add_argument("host", help="Alias from ~/.ssh/config, user@host or an IP")
    p.add_argument("remote_path", help="Absolute path on the remote host")
    p.add_argument("--stat", action="store_true",
                   help="Report line and byte counts instead of content")
    p.add_argument("--head", type=int, metavar="N", default=None,
                   help="First N lines")
    p.add_argument("--tail", type=int, metavar="N", default=None,
                   help="Last N lines (bounded by --max-bytes)")
    p.add_argument("--lines", metavar="A-B", default=None,
                   help="Line range, 1-based and inclusive (e.g. 299700-302600)")
    p.add_argument("--bytes", metavar="A-B", default=None,
                   help="Byte range, 0-based and inclusive")
    p.add_argument("--grep", metavar="PATTERN", default=None,
                   help="Extended regex, matched with `grep -n -E`; output keeps "
                        "line numbers so a hit can be turned into --lines")
    p.add_argument("--context", type=int, default=0, metavar="N",
                   help="With --grep: N lines of context (grep -B/-A)")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                   help=f"Cap on the returned slice (default {DEFAULT_MAX_BYTES}, "
                        f"max {HARD_MAX_BYTES})")
    p.add_argument("--max-lines", type=int, default=200000,
                   help="Cap on returned lines (default 200000)")
    p.add_argument("--next-offset", action="store_true",
                   help="Include data.range.next_offset (always on; kept as a "
                        "readable flag for scripts that want to be explicit)")
    p.add_argument("--output-file", default=None, metavar="PATH",
                   help="Also write the slice to this local file (append with "
                        "--append for a resumable download)")
    p.add_argument("--append", action="store_true",
                   help="With --output-file: append instead of overwrite, so a "
                        "loop over next_offset reconstructs the whole file")
    p.add_argument("--auth", choices=("auto", "key", "password"), default="auto",
                   help="Forwarded to ssh_execute (default: auto)")
    p.add_argument("--password-env", default="SSH_PASSWORD",
                   help="Environment variable holding the password")
    p.add_argument("--user", "-l", default=None, help="Login user")
    p.add_argument("--ssh-config", default=None, help="Pass -F <path> to ssh")
    p.add_argument("--accept-new-hostkey", action="store_true",
                   help="Trust an unknown host key (persisted in known_hosts)")
    p.add_argument("--timeout", type=int, default=120,
                   help="Timeout for the remote read (default: 120)")
    p.add_argument("-o", "--ssh-option", action="append", default=[],
                   metavar="KEY=VALUE", help="Extra `ssh -o` option; repeatable")
    p.add_argument("--no-redact", action="store_true",
                   help="Debug: keep raw host identifiers in the envelope")
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", "-V", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_slice.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    try:
        spec = parse_spec(args)
    except ValueError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID_ARGUMENTS, "invalid_arguments")

    max_bytes = int(args.max_bytes)
    if max_bytes < 1 or max_bytes > HARD_MAX_BYTES:
        return _fail(
            f"--max-bytes must be 1..{HARD_MAX_BYTES} (got {args.max_bytes})",
            args.json, EXIT_INVALID_ARGUMENTS, "invalid_option",
        )
    max_lines = int(args.max_lines)
    if max_lines < 1:
        return _fail("--max-lines must be at least 1", args.json,
                     EXIT_INVALID_ARGUMENTS, "invalid_option")

    command = build_remote_command(spec, args.remote_path, max_bytes, max_lines)
    payload, elapsed = _run_ssh_execute(args.host, command, args, args.timeout)
    data_in = payload.get("data") or {}
    out_text = payload.get("stdout") or ""
    ok = bool(payload.get("success"))
    # 44 is this script's own "no such file" from the remote guard.
    if payload.get("exit_code") == 44 or "__SSHCORE_NOFILE__" in (
        payload.get("stderr") or ""
    ):
        ok = False
        payload["data"] = {
            **data_in,
            "failure_class": "remote_path_missing",
            "remote_path": args.remote_path,
        }
        payload["stderr"] = (
            f"ssh_slice: {args.remote_path} does not exist on {args.host}\n"
        )

    data: dict[str, Any] = {
        "path": args.remote_path,
        "command": command,
        "elapsed_ms": elapsed,
        "max_bytes": max_bytes,
        "max_lines": max_lines,
        "ssh_slice_version": __version__,
        "transport_failure_class": data_in.get("failure_class"),
    }
    if spec.mode == "stat" and ok:
        numbers = out_text.split()
        if len(numbers) >= 2 and numbers[0].isdigit() and numbers[1].isdigit():
            data["stat"] = {"lines": int(numbers[0]), "bytes": int(numbers[1])}
            data["range"] = {
                "mode": "stat", "bytes": int(numbers[1]), "lines": int(numbers[0]),
                "next_offset": None, "eof": True, "offset_unit": "line",
                "truncated": False,
            }
    elif ok:
        data["range"] = _range_from_output(spec, out_text, max_bytes, max_lines)

    # Every successful mode has a body: the sliced lines/bytes for
    # lines/head/tail/bytes/grep, and the `wc -l -c` line for --stat (which is
    # what that mode prints, and what `--output-file` has always received).
    # `data.content` is therefore always the exact text this call produced,
    # which is what makes it byte-comparable with `--output-file` (BUG-005).
    content_text = out_text if ok else ""
    # The transport has its own 10 MB cap; when it fired, the body is not the
    # whole window even if data.range says the range was satisfied.
    transport_elided = int(data_in.get("bytes_elided") or 0)
    content_truncated = bool(
        ok and (
            (data.get("range") or {}).get("truncated")
            or transport_elided
            or data_in.get("truncated")
        )
    )

    if ok and args.output_file:
        try:
            written = _write_slice_file(args, out_text, data.get("range") or {})
        except OSError as exc:
            # The transport succeeded and the body is known, so report it even
            # though the disk write failed: the caller keeps the content and
            # only loses the file.
            data.update(_content_fields(
                content_text, truncated=content_truncated, hint=None,
            ))
            return _fail(
                f"ssh_slice: cannot write --output-file {args.output_file}: {exc}",
                args.json, EXIT_INVALID_ARGUMENTS, "invalid_arguments",
                extra=data,
            )
        # The helper hands back the exact text it wrote; `data.content` must
        # describe that, not whatever the transport returned (they differ only
        # when --append dropped a half line).
        content_text = written.pop("_written_text")
        data["output_file"] = written
        if written.get("dropped_partial_line"):
            data["range"] = {**data.get("range", {}), "output_truncated_to_line": True}
            content_truncated = True

    data.update(
        _content_fields(
            content_text,
            truncated=content_truncated,
            # `--stat` returns two numbers, so neither the byte budget nor the
            # transport cap can cut it: no hint belongs on that answer.
            hint=(
                None if spec.mode == "stat"
                else _truncation_hint(
                    truncated=content_truncated,
                    max_bytes=max_bytes,
                    dropped_partial_line=bool(
                        (data.get("output_file") or {}).get("dropped_partial_line")
                    ),
                )
            ),
        )
    )

    if transport_elided:
        data["transport_bytes_elided"] = transport_elided

    result: dict[str, Any] = {
        "success": ok,
        # JSON mode carries the body in `stdout`, exactly like `ssh_execute
        # --json`; leaving it empty made every documented `--json` example
        # return metadata with no log text (BUG-005).
        "stdout": content_text,
        "stderr": payload.get("stderr") or "",
        "data": {**data, "failure_class": None if ok else
                 (payload.get("data") or {}).get("failure_class", "remote_error")},
    }
    result["exit_code"] = (
        EXIT_OK if ok else payload.get("exit_code", EXIT_INTERNAL)
    )
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        if ok:
            # Human mode: the body, then the hint on stderr (never mixed into
            # the body, so a redirect still produces the exact slice).
            sys.stdout.write(content_text)
            if data.get("content_hint"):
                sys.stderr.write(f"ssh_slice: {data['content_hint']}\n")
        if result["stderr"]:
            sys.stderr.write(result["stderr"])
    return int(result["exit_code"])


def _write_slice_file(
    args: argparse.Namespace, text: str, range_info: dict[str, Any]
) -> dict[str, Any]:
    """Write this window to ``--output-file`` (append or overwrite).

    ``--append`` in a loop is how a caller reconstructs a file bigger than any
    envelope, so the byte stream has to be exact. A window cut by the *byte*
    budget ends mid-line; writing that tail and then appending the next window
    duplicates it (measured: a 2.6 MB file came back 124 bytes too long, and the
    sha256 no longer matched). The half line is therefore dropped and the
    envelope says so.
    """
    target = Path(args.output_file).expanduser()
    mode = "ab" if args.append else "wb"
    payload_bytes = text.encode("utf-8")
    dropped = False
    if args.append and range_info.get("partial_last_line"):
        cut = payload_bytes.rfind(b"\n")
        if cut >= 0:
            payload_bytes = payload_bytes[: cut + 1]
            dropped = True
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, mode) as handle:
        handle.write(payload_bytes)
    info: dict[str, Any] = {
        "path": str(target),
        "appended": bool(args.append),
        "bytes_this_slice": len(payload_bytes),
        "file_bytes": target.stat().st_size,
        # A fresh (non-append) file holds exactly these bytes, so the digest is
        # the verifiable link to `data.content` the issue asks for.
        "sha256": _file_sha256(target) if not args.append else None,
    }
    if dropped:
        info["dropped_partial_line"] = True
    # Consumed by `main`, then removed: `data.content` must describe what was
    # actually written, not what the transport happened to return.
    info["_written_text"] = payload_bytes.decode("utf-8", "replace")
    return info


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(message: str, as_json: bool, code: int, failure_class: str,
          extra: dict[str, Any] | None = None) -> int:
    payload = {
        "success": False,
        "exit_code": code,
        "stdout": "",
        "stderr": message if message.endswith("\n") else message + "\n",
        "data": {**(extra or {}), "failure_class": failure_class},
    }
    if as_json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return code


if __name__ == "__main__":
    sys.exit(guarded_main("ssh_slice.py", main))
