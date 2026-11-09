# docs/

Design notes and operator-facing documentation that does not belong inside a
plugin bundle.

A plugin ships `SKILL.md`, `references/` and `scripts/` — that is what the
marketplace copies into an agent. Anything else (architecture, ADRs, offline
install walkthroughs) lives here so it cannot be mistaken for skill content.

## What is here

| File | Purpose |
|---|---|
| [`offline-install.md`](offline-install.md) | Air-gapped install with `scripts/build_offline_bundle.sh` + `scripts/install_offline_linux.sh` |
| [`architecture.md`](architecture.md) | How the eight plugins compose, where the ssh-core boundary sits, what "success" means, and where runtime state lives |

## Generated files

Two helpers are vendored into every plugin that needs them, because plugins are
distributed independently and must not import across plugin boundaries. They are
generated from `scripts/templates/` and must never be edited in place:

| Template | Copies | Command |
|---|---|---|
| `scripts/templates/_ssh_core_locator.py` | 7 (`plugins/*/skills/*/scripts/**/`) | `python scripts/sync_vendored.py` |
| `scripts/templates/_version.py` | 7 (`plugins/*/skills/*/scripts/`) | `python scripts/sync_vendored.py` |

`python scripts/sync_vendored.py --check` is what CI runs
(`tests/test_manifests.py` asserts the same thing); it exits 1 and lists the
drifted file instead of leaving a contributor to guess which copy is stale.

## Checks you can run locally

CI (`.github/workflows/ci.yml`) runs three jobs; each maps to one command you
can run before pushing:

| CI job | Local command |
|---|---|
| `test` | `pytest tests/ -q` |
| `lint` | `ruff check .` |
| `floor` | `python tools/check_python_floor.py` |

`tools/check_python_floor.py` enforces the declared Python 3.8 floor twice:
`ast.parse(..., feature_version=(3, 8))` for syntax, and an AST walk for the
library APIs that parse but raise on 3.8 (`str.removeprefix`,
`zip(strict=)`, `dataclass(slots=)`, `Path.write_text(newline=)`,
`TemporaryDirectory(ignore_cleanup_errors=)`). A guard that genuinely needs the
newer API marks the line with `# floor-ok` — see `tests/ssh_core/compat.py`.
