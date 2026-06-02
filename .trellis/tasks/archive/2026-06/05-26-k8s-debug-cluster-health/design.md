# Design - k8s-debug cluster_health v1.0

## Architecture

`cluster_health.py` is a read-only cluster snapshot command over the shipped
`scripts/lib/kubectl.py` helper. It does not write kubeconfig, change context,
or mutate cluster resources.

Flow:

1. Probe cluster reachability with `kubectl get nodes -o json`.
2. Collect cluster domains:
   - nodes: from the mandatory nodes JSON.
   - control_plane: visible control-plane pods in `kube-system`, otherwise
     managed-cluster fallback through `kubectl get --raw=/healthz` and
     `kubectl get --raw=/readyz`.
   - kube_system: `kubectl -n kube-system get pods -o json`.
   - events: `kubectl -n kube-system get events -o json`.
   - versions: `kubectl version --short` plus kubelet versions from nodes.
3. Score each domain as `ok | warn | crit`.
4. Sort findings deterministically and expose the top findings.
5. Emit the existing k8s-debug JSON envelope.

## CLI

Surface stays locked:

```bash
cluster_health.py <host> [--json] [--since 30]
```

`--since` is minutes and only affects Event recency intent. Values below `1`
are normalized to `1`; values above `1440` are accepted but reported in
`data.warnings`.

## Failure Classes

`80`-`86` are pass-through `KubectlError` classes from `lib.kubectl`.

Cluster-health-local classes:

| Code | Class | Policy |
|------|-------|--------|
| `100` | `cluster_unreachable` | Hard failure when the mandatory `get nodes` probe reaches kubectl but cannot reach the Kubernetes API server. SSH transport failures remain `80 host_unreachable`. |
| `101` | `healthz_unavailable` | Soft finding when no visible control-plane pods exist and `/healthz` plus `/readyz` cannot be collected. Exit remains `0`. |
| `102` | `versions_partial` | Soft finding when `kubectl version --short` cannot be collected while nodes are otherwise available. Exit remains `0`. |
| `103`-`109` | reserved | Future cluster-health-local classes. |

## Scoring Rules

### Nodes

- `crit`: any node has `Ready` not equal to `True`.
- `warn`: any node has `DiskPressure`, `MemoryPressure`, `PIDPressure`, or
  `NetworkUnavailable` equal to `True`; or `spec.unschedulable=true`.
- `ok`: all nodes are Ready, schedulable, and pressure-free.

Node findings include `kind="Node"`, node name, condition/reason, and the
condition message when Kubernetes provides it.

### Control Plane

Self-hosted path:

- Inspect `kube-system` pods whose labels or names indicate
  `kube-apiserver`, `kube-controller-manager`, `kube-scheduler`, or `etcd`.
- `crit`: any visible control-plane pod is Failed, Pending, not Ready, or has a
  container waiting in CrashLoopBackOff/ImagePullBackOff/ErrImagePull.
- `ok`: visible control-plane pods are Running and Ready.

Managed-cluster fallback:

- If no visible control-plane pods exist, call `/healthz` and `/readyz` through
  kubectl raw endpoints.
- `ok`: both endpoints return text containing `ok`.
- `crit`: either endpoint returns a collected non-ok body.
- `warn`: both endpoint calls fail or are unavailable; add
  `healthz_unavailable` finding because managed clusters may hide these paths.

### kube-system

- `crit`: any kube-system pod is Failed or has a container in
  CrashLoopBackOff/ImagePullBackOff/ErrImagePull/OOMKilled.
- `warn`: any kube-system pod is Pending, not Ready, or has restart count > 0.
- `ok`: no findings.

### Events

- `crit`: any recent kube-system Warning event has reason in
  `{NodeNotReady, Failed, FailedMount, FailedScheduling, BackOff, Unhealthy,
  Killing, OOMKilling}`.
- `warn`: at least three recent Warning events.
- `ok`: no relevant Warning events.

`--since` is represented in `data.since_min`; v1.0 does not rely on kubectl
field selectors for time filtering because Event timestamps are inconsistent
across Kubernetes versions. Tests assert the value is carried through and only
the events command is time-window related.

### Versions

- `warn`: node kubelet minor-version skew is greater than one minor, or
  `kubectl version --short` collection fails.
- `ok`: version data is collected and node kubelet skew is within one minor.

Client/server version text is informational. Node kubelet versions come from
`status.nodeInfo.kubeletVersion` in the nodes payload.

## Findings

Finding fields:

```json
{"severity": "crit|warn", "domain": "nodes", "kind": "Node", "name": "...", "reason": "...", "message": "...", "hint": "..."}
```

Stable sort:

1. severity: `crit`, then `warn`, then `ok`
2. domain order: nodes, control_plane, kube_system, events, versions
3. kind
4. name
5. reason

`data.findings` contains the top 5; `data.all_findings_count` records the full
count.

## JSON Shape

Success data:

```json
{
  "host": "local",
  "since_min": 30,
  "summary": {"nodes": "ok", "control_plane": "ok", "kube_system": "ok", "events": "ok", "versions": "ok"},
  "findings": [],
  "all_findings_count": 0,
  "raw": {"nodes": "...", "kube_system_pods": "...", "kube_system_events": "...", "version": "...", "healthz": "...", "readyz": "..."},
  "warnings": [],
  "failure_class": null
}
```

Raw text is capped at 10 MB per field with a truncation marker.

## Tests

Unit tests monkeypatch `cluster_health.run_kubectl`; no cluster is required.

Live tests use existing `live_k8s`/kind fixtures and skip when kind, kubectl, or
Docker are unavailable. They cover a healthy kind cluster and a degraded
kube-system pod fixture when the local environment supports kind.
