"""Shared CLI plumbing: JSON on argparse errors, `--version`, audit flags.

Two contract gaps this module closes:

* **SSH-18** — argparse's default ``error()`` prints usage to stderr and
  exits 2 with *nothing* on stdout, so an agent that only speaks the JSON
  envelope cannot tell "you typed the flag wrong" from "the script crashed".
  :class:`SSHArgumentParser` always writes a parseable envelope to stdout
  (and keeps the human usage text on stderr).
* **SSH-30** — no script exposed ``--version``. :func:`handle_version`
  answers it from :mod:`lib.version` before argparse runs, so it works even
  when the script normally requires positional arguments.

A third one was added in 1.5 (review A2): :func:`guarded_main` wraps ``main``
so an unexpected ``OSError`` — the askpass scratch directory was the case that
actually shipped — still produces the shared envelope instead of a traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from .exit_codes import EXIT_INTERNAL, EXIT_INVALID_ARGUMENTS, EXIT_OK
from .version import __version__, version_envelope, wants_version


def _json_requested(argv: list[str]) -> bool:
    return "--json" in argv


class SSHArgumentParser(argparse.ArgumentParser):
    """``ArgumentParser`` that emits a JSON envelope on *every* parse error.

    The envelope mirrors the normal contract so a caller can run the same
    ``json.loads`` + ``data.failure_class`` handling on success, runtime
    failure and argv failure alike::

        {"success": false, "exit_code": 2, "stdout": "", "stderr": "...",
         "data": {"failure_class": "invalid_arguments", "prog": "...",
                  "usage": "...", "message": "..."}}

    The human-readable ``prog: error: message`` + usage always goes to
    stderr, so interactive users lose nothing.
    """

    def error(self, message: str) -> None:  # noqa: D102 — argparse contract
        usage = self.format_usage().strip()
        envelope: dict[str, Any] = {
            "success": False,
            "exit_code": EXIT_INVALID_ARGUMENTS,
            "stdout": "",
            "stderr": f"{self.prog}: error: {message}\n{usage}\n",
            "data": {
                "failure_class": "invalid_arguments",
                "prog": self.prog,
                "message": message,
                "usage": usage,
                "version": __version__,
            },
        }
        try:
            json.dump(envelope, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
            sys.stdout.flush()
        except (OSError, ValueError, UnicodeEncodeError):
            # Never let the error path raise: stdout may be closed or the
            # console code page may refuse the characters.
            pass
        sys.stderr.write(f"{self.prog}: error: {message}\n{usage}\n")
        raise SystemExit(EXIT_INVALID_ARGUMENTS)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # noqa: D102
        # ``--help`` goes through here with status 0: keep that path human.
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


def add_version_flag(parser: argparse.ArgumentParser) -> None:
    """Register ``--version`` so it shows up in ``--help``.

    The actual answer is produced by :func:`handle_version` before parsing,
    because ``--version`` must work without the required positional args.
    """
    parser.add_argument(
        "--version",
        "-V",
        action="store_true",
        help="Print the ssh-core version and exit",
    )


def handle_version(prog: str, argv: list[str]) -> int | None:
    """Answer ``--version`` / ``-V`` early. Returns an exit code or ``None``."""
    if requests_version(argv):
        return version_envelope(prog, _json_requested(argv))
    return None


def requests_version(argv: list[str]) -> bool:
    return wants_version(argv)


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit the shared JSON envelope on stdout",
    )


def json_wanted(args: argparse.Namespace, argv: list[str] | None = None) -> bool:
    """True when the caller asked for JSON, however the flag was wired."""
    if getattr(args, "json", False):
        return True
    return _json_requested(list(argv or []))


def failure_envelope(
    message: str,
    *,
    failure_class: str = "internal",
    exit_code: int = EXIT_INTERNAL,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared envelope for a failure raised outside the normal flow."""
    payload: dict[str, Any] = {
        "success": False,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": message if message.endswith("\n") else message + "\n",
        "data": {"failure_class": failure_class, **(data or {})},
    }
    return payload


def guarded_main(
    prog: str,
    main: Callable[..., int],
    argv: list[str] | None = None,
) -> int:
    """Call ``main`` and convert any escaping exception into an envelope.

    Review A2 found exactly one path in the whole skill that could print a raw
    traceback on stdout: ``lib.askpass`` raising ``OSError`` when no scratch
    directory is writable. Every other failure already had a
    ``failure_class``, so a caller parsing the stream hit "unexpected
    character" at the worst possible moment (first password-auth call).

    The rules:

    * ``AskpassUnavailable`` → ``failure_class: "askpass_unavailable"`` (114),
      with the tried directories and both override variables in ``data``;
    * ``OSError`` / ``PermissionError`` → ``failure_class: "internal"`` (1),
      still an envelope;
    * ``KeyboardInterrupt`` → the shell's 130, still an envelope;
    * anything else → ``failure_class: "internal"`` (1) with the exception type
      in ``data.exception``, never a traceback.

    ``--json`` is honoured by scanning argv, because the traceback can happen
    before argparse ever ran. Without ``--json`` the message goes to stderr and
    nothing is written to stdout (a human pipeline stays clean).
    """
    from .askpass import AskpassUnavailable

    argv_list = list(sys.argv[1:] if argv is None else argv)
    as_json = _json_requested(argv_list)
    try:
        return int(main(argv_list))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        payload = failure_envelope(
            f"{prog}: interrupted", failure_class="interrupted", exit_code=130,
        )
        _emit_failure(payload, as_json)
        return 130
    except AskpassUnavailable as exc:
        payload = failure_envelope(
            str(exc),
            failure_class="askpass_unavailable",
            exit_code=exc.exit_code,
            data={
                **exc.as_data(),
                "hint": (
                    "Set SSH_CORE_ASKPASS_DIR (or SSH_CORE_ASKPASS_HOME) to a "
                    "private writable directory, then re-run."
                ),
            },
        )
        _emit_failure(payload, as_json)
        return int(payload["exit_code"])
    except OSError as exc:
        payload = failure_envelope(
            f"{prog}: {type(exc).__name__}: {exc}",
            failure_class="internal",
            exit_code=EXIT_INTERNAL,
            data={"exception": type(exc).__name__},
        )
        _emit_failure(payload, as_json)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 — the whole point is the envelope
        payload = failure_envelope(
            f"{prog}: unexpected {type(exc).__name__}: {exc}",
            failure_class="internal",
            exit_code=EXIT_INTERNAL,
            data={"exception": type(exc).__name__},
        )
        _emit_failure(payload, as_json)
        return EXIT_INTERNAL


def _emit_failure(payload: dict[str, Any], as_json: bool) -> None:
    """Write a last-resort envelope, never raising."""
    try:
        if as_json:
            json.dump(payload, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
            sys.stdout.flush()
        sys.stderr.write(payload["stderr"])
        sys.stderr.flush()
    except (OSError, ValueError, UnicodeEncodeError):
        pass


__all__ = [
    "EXIT_OK",
    "SSHArgumentParser",
    "add_json_flag",
    "add_version_flag",
    "failure_envelope",
    "guarded_main",
    "handle_version",
    "json_wanted",
    "requests_version",
]
