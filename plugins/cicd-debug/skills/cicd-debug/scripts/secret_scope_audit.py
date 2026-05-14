#!/usr/bin/env python3
"""secret_scope_audit.py — CI secret visibility audit (scaffold).

v1.0 surface:
    secret_scope_audit.py gh   --repo <owner>/<repo>     [--org <org>]   [--json]
    secret_scope_audit.py glab --project <group>/<proj>                 [--json]

For each secret:
    name, scope (org / repo / env / dependabot for gh; project / group / instance + protected/masked/env for glab)
    referenced_in_workflows  list of .github/workflows/*.yml or .gitlab-ci.yml jobs
    risk_flags               [
        "exposed-on-fork-PR" (pull_request from fork has empty value — confusion),
        "used-in-pull_request_target-with-PR-checkout" (CRIT),
        "protected-secret-referenced-from-unprotected-job",
        "long-lived-cloud-credential" (suggest OIDC),
        "wildcard-org-scope-on-public-repo"
    ]
    summary                  ok | warn | crit
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "cicd-debug: 'secret_scope_audit' is not implemented in v0.1.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="secret_scope_audit.py")
    p.add_argument("provider", choices=["gh", "glab"])
    p.add_argument("--repo", default=None)
    p.add_argument("--org", default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.provider == "gh" and not args.repo:
        sys.stderr.write("--repo is required for gh.\n")
        return 2
    if args.provider == "glab" and not args.project:
        sys.stderr.write("--project is required for glab.\n")
        return 2
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"provider": args.provider, "repo": args.repo,
                 "org": args.org, "project": args.project},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
