#!/usr/bin/env python3
"""check_ruff_baseline.py - ratchet the ruff finding count instead of ignoring it.

The repository has a large ruff backlog (currently 380 findings). A red
``ruff check .`` in CI is not a gate: it trains people to ignore the job. This
script makes the backlog explicit and *monotonic*:

* a finding count **above** the baseline fails the job;
* any rule code above its baseline count fails, so fixing one rule and adding
  another cannot hide behind an unchanged total;
* a lower count passes and prints the exact command to regenerate the baseline.

The baseline is ``tools/ruff-baseline.json``; it is reviewed like code. The
long-term goal is to shrink it to zero, not to keep it forever.

Usage::

    python tools/check_ruff_baseline.py            # CI / local gate
    python tools/check_ruff_baseline.py --write    # regenerate after a decrease or fix
    python tools/check_ruff_baseline.py --verbose  # per-rule deltas

Exit codes: 0 gate pass, 1 regression, 2 ruff unavailable/bad output,
3 baseline missing or invalid.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# The checker runs on the 3.8 floor and uses percent formatting for readable
# diagnostics; those two style rules are not worth rewriting here.
# ruff: noqa: B904, UP006, UP031, UP045

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "tools" / "ruff-baseline.json"
COMMAND = [sys.executable, "-m", "ruff", "check", ".", "--output-format=json"]


def _load_baseline() -> Dict[str, Any]:
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            "missing %s: run `python tools/check_ruff_baseline.py --write`"
            % BASELINE.relative_to(REPO_ROOT)
        )
    except json.JSONDecodeError as exc:
        raise SystemExit("invalid %s: %s" % (BASELINE, exc))
    if not isinstance(data.get("total"), int) or not isinstance(
        data.get("by_code"), dict
    ):
        raise SystemExit("invalid baseline: expected total + by_code")
    return data


def _run_ruff() -> List[Dict[str, Any]]:
    result = subprocess.run(
        COMMAND,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(
            "ruff failed with exit %d:\n%s" % (result.returncode, result.stderr[:2000])
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit("ruff did not emit JSON: %s" % exc)
    if not isinstance(payload, list):
        raise SystemExit("ruff JSON is not a list")
    return payload


def _counts(findings: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counter: collections.Counter[str] = collections.Counter()
    for finding in findings:
        counter[str(finding.get("code") or "internal")] += 1
    return dict(sorted(counter.items()))


def _write(findings: Sequence[Dict[str, Any]]) -> int:
    counts = _counts(findings)
    data = {
        "ruff_version": _ruff_version(),
        "command": "python -m ruff check . --output-format=json",
        "total": len(findings),
        "by_code": counts,
    }
    BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "wrote %s: %d findings, %d rule codes"
        % (BASELINE.relative_to(REPO_ROOT), data["total"], len(counts))
    )
    return 0


def _ruff_version() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip() or "unknown"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="regenerate the baseline")
    parser.add_argument("--verbose", action="store_true", help="print per-rule deltas")
    args = parser.parse_args(argv)

    findings = _run_ruff()
    if args.write:
        return _write(findings)

    baseline = _load_baseline()
    current = _counts(findings)
    current_total = len(findings)
    baseline_total = int(baseline["total"])
    baseline_codes: Dict[str, int] = {
        str(code): int(count) for code, count in baseline["by_code"].items()
    }

    problems: List[str] = []
    if current_total > baseline_total:
        problems.append(
            "total findings increased: %d > %d (+%d)"
            % (current_total, baseline_total, current_total - baseline_total)
        )
    for code, count in sorted(current.items()):
        allowed = baseline_codes.get(code, 0)
        if count > allowed:
            problems.append("%s increased: %d > %d" % (code, count, allowed))
    for code, allowed in sorted(baseline_codes.items()):
        count = current.get(code, 0)
        if count < allowed and args.verbose:
            print("improved %s: %d -> %d" % (code, allowed, count))

    if args.verbose or not problems:
        print(
            "ruff baseline: %d/%d findings, %d rule codes"
            % (current_total, baseline_total, len(current))
        )
    if problems:
        print(
            "ruff ratchet failed; fix the new findings or, for a reviewed debt "
            "increase, edit tools/ruff-baseline.json deliberately.",
            file=sys.stderr,
        )
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    if current_total < baseline_total:
        print(
            "ruff improved by %d; refresh the baseline with "
            "`python tools/check_ruff_baseline.py --write`."
            % (baseline_total - current_total)
        )
    print("ruff ratchet: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
