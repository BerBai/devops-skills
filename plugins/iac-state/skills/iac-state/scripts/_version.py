"""Give every public script a working ``--version`` / ``-V``.

DEVOPS-GAP-004: only ssh-core's 14 scripts answered ``--version``; the other 30
raised ``error: the following arguments are required`` because the flag was
handled after argparse demanded positionals, or was not defined at all. "Which
build is installed here" is a first-line triage question, and half the
marketplace could not answer it.

``version_envelope()`` mirrors ssh-core's output shape exactly, so one parser
handles every script:

    $ diagnose_host.py --version
    diagnose_host.py (remote-debug 1.0.0)
    $ diagnose_host.py --version --json
    {"success": true, "exit_code": 0, "stdout": "...", "stderr": "",
     "data": {"prog": "...", "version": "0.2.0", "plugin": "remote-debug"}}

The version is read from the plugin's ``.claude-plugin/plugin.json`` at call
time -- that file is the marketplace's source of truth, and copying the number
into seven plugins is how versions drift. When the manifest cannot be read (a
plugin copied out of its package by hand) the module falls back to
``UNKNOWN_VERSION`` rather than failing: ``--version`` must never be the thing
that breaks.

Usage in a script (two lines, no import of the plugin's own lib required)::

    from _version import add_version_flag, handle_version
    ...
    def main(argv):
        early = handle_version(argv)
        if early is not None:
            return early
        args = build_parser().parse_args(argv)

Call ``handle_version`` *before* ``parse_args`` so it answers even when the
script normally requires positional arguments.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Shown when the plugin manifest cannot be located or parsed.
UNKNOWN_VERSION = "0.0.0"

#: Cached ``(plugin, version)`` for this process; the manifest cannot change
#: under a running CLI in any way that matters here.
_CACHE: tuple[str, str] | None = None


def plugin_root(this_file: Path | None = None) -> Path | None:
    """Walk up from a script to the directory holding ``.claude-plugin``."""
    anchor = Path(this_file) if this_file is not None else Path(__file__)
    try:
        anchor = anchor.resolve()
    except OSError:  # pragma: no cover - broken symlink
        return None
    for parent in anchor.parents:
        if (parent / ".claude-plugin" / "plugin.json").is_file():
            return parent
    return None


def manifest(this_file: Path | None = None) -> tuple[str, str]:
    """Return ``(plugin_name, version)`` from the nearest ``plugin.json``."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    root = plugin_root(this_file)
    name = root.name if root is not None else "unknown"
    version = UNKNOWN_VERSION
    if root is not None:
        path = root / ".claude-plugin" / "plugin.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        version = str(data.get("version") or UNKNOWN_VERSION)
        name = str(data.get("name") or name)
    _CACHE = (name, version)
    return _CACHE


def version_line(prog: str, this_file: Path | None = None) -> str:
    """``<script> (<plugin> <version>)`` -- the same shape ssh-core prints."""
    name, version = manifest(this_file)
    return f"{prog} ({name} {version})"


def wants_version(argv: list[str] | None = None) -> bool:
    """True when ``--version`` / ``-V`` appears anywhere on the command line."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    return any(arg in ("--version", "-V") for arg in arguments)


def version_envelope(
    prog: str,
    as_json: bool = False,
    this_file: Path | None = None,
) -> int:
    """Print the version (plain or as a JSON envelope) and return exit 0."""
    name, version = manifest(this_file)
    line = f"{prog} ({name} {version})"
    if as_json:
        payload = {
            "success": True,
            "exit_code": 0,
            "stdout": line + "\n",
            "stderr": "",
            "data": {
                "prog": prog,
                "plugin": name,
                "version": version,
                "failure_class": None,
            },
        }
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(line + "\n")
    return 0


def handle_version(
    prog: str,
    argv: list[str] | None = None,
    this_file: Path | None = None,
) -> int | None:
    """Answer ``--version`` early; return an exit code, or ``None`` to continue.

    Call this as the first statement of ``main``, before ``parse_args``.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not wants_version(arguments):
        return None
    return version_envelope(prog, "--json" in arguments, this_file)


def add_version_flag(parser, help_text: str | None = None) -> None:
    """Register ``--version`` so it also appears in ``--help``.

    The answer itself comes from :func:`handle_version`; this only makes the
    flag discoverable.
    """
    parser.add_argument(
        "--version",
        "-V",
        action="store_true",
        help=help_text or "Print this script's version and exit",
    )


__all__ = [
    "UNKNOWN_VERSION",
    "add_version_flag",
    "handle_version",
    "manifest",
    "plugin_root",
    "version_envelope",
    "version_line",
    "wants_version",
]
