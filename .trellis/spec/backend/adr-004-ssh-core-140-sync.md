# ADR-004: Sync ssh-core 1.4.0 into the devops-skills plugin

- Status: Accepted
- Date: 2026-11-04
- Source: standalone `ssh-core` working tree, version `1.4.0`
- Target: `plugins/ssh-core/skills/ssh-core/`

## Context

The devops-skills plugin had drifted from the independently hardened ssh-core
repository. The plugin metadata still reported `0.2.0`, while the standalone
working tree had completed the 1.3/1.4 hardening releases: byte-safe output,
login-banner handling, cluster stdin/config forwarding, password-aware
transfers, interactive session and daemon envelope parity, config-manager
hardening, normalized exit codes, retry/sudo/follow support, and an explicit
Python 3.8 floor.

Diagnostic plugins do not import ssh-core Python. They locate
`plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py` and invoke it as a
subprocess with the shared JSON envelope contract.

## Decision

1. Replace the whole `plugins/ssh-core/skills/ssh-core/` subtree with the
   standalone repository's matching subtree at version `1.4.0`.
2. Keep the plugin self-contained. Do not copy standalone repository tooling,
   release manifests, caches, distributions, or test harnesses into the plugin.
3. Treat the CLI argv + JSON envelope as the only cross-plugin contract.
   Diagnostic plugins may add new options but must not import `lib.*`.
4. Publish the 1.4.0 version and capability description in the plugin metadata.
5. Test the synced runtime with project-local copies of the standalone runtime
   suites, adapted to resolve the plugin scripts and to skip tests that cover
   standalone packaging tools.

## Consequences

- Consumers keep the documented `ssh_execute.py <host> <command> --json`
  behavior while gaining additive options and richer `data` fields.
- The plugin now includes `ssh_session.py`, `ssh_daemon.py`, `selfcheck.py`,
  `audit.py`, and the expanded shared `lib` package.
- Transfer routes can use SFTP, scp, tar, or rsync depending on flags and
  available dependencies; paramiko is no longer a mandatory runtime dependency.
- The root project no longer claims that `ssh_daemon.py` is deferred.
- Future ssh-core changes must be synchronized deliberately; the standalone
  repository remains the development source of truth for 1.4.0.

## Alternatives

- Selective backports were rejected because the output pipeline, auth helpers,
  transfer backends, session handling, and exit-code table are coupled through
  the expanded shared library. A cherry-pick would create an untested hybrid.
- Vendoring the entire standalone repository was rejected because it would add
  release tooling, CI configuration, packaging manifests, and duplicate test
  infrastructure that do not belong to the Claude plugin target.
