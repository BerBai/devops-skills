"""Catalog-driven secret redaction for ssh-guarded outputs.

Stable surface::

    class Redactor:
        def __init__(self, catalog_path: Path | None = None): ...
        def apply(self, text: str) -> tuple[str, list[dict[str, Any]]]: ...

Design source: design.md §1 C8 + §4.2.

Behavior contract (PRD FR-9 + design Risk row 3):

* Default catalog (compiled into ``_DEFAULT_PATTERNS``) is used when
  ``catalog_path`` is ``None`` or the file does not exist.
* JSON-corrupt catalog → stderr warn + fall back to default. Hard fail
  on catalog problems is the sibling ``redact_check.py`` script's job,
  NOT this lib's.
* Per-pattern ``re.compile`` failure → stderr warn + skip that pattern;
  others continue.
* ``apply(text)`` returns ``(redacted_text, hits)`` where ``hits`` is a
  list of ``{"kind": str, "count": int, "replace_with": str}`` aggregated
  by kind.
* CLI ``--no-redact`` is enforced at the CLI layer (do not instantiate
  this class). The lib itself never bypasses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Default pattern catalog — design §4.2 verbatim.
_DEFAULT_PATTERNS: list[dict[str, str]] = [
    {
        "kind": "aws_access_key",
        "regex": r"AKIA[0-9A-Z]{16}",
        "replace_with": "[REDACTED-AWS-ACCESS-KEY]",
    },
    {
        "kind": "github_token",
        "regex": r"gh[pus]_[A-Za-z0-9]{36}",
        "replace_with": "[REDACTED-GH-TOKEN]",
    },
    {
        "kind": "jwt",
        "regex": (
            r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
        ),
        "replace_with": "[REDACTED-JWT]",
    },
    {
        "kind": "rsa_private",
        "regex": r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "replace_with": "[REDACTED-PRIVATE-KEY-HEADER]",
    },
]


class Redactor:
    """Catalog-backed redactor.

    Parameters
    ----------
    catalog_path:
        Optional path to a user catalog JSON file (schema in design.md §1
        C8). ``None`` or missing file → built-in defaults. Corrupt JSON →
        stderr warn + defaults. Per-pattern compile errors → stderr warn
        + skip that pattern.
    """

    def __init__(self, catalog_path: Path | None = None) -> None:
        self._compiled: list[tuple[str, re.Pattern[str], str]] = []
        patterns = self._load_patterns(catalog_path)
        for entry in patterns:
            kind = str(entry.get("kind", "")).strip() or "unknown"
            regex = entry.get("regex", "")
            replace_with = entry.get("replace_with", "[REDACTED]")
            try:
                compiled = re.compile(regex)
            except re.error as exc:
                sys.stderr.write(
                    f"redact: skipping bad pattern '{kind}': {exc}\n"
                )
                continue
            self._compiled.append((kind, compiled, replace_with))

    # ----- catalog loading --------------------------------------------------

    @staticmethod
    def _load_patterns(catalog_path: Path | None) -> list[dict[str, Any]]:  # noqa: PLR0911
        if catalog_path is None:
            return list(_DEFAULT_PATTERNS)
        try:
            raw = catalog_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return list(_DEFAULT_PATTERNS)
        except OSError as exc:
            sys.stderr.write(
                f"redact: cannot read catalog {catalog_path}: {exc}; using defaults\n"
            )
            return list(_DEFAULT_PATTERNS)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.stderr.write(
                f"redact: catalog {catalog_path} is not valid JSON ({exc}); using defaults\n"
            )
            return list(_DEFAULT_PATTERNS)
        if not isinstance(data, dict):
            sys.stderr.write(
                f"redact: catalog {catalog_path} top-level is not an object; using defaults\n"
            )
            return list(_DEFAULT_PATTERNS)
        patterns = data.get("patterns")
        if not isinstance(patterns, list):
            sys.stderr.write(
                f"redact: catalog {catalog_path} has no 'patterns' list; using defaults\n"
            )
            return list(_DEFAULT_PATTERNS)
        cleaned: list[dict[str, Any]] = []
        for entry in patterns:
            if isinstance(entry, dict) and entry.get("regex"):
                cleaned.append(entry)
        return cleaned or list(_DEFAULT_PATTERNS)

    # ----- redaction --------------------------------------------------------

    def apply(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Return ``(redacted_text, hits)``.

        ``hits`` is one entry per pattern that actually fired, with the
        per-kind count + the replacement string. Patterns that did not
        match are omitted (so the list is greppable).
        """
        if not text:
            return text, []
        out = text
        hits: list[dict[str, Any]] = []
        for kind, compiled, replace_with in self._compiled:
            new_text, count = compiled.subn(replace_with, out)
            if count > 0:
                hits.append(
                    {"kind": kind, "count": count, "replace_with": replace_with}
                )
                out = new_text
        return out, hits


__all__ = ["Redactor"]
