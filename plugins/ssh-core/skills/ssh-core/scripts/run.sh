#!/bin/sh
# ssh-core launcher for POSIX (review A1).
#
# Mostly a convenience: `python3` is usually present on Linux/macOS. It matters
# on the hosts where the only interpreter is the one the harness ships
# ($DSH_PYTHON), and it keeps one command shape across platforms so a runbook
# written on Windows still works on a Kylin box.
#
# Usage:
#   scripts/run.sh ssh_execute.py lab-240 "uptime" --json
#   scripts/run.sh selfcheck.py
#
# Interpreter selection: $SSH_CORE_PYTHON, $DSH_PYTHON, python3, python.
# Exit code: the script's own; 3 (dependency_missing) when no interpreter exists.
set -u
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=${1:-}
if [ -z "$target" ]; then
    echo "ssh-core: scripts/run.sh needs a script name, e.g. run.sh selfcheck.py" >&2
    exit 2
fi
shift

for candidate in "${SSH_CORE_PYTHON:-}" "${DSH_PYTHON:-}" python3 python; do
    if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" "$script_dir/$target" "$@"
    fi
done

echo "ssh-core: no Python interpreter found. Set SSH_CORE_PYTHON (or DSH_PYTHON) or install python3." >&2
exit 3
