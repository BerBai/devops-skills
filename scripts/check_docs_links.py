#!/usr/bin/env python3
"""check_docs_links.py - verify relative links and heading anchors in the docs.

Scope is the documentation surface that ships or explains the marketplace:

    README.md, README.zh-CN.md
    CONTRIBUTING.md, CONTRIBUTING.zh-CN.md
    CHANGELOG.md, SECURITY.md, CLAIM-LEDGER.md
    docs/**/*.md
    plugins/*/skills/*/SKILL.md
    plugins/*/skills/*/references/**/*.md

External URLs (http/https/mailto) are not fetched by default: a docs gate that
needs the network fails for the wrong reason. Use ``--external`` to opt in to a
HEAD request per unique URL.

``--require-index`` also enforces the rule this repository learned the hard way:
every ``docs/**/*.md`` file must be linked from ``README.md`` or
``docs/README.md``. A document nobody links to is a document nobody reads.

Run from the repository root::

    python scripts/check_docs_links.py
    python scripts/check_docs_links.py --require-index
    python scripts/check_docs_links.py --external

Exit 0 when every relative target exists, every ``#fragment`` resolves to a
heading in the target, and (with --require-index) no docs page is orphaned.
Exit 1 with one line per problem otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Sequence

# Percent formatting keeps the report lines readable and 3.8-compatible; the
# return-count and magic-value rules fire on the URL/link classifier by design.
# ruff: noqa: PLR0911, PLR2004, UP031

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Files and globs that make up the documented surface.
SCOPE_GLOBS: tuple[str, ...] = (
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.zh-CN.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "CLAIM-LEDGER.md",
    "docs/**/*.md",
    "plugins/*/skills/*/SKILL.md",
    "plugins/*/skills/*/references/**/*.md",
)

#: Scheme-bearing links are external unless they are file:// (unsupported).
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
#: ``[text](target)`` and ``![alt](target)``; optional <...> and "title".
_INLINE_LINK_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(?:<(?P<angle>[^>]*)>|(?P<plain>[^)\s]+))"
    r"(?:\s+\"[^\"]*\")?\s*\)"
)
#: ``[id]: target`` reference definitions.
_REF_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<(?P<angle>[^>]*)>|(?P<plain>\S+))")
#: Fenced code block delimiters.
_FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")


def _in_scope_files() -> list[Path]:
    found: set[Path] = set()
    for pattern in SCOPE_GLOBS:
        if any(ch in pattern for ch in "*?["):
            found.update(p for p in REPO_ROOT.glob(pattern) if p.is_file())
        else:
            candidate = REPO_ROOT / pattern
            if candidate.is_file():
                found.add(candidate)
    return sorted(found)


def _strip_fences(text: str) -> list[str]:
    """Return lines with fenced code replaced by blank lines (line numbers kept)."""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group("fence")[:1]
                out.append("")
            else:
                out.append(line)
        else:
            if match and match.group("fence").startswith(fence):
                fence = None
            out.append("")
    return out


def _slug(heading: str) -> str:
    """Approximate GitHub's heading anchor algorithm.

    Lowercase, drop markdown formatting, keep Unicode letters/digits, turn
    spaces into hyphens and drop punctuation. Duplicate headings are handled by
    the caller (``-1``, ``-2`` suffixes).
    """
    text = heading.strip()
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.lower()
    text = re.sub(r"[^\w\- \u4e00-\u9fff]", "", text, flags=re.UNICODE)
    text = text.replace(" ", "-")
    return text


def _anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if fence is None and match:
            fence = match.group("fence")[:1]
            continue
        if fence is not None:
            if match and match.group("fence").startswith(fence):
                fence = None
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if heading:
            heading_text = heading.group(2)
            explicit_anchor = re.search(r"\{#([^}]+)\}", heading_text)
            if explicit_anchor:
                anchors.add(explicit_anchor.group(1))
                heading_text = heading_text[: explicit_anchor.start()].rstrip()
            base = _slug(heading_text)
            count = seen.get(base, 0)
            seen[base] = count + 1
            anchors.add(base if count == 0 else "%s-%d" % (base, count))
        for explicit in re.finditer(r"<a\s+(?:name|id)=\"([^\"]+)\"", line):
            anchors.add(explicit.group(1))
    return anchors


def _extract_links(text: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    for number, line in enumerate(_strip_fences(text), start=1):
        for match in _INLINE_LINK_RE.finditer(line):
            target = match.group("angle") or match.group("plain")
            links.append((number, target))
        ref = _REF_DEF_RE.match(line)
        if ref:
            links.append((number, ref.group("angle") or ref.group("plain")))
    return links


def _link_problem(source: Path, line: int, target: str) -> str | None:
    """Return a problem description, or None when the link is fine."""
    raw = target.strip()
    if not raw:
        return "empty link target"
    if raw.startswith("//") or _SCHEME_RE.match(raw):
        return None
    path_part, sep, fragment = raw.partition("#")
    destination = source if not path_part else (source.parent / path_part).resolve()
    try:
        destination.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return f"target escapes the repository: {raw}"
    if not destination.exists():
        return f"missing target: {raw}"
    if sep and fragment:
        if not destination.is_file() or destination.suffix.lower() != ".md":
            return f"fragment on a non-markdown target: {raw}"
        if fragment not in _anchors(destination.read_text(encoding="utf-8")):
            return f"missing anchor #{fragment} in {destination.relative_to(REPO_ROOT)}"
    return None


def _external_ok(url: str, timeout: float) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, ValueError, OSError):
        return False


def _orphans(files: Sequence[Path]) -> list[Path]:
    """docs/**/*.md files not linked from README.md or docs/README.md."""
    index_sources = [REPO_ROOT / "README.md", REPO_ROOT / "docs" / "README.md"]
    referenced: set[Path] = set()
    for source in index_sources:
        if not source.is_file():
            continue
        for _line, target in _extract_links(source.read_text(encoding="utf-8")):
            path_part = target.partition("#")[0]
            if path_part and not _SCHEME_RE.match(path_part):
                referenced.add((source.parent / path_part).resolve())
    docs_root = (REPO_ROOT / "docs").resolve()
    return [
        path
        for path in files
        if docs_root in path.resolve().parents and path.resolve() not in referenced
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require-index",
        action="store_true",
        help="fail when a docs/**/*.md file is not linked from README.md or docs/README.md",
    )
    parser.add_argument(
        "--external",
        action="store_true",
        help="HEAD-check external http(s) links (needs network; off by default)",
    )
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args(argv)

    files = _in_scope_files()
    problems: list[str] = []
    external: dict[str, list[str]] = {}

    for source in files:
        text = source.read_text(encoding="utf-8")
        for line, target in _extract_links(text):
            if _SCHEME_RE.match(target) and target.startswith(("http://", "https://")):
                external.setdefault(target, []).append(
                    "%s:%d" % (source.relative_to(REPO_ROOT), line)
                )
                continue
            problem = _link_problem(source, line, target)
            if problem:
                problems.append(
                    "%s:%d: %s" % (source.relative_to(REPO_ROOT), line, problem)
                )

    if args.external:
        for url, places in sorted(external.items()):
            if not _external_ok(url, timeout=10.0):
                problems.append(f"{places[0]}: external link unreachable: {url}")

    if args.require_index:
        for path in _orphans(files):
            problems.append(
                f"{path.relative_to(REPO_ROOT)}: docs page is not linked from README.md or docs/README.md"
            )

    if not args.quiet:
        print("checked %d files, %d relative links" % (len(files), _link_count(files)))
        print("external links: %d unique (checked: %s)" % (len(external), args.external))

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print("%d problem(s)" % len(problems), file=sys.stderr)
        return 1
    print("links: OK (0 broken, 0 bad anchors%s)" % (", 0 orphans" if args.require_index else ""))
    return 0


def _link_count(files: Iterable[Path]) -> int:
    total = 0
    for source in files:
        total += len(_extract_links(source.read_text(encoding="utf-8")))
    return total


if __name__ == "__main__":
    sys.exit(main())
