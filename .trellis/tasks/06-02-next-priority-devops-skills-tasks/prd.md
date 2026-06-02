# Prioritize and implement next devops skills tasks

## Goal

Identify the next highest-priority unfinished devops-skills work from repository evidence, split it into independently verifiable deliverables, and implement the first safe deliverable.

This parent task owns prioritization and task mapping. Child tasks own implementation.

## Confirmed Facts

- Trellis has no active or assigned tasks before this work.
- The v1.0 rollout parent task is archived and marked complete.
- `CONTRIBUTING.md` still shows a broad v1.0 checklist, but the checklist is stale: many listed scripts already have implementations and tests.
- `tests/README.md` lists the current missing v1.0 roadmap items:
  - Sandbox-escape attempts on `ssh-guarded` beyond the two existing refusal cases.
  - Redact-catalog regression suite for `redact_check.py`.
  - `scan_software.py` live tests.
- The first child selected is `06-02-redact-catalog-regression-coverage` because it is local, deterministic, and does not require a remote host.

## Requirements

- Prioritize backlog candidates using repository evidence rather than stale checklist text alone.
- Prefer tasks that can be validated locally before environment-dependent live tests.
- Split independently verifiable deliverables into child tasks.
- Keep parent scope limited to prioritization, child tracking, and final integration review.
- Do not modify unrelated user changes in the working tree.

## Acceptance Criteria

- [x] At least one child task is created for the highest-priority deliverable.
- [x] The selected child has testable requirements and acceptance criteria.
- [x] The selected child is implemented and checked, or blocked with a concrete blocker.
- [x] Remaining backlog candidates are documented for follow-up ordering.

## Child Task Map

- `06-02-redact-catalog-regression-coverage` — add local regression coverage for `redact_check.py` catalog behavior.
- `06-02-ssh-guarded-sandbox-escape-regression-coverage` — add local regression coverage for documented workdir sandbox escape attempts.

## Follow-Up Candidates

- Add skip-safe live tests for `scan_software.py`.

## Notes

- Source evidence: `tests/README.md` "What's missing (v1.0 roadmap)".
