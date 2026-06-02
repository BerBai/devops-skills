# Plan ssh-core connection reuse V1.1

## Goal

Ship the first safe V1.1 connection-reuse slice for `ssh-core` by adding opt-in OpenSSH ControlMaster support to `ssh_execute.py`, while keeping V1 default behavior unchanged.

This child owns the first implementation slice. It does not implement a custom Python `ssh_daemon.py` protocol.

## Confirmed Facts

- ADR-001 accepted V1 native OpenSSH subprocess execution and explicitly deferred connection reuse to V1.1.
- ADR-001 A2 says V1.1 should evaluate OpenSSH ControlMaster before considering a custom Python daemon.
- `ssh_daemon.py` currently documents the old custom daemon idea and returns `unimplemented(...)`; no shipped V1 code calls it.
- `ssh_execute.py` is the front door used by downstream plugins for remote commands.
- V1 `ssh_execute.py` currently builds argv as:
  - `ssh`
  - `-o BatchMode=yes`
  - `-o ConnectTimeout=N`
  - optional `-F <config>`
  - `<host> <command>`
- Existing tests assert argv list semantics, BatchMode/ConnectTimeout flags, timeout behavior, JSON envelope shape, and local-route shell-metachar refusal.

## Requirements

- Add opt-in ControlMaster support to `ssh_execute.py` without changing default V1 behavior.
- Preserve the existing JSON envelope and failure-class contract.
- Preserve argv-list subprocess execution and `MSYS_NO_PATHCONV` behavior.
- Avoid a custom daemon implementation in this slice.
- Make the generated ControlPath deterministic, private to the current user/tempdir, and safe for aliases/config paths with punctuation.
- Expose reproduction data in `data.ssh_argv` as before.
- Add local unit tests for argv construction and option defaults; do not require live SSH infrastructure for acceptance.
- Update ssh-core docs to describe the V1.1 opt-in ControlMaster path and keep `ssh_daemon.py` deferred unless ControlMaster proves insufficient.

## Proposed CLI Surface

- `--control-master {off,auto}` default `off`
- `--control-persist SECONDS` default `600`
- `--control-path-dir PATH` optional override for tests/debugging

When `--control-master auto` is passed on a remote route, append:

- `-o ControlMaster=auto`
- `-o ControlPersist=<seconds>`
- `-o ControlPath=<dir>/<digest>`

Local route ignores these flags.

## Acceptance Criteria

- [x] `ssh_execute.py --help` lists the new V1.1 ControlMaster flags.
- [x] Default remote argv is unchanged when no new flag is passed.
- [x] `--control-master auto` adds `ControlMaster`, `ControlPersist`, and deterministic `ControlPath` options.
- [x] ControlPath derivation is covered by unit tests and avoids leaking raw aliases in filenames.
- [x] Existing `ssh_execute` unit and integration tests remain skip-safe.
- [x] ssh-core SKILL/reference docs are updated to describe the opt-in path.
- [x] `uv run pytest tests/test_ssh_execute_unit.py tests/test_manifests.py` passes.

## Validation Results

- `uv run pytest tests/test_ssh_execute_unit.py tests/test_manifests.py` → `96 passed`
- `uv run ruff check plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py tests/test_ssh_execute_unit.py` → passed
- `MYPYPATH=plugins/ssh-core/skills/ssh-core/scripts uv run mypy plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py` → passed

## Out of Scope

- Default-on connection reuse.
- Password or keyboard-interactive auth.
- Custom daemon socket protocol.
- Applying ControlMaster to every ssh-core script in this slice.
- `ssh_server_transfer.py` direct/auto/hybrid and `ssh_tunnel.py --idle-timeout`; those become follow-up tasks.

## Notes

- Parent task: `06-02-audit-v1-completion-plan-v11`.
