# Promote ADR-001 to spec

## Goal

Lock the ssh_execute v1.0 decisions from PoC into shared spec so the
upcoming v1.0 implementation task can cite them as authoritative. Also
add the new `exit_code=124` for timeout to `error-handling.md` so the
"don't invent new exit codes" rule explicitly allows the addition.

## Background

The PoC (`.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/`)
produced `adr-001-ssh-execute.md` as a draft. Status is "Proposed" and
the bottom of the file lists "Next steps" that include this promotion.

Today the draft lives only inside the archived task directory — which is
gitignored, not loaded by `trellis-implement` / `trellis-check`, and not
visible to anyone else cloning the repo. To drive v1.0, the decisions
must live in `.trellis/spec/backend/` where the per-task `implement.jsonl`
manifests can point at them.

A secondary issue surfaced in the PoC: `error-handling.md:35-48` currently
lists only `{0, 1, 2}` as allowed exit codes and tells callers "Do not
invent new exit codes". The PoC adopted `124` for timeout (matching GNU
`timeout(1)`). The spec needs to be widened to allow this, or v1.0 will
ship contradicting documented rules.

## Requirements

### R1 — Promote the ADR

Copy `adr-001-ssh-execute.md` from the archived PoC task into
`.trellis/spec/backend/adr-001-ssh-execute.md` with the following edits:

- **Status**: change "Proposed (pending main-session review …)" to
  "Accepted (2026-05-21)".
- **Source evidence** line: update relative paths from
  `findings.md`, `questions.md`, `spike-code/...` to the archived
  location: `.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/...`
  so a future reader can follow the trail.
- **Next steps section**: trim or remove (the next steps are this task).
- No other content changes. The decisions are accepted as-is.

### R2 — Register the ADR in the spec index

Edit `.trellis/spec/backend/index.md` to add a row referencing the new
ADR. Suggested:

| Decision | Description | Status |
|---|---|---|
| [ADR-001 ssh_execute](./adr-001-ssh-execute.md) | v1.0 implementation contract: subprocess+native ssh, JSON envelope, failure classes, local route policy | Accepted 2026-05-21 |

Place the new section between the existing "Guidelines Index" and "How to
Fill These Guidelines" sections, under a new `## Architecture Decisions`
heading.

### R3 — Extend allowed exit codes in error-handling.md

Edit `.trellis/spec/backend/error-handling.md`:

- Add a 4th row to the exit-code table (currently at lines 36-42):
  - `124` → "Python-side timeout enforcement triggered before the underlying command could complete. Matches GNU `timeout(1)` convention."
- Update the line that says "Do not invent new exit codes (3, 4, …)" to:
  "Do not invent new exit codes (3, 4, …) beyond the four listed above (0/1/2/124). New codes require a spec update and a referenced ADR."
- Leave the rest of the file unchanged.

## Constraints

- **Pure markdown changes.** No Python touched. No CONTRIBUTING.md changes
  (the roadmap doesn't reference exit codes by number).
- **No commit to main.** `.trellis/` is gitignored; this whole task lives
  in untracked files. No `git add`, no `git commit`, no `git push`. The
  task wrap-up is `task.py finish` + `task.py archive`, nothing else.
- **Do not modify the archived ADR.** The archived copy is a historical
  artifact of the PoC; the spec copy is the new working copy.
- **Do not touch any other spec file** (database-guidelines, directory-structure,
  logging-guidelines, quality-guidelines). They are out of scope.

## Acceptance Criteria

- [ ] `.trellis/spec/backend/adr-001-ssh-execute.md` exists, with Status
      line reading "Accepted (2026-05-21)" and source-evidence paths
      pointing at the archived PoC.
- [ ] `.trellis/spec/backend/index.md` contains an "Architecture Decisions"
      section with a link to `adr-001-ssh-execute.md`.
- [ ] `.trellis/spec/backend/error-handling.md` exit-code table contains a
      `124` row referencing GNU `timeout(1)`; the "Do not invent" sentence
      explicitly whitelists the 4 codes.
- [ ] `grep -n "exit_code=124\|exit 124\|124" .trellis/spec/backend/error-handling.md`
      shows at least 2 hits (table row + the prose line).
- [ ] `git status --short` shows **no changes** (everything edited is under
      gitignored `.trellis/`). If `git status` shows tracked-file changes,
      something out of scope was edited — back it out.
- [ ] Original archived ADR file at
      `.trellis/tasks/archive/2026-05/05-21-02-poc-ssh-execute/adr-001-ssh-execute.md`
      is byte-identical to before this task (validate by sha256sum before/after).

## Out of scope

- The v1.0 implementation of `ssh_execute.py` itself — separate task,
  unblocked by this one.
- Updating `CONTRIBUTING.md` roadmap (the line "daemon-aware front door"
  is now more precise after the PoC, but rewording it touches public
  contributor docs and is a separate decision).
- Reviewing or revising any of the 10 follow-up gaps listed in
  `findings.md` — those become inputs to v1.0 task PRD, not this task.
- Promoting the spike code into shared `lib/` — the spike code stays as
  reference inside the archived PoC; v1.0 ssh_execute will be written
  fresh against the ADR.

## Notes

This is a lightweight chore task. PRD-only is sufficient; no design.md or
implement.md needed. The work is approximately:

1. Read archived ADR; copy to spec dir; edit 2-3 lines.
2. Append section to `index.md`.
3. Edit two locations in `error-handling.md`.
4. Verify with the grep + git status acceptance checks.

Estimated 15-30 minutes. Main session can do this inline rather than
dispatching `trellis-implement`.
