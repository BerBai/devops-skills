#!/usr/bin/env python3
"""check_python_floor.py - refuse 3.9+/3.10+ constructs in the plugin tree.

The marketplace declares Python ``>=3.8`` (``pyproject.toml``,
``ssh-core/SKILL.md``, ADR-006) because the transport has to run on Kylin 3 and
CentOS 7 with EPEL. A construct that parses on the dev box's 3.12 but not on a
customer's 3.8 only fails *there*, which is the worst place to find out.

Two checks, both cheap enough to run on every commit:

1. ``ast.parse(..., feature_version=(3, 8))`` — catches syntax (walrus is fine,
   ``match`` is not).
2. An **AST** walk for the *library* additions that parse fine but raise
   ``AttributeError``/``TypeError`` on 3.8. Syntax checking cannot see these,
   which is exactly why they shipped once already.

The second check walks calls, not lines, so a comment or docstring that merely
*mentions* ``Path.write_text(newline=)`` (several do — they document the rule)
does not trip it.

A guard that genuinely needs the newer API marks the line::

    tempfile.TemporaryDirectory(..., ignore_cleanup_errors=True)  # floor-ok

``# floor-ok`` on the same line suppresses that one finding. Use it only where
the call is already behind a ``sys.version_info`` guard.

Run it from the repository root::

    python tools/check_python_floor.py              # plugins/ + tests/ + scripts/ + tools/
    python tools/check_python_floor.py --root plugins

Exit 0 when clean, 1 with a per-problem list otherwise.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

#: ``(owner, attr)`` → why it is not usable on 3.8. ``owner`` is matched
#: against the attribute-chain text (``Path.write_text``, ``tempfile.Temporary
#: Directory``), so ``p.write_text(...)`` is caught by the ``*`` wildcard form.
FORBIDDEN_CALLS: dict[tuple[str, str], str] = {
    ("*", "removeprefix"): "str.removeprefix is 3.9+ (use startswith + slice)",
    ("*", "removesuffix"): "str.removesuffix is 3.9+ (use endswith + slice)",
    ("zip", "zip"): "zip(strict=) is 3.10+",
    ("*", "TemporaryDirectory"): (
        "tempfile.TemporaryDirectory(ignore_cleanup_errors=) is 3.10+ "
        "(pass the flag conditionally, or use try/finally + rmtree)"
    ),
    ("*", "write_text"): (
        "Path.write_text(newline=) is 3.10+ (open() with newline= instead)"
    ),
}

#: Keyword arguments that make the call above unsafe. ``write_text`` is only a
#: problem with ``newline=``; ``zip`` only with ``strict=``.
FORBIDDEN_KWARGS: dict[str, frozenset[str]] = {
    "zip": frozenset({"strict"}),
    "TemporaryDirectory": frozenset({"ignore_cleanup_errors"}),
    "write_text": frozenset({"newline"}),
    "removeprefix": frozenset(),   # the method itself is 3.9+
    "removesuffix": frozenset(),
}

#: Decorator keyword arguments (dataclass(slots=) / dataclass(kw_only=)).
FORBIDDEN_DECORATOR_KWARGS: dict[str, frozenset[str]] = {
    "dataclass": frozenset({"slots", "weakref_slot"}),
}

SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})

#: Marker that suppresses a finding on one line.
FLOOR_OK = "floor-ok"


def iter_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not (set(path.parts) & SKIP_DIRS)
    )


def check_syntax(path: pathlib.Path) -> tuple[list[str], ast.Module | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [f"{path}: not UTF-8 ({exc})"], None
    try:
        tree = ast.parse(text, filename=str(path), feature_version=(3, 8))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: {exc.msg} (syntax needs >3.8)"], None
    return [], tree


def _call_name(node: ast.Call) -> str | None:
    """Last attribute name of the callee: ``p.write_text`` → ``write_text``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _keyword_names(node: ast.Call) -> set[str]:
    return {kw.arg for kw in node.keywords if kw.arg}


def _floor_ok(lines: list[str], lineno: int, *, lookback: int = 4) -> bool:
    """True when the call is explicitly opted out with ``# floor-ok``.

    The marker may sit on the call's own line or on a comment directly above
    it — a multi-line call has no room for a trailing marker, and putting the
    explanation on the call line makes it unreadable.
    """
    if not (1 <= lineno <= len(lines)):
        return False
    if FLOOR_OK in lines[lineno - 1]:
        return True
    # Walk *upwards* from the call, stopping at the first non-comment line.
    floor = max(1, lineno - lookback)
    for idx in range(lineno - 1, floor - 1, -1):
        text = lines[idx - 1].strip()
        if not text.startswith("#"):
            break
        if FLOOR_OK in text:
            return True
    return False


def check_calls(path: pathlib.Path, tree: ast.Module) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []

    def report(node: ast.AST, reason: str) -> None:
        lineno = getattr(node, "lineno", 0)
        if _floor_ok(lines, lineno):
            return
        problems.append(f"{path}:{lineno}: {reason}")

    for node in ast.walk(tree):
        # zip(strict=) / TemporaryDirectory(ignore_cleanup_errors=) / ...
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in FORBIDDEN_KWARGS:
                risky = FORBIDDEN_KWARGS[name]
                if not risky or (_keyword_names(node) & risky):
                    reason = next(
                        (why for (owner, attr), why in FORBIDDEN_CALLS.items()
                         if attr == name),
                        f"{name}() needs >3.8",
                    )
                    report(node, reason)
            # dataclass(slots=True) / @dataclass(kw_only=True)
            if name in FORBIDDEN_DECORATOR_KWARGS:
                risky = FORBIDDEN_DECORATOR_KWARGS[name]
                if _keyword_names(node) & risky:
                    report(
                        node,
                        f"{name}({', '.join(sorted(risky))}...) is 3.10+",
                    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_python_floor.py",
        description=(
            "Verify the declared Python 3.8 floor: syntax and the library "
            "APIs that parse but do not exist on 3.8."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help=(
            "Directory to scan (repeatable; default: plugins, tests, scripts "
            "and tools)"
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Only print problems"
    )
    args = parser.parse_args(argv)

    roots = [
        pathlib.Path(r)
        for r in (args.root or ["plugins", "tests", "scripts", "tools"])
    ]
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        sys.stderr.write(
            f"check_python_floor: no such directory: "
            f"{', '.join(str(m) for m in missing)}; run from the repo root\n"
        )
        return 2

    files: list[pathlib.Path] = []
    for root in roots:
        files.extend(iter_python_files(root))
    files = sorted(set(files))

    problems: list[str] = []
    for path in files:
        syntax, tree = check_syntax(path)
        problems.extend(syntax)
        if tree is not None:
            problems.extend(check_calls(path, tree))

    for problem in problems:
        sys.stderr.write(problem + "\n")
    if not args.quiet:
        print(f"scanned {len(files)} files; {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
