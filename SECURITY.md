# Security policy

## Supported versions

| Component | Supported | Notes |
|---|---|---|
| Marketplace release `1.0.x` | yes | the tree and the `v1.0.0` tag are `1.0.0`; `v0.2.0` is the historical scaffold tag (see [`CHANGELOG.md`](./CHANGELOG.md)) |
| `ssh-core` plugin `1.0.x` | yes | contains the standalone 1.5.0 feature set ([ADR-004](./docs/adr/ADR-004-ssh-core-1.5.0-sync.md)) |
| other plugins `1.0.x` | yes | all plugin versions are unified with the marketplace release |
| anything older | no | upgrade to the current plugin version first |

The runtime floor is Python 3.8 (`pyproject.toml`, `tools/check_python_floor.py`).
A host with an older interpreter is unsupported, not silently downgraded.

## Reporting a vulnerability

Open a private GitHub Security Advisory for this repository
(`https://github.com/BerBai/devops-skills/security/advisories/new`). If that is
unavailable, open a normal issue that describes the **class** of problem and
says a private report is available; do not paste credentials, private keys,
hostnames or customer data into an issue.

Please include: the plugin and script name, the version printed by
`--version`, the exact argv, the JSON envelope (redact `stdout`/`stderr` as
needed), and whether the issue reproduces against `local` or only a remote host.

## Credential and host-key model

These are properties of the shipped code, not aspirations:

- **Passwords never enter argv or the JSON envelope.** The password comes from
  an environment variable (default `SSH_PASSWORD`) and is answered through a
  0600 `SSH_ASKPASS` helper. `ssh_execute.py --help` documents `--password-env`;
  `SKILL.md:280-287` states the rule. `--sudo-password-env` sends the sudo
  password over stdin, not argv.
- **Host keys are strict by default.** Key, scp and tar routes use
  `StrictHostKeyChecking=yes`; paramiko uses `RejectPolicy`. An unknown host
  needs the explicit one-shot `--accept-new-hostkey`, which still fails on a
  changed key (`SKILL.md:588`).
- **`-o` passthrough is validated.** `ProxyCommand`, `LocalCommand`,
  `PermitLocalCommand`, `Match`, `HostName`, `KnownHostsCommand`,
  `RemoteCommand` and `IdentityAgent` are refused with
  `failure_class: "invalid_option"` (107) (`SKILL.md:589`).
- **Output is redacted by default.** `HostName`, `User`, `Port`, `IdentityFile`,
  `user@host` and address literals become `<host>` / `<user>` / `<port>` /
  `<key>` in `stderr`, `data.raw_stderr` and `data.ssh_argv`; `--no-redact`
  opts out for debugging (`SKILL.md:553`).
- **Audit records contain commands, not secrets.** Records hold
  `ts/host/route/command/exit_code/failure_class/elapsed_s/bytes`
  (`SKILL.md:559-567`); the directory is 0700 and files are 0600.
- **`ssh-guarded` writes only inside a workdir sandbox** and refuses paths in
  `~/.ssh`, `.env`, `authorized_keys` and private-key files
  (`ssh-guarded/SKILL.md:164-180`).

## Known limitations

- The password environment variable is readable by processes running as the
  same user; it is not a secret store. Use an agent or a dedicated account for
  stronger isolation.
- Askpass scratch directories and audit logs live on the local filesystem. A
  local attacker who can read your user's files can read them; `--no-audit` and
  `SSH_CORE_ASKPASS_DIR` change where they are written, not whether they exist.
- `--no-redact` and `--show-sensitive` deliberately expose host identifiers.
  Do not use them in a transcript you intend to share.
- The plugins diagnose remote systems; they do not patch them. A compromised
  **remote** host can return malicious text, which the tools treat as data.
  They never evaluate that text as a shell command on the local side.
- `uv.lock` is stale (see `CONTRIBUTING.md`); dependency resolution should not
  be trusted from it until it is regenerated.

## Scope

This policy covers the code and documentation in this repository. It does not
cover the security of an operator's SSH server, cloud account, Kubernetes
cluster, CI provider or the secrets those systems hold.
