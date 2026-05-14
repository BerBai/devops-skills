# reports/

This directory holds runtime state, **never** committed.

- `requests/<request_id>.json` — request artifacts drafted by `request_*.py`
- `requests/<request_id>.result.json` — execution result written by `run_request.py --execute`
- `requests/<request_id>.lock` — concurrency lock held during `--execute`
- `jobs/<job_id>.json` — manifest written by `exec_detached.py run`

Cleanup is the user's responsibility. `request_command.py --gc-older-than 7d` (planned) removes expired artifacts.
