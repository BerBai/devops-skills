# tests/

Cheap, fast tests that catch the things which would break `/plugin marketplace add` before any user sees the skill.

## Run (default — no network, no remote hosts)

This repository is a marketplace of plugins, not an installable package: there
is deliberately no `[build-system]`, so `pip install -e ".[dev]"` cannot work
and is not the supported path. Install the test tools directly:

```bash
python -m pip install pytest
pytest tests/
```

Extra tools are optional and only needed by the paths that use them; a missing
one is a supported state, and the script that needs it returns
`dependency_missing` (exit 3) rather than a traceback:

```bash
python -m pip install paramiko   # SFTP backend for ssh_upload / ssh_download / ssh_server_transfer
python -m pip install pyyaml     # cicd-debug/secret_scope_audit.py
python -m pip install python-hcl2  # iac-state/module_validate.py (needs Python >= 3.9)
```

`uv sync --extra sftp --extra yaml --extra hcl` creates the same environment
from the declared extras.

This runs everything **except** tests marked `live_ssh`. CI runs this
suite. It should always pass on a laptop with no SSH setup.

### Windows

The suite runs on Windows; `PYTHONUTF8=1` (or `python -X utf8`) is required
because several reference documents and test fixtures are UTF-8 while the
default Windows code page is not. Cases that genuinely need a POSIX shell
(`setsid`, POSIX permission bits, `/tmp` paths) are marked `needs_posix` and
skip themselves.

## Run (live_ssh — integration against a real host)

Some tests in this tree are marked `live_ssh` and exercise the real
`ssh_execute.py` / `diagnose_host.py` CLIs against a remote host. They
are skipped by default and never run in CI; you opt in locally when
landing changes that touch the ssh transport or diagnostic shell-out
path.

```bash
POC_HOST=lab-240 pytest tests/ -m live_ssh -v
```

### Prerequisites

- An SSH alias the tests can reach. It comes from `POC_HOST`; there is no
  default host committed to the repo, and when `POC_HOST` is unset the whole
  group skips with a message naming the variable. (Older revisions defaulted to
  `pai`, which only existed on one developer's laptop.)
- Credentials for that alias, **either** of:
  - a usable `IdentityFile` in `~/.ssh/config` for the alias, or
  - a private key loaded in `ssh-agent` (`ssh-add -l` lists it).

  `ssh-agent` is not required; on Windows it is usually not running at all.
  A password-only host works too — export `SSH_PASSWORD` and mark the alias
  `# auth: password` in `~/.ssh/config`.
- The remote host runs Linux with standard utils (`uptime`, `df`,
  `free`, `ps`, `cat /proc/loadavg`, `tar`). Most diagnostic probes assume
  these are on `$PATH`.
- Network reachability from the test machine to the host (so DNS,
  routing, and the SSH port are open). Reachability is probed, not inferred
  from a hardcoded subnet.

### Skip reasons and what they mean

Every live module uses the shared guards in `tests/conftest.py`; a skip always
names the capability it could not find:

| Skip reason | Means | Remedy |
|---|---|---|
| `POC_HOST is not set` | No live host configured | `POC_HOST=<alias> pytest -m live_ssh` |
| `host '<alias>' not reachable` | ssh to the alias failed | check `~/.ssh/config`, network |
| `no usable credential for '<alias>'` | Neither key file nor agent identity worked | `ssh-add`, or set `IdentityFile` |
| `paramiko is not installed` (`sftp_only`) | SFTP-only assertions | `pip install paramiko`; the scp/tar fallback is covered by separate cases |
| `needs a POSIX shell` (`needs_posix`) | `setsid`, mode bits, `/tmp` | run on Linux/macOS |
| `the scratch ssh config ... has no \`Host <name>\` stanza` | A case needs a hand-written stanza (`poc-via-agent`, `poc-noauth`) | add one to `$TMPDIR/poc-ssh-config`; see below |
| `kind binary not installed` (`live_k8s`) | No Kubernetes fixture | `docker`: see `tests/fixtures/backends/` |

The scratch SSH config (`POC_CONFIG`, once a hardcoded `/tmp/poc-ssh-config`)
is **not** generated: the two cases that need it require stanzas no generator
could invent — `Host poc-via-agent` must have *no* `IdentityFile` (so only the
agent identity can work) and `Host poc-noauth` must pin `IdentityFile
/dev/null` + `IdentitiesOnly yes` (so the publickey offer is denied on
purpose). Fabricating a plausible-looking block made those cases fail instead of
skipping. Write the stanzas by hand if you want them run; otherwise each case
skips with the missing stanza's name.

### Skipping individual hypotheses

The integration tests are written one-test-per-PoC-hypothesis. If a
specific scenario doesn't apply to your host (e.g. you're testing
against `localhost` and the network-failure case is meaningless),
`pytest -k 'not network'` is the documented escape hatch.

## What's covered today

Manifest baseline (always-on):

- `.claude-plugin/marketplace.json` parses and lists the expected plugins
- Each plugin's `plugin.json` parses and has the required fields
- Every `SKILL.md` starts with YAML frontmatter containing `name` and `description`
- Every `SKILL.md` documents `## When to use` plus a flow section (`## Workflow`
  or `## Decision tree`)
- Every plugin has at least one `references/*.md`
- Every public script in every plugin accepts `--help` without crashing
  (private `_*.py` helpers are exempt — they are imported, not executed)
- Every marketplace entry's `version` matches its plugin's `plugin.json`
- The `_ssh_core_locator.py` copies in all seven plugins are byte-identical

Unit tests (always-on):

- `test_ssh_core_*.py` — the synced ssh-core surface: contracts (JSON on argv
  errors, `--version`), exit-code table, configfile/config-manager, askpass,
  session state machine, sudo/retry, local route, probe/inventory, output
  pipeline, redaction, follow.
- `test_ssh_guarded_lib_*_unit.py` — the shared libs in
  `plugins/ssh-guarded/.../scripts/lib/`: `sandbox` (project-id resolution,
  normalize, documented sandbox escape attempts, sandbox + denylist match),
  `redact` (default catalog, whole-private-key redaction, custom catalog,
  missing/corrupt fallback, per-pattern compile-fail skip), `lockfile` (POSIX
  `fcntl.flock` + Windows degraded `.lock` sentinel + holder-pid lookup +
  cleanup-on-exception), and `request_artifact` (schema v1 load/save atomic,
  `validate` chain with denylist-before-sandbox order plus traversal refusal,
  `allocate_request_id` O_EXCL retry, `parse_expires_in` D11 bounds,
  `compute_risk` per-op D4 table, `save_result` redaction bypass).
- `test_ssh_guarded_request_*_unit.py` + `test_ssh_guarded_run_request_unit.py`
  — the 4 drafters and the `run_request` executor: argparse SUPPRESS pattern
  (D10), per-script `_EXIT_CODES` taxonomy (30-49 for drafters, 50-59 for
  executor), drafter happy-path artifact write, sandbox warn-but-write (C3
  pedagogical), denylist warn-but-write, SHA-256 streaming + drift detection
  (exit 56), executor 7-step validation order (D7, denylist before sandbox),
  lockfile contention (exit 54), confirm-delete gate (exit 59), transport
  routing (D8 argv table), `_resolve_remote_home` + `~/` transport-time
  expansion (D7a), result.json schema (C9 / D9), redaction toggling.
- `test_exec_detached_unit.py` — `exec_detached.py` without SSH: CLI help,
  non-command artifact refusal, lock-held and duplicate-run subclasses, nohup
  wrapper escaping, PID parsing, atomic job-state writes, command excerpt
  truncation, status state machine (running / exited / vanished / corrupt /
  log-unreachable), tail-log redaction toggles, wait timeout, list
  offline/probe, and malformed ssh_execute child envelopes.
- `test_scan_software_unit.py` — cached software inventory coverage for the
  default 37-entry catalog, user catalog merge/override, cache TTL,
  `--has` / `--name`, corrupt cache handling, SSH batch failures, timeout,
  NDJSON parse failures, Xilinx directory scans, and grouped human output.
- `test_redact_check_unit.py` — redaction preview coverage for the built-in
  catalog, catalog override/schema/regex failures, JSON envelope shape,
  report-only mode, per-hit line numbers across a multi-line private key,
  empty stdin, binary stdin, and catalog regression behavior.
- Per-plugin unit suites for `remote-debug`, `k8s-debug`, `docker-quick`,
  `log-aggregator`, `iac-state` and `cicd-debug` scripts (one `_unit.py` per
  script, plus the shared `lib` helpers).

Integration tests (opt-in via `-m live_ssh`):

- `test_ssh_execute_integration.py` — replays PoC hypotheses H1–H6
  against `POC_HOST`: exit code propagation, known-hosts
  noise filtering, alias resolution, failure classification, timeout,
  local route. The alias/DNS cases assert the product's translated codes
  (`4 alias_not_found`, `102 network`) rather than OpenSSH's raw 255, and the
  timeout case asserts `elapsed_s` as a range.
- `test_ssh_upload_integration.py` — against `POC_HOST`: single
  file happy path (transport asserted as whichever backend this machine has),
  local-missing → 160 `local_path_missing`, dir-without-recursive → 161
  `local_path_is_directory`, resume (partial-then-complete + sha256 mismatch →
  9 `verify_failed`, both `sftp_only`), recursive (simple tree + partial
  failure with `failed_files`), `--chmod`/`--resume`/`--bwlimit`, `--no-progress`
  stderr suppression, and the `--overwrite` contract (existing destination + no
  flag → 168 `remote_path_exists`, remote untouched; with the flag → sha256
  matches). Cases marked `sftp_only` assert the paramiko backend; on a
  paramiko-free machine the three SFTP-only flags are instead asserted to be
  *refused* with 166 `mode_deferred` + `data.deferred_flags`, so the scp
  fallback is exercised rather than skipped wholesale.
- `test_ssh_download_integration.py` — against `POC_HOST`:
  single-file happy path (`/etc/hostname` content match), resume
  (partial-then-complete + sha256 mismatch → 9, both `sftp_only`), recursive
  (simple tree, whose layout assertion is the regression test for the tar
  fallback's extra-leaf-directory bug, plus partial failure with
  `failed_files`, the latter skipping when the remote user is root),
  `--bwlimit` wall-clock check, `--no-progress` stderr suppression, and three
  guard branches: remote not found → 162 `remote_path_missing`,
  remote-is-dir-without-recursive → 164 `transfer_io`,
  local-is-dir-without-recursive → 161 `local_path_is_directory`. Exit-code
  assertions import `lib/exit_codes.py` instead of hardcoding 1.
- `test_ssh_tunnel_integration.py` — 5 tests against `POC_HOST`, the whole
  module marked `needs_posix` (detached tunnels use `setsid`, teardown uses
  `os.killpg`, and both are absent on Windows): start->stop roundtrip with
  local-port LISTEN probe + state-file cleanup, `--no-detach` lifecycle
  (foreground spawn, SIGINT reap), empty-status, `stop` with no forward → 126
  `tunnel_not_found`, and the `--idle-timeout` watchdog assertion (a 1 s idle
  limit really does stop the tunnel and clean up the state file).
- `test_ssh_server_transfer_integration.py` — the `direct`/`stream`/`hybrid`
  modes and `--use-rsync` are exercised as *implemented* modes (1.4.0 removed
  the `mode_deferred`/`flag_deferred` refusals), and the round-trip case asserts
  whichever mode `--mode auto` resolved to (`stream` with paramiko, `direct`
  without) while verifying the bytes either way.
- `test_live_exit_code_contract.py` — offline guard for the whole live group: no
  live module may re-declare an `EXIT_*` integer, and every code those modules
  assert must exist in `lib/exit_codes.py` or in a shipped script's own
  `EXIT_CODES` table (discovered by AST, so a new log-aggregator/ssh-guarded
  code registers itself).
- `test_ssh_guarded_pipeline_live.py` — 6 tests against `POC_HOST` exercising
  the full draft → review → execute pipeline: command happy path
  (`echo hello` round-trip, result.json `stdout_redacted` carries the
  output), upload happy path (random 1 KiB blob, remote sha256 matches
  local), mkdir happy path (`--parents`, `test -d` verify), delete happy
  path (mkdir via direct ssh, then `request_delete --recursive` +
  `run_request --execute --confirm-delete`, `test ! -e` verify), denylist
  refusal (`request_delete ~/.ssh/authorized_keys` → drafter warns +
  writes artifact, executor exits 51 `path_in_denylist` without ssh),
  sandbox refusal (`request_command --workdir /etc` → drafter warns +
  writes, executor exits 50 `workdir_outside_sandbox`). All cases pin
  `--project-id devops-skills`, use uuid4-keyed remote paths under
  `~/workspace/devops-skills/`, and clean up remote + local artifacts +
  result.json in `finally`.
- `test_exec_detached_live.py` — 3 tests against `POC_HOST` covering
  detached command launch (`sleep 5; echo done`), status running → wait
  exited with the real exit code, tail-log fetching captured output, and
  duplicate `run --request` refusal with exit 60
  `request_already_executed`. Tests pin `--project-id exec-detached-it`,
  use uuid4-keyed remote workdirs under `~/workspace/exec-detached-it/`,
  and clean up remote workdirs plus the local job state under
  `$SSH_GUARDED_HOME` (default `~/.devops/ssh-guarded`; the plugin tree is
  never written to).
- `test_scan_software_integration.py` — 2 live_ssh tests against `POC_HOST`
  covering a full scan/cache write path and `--has` behavior for present,
  unknown, and user-catalog absent tools. The scan's tool count is compared to
  the catalog the script itself reports (not a pinned 37), the child process
  gets an isolated `HOME`/`USERPROFILE` so cache writes never touch the
  developer's real `~/.devops`, and the tests probe reachability of `POC_HOST`
  instead of assuming a hardcoded PoC subnet.

## Minimal verification set per plugin

Almost every plugin needs a backend that a given machine may not have. That is
not a product failure, and it should not be reported as "verified" either. Each
plugin has three commands that prove its core path once the backend exists; the
`tests/fixtures/backends/` directory has fixtures for the ones that can be
containerised.

| Plugin | Backend needed | Three commands that prove it |
|---|---|---|
| `ssh-core` | any reachable ssh alias | `ssh_execute.py <h> uptime --json`; `ssh_upload.py <h> ./f /tmp/f --json`; `ssh_cluster.py uptime --hosts <h1>,<h2> --json` |
| `ssh-guarded` | any reachable ssh alias | `request_command.py <h> "echo hi" --reason t --json`; `run_request.py --request <artifact> --execute --json`; `gc_artifacts.py --older-than 7d --dry-run --json` |
| `remote-debug` | any reachable ssh alias | `diagnose_host.py <h> --json`; `tail_log.py --hosts <h>,local <path> --json`; `port_check.py <h> --target <ip> --ports 22 --json` |
| `log-aggregator` | `journalctl` / `docker` / a file | `tail_multi.py --sources 'file://<h>/var/log/messages' --lines 20 --json`; `grep_across_sources.py --sources 'journal://<h>/sshd' --pattern open --json`; `correlate.py --sources 'journal://<h>/sshd' --anchor open --window 30s --json` |
| `k8s-debug` | kind + kubectl | `check_namespace.py local default --json`; `diagnose_pod.py local <ns> <pod> --json`; `helm_status.py local <ns> <release> --json` |
| `docker-quick` | a Docker daemon | `inspect_container.py local <container> --json`; `image_audit.py local <image> --json`; `compose_status.py local <project_dir> --json` |
| `iac-state` | terraform/tofu + hcl2 | `state_inspect.py local <project_dir> --json`; `drift_check.py local <project_dir> --json`; `module_validate.py local <module_dir> --json` |
| `cicd-debug` | `gh` / `glab` + credentials | `pipeline_analyzer.py gh <run-id> --project <owner/repo> --json`; `runner_check.py gh --org <org> --json`; `secret_scope_audit.py gh --repo <owner/repo> --json` |

## Verification levels

"Tested" means different things depending on what the machine could provide.
Use these three words in reports so the distinction survives:

| Level | Meaning | Evidence |
|---|---|---|
| `logic-tested` | Offline unit suite exercises every branch with fixture data | `pytest tests/ -m "not live_ssh"` |
| `backend-verified` | The three commands above ran against a real backend | attach their JSON envelopes |
| `device-verified` | A real network device / switch / firewall answered | attach `ssh_session.py` transcripts |

A plugin that only reached `logic-tested` has **not** been shown to work; say
so, and name the missing backend.

## Status

All shipped scripts have local tests, and environment-dependent tests remain
skip-safe. Three caveats worth knowing:

- **Platform.** The suite runs on Windows as well as POSIX; cases that genuinely
  need a POSIX shell are marked `needs_posix` and skip. On Windows, set
  `PYTHONUTF8=1`: the reference documents and several fixtures are UTF-8.
- **Optional dependencies.** `module_validate.py` needs `python-hcl2` for
  static HCL analysis and `secret_scope_audit.py` needs PyYAML for workflow
  parsing. Both import lazily, so `--help` and every non-parsing path work
  without them; the parsing path returns
  `{success: false, exit_code: 3, failure_class: "dependency_missing"}` with the
  install hint instead of a traceback. Likewise paramiko is optional: without
  it the transfer scripts fall back to `scp`/`tar`/`rsync` and the
  `sftp_only` cases skip.
- **Exit codes are imported, not copied.** Test modules import
  `plugins/ssh-core/skills/ssh-core/scripts/lib/exit_codes.py` rather than
  declaring their own `EXIT_*` constants, so a code change cannot leave the
  assertions behind. `test_live_exit_code_contract.py` enforces it offline: it
  fails if a live module re-declares an exit code (the old
  `EXIT_GENERIC_FAILURE = 1`) or asserts a number no shipped table registers.
