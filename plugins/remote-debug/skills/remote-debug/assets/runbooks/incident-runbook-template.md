# Runbook: [SERVICE NAME]

> A runbook is the answer to: "the alert fired at 03:00 and I am the on-call.
> What do I do?" Keep it operational, not architectural.

## At a glance

| Field | Value |
|---|---|
| Service | [name] |
| Owner team | [team / Slack channel] |
| Tier | SEV1-eligible? SEV2-eligible? |
| Dashboards | [link to Grafana / etc.] |
| Source code | [repo URL] |
| Hosts / cluster | [hostnames or aliases that match `~/.ssh/config`] |
| Dependencies | [databases, queues, upstream APIs] |
| Dependants | [who breaks if we break] |

## Alerts → first response

For each alert this service emits, fill in:

### Alert: `<alert-name>`

- **What this means**: <one sentence, what the signal indicates>
- **User impact**: <how a user would feel this>
- **First 3 diagnostics**:
  ```bash
  # 1.
  diagnose_host.py <alias>
  # 2.
  tail_log.py <alias> /var/log/<service>/app.log --since 10min --grep "error"
  # 3.
  port_check.py <alias> --target <upstream> --ports <ports>
  ```
- **Likely causes** (most common first):
  1. <cause> — fix: <link or one-line fix>
  2. <cause> — fix: <link or one-line fix>
- **When to escalate**: <what condition triggers waking up team owner / SEV uplift>

(Repeat per alert.)

## Common operations

### Restart the service

```bash
# Via ssh-guarded (production), the right way:
ssh-guarded → request_command:
  command: "sudo systemctl restart <service>"
  reason:  "ops: restart per runbook section X"
  workdir: ~/workspace/<project_id>
```

Why through `ssh-guarded`? Because the artifact creates a paper trail. The
restart command itself takes 5 seconds; producing the audit takes another
20. Worth it on production.

### Roll back the last deploy

[Fill in the actual commands. Specific. No "consult deploy docs".]

### Drain traffic from one host

[Fill in.]

## Capacity / scale

- **Normal load**: <ops/sec, bytes/sec, whatever the right unit is>
- **Known ceiling**: <when does it fall over?>
- **Scale-up procedure**: <step-by-step>

## On-call gotchas (the painful learned lessons)

- <thing the last on-call wished they'd known>
- <thing the previous incident taught us>
- <flag, config, env var that's easy to get wrong>

## Post-incident

When an incident touches this service, link the post-mortem here so the
next on-call inherits the lessons:

- [YYYY-MM-DD: short description] — [post-mortem link]
