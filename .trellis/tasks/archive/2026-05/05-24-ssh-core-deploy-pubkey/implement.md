# Implementation — deploy_pubkey v1.0

## Phase 0 — Pre-flight (read & lock context)

- [ ] **0.1** Re-read `prd.md` (D1–D7), `design.md` (architecture + module structure + regex tables + exit-code pin)
- [ ] **0.2** Verify exit codes 10-15 don't conflict with `.trellis/spec/backend/error-handling.md` (ssh_config_manager uses 4-8, sftp_transport uses 9, ssh_execute uses 124). If a conflict surfaces, re-pin in `design.md` exit-code table **before** writing code.
- [ ] **0.3** Confirm `ssh-copy-id` exists on dev machine: `which ssh-copy-id` (mac/linux). Note its OpenSSH version (`ssh -V`) — stderr wording is tied to the version family.
- [ ] **0.4** Read sibling reference `ssh_execute.py` once more (257 LOC, the most relevant shape twin for argv-style ssh-spawn + JSON envelope) — confirm convention alignment before typing the first line.

## Phase 1 — Foundation (parser + preflight + argv builder)

- [ ] **1.1** Replace the 42-LOC stub `plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py` docstring with the v1.0 docstring (mirror `ssh_execute.py:1-25` style: surface block + `See:` block pointing to PRD/design + ADR-001 + ADR-003 cross-refs).
- [ ] **1.2** Implement `build_parser()` per PRD D5 (6 flags total: `--key / --connect-timeout / --timeout / --ssh-config / --accept-new-hostkey / --json`). Drop `--strip-password`.
- [ ] **1.3** Implement `_resolve_key(path: str) -> Path`: `Path(path).expanduser().resolve()`.
- [ ] **1.4** Implement `_preflight(args) -> dict | None`:
  - `shutil.which("ssh-copy-id")` → if None, return `_failure_envelope(..., fclass="tool_missing", argv=[], elapsed=0.0, raw_stderr="")`.
  - `key = _resolve_key(args.key)`; if `not key.exists()` or `not os.access(key, os.R_OK)`, return `_failure_envelope(..., fclass="key_file_missing", ...)`.
  - **First-line sanity check** (per design.md "Risks"): read first 200 bytes; if it doesn't start with one of `ssh-rsa / ssh-ed25519 / ecdsa-sha2-* / ssh-dss / sk-ssh-ed25519 / sk-ecdsa-sha2-`, return `key_file_missing` with hint `"{path} doesn't look like an OpenSSH public key"`.
  - else return `None` (preflight passes).
- [ ] **1.5** Implement `_build_argv(args, key_path) -> list[str]` per design.md:
  - Always: `["ssh-copy-id", "-i", str(key_path), "-o", f"ConnectTimeout={args.connect_timeout}", alias]`
  - Conditional: `-F <ssh_config>` if `--ssh-config` set; `-o StrictHostKeyChecking=accept-new` if `--accept-new-hostkey` set.
  - **Never** add `-o BatchMode=yes` (would defeat the password prompt).

**Validation 1**:
```bash
# Parser smoke
python3 plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py --help
# Should show all 6 flags + the alias positional, exit 0.

# Bad key short-circuit (no ssh-copy-id call possible — preflight refuses)
python3 plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py somehost --key /tmp/nope --json
# Expect: {"success": false, "exit_code": 11, "data": {"failure_class": "key_file_missing", ...}}
```

## Phase 2 — Subprocess + result classification

- [ ] **2.1** Implement `_run_ssh_copy_id(argv, timeout) -> tuple[int, str, str, float, bool]` returning `(returncode, stdout, stderr, elapsed_s, timed_out)`. Use `subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=msys_safe_env(), stdin=None, check=False)` — `stdin=None` is critical for the tty prompt.
  - Catch `subprocess.TimeoutExpired` → return `(-1, "", "", float(timeout), True)`.
- [ ] **2.2** Compile regexes at module level (per design.md "stderr parsing" + "Failure-class regex table"):
  - `_INSTALLED_RE`, `_ALREADY_RE`, `_AUTH_RE`, `_HOST_KEY_RE`, `_NETWORK_RE`, `_PROMPT_FAIL_RE`.
- [ ] **2.3** Implement `_parse_success_outcome(stderr) -> tuple[str, int | None]`:
  - If `_INSTALLED_RE` matches → `("installed", int(group(1)))`.
  - Elif `_ALREADY_RE` matches → `("already_installed", 0)`.
  - Else fallback `("installed", None)` — `null` keys_added rather than misreport.
- [ ] **2.4** Implement `_classify_failure(rc, stderr, is_tty, timed_out) -> str` with precedence per design.md table. Critical: idempotency override — if `rc != 0` AND `_ALREADY_RE` matches stderr, the **caller** must treat as success / `already_installed` (don't return a failure_class).
- [ ] **2.5** `_EXIT_CODES` constant dict (failure_class → int) per design.md exit-code pin.

**Validation 2**:
```bash
# stderr parsing unit smoke
python3 -c "
from plugins.ssh-core.skills.ssh-core.scripts.deploy_pubkey import _parse_success_outcome, _classify_failure
print(_parse_success_outcome('Number of key(s) added: 2'))             # ('installed', 2)
print(_parse_success_outcome('WARNING: All keys were skipped...'))     # ('already_installed', 0)
print(_classify_failure(255, 'Could not resolve hostname xyz', True, False))   # 'network'
print(_classify_failure(255, 'Permission denied (publickey,password)', True, False))   # 'auth'
"
```

## Phase 3 — Envelopes + main + human hints

- [ ] **3.1** Implement `_HUMAN_HINTS` constant dict per design.md table.
- [ ] **3.2** Implement `_success_envelope(args, key_path, outcome, keys_added, argv, elapsed, raw_stderr) -> dict` and `_failure_envelope(args, key_path, fclass, argv, elapsed, raw_stderr, hint=None) -> dict`. Both call `json_result(...)`. The failure version pulls `hint = _HUMAN_HINTS[fclass].format(alias=args.alias, path=str(key_path), timeout=args.timeout, rc=...)` defensively.
- [ ] **3.3** Implement `main(argv=None) -> int`:
  ```python
  args = build_parser().parse_args(argv)
  pre = _preflight(args)
  if pre is not None:
      emit(pre, as_json=args.json); return pre["exit_code"]
  key_path = _resolve_key(args.key)
  argv_list = _build_argv(args, key_path)
  rc, stdout, stderr, elapsed, timed_out = _run_ssh_copy_id(argv_list, args.timeout)
  if rc == 0:
      outcome, keys_added = _parse_success_outcome(stderr)
      result = _success_envelope(args, key_path, outcome, keys_added, argv_list, elapsed, stderr)
  elif _ALREADY_RE.search(stderr):           # idempotency override
      result = _success_envelope(args, key_path, "already_installed", 0, argv_list, elapsed, stderr)
  else:
      fclass = _classify_failure(rc, stderr, sys.stdin.isatty(), timed_out)
      result = _failure_envelope(args, key_path, fclass, argv_list, elapsed, stderr,
                                 hint=_HUMAN_HINTS[fclass].format(alias=args.alias, path=str(key_path),
                                                                  timeout=args.timeout, rc=rc))
  emit(result, as_json=args.json)
  return result["exit_code"]
  ```
- [ ] **3.4** Add module-level `if __name__ == "__main__": sys.exit(main())`.

**Validation 3**:
```bash
# Real ssh-copy-id smoke (interactive — only when developer is at terminal)
python3 plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py --help
# If you have a throwaway alias with password auth, you can run it here. Skip if not.

# Non-interactive failure mode (subprocess inherits stdin which is connected to pipe in CI)
echo "" | python3 plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py nonexistent-alias --connect-timeout=2 --json
# Expect: {"success": false, "exit_code": 15, "data": {"failure_class": "network", ...}}
```

## Phase 4 — Unit tests

- [ ] **4.1** Create `tests/test_deploy_pubkey_unit.py`. Header: `import sys` + path injection to scripts dir (mirror `tests/test_ssh_execute_unit.py:1-20`).
- [ ] **4.2** `build_parser` 4 cases (all flags / required alias / unknown flag / --help exits 0).
- [ ] **4.3** `_resolve_key` 3 cases (default `~/.ssh/id_ed25519.pub` / explicit absolute / relative).
- [ ] **4.4** `_preflight` 4 cases (happy / tool_missing / key not exists / key not a pubkey).
- [ ] **4.5** `_build_argv` 6 cases (no flags / --connect-timeout=30 / --ssh-config / --accept-new-hostkey / all flags / `-i` first).
- [ ] **4.6** `_run_ssh_copy_id` 3 cases (rc==0 / rc!=0 / TimeoutExpired). Mock `subprocess.run`.
- [ ] **4.7** `_parse_success_outcome` 4 cases (Number=1 / Number=3 / WARNING-already / ERROR-already).
- [ ] **4.8** `_classify_failure` 8 cases (one per failure_class regex + remote_error catch-all + no_tty special with `is_tty=False`).
- [ ] **4.9** Idempotency override 2 cases (rc != 0 + `_ALREADY_RE` matched → reclassify as success in `main()`).
- [ ] **4.10** `main()` integration 6 cases (success-installed / success-already / each failure_class wired through). Mock `subprocess.run` + `shutil.which` + `Path.exists` + `Path.is_file` + `os.access`.
- [ ] **4.11** Envelope 3 cases (success fields complete / failure fields complete / human-mode hint to stderr without `--json`).

**Validation 4**:
```bash
pytest tests/test_deploy_pubkey_unit.py -v
# Expect: ≥ 43 case PASS. Aim for ≥ 90% statement coverage.
pytest tests/test_deploy_pubkey_unit.py --cov=plugins.ssh-core.skills.ssh-core.scripts.deploy_pubkey
```

## Phase 5 — Integration tests

- [ ] **5.1** Create `tests/test_deploy_pubkey_integration.py`. Mirror `test_ssh_execute_integration.py` skeleton: `POC_HOST = os.environ.get("POC_HOST", "pai")`, module-level `pytest.mark.live_ssh`.
- [ ] **5.2** 3 cases (per PRD D7 + design.md table):
  - `test_help_exits_zero` — `subprocess.run([sys.executable, deploy_pubkey, "--help"], …)`.
  - `test_nonexistent_alias_returns_network` — alias with random suffix, `--connect-timeout=2`, expect `exit_code == 15` / `failure_class == "network"`.
  - `test_missing_key_returns_key_file_missing` — `--key /tmp/deploy_pubkey_missing_key_<rand>.pub` (path that does not exist), expect `exit_code == 11` / `failure_class == "key_file_missing"`. Critically: `data.ssh_copy_id_argv == []` (preflight refused, never spawned subprocess).
- [ ] **5.3** Verify no test mutates anything on POC_HOST (sanity grep for any string that would land on the host).

**Validation 5**:
```bash
pytest tests/test_deploy_pubkey_integration.py -m live_ssh -v
# Expect: 3 PASS.
pytest tests/ -m live_ssh -v
# Expect: prior live_ssh suite size + 3 (e.g. 43 → 46). All PASS.
```

## Phase 6 — Docs sync

- [ ] **6.1** `SKILL.md` quick-reference: add a deploy_pubkey one-liner under `## Quick reference` if not present. Use:
  ```bash
  # Bootstrap a new host: install your pubkey (interactive password prompt)
  python scripts/deploy_pubkey.py prod-web-02 --accept-new-hostkey
  ```
- [ ] **6.2** `SKILL.md` decision tree — add a row under `Manage hosts`:
  ```
  └─ Bootstrap pubkey → deploy_pubkey.py <alias> [--accept-new-hostkey]
  ```
- [ ] **6.3** `references/safety.md` §28 — replace *"There is a planned `deploy_pubkey.py` that you have to invoke explicitly."* with *"Use `deploy_pubkey.py <alias>` to install your pubkey explicitly; first-touch password prompt is interactive (no headless mode in v1.0)."*
- [ ] **6.4** `references/workflows.md` §6 — replace the existing *"The preferred flow is `deploy_pubkey` (planned for v0.2) which runs `ssh-copy-id` and then strips the password."* with the v1.0 reality: `deploy_pubkey` ships; `--strip-password` is **v1.1** (cross-ref PRD D2). Keep the password-comment paragraph as-is since it documents the user opt-in pattern (still valid for v1.1 daemon path).
- [ ] **6.5** **Do NOT** write an ADR-004. Parent v1-rollout PRD Notes §82 says `.trellis/spec/backend/` is personal workspace and not committed; the design decisions live in this task's `prd.md` + `design.md`.

**Validation 6**:
```bash
# Quick reference command runs at least to --help
python3 plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py prod-web-02 --accept-new-hostkey --help
# (--help short-circuits before any network)

# Doc references intact
grep -rn "deploy_pubkey" plugins/ssh-core/skills/ssh-core/{SKILL.md,references/}
# Expect: at least quick-ref + decision-tree + safety.md + workflows.md hits, all consistent
```

## Phase 7 — Commit

- [ ] **7.1** `git status` — confirm changed files:
  - `plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py`
  - `plugins/ssh-core/skills/ssh-core/SKILL.md`
  - `plugins/ssh-core/skills/ssh-core/references/safety.md`
  - `plugins/ssh-core/skills/ssh-core/references/workflows.md`
  - `tests/test_deploy_pubkey_unit.py`
  - `tests/test_deploy_pubkey_integration.py`
  No `.trellis/spec/` changes (per Phase 6.5). `.trellis/tasks/05-24-ssh-core-deploy-pubkey/*` are gitignored.
- [ ] **7.2** `git add` exactly the 6 files above; review `git diff --staged` once more.
- [ ] **7.3** Commit message (HEREDOC):
  ```
  feat(ssh-core): ship deploy_pubkey v1.0 wrapping ssh-copy-id

  - Thin wrapper around system ssh-copy-id; inherits controlling tty for the
    one-time password prompt (first-touch bootstrap before any key exists)
  - 6-flag surface: --key / --connect-timeout / --timeout / --ssh-config /
    --accept-new-hostkey / --json (drops the v0.2 stub --strip-password,
    deferred to v1.1 alongside the password+daemon path restoration)
  - Pre-flight refusals (tool_missing / key_file_missing) short-circuit
    before subprocess; non-pubkey content sanity-checked too
  - 8 failure_class strings + exit codes 10-15 (per-script namespace,
    consistent with ssh_config_manager precedent)
  - Idempotency: rc != 0 + "All keys were skipped" stderr → reclassified
    as success/already_installed (older ssh-copy-id builds exit non-zero)
  - 43+ unit + 3 live_ssh integration cases
  - SKILL.md / safety.md / workflows.md doc sync to reflect v1.0 reality
  - Refs: 05-24-ssh-core-deploy-pubkey {prd,design,implement}.md
  ```
- [ ] **7.4** Post-commit: `pytest tests/` (default) + `pytest tests/ -m live_ssh` both green.

**Validation 7**:
```bash
git log --oneline -1
# Expect: <new hash> feat(ssh-core): ship deploy_pubkey v1.0 wrapping ssh-copy-id

pytest tests/                     # default suite all green
pytest tests/ -m live_ssh -v      # live_ssh suite +3 cases, all green
```

## Phase 8 — Wrap-up

- [ ] **8.1** Update parent task `05-24-devops-skills-v1-rollout/prd.md` Notes — append a 2026-05-25 entry:
  ```
  - 2026-05-25 **deploy_pubkey v1.0 shipped** — commit <hash>. 6 files /
    ~XXX insertions / N deletions. 43+ unit + 3 live_ssh = 46+ new PASS.
    Phase 1 progress: 2/7 (ssh_config_manager + deploy_pubkey).
  ```
- [ ] **8.2** `python3 ./.trellis/scripts/task.py finish` (clears active pointer; no archive yet, batch later).
- [ ] **8.3** `python3 ./.trellis/scripts/task.py current` to confirm pointer cleared.

**Validation 8**:
```bash
python3 ./.trellis/scripts/task.py list --mine
# Expect: 05-24-ssh-core-deploy-pubkey shown as completed (or with status updated).
```

## Final validation commands

```bash
# Baseline protection
pytest tests/                                              # All green
pytest tests/ -m live_ssh -v                              # All green (43 → 46)

# Contract smoke
python3 plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py --help

# Failure modes (no network)
python3 plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py somealias --key /tmp/missing.pub --json
# Expect: failure_class=key_file_missing, exit 11

# Linting
ruff check plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py
mypy plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py
```

## Review gates

| Gate | When | What |
|---|---|---|
| G1 | Before Phase 2 | Parser + preflight + argv builder smoke-pass; `--help` and `--key /tmp/missing` both produce correct envelopes |
| G2 | Before Phase 3 | Regex set parses real OpenSSH ssh-copy-id stderr samples (collect 1-2 samples from `ssh -V` matching the dev OpenSSH version) |
| G3 | Before Phase 4 | `main()` smoke run on a real `ssh-copy-id` invocation against a throwaway alias (developer's call to make manually, or skip with note) |
| G4 | Before Phase 5 | Unit suite ≥ 43 cases, ≥ 90% coverage |
| G5 | Before Phase 6 | Integration 3 cases pass + default suite still green |
| G6 | Before Phase 7 | SKILL.md / safety.md / workflows.md diffs reviewed by user |
| G7 | Done | Commit lands + baseline pytest unchanged |

## Rollback points

- Phases 1–6 are not yet committed; `git restore <file>` reverts any single file.
- Phase 7 commit lands: `git revert <hash>` undoes the single commit cleanly (no `.trellis/spec/` entanglement, no downstream script imports — fully self-contained).
- Phase 8 Notes update is text-only and reversible.

## Dispatch note

Per parent task Notes §82 — sub-agent dispatch (`trellis-implement`) is the right shape for this task (300-400 loc + 43 unit + 3 integration; single phase span). Main session writes design.md/implement.md (this artifact), then dispatches one `trellis-implement` agent with the task path. Sub-agent does Phases 1-5 (code + tests). Phases 6-8 (docs + commit + wrap-up) come back to main session for review-gated handling.
