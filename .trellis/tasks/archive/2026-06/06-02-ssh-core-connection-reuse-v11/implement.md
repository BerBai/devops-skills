# Implementation Plan — ssh-core connection reuse V1.1

## Steps

1. Add parser flags to `ssh_execute.py`:
   - `--control-master {off,auto}` default `off`
   - `--control-persist` int default `600`
   - `--control-path-dir` optional path
2. Extract remote argv construction into a helper that can be unit-tested without running ssh.
3. Add ControlMaster argv construction when mode is `auto`.
4. Add unit tests:
   - default remote argv unchanged
   - `--control-master auto` adds `ControlMaster=auto`, `ControlPersist=...`, `ControlPath=...`
   - ControlPath uses a hash, not raw alias text
   - `--help` lists new flags
5. Update ssh-core docs:
   - `SKILL.md` daemon model / quick reference
   - `references/workflows.md`
   - `references/troubleshooting.md`
6. Validate:
   - `uv run pytest tests/test_ssh_execute_unit.py tests/test_manifests.py`
   - touched-file ruff for changed Python test/code files

## Rollback

Revert changes to `ssh_execute.py`, `tests/test_ssh_execute_unit.py`, and ssh-core docs. No persistent state migration is introduced.

## Follow-Up Candidates

- Apply the same ControlMaster helper to `ssh_cluster.py`.
- Let `ssh_server_transfer.py` direct/auto/hybrid use the ControlMaster decision.
- Revisit `ssh_tunnel.py --idle-timeout` after connection reuse lands.
- Reassess whether `ssh_daemon.py` should remain a stub, become a ControlMaster management wrapper, or be deleted from the public surface.
