# Polish v0.2.0 scaffold WARNs

## Goal

Clean up two scaffold-era inconsistencies surfaced by the v0.2.0 functional
review: hardcoded version strings stuck at `v0.1.0` in 17 places, and dev
dependencies missing from the project venv so `pytest tests/` fails out of
the box for new contributors.

## Background

Source: review of plugin completeness, 2026-05-20 session.

- Marketplace and README already advertise v0.2.0:
  - `.claude-plugin/marketplace.json` `"version": "0.2.0"`
  - `README.md:48` — "v0.2.0 — scaffold stage covering all 8 plugins"
- All 8 per-plugin manifests still say `0.1.0`:
  - `plugins/*/.claude-plugin/plugin.json` `"version": "0.1.0"` (8 files)
- Stub output strings hardcode `v0.1.0 scaffolding`:
  - `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py:79,83` — the
    shared `unimplemented()` helper, called by all 9 ssh-core scripts.
  - `plugins/<plugin>/skills/<plugin>/scripts/*.py` — each non-ssh-core
    plugin defines its own `UNIMPLEMENTED_NOTE` constant. Counted 28 such
    constants across 7 plugins (cicd-debug, docker-quick, iac-state,
    k8s-debug, log-aggregator, remote-debug, ssh-guarded).
- `pyproject.toml:27` declares pytest as a dev dep but the project `.venv`
  is missing it; `.venv/bin/python -m pytest tests/` returns "No module
  named pytest". `CONTRIBUTING.md:100` lists `pytest tests/` as the single
  test entrypoint with no setup instruction, so new contributors hit this
  wall immediately.

## Requirements

### R1 — Sync plugin versions to 0.2.0

Bump `"version": "0.1.0"` → `"version": "0.2.0"` in all 8 files under
`plugins/*/.claude-plugin/plugin.json`.

### R2 — Update stub output strings

Replace the literal string `v0.1.0 scaffolding` with `v0.2.0 scaffolding`
in every occurrence under `plugins/`. Two patterns exist; treat them
uniformly:

- `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py` — the
  `unimplemented()` helper's docstring (line 79) and stderr message
  (line 83).
- Every `UNIMPLEMENTED_NOTE = "<plugin>: '<cmd>' is not implemented in
  v0.1.0 scaffolding."` constant across the 7 non-ssh-core plugins.

### R3 — Document venv bootstrap

Add a `Setup` section to `CONTRIBUTING.md` immediately above the existing
`Testing` section (line 97 area). Document the install command that makes
`pytest tests/` work. Use `uv sync --all-extras` to match how `.venv` was
created (uv-managed Python at `.venv/bin/python -> uv/.../python3.11`).

## Constraints

- **String-only changes.** No argparse signatures, return paths, or JSON
  contract output may change. The scripts must still emit `exit_code=2`
  and still be detected as unimplemented by their callers.
- **No new files** except possibly editing `CONTRIBUTING.md`. No new
  packages, no version bump scripts, no CI workflow additions.
- **Do not touch** review INFO items (`scripts/__init__.py` dead code,
  `directory-structure.md:62` wording, roadmap count drift) — they are
  intentionally out of scope.
- **Do not touch** WARN 3 (two-stub-pattern architectural observation) —
  the duplication disappears when v1.0 lands.

## Acceptance Criteria

- [ ] `grep -rn "v0\.1\.0" plugins/` returns **0** hits.
- [ ] `python3 -c "import json,glob;
      [print(f, json.load(open(f))['version']) for f in
      glob.glob('plugins/*/.claude-plugin/plugin.json')]"` prints `0.2.0`
      for all 8 plugins.
- [ ] `grep -n "v0\.2\.0 scaffolding" plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py`
      matches 2 lines (docstring + stderr message).
- [ ] `grep -rn 'UNIMPLEMENTED_NOTE.*v0\.2\.0 scaffolding' plugins/` returns
      exactly 28 hits (one per constant).
- [ ] `CONTRIBUTING.md` contains an explicit dev-install command
      (`uv sync --all-extras`) in a `Setup` section above `## Testing`.
- [ ] After running the documented install command, `python3 -m pytest tests/`
      exits green; existing `test_manifests.py` checks all pass.
- [ ] `git diff --stat` shows changes only in `plugins/*/.claude-plugin/plugin.json`,
      `plugins/*/skills/*/scripts/*.py` (string lines only), and
      `CONTRIBUTING.md`. No `.py` line containing `def`, `return`, or
      `argparse` changed.

## Out of scope

- The architectural observation that two stub patterns coexist (review
  WARN 3) — duplication evaporates when v1.0 implementation starts.
- Cleaning up `scripts/__init__.py` dead files (review INFO 4).
- Rewording `.trellis/spec/backend/directory-structure.md:62` to better
  reflect `lib/__init__.py` maturity (review INFO 5).
- Reconciling `CONTRIBUTING.md` roadmap counts with actual script counts
  (review INFO 6).
