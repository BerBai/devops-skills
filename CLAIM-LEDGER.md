# CLAIM-LEDGER

Every load-bearing factual claim in the documentation surface, with the primary
source that supports it. The ledger is deliberately narrower than the docs: it
records claims that can be **checked by a machine or a command**, not prose,
style or advice.

Status vocabulary:

| Status | Meaning |
|---|---|
| `verified` | The evidence was read or executed in this checkout; the command/output is in §E or §G. |
| `unverified` | The claim may be true, but this environment cannot prove it. Reason is in §F. |
| `removed` | The claim was false or unsupported and was edited out; the pre-edit location is recorded. |

Evidence types: `argparse`, `test`, `transcript`, `manifest`, `git`, `code`,
`spec` (upstream specification). "Spec" is used only for SemVer 2.0.0, Keep a
Changelog 1.1.0 and Diátaxis; all product claims are repo-local.

Line numbers are for the current working tree at commit `aa6bda6` plus this
change set. A `removed` row uses `path@aa6bda6:line` so it still points at the
text that was deleted.

---

## A. Inventory, Diátaxis placement and unindexed docs

### A.1 What exists

| # | Document | Diátaxis | Actual role | Indexed before? |
|---|---|---|---|---|
| 1 | `README.md` | Tutorial + reference | entry point, install, design principles, release status | n/a (root) |
| 2 | `README.zh-CN.md` | Tutorial + reference | translation of #1 | n/a (root) |
| 3 | `CONTRIBUTING.md` | How-to + reference | layout, conventions, status, roadmap, checks | n/a (root) |
| 4 | `CONTRIBUTING.zh-CN.md` | How-to + reference | translation of #3 | n/a (root) |
| 5 | `docs/README.md` | Reference | docs index + local commands | **no** — not linked from README |
| 6 | `docs/architecture.md` | Explanation | layer map, failure classes, state | only via CONTRIBUTING |
| 7 | `docs/offline-install.md` | How-to | air-gapped bundle/install | only via CONTRIBUTING |
| 8-15 | 8 × `plugins/*/skills/*/SKILL.md` | How-to + reference | skill entry points | yes (marketplace/index) |
| 16-42 | 27 × `plugins/*/skills/*/references/*.md` | Reference + explanation | progressive disclosure | partially (relative links from SKILL) |
| 43 | `docs/adr/ADR-001-ssh-execute.md` | Explanation / decision | recovered from the deleted Trellis blob | yes (docs/README) |
| 44 | `docs/adr/ADR-002-ssh-config-manager.md` | Explanation / decision | recovered from the deleted Trellis blob | yes (docs/README) |
| 45 | `docs/adr/ADR-003-sftp-transport.md` | Explanation / decision | recovered from the deleted Trellis blob | yes (docs/README) |
| 46 | `docs/adr/ADR-004-ssh-core-1.5.0-sync.md` | Explanation / decision | reconstructed 1.5.0 sync record | yes (docs/README) |
| 47 | `docs/adr/ADR-005-ssh-core-boundary.md` | Explanation / decision | reconstructed boundary record | yes (docs/README) |
| 48 | `docs/adr/ADR-006-python-38-floor.md` | Explanation / decision | reconstructed Python floor record | yes (docs/README) |
| 49 | `plugins/ssh-core/skills/ssh-core/references/cli-reference.md` | Reference | generated CLI/exit/failure tables | yes (SKILL.md) |
| 50 | `CHANGELOG.md` | Reference | release history + version-source rule | yes (README) |
| 51 | `SECURITY.md` | Reference | supported versions, reporting, credential model | yes (README) |
| 52 | `CLAIM-LEDGER.md` | Evidence | this file | yes (README) |
| 53 | `docs/prd/v1.0.0-post-release-hardening.md` | Planning | post-1.0.0 hardening PRD | yes (docs/README) |
| 54 | `docs/prd/v1.0.0-external-verification.md` | Planning | external-environment verification matrix | yes (docs/README) |

The 27 references are the original 27; `cli-reference.md` is the 28th and is
generated. `workdir-sandbox.md` was not plain text at the start of this pass: it
contained a literal NUL byte in a table cell (`foo\0bar`). It is now the escaped
text `foo\0bar`; the anchor and line number are unchanged.

### A.2 Duplication and misplacement matrix

| Subject | Where it appears more than once | Verdict |
|---|---|---|
| Plugin roster (8 names/roles) | `README`, `README.zh-CN`, `marketplace.json`, `CONTRIBUTING` inventory | Acceptable at index level; no single source required. |
| Release/version status | `README:71`, `CONTRIBUTING:48`, `CHANGELOG`, `marketplace.json`, `pyproject.toml`, `plugin.json` | **Was contradictory** (`v1.0` vs tag `v0.2.0`). Unified to `1.0.0`; release/tag instructions in CHANGELOG. |
| CI jobs and local commands | `CONTRIBUTING:104-117`, `docs/README:42`, `ci.yml` | Duplicated and **drifted** (three vs four jobs; ruff presented as green). Fixed and gated by the docs job. |
| Vendored files/copies | `CONTRIBUTING:118-128`, `docs/README:22-35` | Duplicated table; both now say 7 + `sync_vendored.py --check`, and the test enforces it. |
| Exit codes | `lib/exit_codes.py`, `SKILL.md:597`, `docs/architecture.md:50-64`, `references/cli-reference.md` | **Hand-copied** in SKILL/architecture. SKILL now points at the generated page; architecture keeps the range map only. |
| CLI options | Each `argparse` parser, `SKILL.md` quick reference, references, `cli-reference.md` | Hand-copied examples are now checked for flag existence by `gen_cli_reference.py --check --all`. |
| Diagnostics↔ssh-core boundary | `README:40`, `docs/architecture.md:33-45`, `CONTRIBUTING:32`, locator docstring, ADR-005 | Deliberate repetition; ADR-005 is now the single decision record. |

### A.3 Misplaced content (not rewritten, only recorded)

- `plugins/ssh-core/skills/ssh-core/SKILL.md` is 628 lines before this change
  and mixes triage with deep reference material (output pipeline, audit trail,
  daemon measurements). It violates the project's own "SKILL.md is for triage"
  rule (`CONTRIBUTING.md:38`). Splitting it is a larger edit than this pass
  allows; the line is recorded here rather than silently rewritten.
- `docs/architecture.md` duplicates the exit-code table that
  `lib/exit_codes.py` owns. The range map is kept because it is explanatory; the
  per-class table is not duplicated there after this pass.

### A.4 Unindexed docs

Before this pass, `docs/README.md`, `docs/architecture.md` and
`docs/offline-install.md` were reachable only from `CONTRIBUTING.md`, and
`docs/README.md` itself was not linked from the root README. After the change,
`README.md:60-66` links `docs/`, architecture, offline install, CHANGELOG,
SECURITY and this ledger. `check_docs_links.py --require-index` now fails if a
new `docs/**/*.md` page is orphaned.

---

## B. Claim table

### B.1 Root READMEs, CONTRIBUTING, docs/

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C01 | `README.md:7` | Eight composable plugins | manifest | `.claude-plugin/marketplace.json` `plugins[]` (8 entries) | verified |
| C02 | `README.md:35` | `ssh-core` is the only hard dependency of the others | code + test | `scripts/templates/_ssh_core_locator.py`; `tests/test_diagnose_host_unit.py:88-133` | verified |
| C03 | `README.md:45` | Every script supports `--json` and the shared envelope | test | `tests/test_manifests.py::test_script_help` (128 manifest tests pass) | verified |
| C04 | `README.md:46` | Every public script answers `--version` from `plugin.json` | test | `tests/test_manifests.py::test_script_version_is_consistent_with_plugin_manifest` | verified |
| C05 | `README.md:47` | A partial source fails the run but keeps collected data; a skipped host stays `success: true` | code | `log-aggregator/.../lib/envelope.py:54-70`; `ssh_cluster` `skipped_hosts` tests | verified |
| C06 | `README.md:78` | `v0.2.0` is the scaffold tag; `v1.0.0` tags this release | git | `git tag -l` → `v0.2.0`, `v1.0.0`; `git log -1 v0.2.0` date | verified |
| C07 | `README.md:73-75` | `ssh-core` is distributed as `1.0.0` and contains the standalone 1.5.0 feature set | manifest + code | `plugins/ssh-core/.claude-plugin/plugin.json`; `scripts/lib/version.py:9-17`, `:59`; ADR-004 version note | verified |
| C08 | `README.md:60-66` | docs/ and the release-level files are indexed | test | `scripts/check_docs_links.py --require-index` | verified |
| C09 | `README.md:71` | Marketplace version is `1.0.0` | manifest | `.claude-plugin/marketplace.json` `metadata.version` | verified |
| C10 | `README.md@aa6bda6:62` | "`v1.0` shipped all 8 plugin surfaces" | git | no `v1.0` tag; `git tag -l` only `v0.2.0`; tag message says "scaffold" | **removed** |
| C11 | `README.zh-CN.md:7` | Same roster as English | test | `scripts/check_docs_parity.py` (headings/rows/code/links) | verified |
| C12 | `README.zh-CN.md@aa6bda6:62` | "`v1.0` 已发布" | git | same as C10 | **removed** |
| C13 | `CONTRIBUTING.md:48` | V1 CLI surface implemented; all plugins `1.0.0`; `v1.0.0` tag created from the release commit | git + test | `git tag -l` (`v0.2.0`, `v1.0.0`); `pytest tests/test_manifests.py` | verified |
| C14 | `CONTRIBUTING.md:53` | All eight plugins have a V1 CLI surface | test | 128 `--help`/`--version`/manifest tests pass | verified |
| C15 | `CONTRIBUTING.md:56` | `ssh-core` is the remote-connection sub-function | code | locator docstring; `docs/architecture.md:29-38` | verified |
| C16 | `CONTRIBUTING.md:91` | `uv.lock` is generated and resolves the 3.8 floor + extras | file | `uv.lock:3` (`requires-python = ">=3.8"`), `uv.lock:773-774` (`devops-skills 1.0.0`) | verified |
| C17 | `CONTRIBUTING.md:104` | CI runs four jobs | file | `.github/workflows/ci.yml` `jobs:` = test, lint, floor, docs | verified |
| C18 | `CONTRIBUTING.md:111` | The docs job runs the link/parity/CLI gates, the example whitelist and markdownlint | file | `ci.yml` `docs` step list | verified |
| C19 | `docs/README.md:16-17` | ADR-004/005 exist and are linked | file + test | `docs/adr/*.md`; link gate | verified |
| C20 | `docs/README.md:30-31` | 7 copies of each vendored helper | test | `sync_vendored.py --check` → `14 copies match`; `tests/test_manifests.py::test_skill_copies_are_identical` | verified |
| C21 | `docs/README.md:33` | `sync_vendored.py --check` is what the `test` job runs | code + test | `tests/test_manifests.py:237-272` imports `sync_vendored.check_all` | verified |
| C22 | `docs/README.md:47` | `pytest tests/ -q` is green: 2009 passed, 151 skipped | transcript | §E.2 | verified |
| C23 | `docs/README.md:48` | Ruff is gated by a 380-finding ratchet; raw `ruff check .` still reports 380 | transcript + script | §E.3; `tools/check_ruff_baseline.py` | verified |
| C24 | `docs/README.md:49` | Floor checker is green: 216 files, 0 problems | transcript | §E.3 | verified |
| C25 | `docs/README.md@aa6bda6:40` | `ruff check .` is a runnable local gate | transcript | §E.3: exits 1 with 444 findings | **removed** |
| C26 | `docs/architecture.md:31` | Diagnostics shell out to `ssh_execute.py` and never import ssh-core | code + test | `tests/test_diagnose_host_unit.py:9-10`; locator docstring | verified |
| C27 | `docs/architecture.md:50-64` | Failure-code ranges per owner | code | `lib/exit_codes.py`; `log-aggregator/lib/envelope.py:62-78`; `module_validate.py:25-31`; `pipeline_analyzer.py:77-85`; `ssh-guarded/run_request.py:83-92` | verified |
| C28 | `docs/architecture.md:69-87` | Argument errors also emit `exit_code: 2` / `invalid_arguments` | test | `tests/test_ssh_core_contracts.py`; parser envelope tests | verified |
| C29 | `docs/architecture.md:93` | Config writes are locked, validated, `.bak`-backed | test | `tests/test_ssh_config_manager_unit.py` (write path); not executed live (§F) | verified (unit) |
| C30 | `docs/offline-install.md:13` | Offline build/install scripts exist | file | `scripts/build_offline_bundle.sh`, `scripts/install_offline_linux.sh` | verified |
| C31 | `docs/offline-install.md:24-28` | Bundle contains `plugins/`, `.claude-plugin/`, READMEs, CONTRIBUTINGs, pyproject, scripts | code | `scripts/build_offline_bundle.sh` copy list | verified |
| C32 | `docs/offline-install.md:45` | Installer copies the repo into `$PREFIX/repo/` | code | `scripts/install_offline_linux.sh` | verified |
| C33 | `docs/offline-install.md:65` | `run.sh selfcheck.py` works | transcript | §E.1: exit 0, version 1.0.0 | verified |
| C34 | `docs/offline-install.md:69` | `doctor.py --require` exits 9 on a regression | test + code | `doctor.py --require` path; `tests/test_ssh_core_slice_json_and_doctor_profile.py` | verified |
| C35 | `docs/offline-install.md:71` | Launcher falls back to `$DSH_PYTHON` / harness Python | code + transcript | `scripts/run.sh`; run picked Python 3.9.6 when venv was absent | verified |

### B.2 ssh-core SKILL and references

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C36 | `ssh-core/SKILL.md:16` | `selfcheck.py` reports tools; `doctor.py` exercises them | transcript | §E.1 both exit 0; `selfcheck` lists 12 checks | verified |
| C37 | `ssh-core/SKILL.md:49` | `doctor` has OK / DEGRADED / UNKNOWN | code | `doctor.py` status vocabulary | verified |
| C38 | `ssh-core/SKILL.md:82` | `--resume`/`--bwlimit`/`--delete` are refused with 166 without SFTP | code | `exit_codes.py` `mode_deferred: 166`; transfer tests | verified |
| C39 | `ssh-core/SKILL.md:173` | Password auth with no determinable user → `user_required` 115 | code | `exit_codes.py:81`, `:163`; `tests/test_ssh_core_session.py` | verified |
| C40 | `ssh-core/SKILL.md:507` | A fresh handshake costs ~200–300 ms | transcript | §E.1: `elapsed_s: 0.292` for `uname` on `pai` | verified (one sample) |
| C41 | `ssh-core/SKILL.md:544` | stdout/stderr cap is 10 MB per stream | code | `lib/__init__.py:677` `MAX_STREAM_BYTES = 10 * 1024 * 1024` | verified |
| C42 | `ssh-core/SKILL.md:546` | `--timeout`/`--connect-timeout` below 1 → `invalid_option` 107 | transcript | §E.1: `--timeout 0` and `-1` → exit 107 | verified |
| C43 | `ssh-core/SKILL.md:549` | A signal-killed child is reported as 128+N | transcript | §E.1: local SIGTERM → `exit_code: 143`, `terminated_by_signal: 15` | verified |
| C44 | `ssh-core/SKILL.md:580` | `subprocess.run/Popen(argv_list)`, never shell strings | test | `tests/test_port_check_unit.py`, `test_diagnose_host_unit.py` invariants | verified |
| C45 | `ssh-core/SKILL.md:588` | Host keys are strict by default | code | `ssh_execute.py:631-633`; `lib/transfer.py:143-145` | verified |
| C46 | `ssh-core/SKILL.md:600` | `references/cli-reference.md` is generated | test | `gen_cli_reference.py --check --all` → OK | verified |
| C47 | `ssh-core/SKILL.md:621` | Python 3.8+, stdlib + `scripts/lib/` + optional paramiko | file + test | `pyproject.toml` `requires-python`; `tools/check_python_floor.py` green; `lib/transfer.py` paramiko import | verified |
| C48 | `ssh-core/SKILL.md:353` | Daemon reuse is "≈3x faster" | none | no benchmark in this pass | **unverified** |
| C49 | `ssh-core/references/platform-matrix.md:11` | "This machine" matrix is generated by `selfcheck --write-matrix` | argparse | `selfcheck.py --help` advertises `--write-matrix` | verified |
| C50 | `ssh-core/references/recipes.md:35` | `--script-file` is a real flag | argparse | `ssh_execute.py --help`; `gen_cli_reference --check` | verified |
| C51 | `ssh-core/references/safety.md:33` | Every script emits the envelope on argument errors | test | `tests/test_ssh_core_contracts.py`; `ssh_download --overwrite` transcript (§E.1) | verified |
| C52 | `ssh-core/references/troubleshooting.md:39` | `user_required` is exit 115 | code | `exit_codes.py` | verified |
| C53 | `ssh-core/references/workflows.md:17-24` | `--allow-exit 0,1` keeps the real exit code and flips `success` | argparse + code | `ssh_execute.py --help`; `tests/test_ssh_core_output_pipeline.py` | verified |

### B.3 ssh-guarded

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C54 | `ssh-guarded/SKILL.md:108` | `success: true` means the action completed, not "dispatched" | test | `tests/test_ssh_guarded_run_request_unit.py` transport-failure cases | verified |
| C55 | `ssh-guarded/SKILL.md:122` | `result.json` is written on success and failure | test | `tests/test_ssh_guarded_run_request_unit.py:593-618` | verified |
| C56 | `ssh-guarded/SKILL.md:128` | Six top-level keys including `error` | code + test | `lib/envelope.py`; `tests/test_ssh_guarded_envelope_unit.py` | verified |
| C57 | `ssh-guarded/SKILL.md:153` | Redaction covers HostName/User/Port/IdentityFile/paths/secrets | code | `lib/redact.py`; `references/redact.md` | verified |
| C58 | `ssh-guarded/SKILL.md:166` | Writes are confined to `~/workspace/<project_id>/` | code | `lib/sandbox.py`, `lib/remote_path.py` | verified |
| C59 | `ssh-guarded/SKILL.md:205` | `scan_software.py` caches under `~/.devops/cache/<host>/software.json` | code | `scan_software.py` cache path | verified |
| C60 | `ssh-guarded/SKILL.md:245` | `gc_artifacts.py` dry-run is the default | argparse + code | `gc_artifacts.py --help`; `lib/gc.py` | verified |
| C61 | `ssh-guarded/references/workdir-sandbox.md:30` | Null bytes are rejected | code | `lib/remote_path.py`; table row at `:93` now shows escaped `\0` | verified |
| C62 | `ssh-guarded/references/request-execute.md:11` | Artifacts live under `$SSH_GUARDED_HOME/reports/<project>/requests/` | code | `lib/request_artifact.py`; `docs/architecture.md:97` | verified |
| C63 | `ssh-guarded/references/redact.md:21` | Secret regex catalog includes AWS/GitHub/JWT/key headers | code | `lib/redact.py` catalog | verified |

### B.4 remote-debug

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C64 | `remote-debug/SKILL.md:30` | Remote probes are forwarded through `ssh_execute.py` | code | `diagnose_host.py` `_run_probe`; `tests/test_diagnose_host_unit.py` | verified |
| C65 | `remote-debug/SKILL.md:37-40` | Local errors have distinct classes (`local_shell_error`, `dependency_missing`, `local_not_found`, skip) | code | `_local_exec.py`; `tests/test_local_route_platform.py` | verified (code); 2 DNS tests red (§F) |
| C66 | `remote-debug/SKILL.md:59-63` | `--target-os` auto/linux/windows probe families | argparse + code | `diagnose_host.py --help`; `tests/test_diagnose_host_unit.py` | verified |
| C67 | `remote-debug/SKILL.md:153` | `--follow-timeout` default 600, floor 5 | argparse | `tail_log.py --help` (default 600); parser guard | verified |
| C68 | `remote-debug/SKILL.md:165` | `port_check` runs from `<alias>`, not the laptop | code | `port_check.py` remote branch | verified |
| C69 | `remote-debug/SKILL.md:184-187` | UDP `refused` / `open-or-filtered` / `source-error` | code + test | `port_check._classify_status`; `tests/test_port_check_unit.py` | verified |
| C70 | `remote-debug/references/common_issues.md:15` | High load vs CPU-pinned symptom tree | doc-only | no executable assertion; advisory content | **unverified** |
| C71 | `remote-debug/references/incident_response.md:5` | SEV1–SEV4 classification | doc-only | process advice, no code path | **unverified** |
| C72 | `remote-debug/references/linux_diagnostics.md:5` | USE-method 60-second triage | doc-only | commands, no assertion | **unverified** |
| C73 | `remote-debug/references/network_debug.md:5` | Layer 1-4 network walk-through | doc-only | commands, no assertion | **unverified** |

### B.5 k8s-debug

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C74 | `k8s-debug/SKILL.md:29-44` | Four scripts and their entry signatures | argparse | each `--help`; `tests/test_manifests.py` | verified |
| C75 | `k8s-debug/SKILL.md:47-50` | Pending releases emit `data.stuck_resources` | code | `helm_status.py:490-527` | verified |
| C76 | `k8s-debug/SKILL.md:56` | Remote route uses `ssh_execute.py <alias> "kubectl ..."` | code | `lib/kubectl.py` | verified |
| C77 | `k8s-debug/SKILL.md:80-101` | JSON data shape (`summary`, `findings`, `raw`) | code | script envelope builders; no live cluster (§F) | verified (code) |
| C78 | `k8s-debug/references/common_issues.md:37` | OOMKilled section is the `#oomkilled` hint target | code + doc | `diagnose_pod.py` hint string; heading anchor | verified |
| C79 | `k8s-debug/references/pod_lifecycle.md:11` | Pending phase diagnostics | doc-only | no live cluster | **unverified** |
| C80 | `k8s-debug/references/networking.md:11` | Service/endpoint diagnosis | doc-only | no live cluster | **unverified** |

### B.6 docker-quick

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C81 | `docker-quick/SKILL.md:58` | `--runtime auto` picks docker then podman locally; remote always docker | code | `_runtime.py:31-39` | verified |
| C82 | `docker-quick/SKILL.md:61` | Missing runtime → `dependency_missing` exit 3 | code | `_runtime.py`; `tests/test_inspect_container_unit.py` | verified |
| C83 | `docker-quick/SKILL.md:96` | All scripts support the shared `--json` envelope | test | `tests/test_manifests.py` `--help`/`--version` | verified |
| C84 | `docker-quick/references/container_issues.md:7` | Exit 137 = OOMKilled | code | `inspect_container.py` classifier; test fixtures | verified |
| C85 | `docker-quick/references/compose_debug.md:19` | `depends_on` does not wait for readiness | doc-only | advisory; no executable assertion | **unverified** |
| C86 | `docker-quick/references/image_optimization.md:29` | `image_audit` checks the listed items | code | `image_audit.py` checks | verified |

### B.7 log-aggregator

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C87 | `log-aggregator/SKILL.md:61` | `--lines` default is 10000 | argparse | `tail_multi.py --help` | verified |
| C88 | `log-aggregator/SKILL.md:107` | Partial fetch → `success: false`, exit 41 | code | `lib/envelope.py:54-70` | verified |
| C89 | `log-aggregator/SKILL.md:112` | All three scripts use the same rule | code | `lib/envelope.py` imported by all three | verified |
| C90 | `log-aggregator/SKILL.md:119` | `fetcher_failed` is an accepted alias, no longer emitted | code | `envelope.py` `ALIASES` / `EXIT_CODES` | verified |
| C91 | `log-aggregator/references/sources.md:12` | `journal://`, `docker://`, `kube://`, `file://` grammar | code | `lib/source_spec.py`; `tests/test_tail_multi_unit.py` | verified |
| C92 | `log-aggregator/references/correlation_patterns.md:5` | Trace-ID is the strongest correlation | doc-only | advisory | **unverified** |
| C93 | `log-aggregator/references/noise_reduction.md:5` | Default filters drop healthchecks/static assets | code | `lib/filters.py`; `tests/test_tail_multi_unit.py` | verified |

### B.8 iac-state

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C94 | `iac-state/SKILL.md:61` | Tool autodetect: tofu, else terraform; `--tool` overrides | code | `lib/terraform.py:82-95` | verified |
| C95 | `iac-state/SKILL.md:54-57` | Remote module files are fetched and parsed locally | code | `module_validate.py` remote read path | verified |
| C96 | `iac-state/SKILL.md:36` | `plan -detailed-exitcode` drives drift classification | code | `drift_check.py`; `tests/test_drift_check_unit.py` | verified |
| C97 | `iac-state/references/drift_patterns.md:5` | exit 0/1/2 meaning for plan | code | `drift_check.py`; `tests/test_drift_check_unit.py` | verified |
| C98 | `iac-state/references/state_health.md:47` | State-lock failure diagnosis | doc-only | no terraform/tofu installed | **unverified** |
| C99 | `iac-state/references/module_audit.md:5` | Pre-upgrade module checklist | doc-only | advisory | **unverified** |

### B.9 cicd-debug

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C100 | `cicd-debug/SKILL.md:36` | GitHub heartbeat returns `null`; GitLab supports it | code | `runner_check.py` provider branches | verified |
| C101 | `cicd-debug/SKILL.md:56` | Remote route runs `gh`/`glab` through ssh-core | code | `lib_cicd.py` remote branch | verified |
| C102 | `cicd-debug/SKILL.md:267` | v1.0 risk flags are the four listed | code | `secret_scope_audit.py:402-429` | verified |
| C103 | `cicd-debug/references/pipeline_failures.md:7` | `#action-version-drift` hint anchor | code + doc | `pipeline_analyzer.py` hint; explicit `{#action-version-drift}` | verified |
| C104 | `cicd-debug/references/runner_debug.md:43` | `#runner-check-findings` anchor | code + doc | `runner_check.py` hint; heading anchor | verified |
| C105 | `cicd-debug/references/secret_scopes.md:5` | Four GitHub scopes + fork model | doc-only | no GitHub/GitLab API access | **unverified** |

### B.10 ADRs, CHANGELOG, SECURITY

| ID | doc:line | Assertion | Evidence type | Evidence location | Status |
|---|---|---|---|---|---|
| C106 | `ADR-004:8-20` | The 1.4.0 ADR text was deleted in `1a6eade`; recovered from `1a6eade^` | git | `git show 1a6eade^:.trellis/spec/backend/adr-004-ssh-core-140-sync.md` | verified |
| C107 | `ADR-004:113-125` | 1.5.0 adds doctor/slice/launchers; 41 files, +5549/−308 | git | `git show --stat d46dee9`; `git diff --name-status` | verified |
| C108 | `ADR-004:101-103` | 114/115 are registered failure classes | code | `exit_codes.py:77-81`, `:162-163` | verified |
| C109 | `ADR-005:60-75` | `diagnose_host` shipped with shell-out in `4a766a5` | git | commit subject + `git show --stat 4a766a5` | verified |
| C110 | `ADR-005:47-55` | The locator resolves a path and never imports | code | `scripts/templates/_ssh_core_locator.py:1-28` | verified |
| C111 | `CHANGELOG.md:5-6` | SemVer 2.0.0 + Keep a Changelog 1.1.0 are the formats | spec | upstream specs; anchors in the file | verified |
| C112 | `CHANGELOG.md:11-24` | Marketplace triple is tag + marketplace + pyproject; uv.lock is generated at 1.0.0 | git + file | `git tag`; `marketplace.json`; `pyproject.toml`; `uv.lock:3`, `:773-774` | verified |
| C113 | `CHANGELOG.md:118-120` | At `v0.2.0`, `ssh-core` was `0.2.0` | git | `git show v0.2.0:plugins/ssh-core/.claude-plugin/plugin.json` | verified |
| C114 | `SECURITY.md:19-27` | Passwords never enter argv/envelope; askpass is 0600 | code | `lib/askpass.py`; `SKILL.md:280-287` | verified |
| C115 | `SECURITY.md:28-40` | Host keys strict; `-o` denylist | code | `ssh_execute.py:631-633`; denylist in `lib/` | verified |
| C116 | `pyproject.toml:8`; `ci.yml:18` | ADR-006 (Python floor) exists | file | `docs/adr/ADR-006-python-38-floor.md` (reconstructed from `pyproject.toml`, CI matrix, `tools/check_python_floor.py`) | verified |
| C117 | `docs/README.md:48` | The lint gate is a ratchet, not a raw `ruff check .` | code + transcript | `tools/check_ruff_baseline.py`; baseline 380; §E.3 | verified |
| C118 | `docs/README.md:18` | ADR-001/002/003 were recovered verbatim from `1a6eade^` and ADR-006 was reconstructed | git + file | `git show 1a6eade^:.trellis/spec/backend/adr-00{1,2,3}-*.md`; `docs/adr/ADR-006-python-38-floor.md` | verified |
| C119 | `docs/README.md:52-53` | Vale is 0 on all 54 markdown files | code + transcript | `.vale.ini`; `.vale/styles/config/vocabularies/devops/accept.txt`; §E.4 | verified |

---

## C. Version consistency

| Source | Value | Evidence | Aligned? |
|---|---|---|---|
| git tag | `v1.0.0` (release commit); `v0.2.0` historical | `git tag -l`; `CHANGELOG.md` | yes |
| `marketplace.json` `metadata.version` | `1.0.0` | file | yes |
| `pyproject.toml` `[project].version` | `1.0.0` | file | yes |
| `plugins/ssh-core/.claude-plugin/plugin.json` | `1.0.0` | file | unified with marketplace |
| matching marketplace entry | `1.0.0` | file | yes |
| other seven `plugin.json` | `1.0.0` | files | yes |
| matching marketplace entries | `1.0.0` | file | yes |
| `ssh-core` `lib/version.py` | `1.0.0` (standalone feature history up to 1.5.0) | `version.py:9-17`, `:59` | yes, with explicit identity note |
| `uv.lock` `devops-skills` | `1.0.0`, `requires-python >=3.8` | `uv.lock:3`, `:773-774` | yes (regenerated with `uv lock`) |
| `CHANGELOG.md` top released entry | `1.0.0` | file | yes |
| `CHANGELOG.md` `[Unreleased]` | empty | file | n/a |

The per-plugin agreement is enforced by
`tests/test_manifests.py::test_marketplace_entry_version_matches_plugin_json`.
The full triple (tag / marketplace / pyproject / plugin.json / version.py) has
no single automated test; the release process records the check here and in
`CHANGELOG.md`. `v1.0.0` is an annotated tag on the release commit.

---

## D. Gate state after the R1/R2 hardening

| Gate | Measured result | Command | Where |
|---|---|---|---|
| `pytest tests/ -q` | `2009 passed, 151 skipped, 123 subtests passed in 45.54s` | `.venv/bin/python -m pytest tests/ -q -o addopts=''` | §E.2 |
| `ruff check .` (raw) | `Found 380 errors.` (101 fixable) | `.venv/bin/ruff check .` | §E.3 |
| ruff ratchet | `ruff baseline: 380/380 findings, 24 rule codes` → `ruff ratchet: OK` | `.venv/bin/python tools/check_ruff_baseline.py` | §E.3 |
| `pytest tests/test_manifests.py` | `128 passed` | `.venv/bin/python -m pytest tests/test_manifests.py -q -o addopts=''` | §E.3 |

The four former failures are fixed deterministically:

1. `test_unresolvable_host_is_a_host_error` — mocks
   `socket.getaddrinfo` to raise `gaierror`; no real DNS.
2. `test_udp_problem_never_raises` — same `gaierror` mock.
3. `test_shell_invocation_names_the_inline_wrapper` — `_shell_name()` uses
   `PureWindowsPath` for tokens with a backslash/drive prefix, so the assertion
   holds on POSIX too.
4. `test_partial_results_still_ship_in_data` — asserts against the actual
   `data.per_source` key instead of recomputing `local + payload`.

The raw ruff count is still 380; that is the tracked backlog, not a passing
`ruff check .`. The `lint` job runs the ratchet, which fails on any increase
and can only go down.

---

## E. Execution records

Environment: macOS 15 (`Darwin 25.6.0`, arm64), local IP `192.168.2.118`,
Python 3.11.14 (`.venv`), OpenSSH 10.3p1. Remote: `pai` = `192.168.2.115`,
Debian 13.5, kernel `7.0.13+deb13-amd64`, Python 3.13.5. The CLAUDE.md rule was
satisfied: the local address is in `192.168.2.0/24`, so the live phase ran.

### E.1 Live commands and exit codes

| # | Command (abridged) | Result | Exit |
|---|---|---|---|
| 1 | `selfcheck.py --json` | 12 checks, `version: 1.0.0`, all required present | 0 |
| 2 | `doctor.py --json` | 12 probes, `works` 12, `does_not_work` 0 | 0 |
| 3 | `ssh_execute.py pai "uname -srm" --json` | `Linux 7.0.13+deb13-amd64 x86_64`, `elapsed_s: 0.292` | 0 |
| 4 | `ssh_execute.py pai "exit 42" --json` | `exit_code: 42`, `failure_class: remote_error` | 42 |
| 5 | `ssh_slice.py pai /etc/debian_version --lines 1 --json` | `13.5`, `content_sha256` set | 0 |
| 6 | `ssh_cluster.py "hostname" --hosts pai --json` | `ok_hosts: ["pai"]`, `stdout: "pai\n"` | 0 |
| 7 | `ssh_config_manager.py list --format json` | 5 host blocks, redacted fields | 0 |
| 8 | `audit.py summarize --since 24h` | 15 calls, 0 failures, `worst: none` | 0 |
| 9 | `diagnose_host.py pai --json` | `severity=ok`, 5/5 probes | 0 |
| 10 | `port_check.py pai --target 127.0.0.1 --ports 22` | `open (447ms)` | 0 |
| 11 | `ssh_upload.py pai /tmp/docs-upload.txt /tmp/docs-upload.txt --overwrite --json` | SFTP, 22 bytes | 0 |
| 12 | `ssh_download.py pai ... /tmp/docs-download.txt --overwrite --json` | **negative control**: `--overwrite` is not a download flag | 2 |
| 13 | `ssh_download.py pai ... /tmp/docs-download.txt --json` | SFTP, 22 bytes; `cmp` matched | 0 |
| 14 | `run.sh selfcheck.py --json` | launcher picked system Python 3.9.6, `version: 1.0.0` | 0 |
| 15 | `ssh_tunnel.py start pai --remote-port 3306 --local-port 13306 --idle-timeout 5 --json` | forward up, pid returned | 0 |
| 16 | `ssh_tunnel.py list --json` / `stop pai --json` | running, then stopped | 0 / 0 |
| 17 | `ssh_execute.py pai "printf 'a\r\nb\r\n'" --json` | `stdout: "a\r\nb\r\n"` (CRLF preserved) | 0 |
| 18 | `ssh_execute.py pai ... --timeout 0.5 --json` | **negative control**: int parse error, `invalid_arguments` | 2 |
| 19 | `ssh_execute.py pai "kill -TERM $$" --json` | remote shell killed; `remote_error` | 104 |
| 20 | `ssh_execute.py pai -- "-v" --json` | remote `bash: -c: option requires an argument` (the example is about argv, not success) | 2 |
| 21 | `ssh_execute.py pai "sleep 2" --timeout 0 --json` | `invalid_option: --timeout` | 107 |
| 22 | `ssh_execute.py pai "echo hi" --connect-timeout 0 --json` | `invalid_option: --connect-timeout` | 107 |
| 23 | `ssh_execute.py local "sh -c 'kill -TERM $$'" --json` | `exit_code: 143`, `terminated_by_signal: 15` | 143 |
| 24 | `tail_multi.py --help` | default `--lines 10000` | 0 |
| 25 | `tail_log.py --help` | `--follow-timeout` default 600 | 0 |

Commands 12, 18, 19 and 20 are deliberately negative controls: they show what
the tool does with a bad flag, a remote signal and an argv that starts with `-`.
They are not "passing examples".

### E.2 Test suite

```text
$ .venv/bin/python -m pytest tests/ -q -o addopts=''
2009 passed, 151 skipped, 123 subtests passed in 45.54s
```

### E.3 Lint / floor / sync / manifest

```text
$ .venv/bin/ruff check .
Found 380 errors.
[*] 101 fixable with the `--fix` option (14 hidden fixes can be enabled with the `--unsafe-fixes` option).

$ .venv/bin/python tools/check_ruff_baseline.py
ruff baseline: 380/380 findings, 24 rule codes
ruff ratchet: OK

$ .venv/bin/python tools/check_python_floor.py
scanned 216 files; 0 problem(s)

$ .venv/bin/python scripts/sync_vendored.py --check
sync_vendored: 14 copies match their templates

$ .venv/bin/python -m pytest tests/test_manifests.py -q -o addopts=''
128 passed in 8.46s
```

### E.4 Docs gates

```text
$ python scripts/check_docs_links.py --require-index
checked 54 files, 68 relative links
external links: 8 unique (checked: False)
links: OK (0 broken, 0 bad anchors, 0 orphans)

$ python scripts/check_docs_parity.py
README.md <-> README.zh-CN.md: headings=same table_rows=same code_blocks=same links=same -> OK
CONTRIBUTING.md <-> CONTRIBUTING.zh-CN.md: headings=same table_rows=same code_blocks=same links=same -> OK
parity: OK (2 pair(s))

$ python scripts/gen_cli_reference.py --check --all
ssh-core CLI reference: OK (45621 bytes)
CLI inventory: OK (8 plugins, ssh-core reference current)

$ python scripts/gen_cli_reference.py --examples
examples: 27 run (all exit 0), 482 skipped with a reason

$ npx --yes markdownlint-cli@0.44.0 --config .markdownlint.jsonc \
    "README.md" "README.zh-CN.md" "CONTRIBUTING.md" "CONTRIBUTING.zh-CN.md" \
    "CHANGELOG.md" "SECURITY.md" "CLAIM-LEDGER.md" "docs/**/*.md" \
    "plugins/*/skills/*/SKILL.md" "plugins/*/skills/*/references/*.md"
(no output; exit 0)

$ npx --yes --package @vvago/vale@3.25.1 vale --config=.vale.ini \
    README.md README.zh-CN.md CONTRIBUTING.md CONTRIBUTING.zh-CN.md \
    CHANGELOG.md SECURITY.md CLAIM-LEDGER.md docs \
    "plugins/*/skills/*/SKILL.md" "plugins/*/skills/*/references/*.md"
✔ 0 errors, 0 warnings and 0 suggestions in 54 files.
```

`markdownlint` is green under the committed `.markdownlint.jsonc` rule set
(0 findings across 54 markdown files). The default rule set is not used because
it would require reformatting the 27 untouched reference files; the disabled
rules are named in the config, not hidden. Vale is green on all 54 markdown files under the committed `.vale.ini`
vocabulary; the 8 `SKILL.md` descriptions are quoted so Vale can parse their
frontmatter, and the technical identifiers are in the reviewed vocabulary.

---

## F. Unverified list

Each row says **why** the claim could not be verified in this environment, not
that it is false.

| Claim / area | Why it cannot be verified here |
|---|---|
| `daemon ≈3x faster` (`SKILL.md:353`) | No benchmark was run; the only timing measured was a single `ssh_execute` call (0.292 s). Speedup depends on RTT and host load. |
| All `prod-web-01` / `prod-db` / `web-1,web-2,web-3` examples | Those aliases do not exist; running them would contact or mutate production hosts. The syntax is checked by `gen_cli_reference.py`, behaviour is not. |
| k8s-debug live behaviour | No Kubernetes cluster / `kubectl` on this host; `live_k8s` tests are skipped by design (`pyproject.toml` markers). |
| docker-quick live behaviour | No Docker daemon was exercised; only code paths and unit tests. |
| iac-state live behaviour | `terraform`/`tofu` are not installed; `live_iac` tests are skipped. |
| cicd-debug live behaviour | No `gh`/`glab` login or repository permission; `cicd_gh`/`cicd_glab` tests are skipped. |
| Windows-specific routes | No Windows host. The Windows path syntax is now covered on POSIX by `test_shell_invocation_names_the_inline_wrapper`; live Windows behavior is still unverified. |
| Password-auth route | No password-only host and no `SSH_PASSWORD` credential was used. `askpass` was exercised only by `doctor.py` staging. |
| `ssh-guarded` execute/mutation flow | Running `run_request.py --execute` would write to a host; only unit tests (`test_ssh_guarded_run_request_unit.py`) were used. |
| Offline bundle build | `build_offline_bundle.sh` downloads wheels; no connected build was run in this pass. |
| External URLs (8 unique) | `check_docs_links.py` does not fetch by default; `--external` was not run because the network is not a stable gate input. |
| UDP firewall semantics | `open-or-filtered` needs a network that drops packets; not reproduced. |
| Reconstructed ADR-006 rationale | The file is built from `pyproject.toml`, CI and the floor checker; no historical ADR-006 text exists, so any rationale beyond those files is not evidenced. |
| Vale vocabulary maintenance | Full-tree Vale is 0 because product names and code identifiers are accepted in `.vale/styles/config/vocabularies/devops/accept.txt`. A prose typo that happens to resemble a listed term would be masked; the vocabulary is therefore reviewed like code and should stay narrow. |

---

## G. Gate status summary

| Gate | Status at the time of this ledger |
|---|---|
| `check_docs_links.py --require-index` | green (0 broken, 0 bad anchors, 0 orphans) |
| `check_docs_parity.py` | green (2 pairs) |
| `gen_cli_reference.py --check --all` | green (ssh-core page current; 8-plugin inventory) |
| `sync_vendored.py --check` | green (14 copies) |
| `tools/check_python_floor.py` | green (216 files, 0 problems) |
| `markdownlint-cli` (project rule set) | green (0 findings, 54 files) |
| `vale` (54 markdown files) | green (0 errors, 0 warnings, 0 suggestions) |
| `gen_cli_reference.py --examples` | green (27 run, 482 annotated skips) |
| `pytest tests/test_manifests.py` | green (128 passed) |
| `pytest tests/ -q` | green (2009 passed, 151 skipped) |
| `ruff check .` raw | red by design (380), ratchet green |
| `tools/check_ruff_baseline.py` | green (380/380; fails on any increase) |

---

## H. Decisions a human still has to make

1. **`ssh-core` semver signal:** the unified `1.0.0` is lower than the standalone
   `1.5.0`; document the “distribution version vs feature history” split wherever
   a consumer compares versions (the `version.py` note and ADR-004 do this now).
2. **Vale vocabulary review:** the full-tree gate is green because
   `.vale/styles/config/vocabularies/devops/accept.txt` accepts the technical
   terms. Keep the list narrow; a broad dictionary would hide real typos.
