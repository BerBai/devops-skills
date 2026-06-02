# Design - k8s-debug helm_status v1.0

## Architecture

`helm_status.py` combines a small Helm CLI runner with the existing
`scripts/lib/kubectl.py` helper.

Flow:

1. Probe Helm version with `helm version --short`.
2. Route by CLI shape:
   - Per-release: `helm -n <namespace> status <release> -o json`,
     `helm -n <namespace> history <release> -o json`, and
     `kubectl -n <namespace> get jobs -l app.kubernetes.io/instance=<release>`.
   - Pending scan: `helm list --all-namespaces --pending -o json`.
3. Normalize Helm JSON variants into stable fields.
4. Score release status and hook jobs.
5. Emit the existing k8s-debug JSON envelope.

Helm commands are not pushed into `lib.kubectl.py`; Helm is a separate CLI with
separate failure classes. Remote Helm execution shells out to
`ssh-core/ssh_execute.py --json`, matching the kubectl wrapper's host model.

## CLI

Surface stays locked:

```bash
helm_status.py <host> <namespace> <release> [--json]
helm_status.py <host> --list-pending [--json]
```

`--list-pending` is mutually exclusive with `<namespace> <release>`.

## Failure Classes

`80`-`86` are pass-through `KubectlError` classes from hook job collection.

Helm-status-local classes:

| Code | Class | Policy |
|------|-------|--------|
| `110` | `helm_missing` | Hard failure when local or remote `helm` is absent. |
| `111` | `release_not_found` | Hard failure when `helm status` cannot find the requested release. |
| `112` | `helm_version_unsupported` | Hard failure for Helm major version below 3. |
| `113` | `history_truncated` | Soft warning when history cannot be collected or parsed. Exit remains `0`. |
| `114` | `pending_scan_partial` | Reserved soft class for future namespace-partial scans. v1.0 uses single `helm list --all-namespaces`, so the command either succeeds or maps to a hard Helm failure. |
| `115`-`119` | reserved | Future helm-status-local classes. |

Subprocess timeout remains `124`.

## Helm JSON Normalization

Per-release status accepts common Helm 3 variants:

- `status.info.status`
- `info.status`
- top-level `status`

History accepts either a top-level list or an object with `releases` /
`history` / `items`. Entries normalize to:

```json
{"revision": 3, "status": "deployed", "chart": "...", "app_version": "...", "updated": "...", "description": "..."}
```

Pending scan accepts a top-level list or object with `releases` / `items` and
normalizes release rows to:

```json
{"namespace": "prod", "name": "api", "status": "pending-upgrade", "revision": 7, "updated": "...", "chart": "..."}
```

## Scoring Rules

### Per-release

- `ok`: `status=deployed` and no stuck/failed associated jobs.
- `warn`: `status` is `superseded`, `uninstalling`, or `uninstalled`; or history
  is unavailable.
- `crit`: `status` is `failed`, `pending-install`, `pending-upgrade`, or
  `pending-rollback`; or any associated hook job is stuck/failed.

`recommended_next_step`:

- pending + stuck job: "inspect/delete stuck hook job, then rollback".
- pending without stuck job: "inspect Helm history and rollback to last deployed revision".
- failed: "inspect failed revision resources and rollback".
- deployed with no findings: "no action".

### Associated Jobs

Jobs come from:

```bash
kubectl -n <namespace> get jobs -l app.kubernetes.io/instance=<release> -o json
```

Stuck/failed rules:

- `failed`: `status.failed > 0` or condition `Failed=True`.
- `stuck`: active job age is older than 5 minutes and no succeeded count.

v1.0 does not diff Helm manifest against live resources. `stuck_resources` is an
empty list with `data.stuck_resources_deferred=true`.

### Pending Scan

Only pending states are returned:

- `pending-install`
- `pending-upgrade`
- `pending-rollback`

The scan is a successful command even when no pending releases exist.

## Findings

Finding fields:

```json
{"severity": "crit|warn", "kind": "HelmRelease", "name": "...", "namespace": "...", "reason": "...", "message": "...", "hint": "..."}
```

Stable sort:

1. severity: `crit`, then `warn`, then `ok`
2. kind
3. namespace
4. name
5. reason

`data.findings` contains top 5; `data.all_findings_count` records the full
count.

## JSON Shape

Per-release success data:

```json
{
  "host": "local",
  "namespace": "prod",
  "release": "api",
  "list_pending": false,
  "status": "deployed",
  "history": [],
  "associated_jobs": {"stuck": [], "failed": [], "all": []},
  "stuck_resources": [],
  "stuck_resources_deferred": true,
  "summary": "ok",
  "recommended_next_step": "no action",
  "findings": [],
  "raw": {"status": "...", "history": "...", "jobs": "...", "helm_version": "..."},
  "failure_class": null
}
```

Pending scan success data:

```json
{
  "host": "local",
  "list_pending": true,
  "pending_releases": [],
  "summary": "ok",
  "findings": [],
  "raw": {"pending": "...", "helm_version": "..."},
  "failure_class": null
}
```

Raw text is capped at 10 MB per field with a truncation marker.

## Tests

Unit tests monkeypatch the Helm runner and `run_kubectl`; no Helm or cluster is
required.

Live tests use existing `live_k8s`/kind fixtures and skip when helm, kind,
kubectl, or Docker are unavailable. They cover a deployed release and a
pending/degraded release scenario when the local environment supports it.
