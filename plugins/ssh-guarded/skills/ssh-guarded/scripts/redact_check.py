#!/usr/bin/env python3
# ruff: noqa: E402, I001
"""redact_check.py - preview what the redaction pipeline would scrub (v1.0).

Public surface::

    redact_check.py [--catalog PATH] [--json] [--emit-redacted] [--report-only] < input.log

Default human output writes redacted text to stdout and a hit summary to stderr.
JSON output writes a single envelope to stdout; redacted text is included only
with ``--emit-redacted``.

Per-script failure_class namespace (exit codes 70-74):
    70  catalog_load_failed
    71  catalog_schema_invalid
    72  regex_compile_failed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import Redactor  # type: ignore[import-not-found]  # noqa: E402
from lib.envelope import GuardedArgumentParser  # type: ignore[import-not-found]  # noqa: E402
from lib.envelope import failure_envelope, success_envelope, write_json  # noqa: E402
from lib.version_cli import add_version_flag, handle_version  # noqa: E402

DEFAULT_USER_CATALOG = Path.home() / ".devops" / "redact.json"

_EXIT_CODES: dict[str | None, int] = {
    None: 0,
    "catalog_load_failed": 70,
    "catalog_schema_invalid": 71,
    "regex_compile_failed": 72,
    "_generic": 1,
}
_SURROGATE_ESCAPE_MIN = 0xDC80
_SURROGATE_ESCAPE_MAX = 0xDCFF


@dataclass(frozen=True)
class Hit:
    kind: str
    line: int
    match_len: int


@dataclass(frozen=True)
class CatalogError(Exception):
    failure_class: str
    exit_code: int
    message: str
    extra: dict[str, Any] | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = GuardedArgumentParser(prog="redact_check.py")
    parser.add_argument(
        "--catalog",
        default=None,
        help="Catalog JSON path. Replaces built-ins when provided.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print only the hits summary, no redacted text",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--emit-redacted",
        action="store_true",
        help="Include data.redacted_text in --json output",
    )
    add_version_flag(parser)
    return parser


def _load_redactor(catalog_arg: str | None) -> Redactor:
    if catalog_arg:
        path = Path(catalog_arg).expanduser()
        _validate_catalog(path)
        return Redactor(path)

    if DEFAULT_USER_CATALOG.exists():
        _validate_catalog(DEFAULT_USER_CATALOG)
        return Redactor(DEFAULT_USER_CATALOG)

    return Redactor(None)


def _validate_catalog(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CatalogError(
            "catalog_load_failed",
            _EXIT_CODES["catalog_load_failed"],
            f"catalog file not found: {path}",
        ) from exc
    except OSError as exc:
        raise CatalogError(
            "catalog_load_failed",
            _EXIT_CODES["catalog_load_failed"],
            f"catalog file could not be read: {path}: {exc}",
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogError(
            "catalog_schema_invalid",
            _EXIT_CODES["catalog_schema_invalid"],
            f"catalog JSON invalid: {exc}",
        ) from exc

    if not isinstance(data, dict) or data.get("version") != 1:
        raise CatalogError(
            "catalog_schema_invalid",
            _EXIT_CODES["catalog_schema_invalid"],
            "catalog must be an object with version: 1",
        )
    patterns = data.get("patterns")
    if not isinstance(patterns, list):
        raise CatalogError(
            "catalog_schema_invalid",
            _EXIT_CODES["catalog_schema_invalid"],
            "catalog 'patterns' must be a list",
        )

    for index, entry in enumerate(patterns):
        if not isinstance(entry, dict):
            raise CatalogError(
                "catalog_schema_invalid",
                _EXIT_CODES["catalog_schema_invalid"],
                f"catalog pattern at index {index} must be an object",
            )
        for key in ("kind", "regex", "replace_with"):
            if not isinstance(entry.get(key), str):
                raise CatalogError(
                    "catalog_schema_invalid",
                    _EXIT_CODES["catalog_schema_invalid"],
                    f"catalog pattern at index {index} missing string field {key!r}",
                )
        try:
            re.compile(entry["regex"])
        except re.error as exc:
            kind = entry["kind"]
            raise CatalogError(
                "regex_compile_failed",
                _EXIT_CODES["regex_compile_failed"],
                f"regex compile failed for kind={kind!r}: {exc}",
                {"offending_kind": kind},
            ) from exc


def _apply_with_details(redactor: Redactor, text: str) -> tuple[str, list[Hit]]:
    """Apply the catalog to the whole text, reporting one-indexed line numbers.

    Patterns are applied to the *entire* buffer rather than per line: the
    private-key rule is deliberately ``(?s)`` multi-line, and a line-by-line
    pass could only ever match its BEGIN header, leaving the key body in the
    output. Every replacement is inline (no newlines added or removed), so
    offsets stay valid and line numbers can be derived from the current text.
    """
    out = text
    hits: list[Hit] = []
    compiled = list(redactor._compiled)  # noqa: SLF001 - shipped lib has no public hit iterator.

    for kind, pattern, replace_with in compiled:
        matches = list(pattern.finditer(out))
        if not matches:
            continue
        for match in matches:
            hits.append(
                Hit(
                    kind=kind,
                    line=out.count("\n", 0, match.start()) + 1,
                    match_len=len(match.group(0)),
                )
            )
        out = pattern.sub(replace_with, out)

    hits.sort(key=lambda hit: hit.line)
    return out, hits


def _has_surrogate_escape(text: str) -> bool:
    return any(_SURROGATE_ESCAPE_MIN <= ord(ch) <= _SURROGATE_ESCAPE_MAX for ch in text)


def _summary_line(hits: list[Hit]) -> str:
    if not hits:
        return "redact_check: 0 hits\n"
    counts = Counter(hit.kind for hit in hits)
    detail = ", ".join(f"{kind}={counts[kind]}" for kind in sorted(counts))
    return f"redact_check: {len(hits)} hits ({detail})\n"


def _hit_to_data(hit: Hit) -> dict[str, Any]:
    return {"kind": hit.kind, "line": hit.line, "match_len": hit.match_len}


def _emit_success(
    *,
    args: argparse.Namespace,
    redacted_text: str,
    hits: list[Hit],
) -> int:
    summary = _summary_line(hits)
    if args.json:
        data: dict[str, Any] = {
            "hits": [_hit_to_data(hit) for hit in hits],
            "redacted_text_len": len(redacted_text),
            "total_hits": len(hits),
            "summary": dict(sorted(Counter(hit.kind for hit in hits).items())),
            "failure_class": None,
        }
        if args.emit_redacted:
            data["redacted_text"] = redacted_text
        envelope = success_envelope(data=data)
        write_json(envelope)
        return 0

    if not args.report_only:
        sys.stdout.write(redacted_text)
    sys.stderr.write(summary)
    return 0


def _emit_error(
    failure_class: str,
    exit_code: int,
    message: str,
    args: argparse.Namespace,
    *,
    extra: dict[str, Any] | None = None,
) -> int:
    stderr = f"failure_class={failure_class}: {message}\n"
    data = {
        "failure_class": failure_class,
        "catalog": getattr(args, "catalog", None),
        **(extra or {}),
    }
    envelope = failure_envelope(
        failure_class=failure_class,
        message=message,
        exit_code=exit_code,
        stderr=stderr,
        data=data,
    )
    if args.json:
        write_json(envelope)
    else:
        sys.stderr.write(stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    early = handle_version("redact_check.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    try:
        redactor = _load_redactor(args.catalog)
        text = sys.stdin.read()
        if _has_surrogate_escape(text):
            return _emit_error(
                "generic",
                _EXIT_CODES["_generic"],
                "stdin decode failed (binary input?)",
                args,
            )
        redacted_text, hits = _apply_with_details(redactor, text)
    except CatalogError as exc:
        return _emit_error(
            exc.failure_class,
            exc.exit_code,
            exc.message,
            args,
            extra=exc.extra,
        )
    except UnicodeDecodeError as exc:
        return _emit_error(
            "generic",
            _EXIT_CODES["_generic"],
            f"stdin decode failed (binary input?): {exc}",
            args,
        )
    except re.error as exc:
        return _emit_error(
            "generic",
            _EXIT_CODES["_generic"],
            f"redaction failed: {exc}",
            args,
        )

    return _emit_success(args=args, redacted_text=redacted_text, hits=hits)


if __name__ == "__main__":
    sys.exit(main())
