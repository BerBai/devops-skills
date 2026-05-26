# tests/

Cheap, fast tests that catch the things which would break `/plugin marketplace add` before any user sees the skill.

## Run (default — no network, no remote hosts)

```bash
pip install -e ".[dev]"
pytest tests/
```

This runs everything **except** tests marked `live_ssh`. CI runs this
suite. It should always pass on a laptop with no SSH setup.

## Run (live_ssh — integration against a real host)

Some tests in this tree are marked `live_ssh` and exercise the real
`ssh_execute.py` / `diagnose_host.py` CLIs against a remote host. They
are skipped by default and never run in CI; you opt in locally when
landing changes that touch the ssh transport or diagnostic shell-out
path.

```bash
pytest tests/ -m live_ssh -v
```

### Prerequisites

- An SSH alias the tests can reach. The default is `pai` (the PoC host).
  Override with `POC_HOST=<alias>` if you need to point at a different
  machine.
- The matching private key loaded in `ssh-agent` (the suite relies on
  agent forwarding, not on `IdentityFile` paths in `~/.ssh/config`).
- The remote host runs Linux with standard utils (`uptime`, `df`,
  `free`, `ps`, `cat /proc/loadavg`). Most diagnostic probes assume
  these are on `$PATH`.
- Network reachability from the test machine to the host (so DNS,
  routing, and the SSH port are open).

### Skipping individual hypotheses

The integration tests are written one-test-per-PoC-hypothesis. If a
specific scenario doesn't apply to your host (e.g. you're testing
against `localhost` and the network-failure case is meaningless),
`pytest -k 'not network'` is the documented escape hatch.

## What's covered today

Manifest baseline (always-on):

- `.claude-plugin/marketplace.json` parses and lists the expected plugins
- Each plugin's `plugin.json` parses and has the required fields
- Every `SKILL.md` starts with YAML frontmatter containing `name` and `description`
- Every plugin has at least one `references/*.md`
- Every script in every plugin accepts `--help` without crashing

Unit tests (always-on, added v0.3.0):

- `test_ssh_core_lib.py` — `filter_ssh_noise` and `classify_failure`
  helpers in `plugins/ssh-core/.../lib/__init__.py`
- `test_ssh_upload_unit.py` — ~22 tests covering CLI guards, `upload_file`
  fast path + resume + sha256 verify, `upload_tree` continue-on-error +
  `failed_files` aggregation, posixpath joining, and 5%-bucket progress
  callback bucketing (paramiko fully mocked).
- `test_ssh_download_unit.py` — 17 tests mirroring the upload unit
  layout for `ssh_download.py` + `download_file` / `download_tree`:
  CLI surface (no `--chmod`), 3 guard branches (remote not found /
  remote is dir / local is dir without `--recursive`), fast-path
  no-verify, resume state machine (missing local / equal size /
  partial / local-larger refuse / sha256 mismatch → exit 9), and
  recursive walk + posixpath + continue-on-error aggregation
  (paramiko fully mocked).
- `test_ssh_tunnel_unit.py` — 71 tests covering CLI surface, state-file
  primitives (write/read roundtrip, corrupt-skip, missing-field-skip,
  scan ordering), port allocator (explicit busy / pool first-attempt /
  pool exhausted), `_kill_with_grace` outcomes (stale / SIGTERM-killed /
  SIGKILL-killed / SIGKILL-permission / both-failed / dies-between-checks),
  every per-script failure_class (30-39 + remote_error catch-all), and
  the `--no-detach` foreground path (`start_new_session` not set, port-busy
  refusal, OSError spawn, KeyboardInterrupt -> exit 130). ssh subprocess is
  fully faked via a `_FakePopen` helper; state IO uses `tmp_path` via a
  monkeypatched `tempfile.gettempdir`.

Integration tests (opt-in via `-m live_ssh`):

- `test_ssh_execute_integration.py` — replays PoC hypotheses H1–H6
  against `POC_HOST` (default `pai`): exit code propagation, known-hosts
  noise filtering, alias resolution, failure classification, timeout,
  local route.
- `test_ssh_upload_integration.py` — 10 tests against `POC_HOST`: single
  file happy path, local-missing / dir-without-recursive guards, `--chmod`,
  resume (partial-then-complete + sha256 mismatch → exit 9), recursive
  (simple tree + partial failure with `failed_files`), `--bwlimit`
  wall-clock check, and `--no-progress` stderr suppression.
- `test_ssh_download_integration.py` — 10 tests against `POC_HOST`:
  single-file happy path (`/etc/hostname` content match), resume
  (partial-then-complete + sha256 mismatch → exit 9), recursive
  (simple tree + partial failure with `failed_files`, latter skips
  when remote user is root), `--bwlimit` wall-clock check,
  `--no-progress` stderr suppression, and three guard branches
  (remote not found / remote-is-dir-without-recursive / local-is-dir-without-recursive).
- `test_ssh_tunnel_integration.py` — 5 tests against `POC_HOST`:
  start->stop roundtrip with local-port LISTEN probe + state-file cleanup,
  `--no-detach` lifecycle (foreground spawn, SIGINT reap), empty-status,
  `stop` with no forward → exit 36 `tunnel_not_found`, and the
  `--idle-timeout` no-op assertion (tunnel survives past the value;
  `flag_deferred=["--idle-timeout"]` marker present).

## What's missing (v1.0 roadmap)

- Sandbox-escape attempts on `ssh-guarded` (the path rules table in `workdir-sandbox.md`)
- Redact-catalog regression: feed known secrets, assert they're scrubbed
- Workflow tests: `request_command → run_request --execute` round-trip
