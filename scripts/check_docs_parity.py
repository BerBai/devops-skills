#!/usr/bin/env python3
"""check_docs_parity.py - enforce structural parity between EN and zh-CN docs.

The repository ships two languages for the two pages a newcomer reads first.
A translation that silently loses a section, a table row, a code block or a
link is not a translation; it is a second, older document. This checker compares
structure only (the words are allowed to differ):

    heading level sequence     e.g. 1,2,2,3,2
    table rows                 every ``|`` row, separator rows included
    fenced code blocks         opening fences, per file
    links                      inline links plus reference definitions

Pairs are declared in ``PAIRS``; add a row when a new bilingual page ships.

    python scripts/check_docs_parity.py
    python scripts/check_docs_parity.py --json

Exit 0 when every pair matches on all four metrics, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

# Percent formatting keeps the report lines readable and 3.8-compatible.
# ruff: noqa: UP031

REPO_ROOT = Path(__file__).resolve().parent.parent

#: (English, translation) pairs that must stay structurally identical.
PAIRS = (
    ("README.md", "README.zh-CN.md"),
    ("CONTRIBUTING.md", "CONTRIBUTING.zh-CN.md"),
)

_FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+")
_LINK_RE = re.compile(r"!?\[[^\]]*\]\((?:<[^>]*>|[^)\s]+)")


def _without_fences(text: str) -> list[str]:
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group("fence")[:1]
                lines.append("")
            else:
                lines.append(line)
        else:
            if match and match.group("fence").startswith(fence):
                fence = None
            lines.append("")
    return lines


def metrics(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    visible = _without_fences(raw)
    headings = [
        len(_HEADING_RE.match(line).group(1))  # type: ignore[union-attr]
        for line in visible
        if _HEADING_RE.match(line)
    ]
    table_rows = sum(1 for line in visible if line.lstrip().startswith("|"))
    code_blocks = sum(1 for line in lines if _FENCE_RE.match(line)) // 2
    links = len(_LINK_RE.findall(raw)) + len(
        re.findall(r"^\s{0,3}\[[^\]]+\]:\s*\S+", raw, flags=re.MULTILINE)
    )
    return {
        "headings": headings,
        "table_rows": table_rows,
        "code_blocks": code_blocks,
        "links": links,
    }


def compare(english: Path, translation: Path) -> list[str]:
    left = metrics(english)
    right = metrics(translation)
    problems: list[str] = []
    for key in ("headings", "table_rows", "code_blocks", "links"):
        if left[key] != right[key]:
            problems.append(
                f"{key}: {key} mismatch: {english.name} has {left[key]!r}, {translation.name} has {right[key]!r}"
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    report = []
    problems: list[str] = []
    for english_name, translation_name in PAIRS:
        english = REPO_ROOT / english_name
        translation = REPO_ROOT / translation_name
        if not english.is_file() or not translation.is_file():
            problems.append(
                f"missing pair member: {english_name} / {translation_name}"
            )
            continue
        pair_problems = compare(english, translation)
        problems.extend(pair_problems)
        report.append(
            {
                "english": english_name,
                "translation": translation_name,
                "metrics": {
                    "english": metrics(english),
                    "translation": metrics(translation),
                },
                "ok": not pair_problems,
            }
        )

    if args.json:
        print(json.dumps({"ok": not problems, "pairs": report, "problems": problems}, indent=2))
    else:
        for entry in report:
            left = entry["metrics"]["english"]
            right = entry["metrics"]["translation"]
            print(
                "{} <-> {}: headings={} table_rows={} code_blocks={} links={} -> {}".format(
                    entry["english"],
                    entry["translation"],
                    "same" if left["headings"] == right["headings"] else "DIFF",
                    "same" if left["table_rows"] == right["table_rows"] else "DIFF",
                    "same" if left["code_blocks"] == right["code_blocks"] else "DIFF",
                    "same" if left["links"] == right["links"] else "DIFF",
                    "OK" if entry["ok"] else "FAIL",
                )
            )
        for problem in problems:
            print(problem, file=sys.stderr)

    if problems:
        print("%d parity problem(s)" % len(problems), file=sys.stderr)
        return 1
    print("parity: OK (%d pair(s))" % len(PAIRS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
