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

Integration tests (opt-in via `-m live_ssh`):

- (added incrementally as v1.0 scripts land)

## What's missing (v1.0 roadmap)

- Sandbox-escape attempts on `ssh-guarded` (the path rules table in `workdir-sandbox.md`)
- Redact-catalog regression: feed known secrets, assert they're scrubbed
- Workflow tests: `request_command → run_request --execute` round-trip
