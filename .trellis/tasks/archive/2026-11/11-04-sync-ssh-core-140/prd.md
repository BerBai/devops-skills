# Sync ssh-core 1.4.0 into project plugin

## Goal

Synchronize the independently hardened ssh-core 1.4.0 skill subtree into plugins/ssh-core while preserving devops-skills integration contracts.

## Requirements

- Sync the production skill subtree from the independently maintained
  `ssh-core/` working tree at version `1.4.0` into
  `plugins/ssh-core/skills/ssh-core/`.
- Keep the plugin self-contained: no source outside the skill subtree, no
  caches, build artifacts, or standalone repository tooling may be imported.
- Preserve the cross-plugin shell-out contract for diagnostic plugins:
  `ssh_execute.py <host> <command> --json` remains a JSON envelope with the
  existing top-level fields and compatible `timeout` semantics.
- Preserve the project plugin discovery path
  `plugins/ssh-core/skills/ssh-core/scripts/...` used by other plugins.
- Carry forward the 1.3/1.4 runtime fixes: byte-safe output, cluster stdin and
  config forwarding, password-capable transfers, interactive/daemon envelope
  parity, config manager hardening, exit-code accuracy, and Python 3.8
  compatibility.
- Update `plugins/ssh-core/.claude-plugin/plugin.json` to version `1.4.0` and
  the 1.4.0 capability description.
- Update root-facing documentation where it describes ssh-core's dependency
  model or capability set.
- Update backend specs/ADRs so the project's documented ssh-core contract no
  longer describes the obsolete implementation as current.
- Replace or adapt obsolete ssh-core-specific tests that assert implementation
  details removed by 1.4.0; retain cross-plugin contract tests.

## Acceptance Criteria

- [ ] Every source file under `plugins/ssh-core/skills/ssh-core/` matches
      `ssh-core/skills/ssh-core/` at version `1.4.0`, excluding `__pycache__`.
- [ ] `plugins/ssh-core/.claude-plugin/plugin.json` reports `1.4.0`.
- [ ] All diagnostic plugins that discover `ssh_execute.py` still resolve the
      project path and can invoke the preserved CLI contract.
- [ ] Project-wide relevant unit and integration tests pass for ssh-core and
      diagnostic plugins that shell out to it.
- [ ] A structural scan confirms no cross-plugin Python imports were added.
- [ ] Backend ADR/spec indexes accurately describe the synced contract.
- [ ] No `__pycache__`, `.mypy_cache`, `.coverage`, `dist/`, or unrelated
      standalone repository files enter the plugin directory.
- [ ] The change is committed as one reversible commit.

## Notes

- The standalone repository is currently an untracked directory in the parent
  repository. It remains the development source of truth for this sync; this
  task only publishes its skill subtree into the plugin target.
