# ADR-005: ssh-core is the only transport; every other plugin shells out

- **Status:** Accepted (reconstructed record)
- **Reconstruction date:** 2026-11-09
- **Evidence commits:** `4a766a5`, `1ead9e8`, `1a6eade`, `d46dee9`
- **Related:** [ADR-004](./ADR-004-ssh-core-1.5.0-sync.md)

## Why this file is a reconstruction

`README.md`, `CONTRIBUTING.md`, `CONTRIBUTING.zh-CN.md` and
`docs/architecture.md` all cite ADR-005, but no ADR-005 text was ever tracked.
`git log --all -S 'ADR-005' -- .` returns a single commit, `1ead9e8`, which adds
only the **references** to it. There is no earlier decision document to recover.

What follows is derived from the code and tests that the decision produced, not
from an invented meeting record. Every load-bearing statement points at a file
and line in this checkout or at a commit that introduced it.

## Provenance (section → source)

| Section | Source |
|---|---|
| "Why this file is a reconstruction" | `git log --all -S 'ADR-005' -- .` → only `1ead9e8` |
| Context | `4a766a5` subject/stat; `docs/architecture.md:29-31` |
| Decision D1/D2 | `tests/test_diagnose_host_unit.py:9-10`; `docs/architecture.md:33-45` |
| Decision D3/D4 | `scripts/templates/_ssh_core_locator.py:1-28`; `scripts/sync_vendored.py`; manifest tests |
| Decision D5 | `ssh-guarded/scripts/lib/request_artifact.py:36`; `exec_detached.py:63` |
| Decision D6 | `tests/test_diagnose_host_unit.py:111-133` (`ssh_execute_missing`) |
| Evidence table | per-row file/line and commit in the table itself |
| Consequences / Alternatives | `docs/offline-install.md`; `pyproject.toml` (no `[build-system]`) |
| Verification | commands in the block at the end of this file |

## Context

The marketplace ships eight plugins. Seven of them (`ssh-guarded`,
`remote-debug`, `k8s-debug`, `docker-quick`, `log-aggregator`, `iac-state`,
`cicd-debug`) need to reach a remote host, but each is distributed
independently: a user may install one plugin without the others, and the
marketplace copies only `plugins/<name>/skills/<name>/`.

The first diagnostic implementation, `diagnose_host`, shipped in `4a766a5`
with the subject "ship `diagnose_host` v1.0 with shell-out to ssh-core". That
commit established the pattern: locate `ssh_execute.py` on disk, invoke it with
an argv list, parse the JSON envelope. It did not import `ssh_core`'s Python.

## Decision

**D1. `ssh-core` owns transport.** SSH authentication, sessions, tunnels,
transfers, sudo, retries and `~/.ssh/config` CRUD live in
`plugins/ssh-core/skills/ssh-core/`. No other plugin re-implements them.
`README.md:40` states the rule; `docs/architecture.md:29` repeats it in the
layer table.

**D2. The only cross-plugin contract is `argv + JSON` on stdout.** A diagnostic
script invokes `ssh_execute.py <host> <command> --json` as a subprocess and
reads `{success, exit_code, stdout, stderr, data}`. `docs/architecture.md:35-45`
is the normative description; `tests/test_diagnose_host_unit.py:9-10` states the
test invariant ("remote-debug invokes ssh-core via subprocess and never via a
Python import"), and its mocked `subprocess.run` stand-in asserts the argv.

**D3. No plugin imports another plugin's Python.** Plugins are separate
distribution units; a cross-plugin import would break a single-plugin install.
The six diagnostic plugins and `ssh-guarded` carry a vendored
`_ssh_core_locator.py` whose module docstring says: "Every diagnostic plugin in
this marketplace shells out to ssh-core's `ssh_execute.py` ... none of them
import its Python." The locator resolves a **path**, never a module
(`scripts/templates/_ssh_core_locator.py:1-28`).

**D4. One locator per plugin, generated from one template.** The resolution
order is `$DEVOPS_SSH_CORE_SCRIPTS`, `$DEVOPS_SSH_CORE_DIR`, the repo layout,
then agent install roots (`~/.claude/plugins/`, `~/.dsh/skills/`,
`~/.agents/skills/`, `~/.codex/skills/`). The copies are rendered by
`scripts/sync_vendored.py` and byte-compared in
`tests/test_manifests.py::test_skill_copies_are_identical` and
`::test_vendored_locator_matches_template`.

**D5. `ssh-guarded` is not an exception.** It adds drafting, redaction, sandbox
and detached-job state on top of transport; its drafters and executor reach
`ssh-core` through the same locator and CLI. `docs/architecture.md:30` and
`ssh-guarded/SKILL.md:10-14` describe the layering
(`user intent -> ssh-guarded -> ssh-core`).

**D6. A failed lookup is a classified failure, not an exception.** The locator
raises a clear error that callers translate into `dependency_missing` or a
plugin-specific class; `tests/test_diagnose_host_unit.py` asserts that a missing
`ssh_execute.py` produces a JSON envelope (`ssh_execute_missing`) instead of a
traceback.

## Evidence

| Claim | Evidence |
|---|---|
| `diagnose_host` shells out | commit `4a766a5`; `tests/test_diagnose_host_unit.py:88-133` |
| No Python import | `tests/test_diagnose_host_unit.py:9-10`; locator docstring |
| Locator is path-only | `scripts/templates/_ssh_core_locator.py:1-28`, `:44-60` |
| Seven copies, one template | `scripts/sync_vendored.py:VENDORED`; `sync_vendored.py --check` reports 14 copies |
| The envelope is shared | `docs/architecture.md:69-87`; `lib/exit_codes.py` for the failure classes |
| `ssh-guarded` uses the same boundary | `ssh-guarded/scripts/lib/request_artifact.py:36`; `exec_detached.py:63` |
| The rule is user-facing | `README.md:40`; `docs/architecture.md:33-38`; both added in `1ead9e8` |

## Consequences

- A user can install `ssh-core` plus one diagnostic plugin and still get a
  working tool; the diagnostic plugin finds ssh-core through the locator or the
  two environment variables.
- Any change to the envelope is a cross-plugin breaking change. That is why the
  envelope and exit-code table are tested as contracts, not as implementation
  details.
- The locator must be kept byte-identical across seven copies; hand-editing one
  copy is the failure mode `sync_vendored.py` exists to prevent.
- Diagnostics cannot share in-process state with ssh-core. That is a deliberate
  cost: no connection pool is shared, so `ssh_daemon.py` / `--commands-file`
  are the supported ways to amortise handshakes, not an in-process client.

## Alternatives considered

- **Import `ssh_core.lib` from diagnostic plugins.** Rejected: it makes a
  single-plugin install fail at import time and couples every plugin to
  ssh-core's internal module layout.
- **Duplicate a minimal SSH wrapper in each plugin.** Rejected: seven copies of
  auth, host-key and redaction logic would drift; the 1.4.0/1.5.0 sync problem
  would be multiplied by seven.
- **A shared install-time package.** Rejected: the marketplace copies plugin
  directories; there is no build step or shared site-packages location to rely
  on (see `docs/offline-install.md` and the absent `[build-system]` in
  `pyproject.toml`).

## Verification

```text
$ .venv/bin/python scripts/sync_vendored.py --check
sync_vendored: 14 copies match their templates

$ .venv/bin/python -m pytest tests/test_manifests.py -q
128 passed

$ .venv/bin/python plugins/remote-debug/skills/remote-debug/scripts/diagnose_host.py pai --json
{"success": true, "exit_code": 0, "stdout": "diagnose_host pai (linux): severity=ok, 5/5 probes ok\n", ...}
```
