# devops-skills

> Open-source DevOps & cross-device debugging skills for [Claude Code](https://claude.com/claude-code).

English | [简体中文](./README.zh-CN.md)

Eight composable plugins covering the full DevOps surface — transport, audit, host debugging, Kubernetes, Docker, logs, IaC, CI/CD.

| Plugin | Layer | Role |
|---|---|---|
| **`ssh-core`** | Transport | Fast lane — command execution, cluster broadcast, tunnels, upload/download, server-to-server transfer |
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
- **Read-only diagnosis by default.** Diagnostic plugins inspect by default. Writes go through `ssh-guarded` (with audit) or human-driven CLI.
- **`<host>` as first argument.** Diagnostic scripts take a host alias as their first argument; `local` runs locally, otherwise commands are forwarded through `ssh-core`.
- **Two-layer docs.** `SKILL.md` is for triage (decision tree + quick reference). Deep content lives in `references/*.md` and loads progressively.
- **`subprocess.run(list, ...)`, never shell strings.** No exceptions.
- **JSON output contract.** Every script supports `--json` and emits `{success, exit_code, stdout, stderr, data}`. Chains compose without natural-language parsing.

## Status

`v1.0` — all 8 plugins have shipped their V1 CLI surfaces with unit coverage and skip-safe integration coverage. The accepted V1 closure keeps `ssh_daemon.py` deferred to V1.1 per ADR-001: V1 uses native OpenSSH subprocess execution, while V1.1 will evaluate OpenSSH ControlMaster before reviving any custom daemon.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the V1.1 roadmap.

## License

MIT. See [`LICENSE`](./LICENSE).
