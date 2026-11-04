# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Plugin/skill/scripts layout, where new files go | Filled |
| [Database Guidelines](./database-guidelines.md) | Local JSON state files (no DB); request/job artifacts and `*.local.json` conventions | Filled |
| [Error Handling](./error-handling.md) | JSON output contract, exit codes, subprocess safety, hard checkpoints | Filled |
| [Quality Guidelines](./quality-guidelines.md) | pytest/ruff/mypy, manifest tests, skill discoverability, review checklist | Filled |
| [Logging Guidelines](./logging-guidelines.md) | stdout/stderr as the only channel, no `logging` module, no emoji, never log secrets | Filled |

---

## Architecture Decisions

| Decision | Description | Status |
|----------|-------------|--------|
| [ADR-001 ssh_execute](./adr-001-ssh-execute.md) | v1.0 implementation contract: subprocess + native ssh, JSON envelope, failure classes, local-route policy, timeout=124 | Accepted 2026-05-21 |
| [ADR-002 ssh_config_manager](./adr-002-ssh-config-manager.md) | v1.0 implementation contract: HostBlock AST, metadata-comment convention, atomic write with `.bak.<unix-ts>`, IdentitiesOnly auto-rule, exit codes 4/5/6/7/8 + failure-class strings, argparse SUPPRESS for parent/subparser shared flags | Accepted 2026-05-24 |
| [ADR-003 SFTP transport](./adr-003-sftp-transport.md) | historical v1.0 contract for paramiko-backed `ssh_upload` / `ssh_download`; superseded by ADR-004's multi-backend 1.4.0 transport | Accepted 2026-05-24 |
| [ADR-004 ssh-core 1.4.0 sync](./adr-004-ssh-core-140-sync.md) | full skill-subtree sync from the standalone 1.4.0 source; byte-safe output, auth routing, transfer fallbacks, session/daemon parity, and CLI-only cross-plugin boundaries | Accepted 2026-11-04 |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
