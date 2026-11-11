# ADR-006: hold the marketplace to a Python 3.8 floor

- **Status:** Accepted (reconstructed record)
- **Reconstruction date:** 2026-11-10
- **Evidence:** `pyproject.toml`, `.github/workflows/ci.yml`,
  `tools/check_python_floor.py`, `plugins/ssh-core/skills/ssh-core/SKILL.md`,
  `tests/ssh_core/compat.py`
- **Related:** [ADR-001](./ADR-001-ssh-execute.md),
  [ADR-004](./ADR-004-ssh-core-1.5.0-sync.md)

## Why this file is a reconstruction

`pyproject.toml:8`, `.github/workflows/ci.yml:18` and
`tools/check_python_floor.py:4-5` all cite ADR-006, but no ADR-006 text was ever
tracked. `git log --all -S 'ADR-006' -- .` returns only `1ead9e8`, the commit
that added the references, not a decision document.

The decision itself is visible in three executable places: the declared floor,
the CI leg that tests it, and the AST checker that refuses newer constructs.
This ADR records that decision and attributes each statement to those places.
It does not invent a meeting, a date, or a rationale that no file supports.

## Context

The marketplace ships one transport plugin (`ssh-core`) and seven plugins that
shell out to it. Operators run those plugins on the same estates as their SSH
tooling: the comments name **Kylin 3** and **CentOS 7 with EPEL** as the hosts
that must keep working. Those hosts do not ship Python 3.10 by default.

The repository therefore cannot take the easy path of a modern-only floor. It
also cannot leave the floor implicit: a construct that parses on a developer's
3.12 but fails on a customer's 3.8 only fails *there*, which is the worst place
to discover it.

## Decision

**D1. The whole tree declares `requires-python = ">=3.8"`.** This is the
`pyproject.toml` value, not a per-plugin suggestion. The comment above it says
the transport has to keep working on Kylin 3 / CentOS 7, so the project holds
the whole tree to that floor.

**D2. CI tests the floor, not just the newest interpreter.** The `test` job
matrix is `3.8`, `3.10`, `3.12`; the comment names 3.8 as the floor and 3.12 as
the dev-box version. A floor that is declared but never executed is a claim, not
a gate.

**D3. A static checker refuses 3.9/3.10-only constructs.** `tools/check_python_floor.py`
runs two checks on every push:

1. `ast.parse(..., feature_version=(3, 8))` for syntax;
2. an AST walk for library APIs that parse on 3.8 but raise at runtime:
   `str.removeprefix`, `str.removesuffix`, `zip(strict=)`,
   `Path.write_text(newline=)` and
   `TemporaryDirectory(ignore_cleanup_errors=)`.

A line that genuinely needs the newer API may carry `# floor-ok`, but only
behind a `sys.version_info` guard — the checker's docstring says so and
`tests/ssh_core/compat.py` is the reference implementation.

**D4. Optional dependencies may require a newer Python only where they are
optional.** `python-hcl2` 8.x needs 3.9+, so it is declared as
`python-hcl2>=8.1,<9; python_version >= '3.9'` and kept out of the 3.8 CI leg.
The scripts that use it import lazily and report `dependency_missing` (exit 3)
instead of failing at import time (the comment in `pyproject.toml` states this).

**D5. Python 3.7 is not supported.** `ssh-core/SKILL.md` says 3.7 reached EOL in
2023-06; the declared floor is 3.8. There is no CI leg below 3.8.

## Evidence

| Claim | Evidence |
|---|---|
| The whole tree declares 3.8 | `pyproject.toml` `requires-python = ">=3.8"` |
| CI actually runs 3.8 | `.github/workflows/ci.yml` matrix `["3.8", "3.10", "3.12"]` |
| The checker exists and is wired into CI | `.github/workflows/ci.yml` `floor` job; `tools/check_python_floor.py` |
| Newer APIs are refused with a documented escape | `tools/check_python_floor.py` `FORBIDDEN_CALLS`; `# floor-ok` |
| The test suite has 3.8 shims | `tests/ssh_core/compat.py` (`temp_dir`, `write_text`) |
| The floor is user-facing | `ssh-core/SKILL.md` "Python 3.8+" bullet |
| `python-hcl2` is gated to 3.9+ | `pyproject.toml` optional dependency marker; `module_validate.py` lazy import |

## Consequences

- A contributor cannot use `match`, `str.removeprefix`, `zip(strict=)` or
  `Path.write_text(newline=)` in shipped code without a guard or a failing CI
  job.
- The `floor` job is cheap (AST only) and runs on 3.12, so it catches the
  problem before the 3.8 matrix leg spends time on it.
- Optional features can still use newer libraries on newer interpreters, but
  the 3.8 path must produce a classified failure rather than an ImportError.
- The marketplace's dependency metadata and its actual floor stay aligned:
  `uv.lock` resolves `requires-python = ">=3.8"` and gates `python-hcl2` with a
  3.9 marker.

## Alternatives considered

- **Declare 3.10 and drop the old-host requirement.** Rejected: the operators'
  Kylin 3 / CentOS 7 fleet is the reason `ssh-core` exists; dropping it would
  make the transport unusable where it is needed.
- **Keep 3.8 in docs but test only 3.12.** Rejected: that is exactly the
  "declared but never executed" failure the floor job exists to prevent.
- **Let each plugin choose its own floor.** Rejected: the plugins shell out to
  each other and copy shared helpers; a split floor would make the vendored
  `_version.py` / `_ssh_core_locator.py` copies diverge by interpreter.
- **Support 3.7.** Rejected: EOL since 2023-06; no CI leg and no dependency
  support.

## Verification

```text
$ python tools/check_python_floor.py
scanned 215 files; 0 problem(s)

$ .venv/bin/python -m pytest tests/test_floor_checker.py -q
... passed
```

The 3.8 matrix leg is exercised by CI, not by the local macOS run; local runs
use 3.11. The declared floor is additionally checked by `uv lock --check`.
