# Implementation Plan: ssh_execute v1.0 + diagnose_host end-to-end

## Phases (sequential — each phase produces a separate commit)

### Phase A — lib helpers (R1, ~60 min)

- [ ] A1: Edit `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py`:
        - Add `SSH_NOISE_PATTERNS` tuple of compiled regexes
        - Add `NETWORK_PATTERN` and `AUTH_PATTERN` constants
        - Add `filter_ssh_noise(stderr) -> (str, list[str])`
        - Add `classify_failure(returncode, stderr) -> str | None`
        - Update module docstring "Stable surface" section
- [ ] A2: Create `tests/test_ssh_core_lib.py`:
        - `test_filter_ssh_noise_strips_known_hosts_warning`
        - `test_filter_ssh_noise_preserves_unknown_warnings`
        - `test_filter_ssh_noise_preserves_trailing_newline`
        - `test_filter_ssh_noise_empty_stderr`
        - `test_classify_failure_success_returns_none`
        - `test_classify_failure_network_on_dns_error`
        - `test_classify_failure_auth_on_publickey_denial`
        - `test_classify_failure_remote_error_default`
        - `test_classify_failure_skips_auth_for_remote_permission_denied`
          (regression test for false-positive risk noted in PoC findings.md)
- [ ] A3: `pytest tests/test_ssh_core_lib.py -v` — all pass.
- [ ] A4: `pytest tests/ -m "not live_ssh"` — confirm 63 existing tests
        still pass (manifest suite + new tests).
- [ ] A5: `ruff check plugins/ssh-core/ tests/test_ssh_core_lib.py` — clean.
- [ ] A6: Commit: `feat(ssh-core/lib): add filter_ssh_noise and classify_failure helpers`.

### Phase B — pyproject pytest marker (~10 min)

- [ ] B1: Edit `pyproject.toml` to add `[tool.pytest.ini_options]`
        section with `live_ssh` marker. If the section already exists,
        amend the `markers` list.
- [ ] B2: Create `tests/README.md` documenting:
        - How to run default suite: `pytest tests/`
        - How to run live_ssh tests: `pytest tests/ -m live_ssh`
        - What `live_ssh` tests assume (pai alias reachable, ed25519 in agent)
        - How to point at a different host (env var, e.g. `POC_HOST=myalias`)
- [ ] B3: `pytest tests/ --collect-only -m live_ssh` — confirm marker
        is recognized (will collect 0 tests at this point, that's fine).
- [ ] B4: Commit: `chore(tests): register live_ssh pytest marker + tests/README`.

### Phase C — ssh_execute.py v1.0 (R2, ~2-3 hours)

- [ ] C1: Replace
        `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py` with
        the v1.0 implementation per design.md. Keep the file header
        docstring (update to v1.0 reality).
- [ ] C2: `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py --help`
        — confirm help text is informative and argv unchanged.
- [ ] C3: Local manual smoke:
        - `... local 'echo hi' --json` → success, stdout=`hi\n`
        - `... local 'ls /tmp | head'` → refused with metachar message
        - `... pai 'true' --json` → success
        - `... pai 'false' --json` → exit_code=1, failure_class=remote_error
        - `... pai 'sleep 60' --timeout 3 --json` → exit_code=124,
          failure_class=timeout, ~3-4s elapsed
- [ ] C4: Create `tests/test_ssh_execute_unit.py`:
        - `test_local_route_simple_command_succeeds`
        - `test_local_route_refuses_pipe`
        - `test_local_route_refuses_redirect`
        - `test_local_route_refuses_semicolon`
        - `test_local_route_shlex_parse_error`
        - `test_local_route_command_not_found`
        - `test_remote_route_envelope_shape` (mock subprocess.run)
        - `test_remote_route_timeout_returns_124`
        - `test_remote_route_populates_failure_class`
        - `test_remote_route_filters_ssh_noise`
        - `test_help_does_not_crash`
- [ ] C5: `pytest tests/ -m "not live_ssh"` — all unit tests pass.
- [ ] C6: Create `tests/test_ssh_execute_integration.py` (marked live_ssh):
        - Test fixtures: `pai` host, configurable via `POC_HOST` env var
        - One test per PoC hypothesis (H1-H6), but using real ssh_execute
        - Skip with clear message if ssh-agent doesn't have the right key
- [ ] C7: `pytest tests/ -m live_ssh -v` — all live tests pass.
- [ ] C8: `ruff check plugins/ssh-core/ tests/test_ssh_execute*.py` — clean.
- [ ] C9: Commit: `feat(ssh-core): ship ssh_execute v1.0 per ADR-001`.

### Phase D — diagnose_host.py v1.0 (R3, ~2 hours)

- [ ] D1: Replace
        `plugins/remote-debug/skills/remote-debug/scripts/diagnose_host.py`
        with the v1.0 implementation per design.md:
        - PROBES catalog (uptime/load/disk/mem/zombie + nproc embedded in load)
        - `_ssh_execute_path()` discovery with clear error
        - `_run_probe()` shell-out wrapper
        - Per-probe parsers (load, disk, mem, zombie; uptime unparsed)
        - `_score()` severity logic
        - argparse with `--check` CSV
- [ ] D2: `python3 plugins/remote-debug/.../diagnose_host.py --help` —
        confirm flags include `--check` and `--json`.
- [ ] D3: Manual end-to-end smoke:
        - `... pai --json` → 5 probes, each `success=True`
        - `... pai --check load,disk --json` → only those 2 probes
        - `... nonexistent.invalid.local --json` → at least one probe
          has failure_class=network (via ssh_execute envelope)
- [ ] D4: Create `tests/test_diagnose_host_unit.py`:
        - `test_ssh_execute_path_discovery_finds_file`
        - `test_ssh_execute_path_discovery_raises_clear_error_when_absent`
          (mock Path.exists to return False)
        - `test_run_probe_parses_ssh_execute_envelope`
        - `test_run_probe_handles_non_json_output`
        - `test_score_ok_when_all_probes_clean`
        - `test_score_warn_on_load_above_cores`
        - `test_score_crit_on_load_2x_cores`
        - `test_score_warn_on_disk_80pct`
        - `test_score_crit_on_disk_95pct`
        - `test_score_warn_on_zombies_above_5`
        - `test_check_filter_runs_only_listed_probes`
        - `test_parse_load_handles_loadavg_format`
        - `test_parse_disk_handles_df_format`
        - `test_parse_mem_handles_free_format`
- [ ] D5: `pytest tests/ -m "not live_ssh"` — all unit tests pass.
- [ ] D6: Create `tests/test_diagnose_host_integration.py` (live_ssh):
        - `test_diagnose_host_pai_returns_healthy_envelope`
        - `test_diagnose_host_invalid_host_classifies_network`
- [ ] D7: `pytest tests/ -m live_ssh -v` — all live tests pass.
- [ ] D8: `ruff check plugins/remote-debug/ tests/test_diagnose_host*.py`
        — clean.
- [ ] D9: Commit: `feat(remote-debug): ship diagnose_host v1.0 with shell-out to ssh-core`.

### Phase E — Cross-plugin verification (~30 min)

- [ ] E1: `grep -rE "from (ssh_core|remote_debug|k8s_debug|docker_quick|log_aggregator|iac_state|cicd_debug)\b" plugins/`
        — 0 hits (cross-plugin import ban holds).
- [ ] E2: `grep -rn "shell=True" plugins/ tests/` — 0 hits.
- [ ] E3: Full suite: `pytest tests/` (still skips live by default) — green.
- [ ] E4: Full suite with live: `pytest tests/ -m live_ssh` — green.
- [ ] E5: All AC checked off in `prd.md`.
- [ ] E6: `git log --oneline` shows 4 distinct commits from this task
        (lib helpers, marker+README, ssh_execute, diagnose_host).

### Phase F — Optional polish (~30 min, deferrable)

- [ ] F1: Update `plugins/ssh-core/.../SKILL.md` workflow section to
        replace "scaffold returns exit 2" with "real behavior" notes.
        Defer if it requires reshuffling decision tree.
- [ ] F2: Update `plugins/remote-debug/.../SKILL.md` similarly.
- [ ] F3: Commit if any docs touched: `docs: update SKILL.md to reflect ssh_execute/diagnose_host v1.0`.

## Validation commands

```bash
# After each phase
pytest tests/ -m "not live_ssh" -v
ruff check plugins/ tests/

# End-to-end before commit
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py pai 'echo OK' --json | python3 -c "import json,sys;d=json.load(sys.stdin);assert d['success'] and d['exit_code']==0"

python3 plugins/remote-debug/skills/remote-debug/scripts/diagnose_host.py pai --json | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['success'], f'not success: {d}'
assert set(d['data']['probes'].keys()) >= {'uptime','load','disk','mem','zombie'}, f'missing probes: {d}'
print('OK')
"

# Live test sweep
pytest tests/ -m live_ssh -v
```

## Time budget

| Phase | Estimate | Cumulative |
|-------|----------|------------|
| A — lib | 1h | 1h |
| B — marker + README | 0.25h | 1.25h |
| C — ssh_execute | 3h | 4.25h |
| D — diagnose_host | 2h | 6.25h |
| E — verification | 0.5h | 6.75h |
| F — polish | 0.5h (deferrable) | 7.25h |

**1 working day cap.** If C or D blow up by >2x, stop and re-plan rather
than rush. Partial completion (Phase A+B+C with D deferred to next task)
is acceptable.

## Rollback triggers

Stop and request main-session decision if:

1. ADR-001 turns out to be wrong on a load-bearing point (e.g.,
   `classify_failure` can't distinguish auth from remote_error reliably
   in real traffic). → New PoC spike, update ADR.
2. `pai` becomes unreachable AND no fallback host can be configured. →
   Defer integration tests, ship unit-only.
3. `diagnose_host` shell-out fails for path-discovery reasons in real
   `/plugin install` layout (which we haven't tested). → Investigate
   how Claude Code actually installs plugins, may need manifest-driven
   discovery instead of relative `__file__`.

## Commit message conventions

Each commit follows the format used in this repo's history:

```
feat(<scope>): <imperative summary, <72 chars>

<2-4 line body explaining the why, not the what>

<optional: refs ADR-001 / closes task xxx>
```

Scopes: `ssh-core/lib`, `ssh-core`, `remote-debug`, `tests`, `docs`.

## Wrap-up

After Phase E:

1. Update prd.md AC checkboxes.
2. Run final full suite (`pytest tests/ -m live_ssh -v` + unit).
3. `task.py finish` then `task.py archive 05-21-04-ssh-execute-v1`.
4. Suggest next task to user (likely: "v1.0: remote-debug remaining
   3 scripts" OR "v1.0: docker-quick or k8s-debug first script").

The four commits from this task should land on `main` and push to
`origin/main` in a single push at the end (user's call).
