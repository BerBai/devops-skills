# Contributing to devops-skills

English | [简体中文](./CONTRIBUTING.zh-CN.md)

Thanks for considering a contribution. This document describes the structure of the repo, the shipped V1 status, and the current V1.1 roadmap.

## Repo layout

```
devops-skills/
├── .claude-plugin/marketplace.json   # marketplace entry — lists all plugins
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

## Skill authoring conventions

1. **`SKILL.md` is for triage, not depth.** Decision trees, hard checkpoints, one-paragraph command reference. Everything else goes in `references/`.
2. **`description` in `plugin.json` decides whether Claude Code picks the skill.** Lead with verbs and concrete keywords. Avoid generic words like "tool" or "helper". Both English and 中文 triggers are welcome — multi-lingual users benefit.
3. **Hard checkpoints.** If a workflow has irreversible side effects, write the checkpoint sequence in `SKILL.md` and refuse to deviate.
4. **JSON output contract.** Every script must support `--json` and emit `{"success": bool, "exit_code": int, "stdout": "...", "stderr": "...", "data": {...}}`. This is what lets Claude Code chain calls without parsing prose.
5. **`subprocess.run(list, ...)`, never shell strings.** No exceptions, even for "trusted" input. Test commands include `;`, `&&`, and ``` ` ``` deliberately.
6. **No emoji in skill output** unless the user explicitly asks. Skill markdown should also stay emoji-free.
7. **Docs examples must show shipped behavior only.** `SKILL.md` and `references/*.md` must not present deferred, not-yet-implemented, or future-version flags as positive examples. If a flag prints a "deferred to vX.Y" notice at runtime, it must not appear in docs without an explicit "not yet implemented" annotation. Same rule for paths or files that don't exist on supported distros (e.g. `/var/log/syslog` on systemd-only Debian 12+ / RHEL 9+ / Ubuntu 22.04+) — annotate or replace.

## V1 status

V1 is complete under the accepted rollout criterion:

- All eight plugins have shipped their V1 CLI surfaces.
- Every shipped script accepts `--help`; every operational script supports the JSON output contract.
- Shipped scripts have unit coverage, and environment-dependent integration tests are skip-safe.
- `ssh_daemon.py` is intentionally deferred to V1.1 per ADR-001 A2. V1 uses native OpenSSH subprocess execution through `ssh_execute.py`; no shipped V1 code calls the daemon stub.

V1 script inventory:

| Plugin | V1 status |
|---|---|
| `ssh-core` | `ssh_execute`, `ssh_cluster`, `ssh_tunnel`, `ssh_server_transfer`, `ssh_config_manager`, `ssh_upload`, `ssh_download`, and `deploy_pubkey` shipped; `ssh_daemon.py` deferred to V1.1. |
| `ssh-guarded` | request drafters, `run_request`, `exec_detached`, `scan_software`, and `redact_check` shipped. |
| `remote-debug` | `diagnose_host`, `tail_log`, `port_check`, and `compare_across_hosts` shipped. |
| `k8s-debug` | `check_namespace`, `diagnose_pod`, `cluster_health`, and `helm_status` shipped. |
| `docker-quick` | `inspect_container`, `image_audit`, and `compose_status` shipped. |
| `log-aggregator` | `tail_multi`, `correlate`, and `grep_across_sources` shipped. |
| `iac-state` | `state_inspect`, `drift_check`, and `module_validate` shipped. |
| `cicd-debug` | `pipeline_analyzer`, `runner_check`, and `secret_scope_audit` shipped. |

## V1.1 roadmap

Prioritize foundational transport work before plugin-specific enhancements:

1. `ssh-core` connection reuse: evaluate OpenSSH ControlMaster first, then decide whether `ssh_daemon.py` remains a custom daemon or becomes a compatibility wrapper.
2. `ssh_server_transfer.py`: implement true `direct` / `auto` / `hybrid` modes and `--use-rsync`.
3. `ssh_tunnel.py`: implement `--idle-timeout` behavior and revisit Windows support after the connection-reuse decision.
4. Long-running log follow behavior: improve `tail_multi.py`, `tail_log.py`, and related follow/reconnect workflows.
5. Redaction hardening: multiline private-key buffering, default catalog export, and optional regex timeout protection.
6. Plugin-specific enhancements: CI secret-scope deeper risk flags, request upload-root configurability, and other documented V1.1 follow-ups.

## Setup

This project uses [uv](https://github.com/astral-sh/uv) for dependency management. After cloning:

```bash
uv sync --all-extras
```

This creates `.venv/` with `pytest`, `ruff`, and `mypy` installed — the tools the test and lint sections below assume are on `PATH`.

## Testing

```bash
pytest tests/
```

CI should at minimum:
1. JSON-lint every `marketplace.json` and `plugin.json`.
2. Lint every `SKILL.md` for required sections (`description`, `When to use`, `Workflow`).
3. Smoke-test each script with `--help` (every script must accept it).
