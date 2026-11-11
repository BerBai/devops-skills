# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
for the marketplace release and for each plugin.

## Version sources

Four places can claim a version. Only three are authoritative, and they must
agree within their own scope:

| Scope | Source of truth | Current value | Enforced by |
|---|---|---|---|
| Marketplace release | `.claude-plugin/marketplace.json` `metadata.version` + `pyproject.toml` `[project].version` + git tag | `1.0.0` | release review; the tag is cut from the release commit |
| Plugin `ssh-core` | `plugins/ssh-core/.claude-plugin/plugin.json` + the matching marketplace entry + `scripts/lib/version.py` | `1.0.0` | `tests/test_manifests.py::test_marketplace_entry_version_matches_plugin_json`, `::test_script_version_is_consistent_with_plugin_manifest` |
| Other seven plugins | each `plugin.json` + its marketplace entry | `1.0.0` | same manifest tests |
| `uv.lock` | generated lock file | `devops-skills 1.0.0`, `requires-python = ">=3.8"` | `uv lock` reproducibility; regenerated with `pyproject.toml` |

`uv.lock` is generated, not hand-edited. It is regenerated with
`uv lock && uv sync --all-extras` and committed with `pyproject.toml`; the
checked-in lock resolves the `>=3.8` floor and the optional `sftp` / `yaml` /
`hcl` / `dev` extras.

The only tag that exists in this checkout is `v0.2.0` (2026-05-21), whose own
message calls it the first public *scaffold* release. The tree is now `1.0.0`;
`v1.0.0` is tagged from the commit that contains this file. `ssh-core` carries the
standalone 1.5.0 feature set under the unified distribution version `1.0.0`
([ADR-004](docs/adr/ADR-004-ssh-core-1.5.0-sync.md)).

## [Unreleased]

Nothing yet.

## [1.0.0] - 2026-11-11

The first unified marketplace release. All eight plugins are versioned `1.0.0`;
`ssh-core` keeps the standalone 1.5.0 feature set but reports the marketplace
version. The `v1.0.0` tag is cut from this release commit.

### Added

- `docs/adr/ADR-001-ssh-execute.md`, `ADR-002-ssh-config-manager.md` and
  `ADR-003-sftp-transport.md` — recovered verbatim from
  `1a6eade^:.trellis/spec/backend/`; the blob was deleted in `1a6eade`.
- `docs/adr/ADR-006-python-38-floor.md` — reconstructed from
  `pyproject.toml`, the CI matrix and `tools/check_python_floor.py`; the
  historical ADR-006 text does not exist.
- `tools/check_ruff_baseline.py` + `tools/ruff-baseline.json` — a monotonic
  ratchet for the ruff backlog (380 findings, 24 rule codes at this release).
- `docs/adr/ADR-004-ssh-core-1.5.0-sync.md` — the 1.5.0 feature-sync decision,
  reconstructed from the deleted 1.4.0 ADR (`1a6eade^`) and the real sync diff
  (`d46dee9`).
- `docs/adr/ADR-005-ssh-core-boundary.md` — the "ssh-core is the only
  transport" decision, reconstructed from `4a766a5`, `1ead9e8`, the vendored
  locator and the test suite.
- `CLAIM-LEDGER.md` — file:line claim inventory with evidence type and status.
- `SECURITY.md` — supported versions, reporting, credential model and known
  limitations.
- `scripts/check_docs_links.py` — relative-link and heading-anchor gate, with
  `--require-index` for orphaned `docs/**/*.md` pages.
- `scripts/check_docs_parity.py` — EN/zh-CN heading, table-row, code-block and
  link-count parity.
- `scripts/gen_cli_reference.py` — generates
  `plugins/ssh-core/skills/ssh-core/references/cli-reference.md` from the real
  `argparse` parsers and `lib/exit_codes.py`; `--check --all` also verifies the
  documented script inventory of all eight plugins, and `--examples` runs the
  whitelisted help/version/local probes while annotating every skip with a
  reason (`--list` prints each one).
- `.markdownlint.jsonc` and the CI `docs` job: link/anchor, parity, generated
  CLI reference, example whitelist, markdownlint and Vale all run on every
  push.
- `.vale.ini` + `.vale/styles/config/vocabularies/devops/accept.txt` and the
  quoted `SKILL.md` frontmatter: Vale is 0 on all 53 markdown files instead of
  only the 15 files it could parse before.

### Changed

- **Version unification (breaking for version comparisons only):** every plugin
  moves to `1.0.0`, including `ssh-core` (was `1.5.0`). The standalone ssh-core
  history is retained in `lib/version.py` and ADR-004; the marketplace
  distribution identity is now one number.
- `uv.lock` regenerated with `uv lock` at `1.0.0` / `requires-python >=3.8`;
  the stale-lock note is removed from `CONTRIBUTING.md`.
- `ruff` backlog reduced from 444 to 380 with the safe `I001`/`UP*` fixes; the
  `lint` job now runs the ratchet instead of a permanently red `ruff check .`.
- Four environment-shaped test failures were replaced with deterministic
  assertions (`getaddrinfo` mocks, Windows path semantics, actual
  `per_source` key); `pytest tests/` is now `2009 passed, 151 skipped`.
- `README.md` / `README.zh-CN.md` state the release fact (all plugins `1.0.0`;
  existing tag `v0.2.0` is the scaffold) and link `docs/`, the ADRs, the
  changelog, the ledger and the security policy.
- `CONTRIBUTING.md` / `CONTRIBUTING.zh-CN.md` status wording matches the unified
  version; the zh-CN tree and authoring rules now match the English structure
  (docs/ scripts/ .github, doc links, rule 7).
- `docs/README.md` local-command table lists the docs gate and marks the
  currently red commands with their measured output.
- `docs/architecture.md` log-aggregator range corrected from `40-41` to
  `40-49` (`clock_skew_failed` 42, `normalize_failed` 43,
  `no_sources_resolved` 44; correlate 90-92).

### Fixed

- ADR-004/ADR-005 were cited from four documents but had no files; both now
  exist with per-section source commits.
- `docs/` was linked only from `CONTRIBUTING.md`; the root README now indexes it.
- zh-CN `CONTRIBUTING` had lost the English "docs examples must show shipped
  behaviour" rule and the `docs/` + `scripts/` tree lines; restored.
- `ssh-guarded/references/workdir-sandbox.md` contained a literal NUL byte in a
  table example; it is now the escaped text `\0`, so the file is plain UTF-8.
- CI portability: the test job runs on `ubuntu-22.04` (3.8 is absent from the
  ubuntu-26.04 image), ruff is pinned to `0.15.13` so the ratchet baseline is
  deterministic, the Vale step lets the shell expand its globs, and the
  `doctor --profile full` end-to-end tests now pick a genuinely unready plugin
  instead of assuming `k8s-debug` is degraded on every machine.
- `list_backups()` sorts by the backup filename (`epoch-ms-pid-counter`), not
  mtime: `copy2` preserves the source mtime, so equal-mtime backups had an
  arbitrary glob order and `restore latest` could restore the wrong generation.
- `live_k8s` tests are skipped unless selected with `pytest -m live_k8s`; they
  no longer try to provision a kind cluster on a runner that merely has
  docker/kind installed.

### Removed

- The unsupported claim that `v1.0` shipped all eight plugin surfaces.
- The hand-copied exit-code paragraph in `ssh-core/SKILL.md`; it now points at
  the generated `references/cli-reference.md`.
- The four permanently red tests and the permanently red `ruff check .` gate;
  both now have executable, reviewed contracts.

## [0.2.0] - 2026-05-21

### Added

- The first public scaffold tag: all eight plugins registered in
  `.claude-plugin/marketplace.json` with `SKILL.md`, `references/` and their
  initial CLI surfaces. The tag message is explicit that this is a scaffold
  release, not a V1 completion.

### Changed

- Repository version strings were aligned to `0.2.0` before tagging
  (`b942cc1`).

### Notes

- `ssh-core` was still `0.2.0` at this tag. Its later `1.4.0` and `1.5.0`
  standalone versions are the feature history preserved by ADR-004; the
  marketplace now distributes that feature set as `1.0.0`.

[Unreleased]: https://github.com/BerBai/devops-skills/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/BerBai/devops-skills/releases/tag/v1.0.0
[0.2.0]: https://github.com/BerBai/devops-skills/releases/tag/v0.2.0
