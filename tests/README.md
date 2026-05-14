# tests/

Cheap, fast tests that catch the things which would break `/plugin marketplace add` before any user sees the skill.

## Run

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## What's covered today (v0.1.0)

- `.claude-plugin/marketplace.json` parses and lists the expected plugins
- Each plugin's `plugin.json` parses and has the required fields
- Every `SKILL.md` starts with YAML frontmatter containing `name` and `description`
- Every plugin has at least one `references/*.md`
- Every script in every plugin accepts `--help` without crashing

## What's missing (v1.0 roadmap)

- Integration tests against a real ssh daemon (use `paramiko`'s `ssh_server`)
- Sandbox-escape attempts on `ssh-guarded` (the path rules table in `workdir-sandbox.md`)
- Redact-catalog regression: feed known secrets, assert they're scrubbed
- Workflow tests: `request_command → run_request --execute` round-trip
