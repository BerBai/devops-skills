# reports/ — documentation only

This directory holds **no runtime state**. It ships one file: this README.

## Where state actually lives

```
$SSH_GUARDED_HOME/reports/<project_id>/
├── requests/
│   ├── <request_id>.json          # drafted by request_*.py
│   ├── <request_id>.result.json   # written by run_request.py --execute
│   └── <request_id>.lock          # held during --execute
└── jobs/
    └── <job_id>.json              # written by exec_detached.py run
```

`$SSH_GUARDED_HOME` defaults to `~/.devops/ssh-guarded`
(`%USERPROFILE%\.devops\ssh-guarded` on Windows). `<project_id>` is
`--project-id`, else `SSH_GUARDED_PROJECT_ID`, else the local git repo basename.

Two overrides exist:

| Override | Scope |
|---|---|
| `$SSH_GUARDED_HOME` | the whole state tree, including the legacy-note marker |
| `--reports-dir PATH` | the `.../reports` level only — on the drafters, `exec_detached.py` and `gc_artifacts.py` (`run_request.py` writes next to the artifact it is handed) |

## Why it moved out of the skill directory

This directory used to be the write target. That broke on every read-only
installation (enterprise image, container), lost pending approvals whenever the
plugin was upgraded, and mixed two projects' artifacts into one flat folder.
See `docs/architecture.md` ("Where the state lives") and DEVOPS-GAP-006.

If artifacts from the old layout are still present, the first run of any
drafter or `exec_detached.py` prints a one-time note naming both paths. Nothing
is moved or deleted for you.

## Cleanup

`gc_artifacts.py` prunes the state tree, per project. It is a standalone verb so
it works when no request is being drafted:

```bash
python scripts/gc_artifacts.py                                                # dry run, 7-day cut-off, all projects
python scripts/gc_artifacts.py --older-than 30d --apply
python scripts/gc_artifacts.py --project-id myproj --older-than 12h --apply
python scripts/gc_artifacts.py --reports-dir /var/ci/reports --older-than 1d --apply
```

`--dry-run` is the default; `--apply` is the explicit opt-in. Three things are
never removed regardless of age:

| Kept | Why |
|---|---|
| a lock that is still held | deleting it would let two `--execute` runs collide |
| a request whose `expires_at` has not passed | it is waiting for a human decision |
| anything not matching the artefact name pattern | the state dir is not a scratch directory |

Files are aged by mtime; `--json` reports every path considered and why it was
kept or removed, plus a per-project roll-up when more than one project is swept.
