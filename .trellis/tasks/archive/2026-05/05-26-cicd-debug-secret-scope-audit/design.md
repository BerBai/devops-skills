# Design - cicd-debug secret_scope_audit v1.0

> Paired PRD: `prd.md`.
> Implementation checklist: `implement.md`.
> This task extends the `lib_cicd.py` provider abstraction introduced by the
> pipeline_analyzer sibling. It stays read-only and must never serialize secret
> values.

---

## 1. Architecture overview

```
secret_scope_audit.py
├── argparse surface from PRD D6
├── validate_scope(args) -> Scope
├── resolve_provider(args.provider, gh_bin, glab_bin) -> GhAdapter | GlabAdapter
├── provider.list_secrets(scope, include_codespaces, include_dependabot)
├── provider.list_environments(scope)              # gh repo scope only
├── provider.fetch_workflow_files(scope, cap)      # repo/project scope only
├── analyze_workflows(files) -> WorkflowAnalysis
├── attach_references(secrets, workflow_analysis)
├── compute_risk_flags(...)
└── lib.emit(json_result(...))

lib_cicd.py additions
├── dataclass SecretData
├── dataclass EnvData
├── dataclass WorkflowFile
├── GhAdapter.list_secrets/list_environments/fetch_workflow_files
└── GlabAdapter.list_secrets/list_environments/fetch_workflow_files
```

Data flow:

```
argv -> Scope -> secret metadata
              -> optional env metadata
              -> optional workflow files
              -> YAML parse + reference graph
              -> per-secret risk flags + findings
              -> JSON envelope
```

The envelope `success` describes whether the auditor ran successfully. A repo
with risky secrets still exits 0 with `summary="warn"` or `"crit"`.

---

## 2. Module boundaries

### `secret_scope_audit.py`

Owns user-facing behavior:

- CLI parsing and cross-provider scope validation.
- Workflow YAML parsing and reference extraction.
- Risk-flag heuristics and finding construction.
- Summary computation and human output.
- Mapping provider/library exceptions to the 170-179 + 124 + 1 namespace.

### `lib_cicd.py`

Owns provider IO only:

- subprocess wrapper and binary resolution inherited from pipeline_analyzer.
- provider-specific argv/API construction.
- JSON shape validation at the provider boundary.
- dataclass normalization into `SecretData`, `EnvData`, and `WorkflowFile`.

`lib_cicd.py` does not compute risk flags. It returns metadata and workflow
content only.

---

## 3. Shared dataclasses

```python
@dataclass(frozen=True)
class SecretData:
    name: str
    scope_kind: str      # repo | env | org | codespaces | dependabot | project | group
    scope_value: str
    kind: str            # actions | codespaces | dependabot | ci_variable
    visibility: str | None = None
    num_selected_repos: int | None = None
    masked: bool | None = None
    protected: bool | None = None
    environment_scope: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class EnvData:
    name: str
    has_protection_rules: bool
    scoped_secret_count: int


@dataclass(frozen=True)
class WorkflowFile:
    path: str
    content: str
    source: str          # gh_contents | glab_repository_file
```

Script-local dataclasses:

```python
@dataclass(frozen=True)
class WorkflowRef:
    secret_name: str
    path: str
    jobs: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowScan:
    path: str
    parses_ok: bool
    secret_refs: tuple[str, ...]
    event_triggers: tuple[str, ...]
    pull_request_target_with_unsafe_checkout: bool
    jobs_by_secret: Mapping[str, tuple[str, ...]]
    jobs_with_environment: Mapping[str, str]
```

No dataclass has a `value` field. GitLab variable values are dropped while
normalizing provider output.

---

## 4. Provider command contracts

### GitHub secret enumeration

Repo scope (`--repo owner/name`):

| Kind | Command |
|---|---|
| Actions repo secrets | `gh secret list -R owner/name -a actions --json name,updatedAt` |
| Dependabot secrets | `gh secret list -R owner/name -a dependabot --json name,updatedAt` |
| Codespaces secrets | `gh secret list -R owner/name -a codespaces --json name,updatedAt` |
| Environments | `gh api repos/{owner}/{repo}/environments --paginate` |
| Env secrets | `gh secret list -R owner/name -e <env> --json name,updatedAt` |

Org scope (`--org org`):

| Kind | Command |
|---|---|
| Org Actions secrets | `gh secret list -o org --json name,visibility,updatedAt,numSelectedRepos,selectedReposURL` |

Org scope does not enumerate every repo in the org. It returns
`referenced_in_workflows=null` for every secret.

### GitHub workflow fetch

For repo scope only:

1. List `.github/workflows` via `gh api repos/{owner}/{repo}/contents/.github/workflows`.
2. Keep files ending in `.yml` or `.yaml`; ignore other entries.
3. If entries exceed `--workflow-fetch-cap`, fetch only the first `N` sorted by
   path and emit `workflow_scan_truncated`.
4. Fetch each file with `gh api repos/{owner}/{repo}/contents/.github/workflows/<path>`.
5. Decode the base64 `content` field.

If the directory is missing, return an empty workflow list and let the script
emit the soft `workflow_dir_missing` class.

### GitLab variable enumeration

Project scope:

`glab variable list --repo group/project --output json`

Group scope:

`glab variable list --group group/path --output json`

Normalize fields:

- `key` -> `SecretData.name`
- `masked`, `protected`, `environment_scope`, `variable_type`
- explicitly ignore/drop `value`

GitLab group scope does not enumerate all child projects in v1.0. It mirrors GH
org behavior: `referenced_in_workflows=null` for group variables, with
`workflow_files=[]`.

### GitLab workflow fetch

For project scope only:

1. Fetch project metadata via `glab api projects/:encoded_project`.
2. Read `default_branch`; fall back to `main` only if the field is absent.
3. Fetch `.gitlab-ci.yml` with
   `glab api projects/:encoded_project/repository/files/.gitlab-ci.yml?ref=<default_branch>`.
4. Decode base64 `content`.

GitLab `include:` chains are not followed in v1.0.

---

## 5. YAML parser and reference graph

Use `yaml.safe_load`. If the parsed document is not a mapping, treat the file as
parse-failed for audit purposes.

GitHub event extraction:

- `on: pull_request`
- `on: [push, pull_request]`
- `on: {pull_request: {types: [...]}}`
- `on: pull_request_target`

PyYAML may parse the key `on` as boolean `True` under YAML 1.1 behavior. The
parser must check both `"on"` and `True` keys.

GitHub secret references:

- Scan the loaded YAML tree recursively.
- Match string values containing `${{ secrets.NAME }}`.
- Also detect `${{ secrets[NAME] }}` and `${{ secrets['NAME'] }}` when the name
  is a literal.
- Do not attempt to resolve dynamic expressions like `${{ secrets[matrix.key] }}`;
  add a `dynamic_secret_reference` info finding instead.

Job attribution:

- For GitHub, jobs live under `jobs.<job_id>`.
- A secret referenced under a job subtree maps to that job id.
- A secret referenced outside `jobs` maps to an empty `jobs` list and still
  appears in `workflow_files[].secret_refs`.

GitLab variable references:

- Scan `.gitlab-ci.yml` recursively for `$NAME`, `${NAME}`, and `%NAME%`.
- Match only names present in the enumerated CI/CD variable set to avoid
  flagging ordinary shell variables.
- Job attribution is any top-level mapping key that is not a reserved GitLab
  keyword (`stages`, `variables`, `workflow`, `include`, `default`, `image`,
  `services`, `before_script`, `after_script`, `cache`).

Parse-error policy:

- Track `parses_ok` per workflow file.
- If more than 50 percent of fetched workflow files fail parsing, set
  `failure_class="workflow_yaml_parse_failed"` and return partial data.
- If 50 percent or fewer fail parsing, keep `failure_class=null` and emit a warn
  finding per failed file.

---

## 6. Risk flags

### `long_lived_cloud_credential`

Emit when the secret name matches:

`^(AWS|AZURE|GCP|GOOGLE|SP|SERVICE_PRINCIPAL|DOCKER)_(.*)$`

and does not contain `OIDC`, `FEDERATED`, or `WIF`.

Provider-independent; works even when `--no-workflow-scan` is used.

### `exposed_on_fork_pr`

GitHub only. Emit when:

- workflow has a `pull_request` trigger, and
- that workflow references the secret.

This is a reliability/confusion warning: GitHub does not populate repo secrets
for fork PRs on `pull_request`.

### `pull_request_target_unsafe_checkout`

GitHub only. Emit when:

- workflow has a `pull_request_target` trigger, and
- a step uses `actions/checkout@...`, and
- the step `with.ref` contains one of:
  - `github.event.pull_request.head.sha`
  - `github.event.pull_request.head.ref`
  - `github.head_ref`

This is file-level, but PRD summary is per secret. Attach the flag to all secrets
referenced by the unsafe workflow file. Also emit one `crit` finding per file.

### `unused`

Emit when enabled and a secret has no workflow references. For org/group scope,
do not emit `unused` because the auditor intentionally does not scan all
repositories.

Deferred flags from the v0.2 stub remain documented but are not emitted by v1.0:

- `protected_secret_referenced_from_unprotected_job`
- `wildcard_org_scope_on_public_repo`

Callers must tolerate unknown future values.

---

## 7. Failure handling

`secret_scope_audit.py` maps exceptions into the PRD D3 namespace:

| Class | Exit | Notes |
|---|---:|---|
| `tool_missing` | 170 | missing binary during binary resolution |
| `provider_unavailable` | 171 | requested provider cannot be constructed |
| `auth` | 172 | 401, "not logged in", auth-required stderr |
| `not_found` | 173 | target repo/org/project/group missing |
| `permission` | 174 | 403, admin/member permission error |
| `network` | 175 | DNS, connect timeout, 5xx without useful body |
| `bad_args` | 176 | post-arg validation failure |
| `api_unexpected` | 177 | provider JSON shape mismatch |
| `workflow_yaml_parse_failed` | 178 | partial result, >50 percent YAML parse failed |
| `workflow_dir_missing` | 179 | soft class; exit 0 with empty references |
| `timeout` | 124 | subprocess timeout |
| `internal` | 1 | defensive catch-all |

Soft classes:

- `workflow_dir_missing` returns exit 0 and `success=true`.
- `workflow_yaml_parse_failed` returns exit 178 only when parse failure prevents a
  trustworthy workflow graph. It still includes partial `data`.

---

## 8. Output shaping

Per-secret output is built from `SecretData` plus reference/risk analysis:

```json
{
  "name": "AWS_DEPLOY_KEY",
  "scope_kind": "repo",
  "scope_value": "owner/repo",
  "kind": "actions",
  "visibility": null,
  "num_selected_repos": null,
  "masked": null,
  "protected": null,
  "environment_scope": null,
  "updated_at": "2026-05-01T00:00:00Z",
  "referenced_in_workflows": [
    {"path": ".github/workflows/deploy.yml", "jobs": ["deploy"]}
  ],
  "risk_flags": ["long_lived_cloud_credential"]
}
```

Safety constraints:

- Never serialize a key named `value`.
- Never include raw workflow file content in `data`.
- Redact provider stderr before including it in failure envelopes if it contains
  token-like substrings; prefer generic provider error messages.
- Unit tests assert `"value" not in json.dumps(payload)`.

---

## 9. Compatibility notes

- `pyyaml>=6.0` is a runtime dependency because workflow parsing is core
  functionality.
- If mypy reports missing PyYAML stubs, add `types-PyYAML` to dev dependencies.
- `cicd_gh` and `cicd_glab` markers are shared with pipeline_analyzer.
- This task must not require pipeline_analyzer to be fully implemented at
  runtime, but it does require the shared `lib_cicd.py` shape from that sibling.
  If `lib_cicd.py` is still absent when implementation begins, create it here
  with the shared methods needed by this task and keep the pipeline_analyzer PRD
  contract intact.

---

## 10. Operational checks

Before implementation starts, verify:

- `secret_scope_audit.py` still contains `UNIMPLEMENTED_NOTE`.
- `lib_cicd.py` exists or the implementation plan includes creating it.
- `pyproject.toml` does not already have `pyyaml`.
- `gh --version` works, and `GH_BIN` can override the binary.
- `glab` may be absent; this is not a blocker for unit tests.
