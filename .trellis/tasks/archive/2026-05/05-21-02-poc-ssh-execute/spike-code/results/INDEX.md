# PoC Results Index

| Case | Description | File | Quick verdict |
|------|-------------|------|---------------|
| H1-1_true | exit 0 via 'true' | `H1-1_true.txt` | success=True exit=0 fail_class=None |
| H1-2_false | exit 1 via 'false' | `H1-2_false.txt` | success=False exit=1 fail_class=remote_error |
| H1-3_exit42 | exit 42 via bash -c | `H1-3_exit42.txt` | success=False exit=42 fail_class=remote_error |
| H1-4_nocmd | missing command -> 127 | `H1-4_nocmd.txt` | success=False exit=127 fail_class=remote_error |
| H1-5_mixedio | stdout+stderr+exit3 | `H1-5_mixedio.txt` | success=False exit=3 fail_class=remote_error |
| H2-1_freshkh | no known_hosts entry | `H2-1_freshkh.txt` | success=True exit=0 fail_class=None |
| H3-1_explicit | explicit IdentityFile | `H3-1_explicit.txt` | success=True exit=0 fail_class=None |
| H3-2_agent | no IdentityFile, agent | `H3-2_agent.txt` | success=True exit=0 fail_class=None |
| H3-3_bogus | unknown alias | `H3-3_bogus.txt` | success=False exit=255 fail_class=network |
| H4-1_network | DNS unresolvable | `H4-1_network.txt` | success=False exit=255 fail_class=network |
| H4-2_auth | auth blocked | `H4-2_auth.txt` | success=False exit=255 fail_class=auth |
| H4-3_remote | remote nonzero | `H4-3_remote.txt` | success=False exit=1 fail_class=remote_error |
| H5-1_timeout | sleep 60 with timeout=3 | `H5-1_timeout.txt` | success=False exit=124 fail_class=timeout |
| H6-1_simple | local: ls /tmp | `H6-1_simple.txt` | success=True exit=0 fail_class=None |
| H6-2_args | local: ls -la /tmp | `H6-2_args.txt` | success=True exit=0 fail_class=None |
| H6-3_pipe | local: pipe (expect FAIL) | `H6-3_pipe.txt` | success=False exit=1 fail_class=None |
| H6-4_quoted | local: quoted echo | `H6-4_quoted.txt` | success=True exit=0 fail_class=None |
| H6-5_redir | local: redirect (expect FAIL) | `H6-5_redir.txt` | success=True exit=0 fail_class=None |
