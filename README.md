# devops-skills

> Open-source DevOps & cross-device debugging skills for [Claude Code](https://claude.com/claude-code).

English | [简体中文](./README.zh-CN.md)

Eight composable plugins covering the full DevOps surface — transport, audit, host debugging, Kubernetes, Docker, logs, IaC, CI/CD.

| Plugin | Layer | Role |
|---|---|---|
| **`ssh-core`** | Transport | Fast lane — persistent daemons, cluster broadcast, tunnels, server-to-server transfer |
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
- **Read-only diagnosis by default.** v0.2 plugins (k8s/docker/log/iac/cicd) only inspect. Writes go through `ssh-guarded` (with audit) or human-driven CLI.
- **`<host>` as first argument.** All v0.2 diagnostic scripts take a host alias as their first argument; `local` runs locally, otherwise commands are forwarded through `ssh-core`.
- **Two-layer docs.** `SKILL.md` is for triage (decision tree + quick reference). Deep content lives in `references/*.md` and loads progressively.
- **`subprocess.run(list, ...)`, never shell strings.** No exceptions.
- **JSON output contract.** Every script supports `--json` and emits `{success, exit_code, stdout, stderr, data}`. Chains compose without natural-language parsing.

## Status

`v0.2.0` — scaffold stage covering all 8 plugins. Manifests, SKILL.md, references, and Python script stubs (all with `--help` smoke tests) are in place. Real implementations land in v1.0; see [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the roadmap.

## License

MIT. See [`LICENSE`](./LICENSE).
