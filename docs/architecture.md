# Architecture

Eight plugins, one transport layer. This page is the map; each plugin's
`SKILL.md` is the manual.

```
                       ┌──────────────────────────────────────────┐
                       │  agent (Claude Code / DSH / Codex / ...)  │
                       └───────────────────┬──────────────────────┘
                                           │ picks one skill
        ┌──────────────────────────────────┼──────────────────────────────────┐
        │                                  │                                  │
   ┌────▼─────┐                     ┌──────▼──────┐                   ┌───────▼──────┐
   │ ssh-core │◄────────────────────┤ ssh-guarded │                   │  diagnostics │
   │transport │   request/execute   │   writes    │                   │  (read only) │
   └────┬─────┘                     └──────┬──────┘                   └───────┬──────┘
        │                                  │                                  │
        │  ssh / scp / sftp / tar / rsync  │  sandbox + redact + audit         │
        ▼                                  ▼                                  ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │ local host │ jumphost │ Linux server │ network device │ Windows target      │
   └─────────────────────────────────────────────────────────────────────────────┘
```

## Layers

| Layer | Plugin | Contract |
|---|---|---|
| Transport | `ssh-core` | One JSON envelope per call. Every remote action in the marketplace goes through it (ADR-005). |
| Safety | `ssh-guarded` | Drafts a request artifact, the human reviews it, `run_request.py` executes it. Redacted by default, workdir-sandboxed. |
| Application | `remote-debug`, `k8s-debug`, `docker-quick`, `log-aggregator`, `iac-state`, `cicd-debug` | Read-only diagnosis. They shell out to `ssh_execute.py` and never import ssh-core. |

## The one hard boundary

Diagnostics invoke ssh-core **as a subprocess**, with an argv list and a JSON
envelope on stdout. Nothing imports `ssh_core`'s Python. That is what lets a
user install a single plugin and still get a working tool — and what makes
`_ssh_core_locator.py` the only place that needs to know where ssh-core lives.

```
diagnostic script ──subprocess──► ssh_execute.py <host> "<cmd>" --json
                                        │
                                        ▼
                              {success, exit_code, stdout, stderr, data}
```

## Failure classes

Every plugin maps its own domain onto the shared exit-code table for transport
failures (100-129) and keeps a private range for its own taxonomy:

| Range | Owner |
|---|---|
| 0-9, 124 | shared (`ok`, `internal`, `invalid_arguments`, `dependency_missing`, `not_found`, `already_exists`, `config_missing`, `parse_error`, `permission`, `verify_failed`, `timeout`) |
| 40-41 | `log-aggregator` (`partial_fetch` 41 = at least one source could not be read; `fetcher_failed` is an accepted alias of the same class) |
| 100-129 | ssh-core transport + tunnel |
| 140-146 | `deploy_pubkey`, `iac-state` module validation |
| 150-158 | `cicd-debug` |
| 160-167 | transfers (166 = the transport cannot honour the requested mode) |
| 168 | transfers: `remote_path_exists` — the destination exists and `--overwrite` was not given |
| 180-181 | `ssh_config_manager` |
| 200-202, 220-221 | `ssh_daemon`, `audit` |
| 50-59 | `ssh-guarded` executor (`workdir_outside_sandbox` 50, `path_in_denylist` 51, lock 54, verify 56, confirm-delete 59) |
| 30-46 | `ssh-guarded` drafters (46 = `recursive_required_for_directory`) |

A caller that only needs "did it work" reads `success`; one that needs to branch
reads `data.failure_class`. Neither has to parse prose.

Three rules make that true across plugin boundaries:

1. **`success` means the action completed**, never "the request was dispatched".
   An executor whose child process exited non-zero reports `success: false` and
   exits non-zero itself.
2. **Partial reads fail.** One unreadable source, one failed fetcher, one host
   that really failed: the run is `success: false` with
   `failure_class: "partial_fetch"` (log-aggregator, exit 41) or
   `partial_failure` (ssh_cluster), and the data that *was* collected is still
   in `data`. A host that a platform check or a `--health-check` deliberately
   skipped is not a failure; it is reported under `data.skipped_hosts` next to
   `data.ok_hosts` / `data.failed_hosts`, and the run stays `success: true`.
3. **One envelope.** `{success, exit_code, stdout, stderr, data}` with
   `data.failure_class` on failure. `ssh-guarded` adds `error` as an extra
   object (`{message, failure_class, exit_code}`, with `error.exit_code` always
   equal to the top-level `exit_code`) but carries the same top-level
   `exit_code` / `stdout` / `stderr`, so `jq -r .exit_code` works on every
   script in the marketplace — including on an argparse error, which emits
   `exit_code: 2` / `failure_class: "invalid_arguments"` instead of nothing.

## Where the state lives

| Path | Written by | Lifetime |
|---|---|---|
| `~/.ssh/config` | `ssh_config_manager` | permanent, `.bak` on every write |
| `$TMPDIR/ssh_tunnel/*.json` | `ssh_tunnel` | while the tunnel runs |
| `$TMPDIR/ssh_daemon/<md5>.json` | `ssh_daemon` | while the daemon runs |
| `$TMPDIR/ssh-core/<md5>.jsonl` | any script with `--audit-log` | rotated by `audit.py` |
| `$SSH_GUARDED_HOME/reports/<project>/requests/*.json` | `ssh-guarded` drafters | until the human acts on it |
| `$SSH_GUARDED_HOME/reports/<project>/jobs/*.json` | `exec_detached` | until the job is collected |

`ssh-guarded` defaults `$SSH_GUARDED_HOME` to `~/.devops/ssh-guarded`
(`%USERPROFILE%\.devops\ssh-guarded` on Windows). It used to write into its own
skill directory, which breaks on a read-only install, loses pending approvals on
upgrade, and mixes two projects' artifacts together. The drafters,
`gc_artifacts.py` and `exec_detached.py` honour `--reports-dir` for CI and
containers; `run_request.py` writes `result.json` next to the artifact it was
handed. The plugin tree keeps only `reports/README.md`.

Everything under `$TMPDIR` is best-effort: a wiped temp directory loses
in-flight tunnels and daemons, not configuration.
