"""Single source of truth for the ssh-core version string.

``plugin.json``, ``pyproject.toml``, ``VERSION``, every script's ``--version``
output and ``CHANGELOG.md`` all read the same value from here (SSH-30, DV-11).
Bump this one constant with ``python tools/bump-version.py X.Y.Z`` and the
packaging metadata plus the CLI surface stay in lockstep — the test suite fails
if any other file drifts.

History
-------
0.2.0   scaffolding (stubs, no pty, no tests)
1.0.0   first complete surface: exec / session / cluster / tunnel /
        config CRUD / deploy / SFTP-like transfers
1.1.0   gap-closure release: local-route metacharacter fix, atomic config
        writes with locking + restore, Windows askpass + pipe session,
        native key deploy, scp/direct transfer fallbacks, -R/-D tunnels,
        daemon, unified exit codes, JSON on argparse errors, redaction,
        audit rotation, regression suite
1.2.0   devops release: installable skill layout for DSH/agents, selfcheck,
        deterministic dist + marketplace, audit summarize/prune, real
        health checks with TCP probes, sudo, --follow/--tail, retry with
        backoff, inventory export, --preserve/--delete mirrors, recipes,
        examples, ADRs, SECURITY and the full engineering toolchain
1.3.0   CI/CD release: GitHub Actions matrix + stdlib-only job, Python floor
        gate, structural guards, integration layer against a local sshd,
        offline install path, measured coverage gate, session/config/pubkey
        coverage
1.3.1   coverage follow-up: lib/pubkey and lib/configfile wired in, coverage
        shard collision fixed, two orphaned modules covered directly
1.3.2   full-audit follow-up (FA-01..FA-44): exact-match config writes,
        -o validation in the daemon, no tracebacks on file errors, rsync
        started on the source host, --delete refused instead of faked,
        line-buffered --tail, session transcripts in the envelope, 3.8
        compatibility (write_text/newline, TemporaryDirectory), accurate
        exit codes and docs
1.4.0   output-pipeline release (SSHCORE-015..027): byte-exact capture with
        one UTF-8 decode, login-banner filtering, live human mode, signal
        exit codes, validated budgets, cluster --ssh-config/stdin forwarding,
        named openssh transfer backend, explicit HostName metadata
"""

from __future__ import annotations

__version__ = "1.4.0"

#: Human-readable one-liner used by ``--version`` and the README badge.
VERSION_LINE = f"ssh-core {__version__}"


def version_envelope(prog: str, as_json: bool) -> int:
    """Print the version (plain or as a JSON envelope) and return exit 0.

    Kept here rather than in ``lib.cli`` so ``--version`` works even for the
    tiny scripts that do not import the parser helpers.
    """
    import json
    import sys

    if as_json:
        json.dump(
            {
                "success": True,
                "exit_code": 0,
                "stdout": f"{VERSION_LINE}\n",
                "stderr": "",
                "data": {
                    "prog": prog,
                    "version": __version__,
                    "failure_class": None,
                },
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        sys.stdout.write(f"{prog} ({VERSION_LINE})\n")
    return 0


def wants_version(argv: list[str]) -> bool:
    """True when ``--version`` / ``-V`` appears anywhere on the command line."""
    return any(a in ("--version", "-V") for a in argv)


__all__ = ["VERSION_LINE", "__version__", "version_envelope", "wants_version"]
