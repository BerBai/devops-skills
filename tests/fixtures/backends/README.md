# Backend fixtures

Every diagnostic plugin needs a backend that a given machine may not have:
Kubernetes, a Docker daemon, Terraform, `gh`/`glab`. When the backend is absent
the offline suite still passes (it exercises the "backend missing" envelopes),
which is a real but *small* result: it proves the failure path, not the feature.

This directory closes that gap. The fixtures here can be started on any machine
with Docker, so "the plugin works" becomes a claim someone else can reproduce
instead of "it worked on the tester's laptop".

## What is here

| Fixture | Provides | Plugin | Marker |
|---|---|---|---|
| `compose.kind.yml` | one-node kind cluster (kubectl API, Helm-capable) | `k8s-debug` | `live_k8s` |
| `compose.docker.yml` | Docker-in-Docker daemon reachable through `DOCKER_HOST` | `docker-quick` | `live_docker` |
| `compose.iac.yml` | MinIO as an S3 backend plus Terraform/OpenTofu | `iac-state` | `live_iac` |
| `README.md` (this file) | the `gh`/`glab` path, which cannot be containerised | `cicd-debug` | `cicd_gh` / `cicd_glab` |

## Start

```bash
# Kubernetes: the same cluster name the suite expects, so kind_cluster reuses it
docker compose -f tests/fixtures/backends/compose.kind.yml up -d
pytest tests/ -m live_k8s -q

# Docker: the suite reads DOCKER_HOST from the environment
docker compose -f tests/fixtures/backends/compose.docker.yml up -d
export DOCKER_HOST=tcp://127.0.0.1:2375
pytest tests/ -m live_docker -q

# IaC: state backend + a throwaway project
docker compose -f tests/fixtures/backends/compose.iac.yml up -d
pytest tests/ -m live_iac -q

# Stop everything
docker compose -f tests/fixtures/backends/compose.kind.yml down
```

Each suite skips (never fails) when its backend is missing, so these fixtures
are an opt-in upgrade of the evidence, not a new requirement.

## CI/CD cannot be containerised

`cicd-debug` talks to a real GitHub or GitLab instance: `gh` and `glab` need
credentials, and the valuable assertions are about *other people's* pipelines.
The reproducible substitute is a fixture repository:

1. Create `CICD_GH_REPO=<owner>/<repo>` with one deliberately failing
   workflow, one runner that is offline, and a workflow that references a
   secret from a pull request.
2. `gh auth status` must succeed for the account running the suite.
3. `pytest tests/ -m cicd_gh -q`.

`glab` follows the same shape with `CICD_GLAB_PROJECT`. Both markers skip when
the CLI or the repository variable is missing, and the skip message names which
one.

## Which level did I reach?

See "Verification levels" in `tests/README.md`: `logic-tested` (offline suite),
`backend-verified` (one of these fixtures, or a real backend, plus the three
commands listed per plugin), `device-verified` (a real switch/firewall). Report
the level next to the plugin name; do not imply `backend-verified` from a
passing offline run.
