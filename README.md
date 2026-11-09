# devops-skills

> Open-source DevOps & cross-device debugging skills for [Claude Code](https://claude.com/claude-code).

English | [简体中文](./README.zh-CN.md)

Eight composable plugins covering the full DevOps surface — transport, audit, host debugging, Kubernetes, Docker, logs, IaC, CI/CD.

| Plugin | Layer | Role |
|---|---|---|
| **`ssh-core`** | Transport | Fast lane — byte-safe command execution, key/password auth, interactive sessions, cluster broadcast, tunnels, multi-backend transfers, sudo, retries, and SSH config CRUD |
| **`ssh-guarded`** | Safety | Slow lane — request/execute two-phase writes, redacted output, workdir sandbox |
| **`remote-debug`** | Host debug | Playbook — symptom decision trees, incident response, Linux & network diagnostics |
| **`k8s-debug`** | K8s | Pod/Deployment/Service diagnosis, CrashLoopBackOff/OOMKilled, Helm release health |
| **`docker-quick`** | Container | Container inspect, exit codes, image audit, Compose stack health |
| **`log-aggregator`** | Logs | Multi-source tail + correlation by trace-id / time-window across hosts/containers/Pods |
| **`iac-state`** | IaC | Terraform/OpenTofu state, drift detection, module audit |
| **`cicd-debug`** | CI/CD | GitHub Actions / GitLab CI pipeline analysis, runner health, secret scope audit |

## Install

```bash
# Inside Claude Code
/plugin marketplace add https://github.com/BerBai/devops-skills
/plugin install ssh-core@devops-skills
/plugin install ssh-guarded@devops-skills
/plugin install remote-debug@devops-skills
/plugin install k8s-debug@devops-skills
/plugin install docker-quick@devops-skills
/plugin install log-aggregator@devops-skills
/plugin install iac-state@devops-skills
/plugin install cicd-debug@devops-skills
```

Install any subset. `ssh-core` is the only hard dependency for the others (used for remote command execution).

## Design principles

- **One plugin per problem domain.** Narrow, opinionated `description` strings make Claude Code's skill matcher more accurate.
- **`ssh-core` is the remote-connection sub-function.** Transport, auth, sessions, tunnels, transfers and `~/.ssh/config` live there and nowhere else; every other plugin reaches a remote host by shelling out to it (ADR-005).
- **Read-only diagnosis by default.** Diagnostic plugins inspect by default. Writes go through `ssh-guarded` (with audit) or human-driven CLI.
- **`<host>` as first argument.** Every diagnostic script takes a host as its first argument; `local` runs locally, otherwise commands are forwarded through `ssh-core`. Omitting it means `local`.
- **Two-layer docs.** `SKILL.md` is for triage (decision tree + quick reference). Deep content lives in `references/*.md` and loads progressively.
- **`subprocess.run(list, ...)`, never shell strings.** No exceptions.
- **JSON output contract.** Every script supports `--json` and emits `{success, exit_code, stdout, stderr, data}`. Chains compose without natural-language parsing. `success` means the action completed, never merely "the request was dispatched".
- **`--version` everywhere.** Every public script answers `--version` / `-V` with `<script> (<plugin> <x.y.z>)`, sourced from the plugin's `plugin.json`, so "which build is installed" is one flag away.
- **One `partial_fetch` rule.** Any source or host that could not be read makes the run fail (`success: false` + a non-zero exit code) while the data that *was* collected is still returned under `data` — never a silent empty result.

## Where ssh-core lives

Diagnostic plugins do not guess: each one ships its own `scripts/_ssh_core_locator.py` (vendored from `scripts/templates/`, not shared) that resolves ssh-core in this order.

1. `$DEVOPS_SSH_CORE_SCRIPTS` — the `.../ssh-core/scripts` directory
2. `$DEVOPS_SSH_CORE_DIR` — the `.../ssh-core` skill root
3. the repo layout (`plugins/ssh-core/...` next to the calling plugin)
4. installed agent roots (`~/.claude/plugins/`, `~/.dsh/skills/`, `~/.agents/skills/`, `~/.codex/skills/`)

Set the first variable when you install the plugins into unrelated directories, and the error message tells you exactly what was looked for.

## Status

`v1.0` shipped all 8 plugin surfaces. `ssh-core` now tracks the standalone tree at `1.5.0` (ADR-004, ADR-005), adding `doctor.py` capability probes, `ssh_slice.py` windowed log reads, the `run.cmd`/`run.sh` launchers, `--output-file`, `--script-file`, `--allow-exit`, and the `user_required` failure class. Other plugins still communicate with it only through CLI + JSON.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the V1.1 roadmap.

## License

MIT. See [`LICENSE`](./LICENSE).
