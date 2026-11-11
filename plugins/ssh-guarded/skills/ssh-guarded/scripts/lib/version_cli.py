"""``--version`` / ``-V`` for every ssh-guarded script (DEVOPS-GAP-004).

The version itself comes from the plugin manifest (``.claude-plugin/plugin.json``)
through the vendored ``scripts/_version.py`` helper every plugin ships — see
``scripts/templates/_version.py``. This module only adds the ssh-guarded
envelope around it, so ``--version --json`` has the same six keys as every
other output of this plugin (GAP-005)::

    {"success": true, "exit_code": 0, "stdout": "run_request.py (ssh-guarded 1.0.0)\\n",
     "stderr": "", "data": {"prog": ..., "plugin": ..., "version": ...}, "error": null}

Call :func:`handle_version` as the *first* statement of ``main`` so it answers
even when the script normally requires positional arguments, and call
:func:`add_version_flag` in ``build_parser`` so the flag shows up in ``--help``.

Both dependencies are loaded by file path, so this module works whether it is
imported as ``lib.version_cli`` or loaded standalone next to ``lib/envelope.py``
(the two scripts that talk to ssh-core import a different ``lib`` package and
load these helpers by path instead).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

#: Shown when the manifest cannot be located (plugin copied out by hand).
UNKNOWN_VERSION = "0.0.0"

#: Cached vendored helper module.
_VENDORED: ModuleType | None = None

#: Cached envelope module.
_ENVELOPE: ModuleType | None = None

#: Cached ``(plugin, version)``; a CLI run cannot see the manifest change.
_CACHE: tuple[str, str] | None = None


def _load_by_path(name: str, path: Path) -> ModuleType | None:
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _here() -> Path:
    return Path(__file__).resolve().parent


def _envelope() -> ModuleType | None:
    """Load ``lib/envelope.py`` by path (it has no relative imports)."""
    global _ENVELOPE
    if _ENVELOPE is None:
        _ENVELOPE = _load_by_path("ssh_guarded_envelope", _here() / "envelope.py")
    return _ENVELOPE


def _vendored() -> ModuleType | None:
    """Load ``scripts/_version.py`` by path."""
    global _VENDORED
    if _VENDORED is None:
        _VENDORED = _load_by_path(
            "ssh_guarded_version", _here().parent / "_version.py"
        )
    return _VENDORED


def manifest() -> tuple[str, str]:
    """Return ``(plugin_name, version)`` from the nearest ``plugin.json``."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    module = _vendored()
    if module is None:  # pragma: no cover - vendored helper is shipped
        _CACHE = ("ssh-guarded", UNKNOWN_VERSION)
        return _CACHE
    _CACHE = module.manifest()
    return _CACHE


def version_line(prog: str) -> str:
    """``<script> (<plugin> <version>)`` — the shape ssh-core prints."""
    name, version = manifest()
    return f"{prog} ({name} {version})"


def wants_version(argv: list[str] | None = None) -> bool:
    """True when ``--version`` / ``-V`` appears anywhere on the command line."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    return any(arg in ("--version", "-V") for arg in arguments)


def _json_requested(argv: list[str]) -> bool:
    return "--json" in argv


def _write(line: str, prog: str, name: str, version: str, as_json: bool) -> None:
    if not as_json:
        sys.stdout.write(line + "\n")
        return
    envelope_module = _envelope()
    payload = {
        "prog": prog,
        "plugin": name,
        "version": version,
        "failure_class": None,
    }
    if envelope_module is not None:
        envelope = envelope_module.success_envelope(stdout=line + "\n", data=payload)
        envelope_module.write_json(envelope)
        return
    # Pragmatic fallback: still the same six keys if envelope.py is missing.
    import json  # noqa: PLC0415 - only on this fallback path

    json.dump(
        {
            "success": True,
            "exit_code": 0,
            "stdout": line + "\n",
            "stderr": "",
            "data": payload,
            "error": None,
        },
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")


def handle_version(prog: str, argv: list[str] | None = None) -> int | None:
    """Answer ``--version`` early; return an exit code, or ``None`` to continue."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not wants_version(arguments):
        return None
    name, version = manifest()
    line = f"{prog} ({name} {version})"
    _write(line, prog, name, version, _json_requested(arguments))
    return 0


def add_version_flag(parser: Any, help_text: str | None = None) -> None:
    """Register ``--version`` so it also appears in ``--help``."""
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
    "version_line",
    "wants_version",
]
