# ship ssh-core deploy_pubkey v1.0

## Goal

Install a local public key on a remote host so subsequent `ssh-core` calls go through the fast key-auth path. This is the **bootstrap** step — by definition, the remote does **not yet** have our key, so first-touch auth must use something other than the key we're about to install.

Sibling of the already-shipped v1.0 trio (`ssh_execute` 257 loc, `ssh_cluster` 619 loc, `ssh_config_manager` 1055 loc); expected to follow the same conventions (lib helpers, JSON envelope, exit codes, ruff/mypy clean, unit + live_ssh integration tests).

## Confirmed facts (from code / spec)

- **`ssh_execute` v1.0 is BatchMode-only** (`adr-001-ssh-execute.md` D2 + Negative consequences): no paramiko, no password auth, no daemon. Users with password-only targets are told to "arrange key auth or use `sshpass` themselves." → `deploy_pubkey` **cannot delegate first-touch auth to `ssh_execute`**.
- **`ssh_config_manager` v1.0 metadata keys** are `description / environment / tags / location` only (`ssh_config_manager.py:125`). The string `password` does not appear anywhere in `ssh_config_manager.py`. There is no `# password:` parser in v1.0.
- **`safety.md` policy**: "No password storage in our own files. If the user records one in a comment in `~/.ssh/config`, that's their decision; we read it but never write it." Also: "No auto-`ssh-copy-id`. Public key deployment requires human intent."
- **`workflows.md §6`** documents `# password` in a config comment as the trigger for the paramiko+daemon path — but that path itself is not shipped in v1.0.
- **`lib/__init__.py`** exposes the stable helpers `json_result`, `emit`, `msys_safe_env`, `alias_state_path`, `filter_ssh_noise`, `classify_failure`. No paramiko import anywhere in `lib/` for v1.0.
- **`lib/sftp_transport.py`** (538 loc) exists — added by ADR-003 for `ssh_upload`/`ssh_download` v1.0 — and uses paramiko v3+. So paramiko **is** a v1.0 runtime dep for the project (just not for `ssh_execute`).
- **`ssh_config_manager` defines its own `failure_class` namespace** (`not_found / already_exists / config_missing / parse_error / permission / internal`, see `ssh_config_manager.py:51,90`), independent of `lib/classify_failure`'s 4-class set. → per-script `failure_class` taxonomy is the established pattern.
- **`live_ssh` integration tests never run in CI** (`tests/README.md`); they assume ssh-agent + key auth (`POC_HOST=pai`), no password / sshpass anywhere.

## Design decisions (locked)

### D1. First-touch auth path: `ssh-copy-id` + native ssh, inherit controlling tty

- Implementation primitive: `subprocess.run(["ssh-copy-id", "-i", <pubkey>, ...ssh_opts, <alias>], stdin=None, stdout=PIPE, stderr=PIPE, text=True, timeout=N)`. Parent terminal handles the password prompt exactly once. `ssh-copy-id` itself does `mkdir -p ~/.ssh && chmod 700`, `touch ~/.ssh/authorized_keys && chmod 600`, and `grep`-based dedupe before append. We add the JSON envelope and failure classification.
- **No paramiko in `deploy_pubkey`.** `lib/sftp_transport.py` already imports paramiko for ssh_upload/ssh_download — we deliberately do not extend that import surface into the bootstrap path. ssh_execute / ssh_cluster / deploy_pubkey stay on native OpenSSH binaries.
- **Non-TTY refuses cleanly.** Detect `sys.stdin.isatty() is False`; if true AND `ssh-copy-id` exits non-zero on a `Permission denied` / password-related stderr pattern, emit `failure_class="no_tty_for_password"` with remediation hint ("re-run from an interactive terminal, or run `ssh-copy-id` manually then re-invoke deploy_pubkey").
- **`ssh-copy-id` not found** → `failure_class="tool_missing"` with the missing binary named in the message.
- Rationale: aligns with ADR-001 A1 (paramiko rejected for ssh_execute v1.0), ADR-001 A2 (no custom daemon), and `safety.md` §28 ("Public key deployment requires human intent").

### D2. `--strip-password` is **removed** in v1.0

- Surface: the flag from the stub (`scripts/deploy_pubkey.py:31`) is **not** present in v1.0.
- Rationale: `ssh_config_manager` v1.0 has no `# password:` parser, `ssh_execute` v1.0 is BatchMode-only — nothing in v1.0 reads the `# password:` comment, so "strip after deploy" has no operational meaning today. Deferred to v1.1 (alongside `ssh_config_manager` schema extension and the `ssh_execute` password+daemon path restoration). Removing the flag also lets `deploy_pubkey` stay single-purpose ("ship the key") without taking on `~/.ssh/config` write-back responsibility.
- Follow-up: a TODO line is added in `references/workflows.md` §6 noting strip-password is deferred until the paramiko+daemon path returns.

### D3. Idempotency: ssh-copy-id owns dedupe; we surface outcome in `data.outcome`

- `ssh-copy-id` already greps `authorized_keys` before appending. We do **not** override or pre-check; we **parse its stderr** to distinguish two success outcomes:
  - `data.outcome="installed"` — at least one key was newly appended (ssh-copy-id stderr says "Number of key(s) added: N" with N ≥ 1).
  - `data.outcome="already_installed"` — every key in the pubkey file was already present (ssh-copy-id stderr says "WARNING: All keys were skipped because they already exist on the remote system").
- **Both are success** (`success=true`, `exit_code=0`, `failure_class=null`). Idempotent operation: the user's intent — "remote should have this key" — is satisfied either way. Callers that need to distinguish read `data.outcome`.

### D4. `failure_class` taxonomy (deploy_pubkey-local; does NOT touch `lib`)

Pattern follows `ssh_config_manager`: a per-script string namespace, all strings spelled out in this PRD. Set:

| `failure_class` | Trigger | exit_code |
|---|---|---|
| `null` | success (outcome=`installed` OR `already_installed`) | 0 |
| `tool_missing` | `ssh-copy-id` not on PATH (resolved via `shutil.which`) | 10 |
| `key_file_missing` | `--key` path does not exist OR is not readable | 11 |
| `no_tty_for_password` | stdin not a TTY AND ssh-copy-id failed on auth prompt | 12 |
| `auth` | password wrong / pubkey auth refused (ssh-copy-id stderr: `Permission denied`) | 13 |
| `host_key_unknown` | known_hosts has no entry, user didn't pass `--accept-new-hostkey` (ssh-copy-id stderr: `Host key verification failed`) | 14 |
| `network` | unreachable, DNS, timeout-at-connect (matches lib's `NETWORK_PATTERN`, scoped to `ssh-copy-id` returncode != 0) | 15 |
| `timeout` | `subprocess.TimeoutExpired` raised | 124 |
| `remote_error` | ssh-copy-id non-zero with no match in the above | 1 |

- Exit codes pick a fresh contiguous range starting at 10 (above the existing 0/1/2/124 from ssh_execute and the 4-9 range used by ssh_config_manager / sftp_transport). **TODO during implement.md:** verify no exit-code collision against the latest `error-handling.md`; reassign if needed.
- `data.failure_class` is `null` on success, matching `ssh_config_manager` and `ssh_execute` conventions.

### D5. CLI surface (v1.0)

```
deploy_pubkey.py <alias>
    [--key <path>]                  default: ~/.ssh/id_ed25519.pub
    [--connect-timeout N]           default: 8     (mirrors ssh_execute)
    [--timeout N]                   default: 180   (longer than ssh_execute's 120 to accommodate interactive password)
    [--ssh-config <path>]           default: unset (ssh-copy-id reads ~/.ssh/config by default)
    [--accept-new-hostkey]          default: off; opt-in flag (matches safety.md §13 "one-shot relaxation per call")
    [--json]
```

- All new flags map cleanly to `ssh-copy-id -o KEY=VALUE` or `-F PATH`:
  - `--connect-timeout` → `-o ConnectTimeout=N`
  - `--ssh-config`     → `-F <path>`
  - `--accept-new-hostkey` → `-o StrictHostKeyChecking=accept-new`
- **`--strip-password` removed** (see D2).
- No `--no-daemon` (ssh_execute keeps it as `argparse.SUPPRESS` for v0.2 compat; deploy_pubkey has no v0.2 surface to honor).

### D6. JSON envelope `data` payload

Additive to the shared envelope contract (ADR-001 D3); callers must tolerate unknown keys:

| Key | Type | Meaning |
|---|---|---|
| `alias` | str | Resolved alias passed in argv |
| `key_path` | str | Absolute path to the `.pub` file used |
| `outcome` | `"installed"` \| `"already_installed"` \| `null` | Success-only; null on failure |
| `keys_added` | int \| null | Parsed from `ssh-copy-id` stderr ("Number of key(s) added: N"); null if not present |
| `failure_class` | str \| null | Per D4 table |
| `ssh_copy_id_argv` | list[str] | Exact argv for reproduction |
| `elapsed_s` | float | Wall-clock duration |
| `raw_stderr` | str | Verbatim ssh-copy-id stderr (for audit / debugging) |

`stdout` (top-level) is left empty (ssh-copy-id writes nothing useful to stdout). `stderr` (top-level) is the filtered human-readable summary; raw lines stay in `data.raw_stderr`.

### D7. Test strategy

- **Unit (`test_deploy_pubkey_unit.py`)** — primary coverage. Mock `subprocess.run` and `shutil.which`; assert argv construction for every flag combo; assert stderr-parsing produces correct `outcome` / `keys_added`; assert every `failure_class` row in D4 is triggered by the right stderr pattern; assert `--key` missing-file case is detected **before** subprocess (cheap fail-fast). Target: ≥ 90% statement coverage.
- **Integration (`test_deploy_pubkey_integration.py`, `live_ssh` mark)** — minimal, non-mutating:
  - `--help` exits 0 (smoke)
  - non-existent alias produces `failure_class="network"` (ssh-copy-id fails fast on DNS without any auth attempt)
  - `--key /nonexistent` produces `failure_class="key_file_missing"` (never touches network)
  - **Does NOT** run an actual `ssh-copy-id` against POC_HOST. Reasoning: POC_HOST=pai already trusts the developer's key; running ssh-copy-id again would test the `already_installed` path but requires ssh-agent forwarding for the bootstrap (which leaves the key situation unchanged — testable but low signal). End-to-end "bootstrap a fresh user" is left to the e2e validation report (`05-24-e2e-validate-skills-on-pai`), per existing project pattern.
- Manifest test (`test_manifests.py`) already exercises `--help`; we get that for free.

## Requirements

1. CLI surface per D5. Argparse with `prog="deploy_pubkey.py"`, description matching SKILL.md decision-tree entry.
2. `subprocess.run` with argv list (never `shell=True`). `MSYS_NO_PATHCONV=1` via `lib.msys_safe_env()`. No credentials in argv.
3. JSON envelope per D6, emitted via `lib.emit(result, as_json=args.json)`.
4. `failure_class` strings per D4. Exit codes per D4 (subject to D4 TODO).
5. Human-mode output (no `--json`) is a 1-3 line summary: outcome + alias + key path on success; failure_class + remediation hint on failure.
6. Source file header docstring matches the existing v1.0 pattern (`ssh_execute.py:1-25` style): "(v1.0)" tag, surface block, `See:` block pointing to relevant spec files.
7. ruff + mypy clean (mirror `pyproject.toml` settings).
8. Unit + integration tests per D7.

## Acceptance Criteria

- [ ] `deploy_pubkey.py` is no longer a stub: docstring says `(v1.0)`, no `unimplemented()` call, loc in the 250-400 range (in line with `ssh_execute` 257).
- [ ] `python deploy_pubkey.py --help` shows the v1.0 surface from D5 and exits 0.
- [ ] All argparse flags from D5 are present and parse correctly.
- [ ] `tests/test_deploy_pubkey_unit.py` exists; pytest covers every `failure_class` row in D4; ≥ 90% statement coverage on `deploy_pubkey.py`.
- [ ] `tests/test_deploy_pubkey_integration.py` exists with `pytest.mark.live_ssh`; three minimal cases (help / bad alias / bad key) pass against POC_HOST=pai.
- [ ] `pytest tests/` default suite green.
- [ ] `pytest tests/ -m live_ssh -k deploy_pubkey -v` green (manual, against POC_HOST=pai).
- [ ] `ruff check plugins/ssh-core` and `mypy plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py` clean.
- [ ] Exit-code table (D4) verified non-conflicting against latest `error-handling.md`.
- [ ] `references/workflows.md` §6 has a TODO line documenting the `--strip-password` deferral (D2).
- [ ] `SKILL.md` decision tree updated if it mentioned `--strip-password`. (Spot-check: does not currently mention deploy_pubkey at all → optionally add to quick reference, optional in v1.0.)

## Out of scope

- Generating a new keypair (user supplies an existing `.pub`; `ssh-keygen` is the user's job).
- Removing keys from `authorized_keys` (ssh-copy-id only appends; we don't override).
- Managing multiple keys per alias in one invocation (`--key` is singular).
- Windows OpenSSH server compatibility (POSIX remote shell assumed; matches the rest of ssh-core; ssh-copy-id missing on Windows OpenSSH is acknowledged in D4 `tool_missing`).
- Headless / CI use (no controlling tty). v1.0 refuses cleanly with `no_tty_for_password`; CI flows run `ssh-copy-id` themselves or use a separate `--password-file`-style entry point, deferred to v1.1.
- The `# password:` config-comment lifecycle (D2 deferred to v1.1).
- Telling the user to add the alias to `~/.ssh/config` if missing (`ssh-copy-id` fails with a clear "Could not resolve hostname" → `failure_class="network"` is enough; no need for deploy_pubkey to pre-check via `ssh_config_manager`).

## Open questions blocking planning

None. Awaiting user review of the locked decisions; if all accepted, proceed to `design.md` + `implement.md`.
