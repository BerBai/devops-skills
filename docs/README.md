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
| [`architecture.md`](architecture.md) | How the eight plugins compose, the ssh-core boundary, "success", and runtime state |
| [`prd/v1.0.0-post-release-hardening.md`](prd/v1.0.0-post-release-hardening.md) | Post-1.0.0 hardening PRD: lint baseline, four red tests, ADR recovery, Vale scope |
| [`prd/v1.0.0-external-verification.md`](prd/v1.0.0-external-verification.md) | External-environment verification matrix: SSH, Docker, K8s, IaC, CI, Windows, password auth, offline install |

ADRs: [`ADR-001`](adr/ADR-001-ssh-execute.md), [`ADR-002`](adr/ADR-002-ssh-config-manager.md), [`ADR-003`](adr/ADR-003-sftp-transport.md), [`ADR-004`](adr/ADR-004-ssh-core-1.5.0-sync.md), [`ADR-005`](adr/ADR-005-ssh-core-boundary.md), [`ADR-006`](adr/ADR-006-python-38-floor.md).

Release-level documents stay at the root: [`CHANGELOG.md`](../CHANGELOG.md), [`SECURITY.md`](../SECURITY.md), [`CLAIM-LEDGER.md`](../CLAIM-LEDGER.md).

## Generated files

Two helpers are vendored into every plugin that needs them, because plugins are
distributed independently and must not import across plugin boundaries. They are
generated from `scripts/templates/` and must never be edited in place:

| Template | Copies | Command |
|---|---|---|
| `scripts/templates/_ssh_core_locator.py` | 7 (`plugins/*/skills/*/scripts/**/`) | `python scripts/sync_vendored.py` |
| `scripts/templates/_version.py` | 7 (`plugins/*/skills/*/scripts/`) | `python scripts/sync_vendored.py` |

`python scripts/sync_vendored.py --check` is what the `test` job runs
(`tests/test_manifests.py` asserts the same thing); it exits 1 and names the
drifted file. `plugins/ssh-core/skills/ssh-core/references/cli-reference.md` is
generated from `argparse` + `exit_codes.py` by
`python scripts/gen_cli_reference.py --plugin ssh-core --write`; the `docs` job
runs `--check --all`.

## Checks you can run locally

CI (`.github/workflows/ci.yml`) runs four jobs. Three are code gates; one is the
documentation gate:

| CI job | Local command | Status after the 1.0.0 hardening |
|---|---|---|
| `test` | `pytest tests/ -q` | green: `2009 passed, 151 skipped` (macOS 15 / Python 3.11) |
| `lint` | `python tools/check_ruff_baseline.py` | green: `380/380` findings; raw `ruff check .` still reports 380 |
| `floor` | `python tools/check_python_floor.py` | green: `scanned 216 files; 0 problem(s)` |
| `docs` | `python scripts/check_docs_links.py --require-index && python scripts/check_docs_parity.py && python scripts/gen_cli_reference.py --check --all && python scripts/gen_cli_reference.py --examples` + markdownlint + Vale | green: three `OK` lines, `27 run, 482 skipped`, 0 markdownlint, 0 Vale |

The `docs` job also runs `markdownlint-cli --config .markdownlint.jsonc` and
`vale --config .vale.ini` (pinned `@vvago/vale@3.25.1`) over the same file set. The four former test failures are now deterministic
(`getaddrinfo` mocks, Windows path semantics, an actual `per_source` key);
`ruff` debt is explicit in `tools/ruff-baseline.json` and may only shrink.
`tools/check_python_floor.py` enforces the declared Python 3.8 floor twice:
`ast.parse(..., feature_version=(3, 8))` for syntax, and an AST walk for APIs
that parse but raise on 3.8 (`str.removeprefix`, `zip(strict=)`,
`Path.write_text(newline=)`, `TemporaryDirectory(ignore_cleanup_errors=)`). A
guard that genuinely needs the newer API marks the line with `# floor-ok` — see
`tests/ssh_core/compat.py`.
