#!/usr/bin/env bash
# Driver for ssh_execute PoC experiments.
# Runs every hypothesis case; tees raw output to results/H<n>-<case>.txt;
# writes a one-line index to results/INDEX.md.
#
# Usage:  bash run-all.sh
# Assumes:
#   - /tmp/poc-ssh-config exists with poc-explicit-key / poc-via-agent / poc-noauth
#   - `pai` alias is reachable in real ~/.ssh/config
#   - ssh-agent has the right key loaded

set -u  # not -e: we WANT failures to be recorded, not abort

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE="$HERE/ssh_execute_spike.py"
RESULTS="$HERE/results"
INDEX="$RESULTS/INDEX.md"
SCRATCH_CONFIG="/tmp/poc-ssh-config"

mkdir -p "$RESULTS"
: > "$INDEX"
echo "# PoC Results Index" >> "$INDEX"
echo "" >> "$INDEX"
echo "| Case | Description | File | Quick verdict |" >> "$INDEX"
echo "|------|-------------|------|---------------|" >> "$INDEX"

run() {
    local case_id="$1"; shift
    local desc="$1"; shift
    local out="$RESULTS/${case_id}.txt"
    {
        echo "### $case_id : $desc"
        echo "ARGV: $*"
        echo "----- BEGIN RUN -----"
        python3 "$SPIKE" "$@" --json
        local rc=$?
        echo "----- END RUN -----"
        echo "shell_rc=$rc"
    } > "$out" 2>&1
    local quick
    quick=$(python3 -c "import json,sys
try:
    txt=open('$out').read()
    block=txt.split('----- BEGIN RUN -----')[1].split('----- END RUN -----')[0]
    d=json.loads(block)
    print(f\"success={d['success']} exit={d['exit_code']} fail_class={d.get('data',{}).get('failure_class')}\")
except Exception as e:
    print(f'parse_err: {e}')")
    echo "| $case_id | $desc | \`$(basename "$out")\` | $quick |" >> "$INDEX"
    echo "  $case_id : $quick"
}

echo "==== H1: remote exit-code propagation ===="
run H1-1_true     "exit 0 via 'true'"        pai 'true'                                    --ssh-config "$SCRATCH_CONFIG"
run H1-2_false    "exit 1 via 'false'"       pai 'false'                                   --ssh-config "$SCRATCH_CONFIG"
run H1-3_exit42   "exit 42 via bash -c"      pai 'bash -c "exit 42"'                       --ssh-config "$SCRATCH_CONFIG"
run H1-4_nocmd    "missing command -> 127"   pai 'nonexistent_command_xyz'                 --ssh-config "$SCRATCH_CONFIG"
run H1-5_mixedio  "stdout+stderr+exit3"      pai 'bash -c "echo TO_OUT; echo TO_ERR >&2; exit 3"'  --ssh-config "$SCRATCH_CONFIG"

echo "==== H2: stderr noise — uses fresh known_hosts ===="
TMP_KH=$(mktemp)
run H2-1_freshkh  "no known_hosts entry"  pai 'echo clean_run' --ssh-config "$SCRATCH_CONFIG"
# Force a missing-known-hosts scenario by pointing UserKnownHostsFile to an empty file:
echo "extra: forcing fresh UserKnownHostsFile=$TMP_KH (manual run below)"
ssh -F "$SCRATCH_CONFIG" -o "UserKnownHostsFile=$TMP_KH" -o "StrictHostKeyChecking=accept-new" pai 'echo with_warning' > "$RESULTS/H2-2_force_warn.txt" 2>&1
echo "  H2-2 captured (raw ssh, see file)"
rm -f "$TMP_KH"

echo "==== H3: alias resolution ===="
run H3-1_explicit "explicit IdentityFile"  poc-explicit-key 'echo via_explicit' --ssh-config "$SCRATCH_CONFIG"
run H3-2_agent    "no IdentityFile, agent" poc-via-agent    'echo via_agent'    --ssh-config "$SCRATCH_CONFIG"
run H3-3_bogus    "unknown alias"          bogus-not-defined 'true'             --ssh-config "$SCRATCH_CONFIG"

echo "==== H4: failure mode discrimination ===="
run H4-1_network  "DNS unresolvable"       nonexistent.invalid.local 'uptime'   --ssh-config "$SCRATCH_CONFIG"
run H4-2_auth     "auth blocked"           poc-noauth 'uptime'                  --ssh-config "$SCRATCH_CONFIG"
run H4-3_remote   "remote nonzero"         pai 'false'                          --ssh-config "$SCRATCH_CONFIG"

echo "==== H5: timeout cleanup ===="
# Start a sleep on the remote, time out at 3s, then check for orphan.
SLEEP_MARKER="poc_orphan_check_$$_$(date +%s)"
run H5-1_timeout  "sleep 60 with timeout=3" pai "sleep 60 # $SLEEP_MARKER"     --ssh-config "$SCRATCH_CONFIG" --timeout 3
# Allow remote a moment to settle
sleep 1
# Probe for orphan sleeps tagged with our marker comment
echo "----- ORPHAN PROBE: pgrep on remote -----" >> "$RESULTS/H5-1_timeout.txt"
ssh -F "$SCRATCH_CONFIG" -o BatchMode=yes pai "pgrep -af sleep | grep -F '$SLEEP_MARKER' || echo NO_ORPHAN_FOUND" >> "$RESULTS/H5-1_timeout.txt" 2>&1

echo "==== H6: local route + shlex ===="
run H6-1_simple   "local: ls /tmp"                    local 'ls /tmp'
run H6-2_args     "local: ls -la /tmp"                local 'ls -la /tmp'
run H6-3_pipe     "local: pipe (expect FAIL)"         local 'ls /tmp | head -3'
run H6-4_quoted   "local: quoted echo"                local 'echo "hello world"'
run H6-5_redir    "local: redirect (expect FAIL)"     local 'echo hi > /tmp/poc_spike_redir.tmp'

echo ""
echo "==== Done. Index: $INDEX ===="
cat "$INDEX"
