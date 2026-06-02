# Design — k8s-debug check_namespace v1.0

## Architecture

`check_namespace.py` is a thin namespace-health aggregator over the shipped
`lib.kubectl.run_kubectl` helper.

Flow:

1. Probe namespace exists with `kubectl get ns <namespace>`.
2. Collect five domains concurrently:
   - pods: `get pods -o json`
   - events: `get events -o json`
   - deployments: `get deployments -o json`
   - services: `get services -o json`
   - endpoints: `get endpoints -o json`
   - pvcs: `get pvc -o json`
3. Score domains as `ok | warn | crit`.
4. Build stable `top-3 findings`.
5. Emit the existing k8s-debug JSON envelope.

## CLI

Surface stays locked:

```bash
check_namespace.py <host> <namespace> [--since 60] [--json]
```

`--since` is minutes and only affects Event recency filtering. Values below 1
are normalized to 1; values above 1440 are accepted but reported in
`data.warnings`.

## Scoring Rules

### Pods

- `crit`: at least one pod has `CrashLoopBackOff`, `ImagePullBackOff`,
  `ErrImagePull`, `OOMKilled`, phase `Failed`, or at least three pods are
  `Pending` / `Failed`.
- `warn`: at least one pod is `Pending`, has a not-ready container, or has
  restart count > 0.
- `ok`: no findings.

### Events

- `crit`: at least one recent Warning event has reason in
  `{Failed, FailedMount, FailedScheduling, BackOff, Unhealthy, Killing,
  OOMKilling}`.
- `warn`: at least three recent Warning events.
- `ok`: no recent relevant Warning events.

### Deployments

- `crit`: at least one deployment has desired replicas > ready replicas and
  observed generation caught up.
- `warn`: at least one deployment has desired replicas > available replicas,
  or progressing condition is neither `True` nor absent.
- `ok`: no findings.

### Services

- `crit`: at least one ClusterIP/NodePort/LoadBalancer service has no endpoint
  addresses.
- `ok`: all non-ExternalName services have endpoints.

### PVCs

- `crit`: at least one PVC phase is `Pending` or `Lost`.
- `ok`: all PVCs are bound or no PVCs exist.

## Findings

Finding fields:

```json
{"severity": "crit|warn", "domain": "pods", "kind": "Pod", "name": "...", "reason": "...", "message": "...", "hint": "..."}
```

Sort order is stable:

1. severity: `crit`, then `warn`, then `ok`
2. domain order: pods, events, deployments, services, pvcs
3. kind
4. name
5. reason

`data.findings` contains top 3 only; `data.all_findings_count` records the full
count.

## Partial Collection

Namespace probe and hard `KubectlError` classes 80-86 are passed through.

For domain collection:

- 1-3 failed domains: success exit 0, `data.partial_failures` populated, domain
  scores are `warn`.
- 4+ failed domains: exit 88, `failure_class=partial_collection`.

`88` is the only new check_namespace-local exit code. The old PRD draft's
`90-99` range is not used because it is already owned by log-aggregator
`correlate`.

## JSON Shape

Success data:

```json
{
  "host": "local",
  "namespace": "default",
  "since_min": 60,
  "summary": {"pods": "ok", "events": "warn", "deployments": "ok", "services": "ok", "pvcs": "ok"},
  "findings": [],
  "all_findings_count": 0,
  "partial_failures": [],
  "raw": {"pods": "...", "events": "...", "deployments": "...", "services": "...", "endpoints": "...", "pvcs": "..."},
  "failure_class": null
}
```

Human mode prints a compact summary table and findings; errors go to stderr via
`lib.emit`.

## Tests

Unit tests monkeypatch `check_namespace.run_kubectl`; no cluster is required.

Integration tests use `live_k8s` and skip unless `kubectl` can talk to a
cluster. They cover a healthy namespace and a degraded namespace fixture when
kind/Docker is available.
