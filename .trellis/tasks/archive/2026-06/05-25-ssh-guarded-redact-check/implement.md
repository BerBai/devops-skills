# Implementation — ssh-guarded redact_check v1.0

## Phase 0 — Pre-flight

- [x] Re-read `prd.md` and `design.md`.
- [x] Verify Child A delivered `plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/redact.py`.
- [x] Record API drift: current `Redactor.apply(text)` returns aggregate hits
      (`kind`, `count`, `replace_with`), not PRD's per-match `line/match_len`.
      This child will keep `lib/redact.py` stable and compute strict
      `redact_check` hit details inside the CLI using the redactor's compiled
      catalog.

## Phase 1 — CLI foundation

- [ ] Rewrite `scripts/redact_check.py` from scaffold to v1.0.
- [ ] Preserve CLI surface:
      `--catalog`, `--json`, `--emit-redacted`, `--report-only`; no positional args.
- [ ] Use `DEFAULT_USER_CATALOG = Path.home() / ".devops" / "redact.json"`.
- [ ] Add exit map:
      `70 catalog_load_failed`, `71 catalog_schema_invalid`,
      `72 regex_compile_failed`, `1 generic`.

## Phase 2 — Strict catalog loading for this CLI

- [ ] Explicit `--catalog PATH`:
      missing/unreadable file → exit 70; valid file replaces built-in.
- [ ] No `--catalog`: use `~/.devops/redact.json` if it exists; otherwise built-in.
- [ ] Validate override schema strictly:
      top object, `version == 1`, `patterns` list, each entry has
      `kind`, `regex`, `replace_with` strings.
- [ ] Compile every regex up front; compile error → exit 72 with
      `offending_kind` in JSON `data`.
- [ ] Built-in path uses `Redactor(None)` so it stays aligned with request-pipeline.

## Phase 3 — Redaction and hit details

- [ ] Read stdin as text and preserve newlines.
- [ ] For each input line, use compiled patterns in catalog order:
      record one hit per regex match with `kind`, `line`, `match_len`;
      redact via sequential `sub`.
- [ ] For built-in catalog, use the same compiled patterns from `Redactor(None)`;
      do not duplicate `_DEFAULT_PATTERNS` literals in `redact_check.py`.
- [ ] Summary counts are grouped by kind and sorted by kind for stable stderr.

## Phase 4 — Output contracts

- [ ] Human default: redacted text to stdout unless `--report-only`; summary to stderr:
      `redact_check: N hits (kind=X, ...)`.
- [ ] Human errors: write one stderr line containing `failure_class=<class>`.
- [ ] JSON success:
      `{success, exit_code, stdout, stderr, data}` with
      `data.hits`, `data.total_hits`, `data.redacted_text_len`,
      and optional `data.redacted_text` only when `--emit-redacted`.
- [ ] JSON errors use the same envelope and include `data.failure_class`.

## Phase 5 — Tests

- [ ] Add `tests/test_redact_check_unit.py`.
- [ ] Cover help surface and all four flags.
- [ ] Cover built-in patterns that exist in current `lib/redact.py`:
      `aws_access_key`, `github_token`, `jwt`, `rsa_private`.
- [ ] Cover catalog override replace semantics, missing file, bad JSON,
      bad schema, bad regex/offending kind.
- [ ] Cover default human output, `--json`, `--json --emit-redacted`,
      `--report-only`, multiline line numbers, empty stdin, no hits.
- [ ] Cover subprocess binary input generic failure if feasible; otherwise
      document why Python text-mode stdin makes it non-deterministic.

## Phase 6 — Quality gate

- [ ] `uv run ruff check plugins/ssh-guarded/ tests/test_redact_check_unit.py`
- [ ] `uv run mypy plugins/ssh-guarded/skills/ssh-guarded/scripts/redact_check.py`
- [ ] `uv run pytest tests/test_redact_check_unit.py -q`
- [ ] `uv run pytest tests/ -q`

## Phase 7 — Commit / archive

- [ ] Commit only:
      `plugins/ssh-guarded/skills/ssh-guarded/scripts/redact_check.py`,
      `tests/test_redact_check_unit.py`, and any necessary task/spec notes.
- [ ] Archive `05-25-ssh-guarded-redact-check`.
- [ ] Update rollout parent notes with commit hash and validation summary.

## Rollback points

- If using `Redactor._compiled` proves unstable, add a public
  `iter_matches` helper to `lib/redact.py` and update existing lib tests in the
  same change.
- If built-in pattern names differ from old PRD names, prefer current shipped
  `lib/redact.py` names and record the drift in task notes.
