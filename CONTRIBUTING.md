# Contributing to devops-skills

English | [简体中文](./CONTRIBUTING.zh-CN.md)

Thanks for considering a contribution. This document describes the structure of the repo, the shipped V1 status, and the current V1.1 roadmap.

## Repo layout

```
devops-skills/
├── .claude-plugin/marketplace.json   # marketplace entry — lists all plugins
├── .github/workflows/ci.yml          # the three jobs described under Testing
├── docs/                             # architecture + offline-install notes
├── scripts/                          # offline bundle build / install (docs/offline-install.md)
├── plugins/
│   ├── ssh-core/                     # transport layer
│   │   ├── .claude-plugin/plugin.json
│   │   └── skills/ssh-core/
│   │       ├── SKILL.md              # workflow + quick reference (skill entry)
│   │       ├── references/*.md       # progressively-loaded deep references
│   │       └── scripts/              # Python CLIs called by Claude Code
│   ├── ssh-guarded/                  # safety layer
│   ├── remote-debug/                 # host debugging
│   ├── k8s-debug/                    # Kubernetes diagnosis
│   ├── docker-quick/                 # Docker / container diagnosis
│   ├── log-aggregator/               # multi-source log aggregation
│   ├── iac-state/                    # Terraform / OpenTofu state
│   └── cicd-debug/                   # GitHub Actions / GitLab CI
└── tests/                            # pytest suites + manifest linters
```

Each plugin is **self-contained** — its `skills/<name>/scripts/` reaches outside the plugin only when explicitly documented. Diagnosis plugins (`remote-debug` / `k8s-debug` / `docker-quick` / `log-aggregator` / `iac-state` / `cicd-debug`) depend on `ssh-core` for remote command execution but do not import its Python directly; they shell out to the user-installed `ssh-core` CLIs. `ssh-guarded` plays the same shell-out game.

Nothing under `docs/` or `scripts/` is shipped to a user who installs a plugin: the marketplace copies `plugins/<name>/skills/<name>/` and nothing else. See [`docs/architecture.md`](./docs/architecture.md) for the layer map and [`docs/offline-install.md`](./docs/offline-install.md) for the air-gapped path.

## Skill authoring conventions

1. **`SKILL.md` is for triage, not depth.** Decision trees, hard checkpoints, one-paragraph command reference. Everything else goes in `references/`.
2. **`description` in `plugin.json` decides whether Claude Code picks the skill.** Lead with verbs and concrete keywords. Avoid generic words like "tool" or "helper". Both English and 中文 triggers are welcome — multi-lingual users benefit.
3. **Hard checkpoints.** If a workflow has irreversible side effects, write the checkpoint sequence in `SKILL.md` and refuse to deviate.
4. **JSON output contract.** Every script must support `--json` and emit `{"success": bool, "exit_code": int, "stdout": "...", "stderr": "...", "data": {...}}`. This is what lets Claude Code chain calls without parsing prose.
5. **`subprocess.run(list, ...)`, never shell strings.** No exceptions, even for "trusted" input. Test commands include `;`, `&&`, and ``` ` ``` deliberately.
6. **No emoji in skill output** unless the user explicitly asks. Skill markdown should also stay emoji-free.
7. **Docs examples must show shipped behavior only.** `SKILL.md` and `references/*.md` must not present deferred, not-yet-implemented, or future-version flags as positive examples. If a flag prints a "deferred to vX.Y" notice at runtime, it must not appear in docs without an explicit "not yet implemented" annotation. Same rule for paths or files that don't exist on supported distros (e.g. `/var/log/syslog` on systemd-only Debian 12+ / RHEL 9+ / Ubuntu 22.04+) — annotate or replace.

## Status

The V1 CLI surface is implemented and every plugin is versioned `1.0.0`. The
`v0.2.0` (2026-05-21) is the first public **scaffold**; `v1.0.0` tags this
release.
The version-source rule is recorded in [`CHANGELOG.md`](./CHANGELOG.md).

- All eight plugins have shipped their V1 CLI surfaces.
- Every public script accepts `--help`; every operational script supports the JSON output contract.
- Shipped scripts have unit coverage, and environment-dependent integration tests are skip-safe.
- `ssh-core` **1.0.0** contains the standalone **1.5.0** feature set ([ADR-004](./docs/adr/ADR-004-ssh-core-1.5.0-sync.md), [ADR-005](./docs/adr/ADR-005-ssh-core-boundary.md)). It is the marketplace's remote-connection sub-function: transport, auth, sessions, tunnels, transfers, `~/.ssh/config` CRUD, plus `selfcheck.py` / `doctor.py` capability probes. Other plugins shell out through `ssh_execute.py` and never import it (ADR-005).

Plugin surface inventory:

| Plugin | Shipped |
|---|---|
| `ssh-core` | `ssh_execute`, `ssh_session`, `ssh_slice`, `ssh_cluster`, `ssh_tunnel`, `ssh_server_transfer`, `ssh_config_manager`, `ssh_upload`, `ssh_download`, `deploy_pubkey`, `ssh_daemon`, `selfcheck`, `doctor`, and `audit` at 1.0.0 (standalone feature set 1.5.0), plus the `run.cmd` / `run.sh` launchers. |
| `ssh-guarded` | request drafters, `run_request`, `exec_detached`, `scan_software`, `redact_check`, and `gc_artifacts`. |
| `remote-debug` | `diagnose_host`, `tail_log`, `port_check`, and `compare_across_hosts` — all honour `local`; `diagnose_host` has a Windows probe family (`--target-os`), `tail_log` streams (`--follow`), `port_check` probes UDP (`--udp`). |
| `k8s-debug` | `check_namespace`, `diagnose_pod`, `cluster_health`, and `helm_status` (pending releases list their stuck pods). |
| `docker-quick` | `inspect_container`, `image_audit`, and `compose_status`. |
| `log-aggregator` | `tail_multi`, `correlate`, and `grep_across_sources`. |
| `iac-state` | `state_inspect`, `drift_check`, and `module_validate` — the static HCL analysis also works for a remote module (files are read over ssh-core, parsed locally). |
| `cicd-debug` | `pipeline_analyzer`, `runner_check`, and `secret_scope_audit` — optional leading `<host>` runs `gh`/`glab` through ssh-core, with an auth preflight on the remote box. |

## Roadmap

Prioritize foundational transport work before plugin-specific enhancements:

1. `ssh-core`: keep the plugin tree byte-identical to the standalone release (feature set currently 1.5.0; distribution version 1.0.0) and re-run the plugin suite after every sync.
2. `ssh_server_transfer.py`: validate named-OpenSSH/tar/rsync modes across additional host families.
3. `ssh_tunnel.py`: Windows support, revisited after the connection-reuse decision (`--idle-timeout` shipped in 1.4.0).
4. Redaction hardening: default catalog export and optional regex timeout protection.
5. Plugin-specific enhancements: CI secret-scope deeper risk flags, request upload-root configurability, and other documented follow-ups.

## Setup

This project uses [uv](https://github.com/astral-sh/uv) for dependency management. After cloning:

```bash
uv sync --all-extras
```

This creates `.venv/` with `pytest`, `ruff`, and `mypy` installed — the tools the test and lint sections below assume are on `PATH`.

`uv.lock` is generated, not hand-edited. It is regenerated with
`uv lock && uv sync --all-extras` and committed together with
`pyproject.toml`; the checked-in lock resolves the `>=3.8` floor and the
optional `sftp` / `yaml` / `hcl` / `dev` extras.

## Testing

```bash
pytest tests/
```

CI runs four jobs on Linux (`.github/workflows/ci.yml`). The three code jobs
cover four checks:

1. JSON-lint every `marketplace.json` and `plugin.json`.
2. Lint every `SKILL.md` for `## When to use` plus a flow section (`## Workflow` or `## Decision tree`) — enforced by `tests/test_manifests.py`.
3. Smoke-test each public script with `--help` (private `_*.py` helpers are exempt).
4. Assert the vendored helpers match their templates — enforced by `tests/test_manifests.py::test_vendored_locator_matches_template` and `::test_vendored_version_helper_matches_template`.

The `docs` job adds six more gates: relative links and heading anchors
(`scripts/check_docs_links.py --require-index`), EN/zh-CN structure parity
(`scripts/check_docs_parity.py`), the generated ssh-core CLI reference plus the
documented-script inventory for all eight plugins
(`scripts/gen_cli_reference.py --check --all`), the whitelisted help/version/
local examples (`--examples`), `markdownlint-cli` under
`.markdownlint.jsonc`, and `vale` with the committed `.vale.ini` vocabulary.

## Vendored files

Two helpers exist in more than one plugin because plugins are distributed
independently and must not import across plugin boundaries
(`docs/architecture.md`), yet keeping N hand-synced copies is how a real fix gets
silently reverted. Both are generated from one template each:

| Template | Copies | Purpose |
|---|---|---|
| `scripts/templates/_ssh_core_locator.py` | 7 | resolve the ssh-core install every remote shell-out needs |
| `scripts/templates/_version.py` | 7 | answer `--version` from the plugin manifest |

```bash
# edit the template, then render every copy
$EDITOR scripts/templates/_ssh_core_locator.py
python scripts/sync_vendored.py
python scripts/sync_vendored.py --check     # what CI runs
```

Commit the template and the rendered copies together. Never edit a copy in
place: CI fails when a copy drifts from the template, and the fix is always to
re-render, not to copy an old file over the new one.
