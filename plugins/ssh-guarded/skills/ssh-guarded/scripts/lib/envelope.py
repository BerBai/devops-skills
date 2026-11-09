"""The single JSON envelope every ssh-guarded script emits (GAP-005).

Before this module each script assembled its own dict, and five of the nine
emitted a shape the rest of the marketplace could not read::

    ssh-guarded  {"success", "data", "error"}
    everyone else {"success", "exit_code", "stdout", "stderr", "data"}

The shape is now one thing everywhere::

    {
      "success":  bool,          # the action completed, not "was dispatched"
      "exit_code": int,          # always an int, always the process exit code
      "stdout":   str,           # the human-mode stdout text
      "stderr":   str,           # the human-mode stderr text
      "data":     object|null,   # script-specific payload
      "error":    object|null    # {"message", "failure_class", "exit_code"}
    }

``error`` is kept for backwards compatibility with the v1.0 drafters; its
``exit_code`` always equals the top-level ``exit_code``. ``jq -r .exit_code``
therefore never returns ``null``, including on the argparse-error path, which
used to write usage to stderr and nothing at all to stdout.

The module deliberately imports nothing but the standard library and has no
relative imports: :mod:`lib` is a package name several plugins ship, so a
script that must not depend on which ``lib`` won ``sys.modules`` can load this
file as a plain module.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any

#: Shared argparse / usage exit code (docs/architecture.md "0-9 shared").
EXIT_INVALID_ARGUMENTS = 2

#: argv of the parse currently in flight; subparsers inherit it because
#: argparse calls ``parse_known_args`` on them, not ``parse_args``.
_ACTIVE_ARGV: list[str] = []


def _utf8_stdio() -> None:
    """Force UTF-8 on stdout/stderr before writing an envelope.

    Windows consoles default to the ANSI code page (cp936 on a Chinese
    install), and ``json.dump(..., ensure_ascii=False)`` then raises
    ``UnicodeEncodeError`` *after* half the envelope was written, so the caller
    gets unparseable JSON. Best-effort: streams that cannot be reconfigured are
    left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def json_envelope(
    *,
    success: bool,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    data: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the six-key envelope. Every script goes through this."""
    return {
        "success": bool(success),
        "exit_code": int(exit_code),
        "stdout": str(stdout or ""),
        "stderr": str(stderr or ""),
        "data": data,
        "error": error,
    }


def success_envelope(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    data: Any = None,
) -> dict[str, Any]:
    """Envelope for a completed action (``error`` is null)."""
    return json_envelope(
        success=True,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        data=data,
        error=None,
    )


def error_object(
    failure_class: str | None,
    message: str,
    exit_code: int,
) -> dict[str, Any]:
    """The ``error`` member, kept in step with the top-level ``exit_code``."""
    return {
        "message": message,
        "failure_class": failure_class,
        "exit_code": int(exit_code),
    }


def failure_envelope(
    *,
    failure_class: str | None,
    message: str,
    exit_code: int,
    stdout: str = "",
    stderr: str | None = None,
    data: Any = None,
) -> dict[str, Any]:
    """Envelope for a classified failure.

    ``stderr`` defaults to ``message`` plus a trailing newline so the
    human-mode text and the machine field never disagree.
    """
    if stderr is None:
        stderr = message if message.endswith("\n") else message + "\n"
    return json_envelope(
        success=False,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        data=data,
        error=error_object(failure_class, message, exit_code),
    )


def write_json(envelope: dict[str, Any]) -> None:
    """Write ``envelope`` as one JSON line on stdout."""
    _utf8_stdio()
    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def emit(envelope: dict[str, Any], as_json: bool = False) -> None:
    """Write ``envelope``: one JSON line, or the human ``stdout``/``stderr``."""
    if as_json:
        write_json(envelope)
        return
    _utf8_stdio()
    stdout = envelope.get("stdout") or ""
    stderr = envelope.get("stderr") or ""
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")


class GuardedArgumentParser(argparse.ArgumentParser):
    """``ArgumentParser`` that still produces an envelope when argv is wrong.

    argparse's stock ``error()`` writes usage to stderr and exits 2 with an
    empty stdout, which makes ``jq -r .exit_code`` print ``null`` for exactly
    the failures a CI pipeline is most likely to hit. This subclass keeps the
    human message on stderr and adds the shared envelope on stdout whenever
    ``--json`` was on the command line.
    """

    _guarded_argv: list[str] | None = None

    def parse_args(
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        argv = list(sys.argv[1:] if args is None else args)
        self._guarded_argv = argv
        global _ACTIVE_ARGV
        _ACTIVE_ARGV = argv
        return super().parse_args(args, namespace)

    def _effective_argv(self) -> list[str]:
        if self._guarded_argv is not None:
            return self._guarded_argv
        if _ACTIVE_ARGV:
            return _ACTIVE_ARGV
        return list(sys.argv[1:])

    def error(self, message: str) -> None:  # noqa: D102 - argparse contract
        usage = self.format_usage().strip()
        text = f"{self.prog}: error: {message}\n{usage}\n"
        envelope = failure_envelope(
            failure_class="invalid_arguments",
            message=message,
            exit_code=EXIT_INVALID_ARGUMENTS,
            stderr=text,
            data={
                "failure_class": "invalid_arguments",
                "prog": self.prog,
                "message": message,
                "usage": usage,
            },
        )
        if "--json" in self._effective_argv():
            try:
                write_json(envelope)
            except (OSError, ValueError, UnicodeEncodeError):
                # Never let the error path raise: stdout may be closed.
                pass
        sys.stderr.write(text)
        raise SystemExit(EXIT_INVALID_ARGUMENTS)


__all__ = [
    "EXIT_INVALID_ARGUMENTS",
    "GuardedArgumentParser",
    "emit",
    "error_object",
    "failure_envelope",
    "json_envelope",
    "success_envelope",
    "write_json",
]
