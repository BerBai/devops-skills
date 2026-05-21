# Contributing to devops-skills

English | [简体中文](./CONTRIBUTING.zh-CN.md)

Thanks for considering a contribution. This document describes the structure of the repo and what's needed to take it from scaffold to v1.

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

Each plugin is **self-contained** — its `skills/<name>/scripts/` reaches outside the plugin only when explicitly documented. v0.2 diagnosis plugins (`k8s-debug` / `docker-quick` / `log-aggregator` / `iac-state` / `cicd-debug`) depend on `ssh-core` for remote command execution but do not import its Python directly; they shell out to the user-installed `ssh-core` CLIs. `ssh-guarded` plays the same shell-out game.

## Skill authoring conventions

1. **`SKILL.md` is for triage, not depth.** Decision trees, hard checkpoints, one-paragraph command reference. Everything else goes in `references/`.
2. **`description` in `plugin.json` decides whether Claude Code picks the skill.** Lead with verbs and concrete keywords. Avoid generic words like "tool" or "helper". Both English and 中文 triggers are welcome — multi-lingual users benefit.
3. **Hard checkpoints.** If a workflow has irreversible side effects, write the checkpoint sequence in `SKILL.md` and refuse to deviate.
4. **JSON output contract.** Every script must support `--json` and emit `{"success": bool, "exit_code": int, "stdout": "...", "stderr": "...", "data": {...}}`. This is what lets Claude Code chain calls without parsing prose.
5. **`subprocess.run(list, ...)`, never shell strings.** No exceptions, even for "trusted" input. Test commands include `;`, `&&`, and ``` ` ``` deliberately.
6. **No emoji in skill output** unless the user explicitly asks. Skill markdown should also stay emoji-free.

## Roadmap from scaffold to v1.0

### `ssh-core`
- [ ] `ssh_daemon.py` — local TCP daemon, length-prefixed JSON protocol, 60s heartbeat, 30-min idle exit
- [ ] `ssh_execute.py` — daemon-aware front door; native ssh for key auth, paramiko fallback for password
- [ ] `ssh_tunnel.py` — port-forward daemon, ports 10000–20000 pool, state files keyed by `md5(alias)`
- [ ] `ssh_cluster.py` — ThreadPoolExecutor broadcast, tag/environment filters
- [ ] `ssh_server_transfer.py` — direct/stream/hybrid/auto modes
- [ ] `ssh_config_manager.py` — CRUD on `~/.ssh/config` with comment-line metadata
- [ ] `ssh_upload.py` / `ssh_download.py` — per-file SFTP transfer with progress and resume
- [ ] `deploy_pubkey.py` — idempotent public-key push to remote `authorized_keys`
- [ ] Windows `MSYS_NO_PATHCONV=1` plumbing in every transfer path

### `ssh-guarded`
- [ ] `request_command.py` / `request_upload.py` / `request_mkdir.py` / `request_delete.py` — generate JSON request artifacts under `reports/requests/`
- [ ] `run_request.py` — review-then-execute; refuses to run without `--execute`
- [ ] `exec_detached.py` — nohup + local job manifest, `status` and `tail-log` subcommands
- [ ] `scan_software.py` — cache `python/cuda/gcc/cmake/kubectl/terraform/docker/...` per host
- [ ] `redact_check.py` — sanity-check CLI for the redact module, which is default-on for all string output paths

### `remote-debug`
- [ ] `diagnose_host.py` — uptime/load/disk/mem/net/zombie checks, severity scoring
- [ ] `tail_log.py` — multi-host log tail with prefix
- [ ] `port_check.py` — TCP reachability matrix
- [ ] `compare_across_hosts.py` — diff a file across N machines
- [ ] Symptom → root-cause reference (`common_issues.md`)
- [ ] SEV1–SEV4 incident response reference

### `k8s-debug`
- [ ] `check_namespace.py` — namespace health snapshot (pods/events/deployments/services/PVCs)
- [ ] `diagnose_pod.py` — single Pod drill-down (describe + logs + previous-logs + events)
- [ ] `cluster_health.py` — nodes / control-plane / kube-system events
- [ ] `helm_status.py` — release status + stuck hook jobs + `--list-pending` cluster-wide
- [ ] Host transport: support `local` (subprocess) and remote (via ssh-core)

### `docker-quick`
- [ ] `inspect_container.py` — state + config + health log + recent logs, with exit-code classification
- [ ] `image_audit.py` — per-layer size + waste detection (apt/pip/npm cache, .git inside, root user)
- [ ] `compose_status.py` — Compose stack health + depends_on / healthcheck / port collision detection

### `log-aggregator`
- [ ] `tail_multi.py` — multi-source tail with normalized timestamps + noise filter + level filter + suppression
- [ ] `correlate.py` — anchor + window correlation across sources, with root-cause hint
- [ ] `grep_across_sources.py` — pattern hunt across N sources, summary / raw / json output modes
- [ ] Source spec parser supporting `journal://`, `docker://`, `kube://`, `file://` with brace/glob expansion (embedded in `tail_multi.py`)
- [ ] Clock-skew probe at startup; emit `clock_skew_ms` metadata per source (embedded in `tail_multi.py`)

### `iac-state`
- [ ] `state_inspect.py` — backend + size + lock status + workspace list
- [ ] `drift_check.py` — `plan -detailed-exitcode` parse, per-resource classify (no-op/update/replace/destroy)
- [ ] `module_validate.py` — module audit checklist, optional `--target-version` upgrade diff
- [ ] Tool autodetect: `tofu` if available, otherwise `terraform`

### `cicd-debug`
- [ ] `pipeline_analyzer.py` — single run analysis + diff vs reference run + cache/actions/runner summary
- [ ] `runner_check.py` — runner pool health (online/offline/busy/labels), queue depth, p95 wait
- [ ] `secret_scope_audit.py` — secret visibility map + risk flags (fork-PR exposure, long-lived creds, etc.)
- [ ] Provider auth via `gh auth status` / `glab auth status` — fail fast if not logged in

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
