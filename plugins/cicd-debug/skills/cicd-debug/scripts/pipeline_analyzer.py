#!/usr/bin/env python3
"""pipeline_analyzer.py — single CI run analysis (scaffold).

v1.0 surface:
    pipeline_analyzer.py <provider> <run-id|pipeline-id>
        [--project <owner/repo or group/proj>]
        [--diff-against last-success | <other-run-id>]
        [--json]

provider:
    gh    → uses `gh` CLI (requires gh auth login)
    glab  → uses `glab` CLI (requires glab auth login)

Analysis:
    job_states         per-job status + duration + retry count
    failed_steps       step-level failure with last 20 log lines
    diff_summary       vs reference run: action versions, env, runners, cache
    cache_summary      hit/miss/save events
    actions_used       full list of actions/<repo>@<ref> seen
    runner_used        runner_id / labels / runs_on
    summary            ok | warn | crit + top findings + hint links
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "cicd-debug: 'pipeline_analyzer' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pipeline_analyzer.py")
    p.add_argument("provider", choices=["gh", "glab"])
    p.add_argument("run_id", help="GitHub run-id or GitLab pipeline-id")
    p.add_argument("--project", default=None,
                   help="owner/repo (gh) or group/project (glab) — required for glab")
    p.add_argument("--diff-against", default=None,
                   help='"last-success" or another run/pipeline id')
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.provider == "glab" and not args.project:
        sys.stderr.write("--project is required for glab.\n")
        return 2
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"provider": args.provider, "run_id": args.run_id,
                 "project": args.project, "diff_against": args.diff_against},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
