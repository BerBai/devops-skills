"""Render the vendored per-plugin helper files from single templates.

Two files exist in more than one plugin because plugins are distributed
independently and must not import across plugin boundaries
(``docs/architecture.md``), yet keeping N hand-synced copies is how a real fix
gets silently reverted ("copy the old file over the new one" also passes a
byte-comparison test):

1. ``_ssh_core_locator.py`` -- decides whether a plugin can find ssh-core at
   all; the single failure point for every remote call seven plugins make
   (DEVOPS-OPT-002).
2. ``_version.py`` -- answers ``--version`` from the plugin manifest
   (DEVOPS-GAP-004).

Both are *generated* from ``scripts/templates/``:

    python scripts/sync_vendored.py            # rewrite the copies
    python scripts/sync_vendored.py --check    # exit 1 if a copy drifted

Edit the template, run the command, commit the template and the rendered
copies together. ``tests/test_manifests.py`` runs the same check in CI, so a
stale copy fails the build with the exact command to run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "scripts" / "templates"

#: ``template name -> (destination glob, expected copy count)``.
#: Discovered by glob so a new plugin cannot be forgotten; the count is the
#: deliberate review moment when one is added.
VENDORED: dict[str, tuple[str, int]] = {
    "_ssh_core_locator.py": ("plugins/*/skills/*/scripts/**/_ssh_core_locator.py", 7),
    "_version.py": ("plugins/*/skills/*/scripts/_version.py", 7),
}


def destinations(template_name: str) -> list[Path]:
    """Return the vendored copies of one template, sorted, excluding caches."""
    pattern, _expected = VENDORED[template_name]
    found = [
        path
        for path in REPO_ROOT.glob(pattern)
        if "__pycache__" not in path.parts
    ]
    return sorted(found)


def render(template_name: str) -> str:
    """Return the template text exactly as it must appear in every copy."""
    return (TEMPLATES / template_name).read_text(encoding="utf-8")


def drifted(template_name: str) -> list[Path]:
    """Return the copies whose bytes differ from the template."""
    expected = render(template_name)
    return [
        path
        for path in destinations(template_name)
        if path.read_text(encoding="utf-8") != expected
    ]


def check_all() -> list[str]:
    """Return human-readable problem lines; empty means everything matches."""
    problems: list[str] = []
    for name, (_pattern, expected) in VENDORED.items():
        if not (TEMPLATES / name).is_file():
            problems.append(f"template missing: scripts/templates/{name}")
            continue
        copies = destinations(name)
        if len(copies) != expected:
            problems.append(
                f"{name}: expected {expected} copies, found {len(copies)}; "
                "update VENDORED in scripts/sync_vendored.py"
            )
        for path in drifted(name):
            problems.append(
                f"{path.relative_to(REPO_ROOT)} drifted from "
                f"scripts/templates/{name}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync_vendored.py",
        description="Render the vendored per-plugin helper files from templates.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 when a copy differs from its template.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only report problems.",
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = check_all()
        if problems:
            sys.stderr.write("sync_vendored: vendored files are out of date:\n")
            for line in problems:
                sys.stderr.write(f"  {line}\n")
            sys.stderr.write(
                "Run `python scripts/sync_vendored.py` and commit the result.\n"
            )
            return 1
        if not args.quiet:
            total = sum(len(destinations(name)) for name in VENDORED)
            print(f"sync_vendored: {total} copies match their templates")
        return 0

    changed = 0
    total = 0
    for name, (_pattern, expected) in VENDORED.items():
        template = TEMPLATES / name
        if not template.is_file():
            sys.stderr.write(f"sync_vendored: template missing: {template}\n")
            return 1
        copies = destinations(name)
        total += len(copies)
        if len(copies) != expected:
            sys.stderr.write(
                f"sync_vendored: {name}: expected {expected} copies, found "
                f"{len(copies)}. Update VENDORED in scripts/sync_vendored.py.\n"
            )
            return 1
        payload = template.read_text(encoding="utf-8")
        for path in copies:
            if path.read_text(encoding="utf-8") != payload:
                # open() rather than Path.write_text(newline=): the latter is
                # 3.10+ and `tools/check_python_floor.py --root scripts` flags it.
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                changed += 1
                if not args.quiet:
                    print(f"wrote {path.relative_to(REPO_ROOT)}")
    if not args.quiet:
        print(f"sync_vendored: {total} copies checked, {changed} rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
