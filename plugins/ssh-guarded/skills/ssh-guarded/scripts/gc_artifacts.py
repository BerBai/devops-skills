#!/usr/bin/env python3
"""gc_artifacts.py — prune old ssh-guarded runtime artefacts.

Artifacts accumulate one file per drafted request, per execution result, per
held lock and per detached job. Nothing ever removed them, and the README
promised a ``--gc-older-than`` flag that did not exist. This is that flag, as a
verb of its own: it must be able to run when no request is being drafted.

    gc_artifacts.py [--older-than 7d] [--dry-run | --apply]
                    [--reports-dir PATH] [--project-id ID] [--json]

Layout (GAP-006)::

    $SSH_GUARDED_HOME/reports/<project_id>/requests/<id>.json
    $SSH_GUARDED_HOME/reports/<project_id>/jobs/<job_id>.json

``$SSH_GUARDED_HOME`` defaults to ``~/.devops/ssh-guarded``. ``--reports-dir``
replaces the ``.../reports`` level. ``--project-id`` narrows the sweep to one
project; without it every project under the root is cleaned, each with its own
``requests/`` and ``jobs/`` sub-directory.

Safety rules (see ``lib/gc.py``):

* a lock that is still held is never deleted
* a drafted request whose ``expires_at`` has not passed is never deleted
* symlinks and files that do not match the artefact pattern are ignored

``--dry-run`` is the default: it reports what would go. ``--apply`` is the
explicit opt-in.

Exit codes::

    0    ran (with or without deletions)
    1    ran but at least one unlink failed (details in ``data.errors``)
    2    argv error (bad --older-than, both --dry-run and --apply)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import (  # noqa: E402, I001
    GuardedArgumentParser,
    add_version_flag,
    emit,
    failure_envelope,
    gc_reports,
    handle_version,
    parse_age,
    project_ids,
    reports_root,
    success_envelope,
    write_json,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2

DEFAULT_OLDER_THAN = "7d"


def build_parser() -> argparse.ArgumentParser:
    p = GuardedArgumentParser(
        prog="gc_artifacts.py",
        description=(
            "Delete ssh-guarded artefacts older than a cut-off. Held locks and "
            "still-valid requests are always kept."
        ),
    )
    p.add_argument(
        "--older-than",
        default=DEFAULT_OLDER_THAN,
        metavar="SPEC",
        help=(
            "Age cut-off: <n>{s,m,h,d}, or a bare number of seconds "
            f"(default: {DEFAULT_OLDER_THAN})"
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        default=False,
        help="Report what would be deleted (default)",
    )
    mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="Actually delete the matching artefacts",
    )
    p.add_argument(
        "--reports-dir",
        dest="reports_dir",
        default=None,
        help=(
            "Reports root holding <project_id>/{requests,jobs} "
            "(default: $SSH_GUARDED_HOME/reports)"
        ),
    )
    p.add_argument(
        "--project-id",
        dest="project_id",
        default=argparse.SUPPRESS,
        help="Only clean this project (default: every project under the root)",
    )
    p.add_argument("--json", action="store_true", help="Emit the JSON envelope")
    add_version_flag(p)
    return p


def _human(data: dict[str, Any]) -> str:
    lines = [
        f"gc {data['root']}: scanned {data['scanned']} artefact(s), "
        f"cut-off {data['older_than_seconds']}s"
    ]
    for entry in data["entries"]:
        if entry["action"] == "kept":
            lines.append(f"  kept     {entry['path']}  ({entry['reason']})")
        else:
            lines.append(f"  {entry['action']:<12} {entry['path']}")
    if data["errors"]:
        lines.append(f"  {len(data['errors'])} error(s)")
    verb = "would delete" if data["dry_run"] else "deleted"
    lines.append(
        f"{verb} {data['would_delete'] if data['dry_run'] else data['deleted']} "
        "artefact(s)"
    )
    return "\n".join(lines) + "\n"


def _target_projects(root: Path, explicit: str | None) -> list[str]:
    """Project ids to sweep: the explicit one, or every directory under root."""
    if explicit:
        return [explicit]
    found = project_ids(root)
    return found


def _sweep(root: Path, older_than: int, dry_run: bool, project: str) -> dict[str, Any]:
    """Prune one project subtree (``<root>/<project>/{requests,jobs}``)."""
    project_root = root / project
    result = gc_reports(project_root, older_than, dry_run=dry_run)
    data = result.as_data()
    return {
        "root": str(root),
        "project_id": project,
        "older_than_seconds": older_than,
        "dry_run": dry_run,
        "scanned": data["scanned"],
        "deleted": data["deleted"],
        "would_delete": data["would_delete"],
        "entries": data["entries"],
        "errors": data["errors"],
    }


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912, PLR0915
    early = handle_version("gc_artifacts.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    try:
        older_than = parse_age(args.older_than)
    except ValueError as exc:
        # Keep the argv error on stderr for humans and in the envelope for
        # machines; both are part of the documented surface.
        sys.stderr.write(f"gc_artifacts: {exc}\n")
        envelope = failure_envelope(
            failure_class="invalid_arguments",
            message=str(exc),
            exit_code=EXIT_ARGS,
            stderr=f"gc_artifacts: {exc}\n",
            data={"failure_class": "invalid_arguments", "older_than": args.older_than},
        )
        if args.json:
            write_json(envelope)
        return EXIT_ARGS

    root = reports_root(reports_dir=getattr(args, "reports_dir", None))
    explicit_project = getattr(args, "project_id", None)
    projects = _target_projects(root, explicit_project)
    sweeps: list[dict[str, Any]] = []
    if not projects and explicit_project is None and (
        (root / "requests").is_dir() or (root / "jobs").is_dir()
    ):
        # A pre-GAP-006 flat tree was pointed at directly (``--reports-dir`` on
        # an old install). Sweep it as one pseudo-project rather than silently
        # reporting "nothing to do".
        sweeps = [_sweep(root, older_than, not args.apply, ".")]
    if not projects and not sweeps:
        # Nothing drafted yet: still a successful, informative run.
        data = {
            "root": str(root),
            "project_id": None,
            "older_than_seconds": older_than,
            "dry_run": not args.apply,
            "scanned": 0,
            "deleted": 0,
            "would_delete": 0,
            "entries": [],
            "errors": [],
        }
        envelope = success_envelope(stdout=_human(data), data=data)
        emit(envelope, args.json)
        return EXIT_OK

    if projects:
        sweeps = [
            _sweep(root, older_than, not args.apply, project)
            for project in projects
        ]
    combined: dict[str, Any] = {
        "root": str(root),
        "projects": projects,
        "older_than_seconds": older_than,
        "dry_run": not args.apply,
        "scanned": sum(s["scanned"] for s in sweeps),
        "deleted": sum(s["deleted"] for s in sweeps),
        "would_delete": sum(s["would_delete"] for s in sweeps),
        "entries": [entry for s in sweeps for entry in s["entries"]],
        "errors": [err for s in sweeps for err in s["errors"]],
        "by_project": [
            {
                "project_id": s["project_id"],
                "scanned": s["scanned"],
                "deleted": s["deleted"],
                "would_delete": s["would_delete"],
            }
            for s in sweeps
        ],
    }
    exit_code = EXIT_FAIL if combined["errors"] else EXIT_OK
    human = _human(combined)
    if len(sweeps) > 1:
        per_project = "\n".join(
            f"  {row['project_id']}: scanned {row['scanned']}, "
            f"{'would delete' if combined['dry_run'] else 'deleted'} "
            f"{row['would_delete'] if combined['dry_run'] else row['deleted']}"
            for row in combined["by_project"]
        )
        human += per_project + "\n"
    if exit_code == EXIT_OK:
        envelope = success_envelope(exit_code=exit_code, stdout=human, data=combined)
    else:
        envelope = failure_envelope(
            failure_class="internal",
            message=f"{len(combined['errors'])} unlink error(s)",
            exit_code=exit_code,
            stdout=human,
            stderr=f"gc_artifacts: {len(combined['errors'])} unlink error(s)\n",
            data={**combined, "failure_class": "internal"},
        )
    emit(envelope, args.json)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
