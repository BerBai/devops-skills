# Quality Guidelines

> What every change must pass before review.

---

## Test entrypoint

```bash
pytest tests/
```

CONTRIBUTING.md line 100 and `pyproject.toml` lines 42-44 define the
single test entrypoint. There are no other suites; `tests/test_manifests.py`
is the load-bearing file.

---

## What `tests/test_manifests.py` enforces

These tests will fail CI for any change that breaks the marketplace surface:

| Test | What it checks | Lines |
|------|----------------|-------|
| `test_marketplace_json_is_valid` | `.claude-plugin/marketplace.json` parses, has ≥3 plugins, contains `ssh-core/ssh-guarded/remote-debug`. | 28-36 |
| `test_plugin_json_exists_and_parses` | Per-plugin `plugin.json` exists, `name` matches, `description` is `>= 40 chars`. | 40-46 |
| `test_skill_md_has_frontmatter` | `SKILL.md` starts with `---\n`, frontmatter contains `name:` and `description:`. | 50-56 |
| `test_references_directory_populated` | `references/` exists and has at least one `*.md`. | 60-64 |
| `test_marketplace_plugin_sources_resolve` | Every `source` in marketplace.json points to an actual directory with `plugin.json`. | 67-73 |
| `test_script_help` | Every script under `scripts/*.py` exits `0` on `--help` and prints a `usage:` line. | 85-94 |

If you add a plugin, all of the above start applying automatically — the
plugin list is auto-discovered from `marketplace.json` (line 25).

---

## Lint

```bash
ruff check .
```

`pyproject.toml` lines 34-40:

```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "PL"]
ignore = ["E501", "PLR0913"]
```

Notable: `E501` (line-too-long) is ignored — `line-length=100` is a hint
to the formatter, not a hard wall. `PLR0913` (too-many-arguments) is
ignored because CLI parsers naturally take many.

---

## Type check

`mypy` is declared as a dev dependency (`pyproject.toml` line 27). Run it
locally before sending a change; CI does not yet gate on it but will once
the scaffolds are filled in.

---

## Skill discoverability (review-blocking)

CONTRIBUTING.md line 34: the `description` field — in BOTH `plugin.json`
and the `SKILL.md` YAML frontmatter — is what Claude Code matches against
the user's prompt. Review must verify:

- Lead with verbs and concrete keywords.
- No generic words like "tool" or "helper" (those match everything and so
  match nothing usefully).
- Multilingual triggers welcome (English + 中文 are both in `ssh-core`'s
  description, see `plugins/ssh-core/skills/ssh-core/SKILL.md` line 3).
- `description` is `>= 40 chars` (the test enforces; the spirit is "be
  specific enough that the test would never have been needed").

---

## Code review checklist

Before approving any change to a script or skill, verify:

1. **JSON output contract** — script supports `--json` and emits
   `{"success", "exit_code", "stdout", "stderr", "data"}`. See
   `error-handling.md`.
2. **No shell strings** — `subprocess.run(list, ...)`, never `shell=True`.
3. **No emoji** — not in code, not in `SKILL.md`, not in `references/*.md`.
4. **No secrets in output** — keys, passwords, `IdentityFile` paths must
   never reach stdout/stderr. See `logging-guidelines.md`.
5. **State files** — if the change writes persistent state, it goes under
   `reports/{requests,jobs}/` or a `*.local.json`, and is covered by
   `.gitignore`. See `database-guidelines.md`.
6. **Cross-plugin contract** — if the change touches another plugin's
   behavior, it goes through CLI shell-out, not a Python import
   (CONTRIBUTING.md line 29).
7. **Tests pass** — `pytest tests/` is green, and any new script gets
   auto-included in `test_script_help`.
8. **Docs examples only show shipped behavior** — `SKILL.md` and
   `references/*.md` must not present deferred or future-version flags as
   positive examples. If a flag prints a "deferred to vX.Y" stderr notice
   at runtime (e.g. `tail_log.py --follow` as of 2026-05-24, see
   `plugins/remote-debug/skills/remote-debug/scripts/tail_log.py:65`
   `FOLLOW_DEFERRED_MSG`), it must not appear in docs without an explicit
   "not yet implemented" annotation. Same rule for paths/files that
   don't exist on supported distros — annotate or replace. See Finding #3
   in `.trellis/tasks/05-24-e2e-validate-skills-on-pai/validation-report.md`
   for the concrete failure: `/var/log/syslog` example crashed on
   Debian 13 (journald-only).

---

## What NOT to do

- Do NOT add a script that does not accept `--help` — the test will fail.
- Do NOT skip `--json` "because the output is simple" — Claude Code calls
  every script with `--json` when chaining.
- Do NOT add new test files just to assert plugin presence; the parametric
  test in `test_manifests.py` already covers every plugin in
  `marketplace.json`.
