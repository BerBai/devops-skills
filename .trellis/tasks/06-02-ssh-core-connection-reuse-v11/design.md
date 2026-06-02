# Design — ssh-core connection reuse V1.1 first slice

## Boundary

This slice adds an opt-in ControlMaster path to `ssh_execute.py` only. It deliberately does not revive `ssh_daemon.py` as a Python TCP daemon. The implementation stays inside `ssh_execute.py` plus ssh-core docs/tests.

## Contract

Existing default:

```bash
ssh_execute.py <host> "<command>" [--timeout N] [--connect-timeout N] [--ssh-config PATH] [--json]
```

New additive flags:

```bash
ssh_execute.py <host> "<command>" --control-master auto \
  [--control-persist 600] [--control-path-dir PATH] [--json]
```

`--control-master off` is the default and emits the same argv as V1. `--control-master auto` appends OpenSSH config options through normal argv-list `-o` pairs.

## ControlPath

Use a deterministic path under a private temp directory:

```text
<tmp>/ssh_controlmaster/<sha256(host + "\0" + ssh_config_or_empty)[:24]>
```

Rationale:

- Does not leak raw aliases, hostnames, or config paths in filenames.
- Keeps paths short enough for Unix-domain socket limits.
- Separates aliases that resolve through different `--ssh-config` files.
- Lets OpenSSH create/reuse the master socket without a Python daemon.

The directory is created with mode `0o700` before spawning ssh.

## Data Flow

1. Parser accepts the new flags.
2. Local route ignores ControlMaster flags and continues existing behavior.
3. Remote route calls a helper to build the ssh argv.
4. The helper adds required V1 flags first, optional `-F`, then optional ControlMaster `-o` flags, then host/command.
5. Subprocess execution, stderr filtering, failure classification, and JSON envelope stay unchanged.

## Compatibility

- Default callers see no argv or behavior change.
- `--no-daemon` remains a hidden no-op for old callers.
- Downstream scripts that parse `data.ssh_argv` tolerate additive flags because the field is already reproduction/debug data.
- Live SSH tests remain skip-safe; unit tests own the new behavior.

## Why Not Custom `ssh_daemon.py`

ADR-001 A2 rejected a custom daemon for V1 and directed V1.1 to evaluate ControlMaster first. This slice follows that direction. `ssh_daemon.py` should remain documented as deferred until there is evidence ControlMaster cannot satisfy a concrete workload.

## Risks

- ControlPath length limits: mitigated by short hashed filenames.
- Stale master sockets: OpenSSH `ControlPersist` manages expiry; no Python cleanup is needed for this slice.
- Behavior surprise if default-on: avoided by keeping default `off`.
