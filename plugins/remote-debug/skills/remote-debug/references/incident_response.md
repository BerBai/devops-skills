# Incident response

A workable, opinionated process for incidents on remote infrastructure. Use it when the user says "we're down" / "production is broken" / "users are complaining". For a single weird symptom on a quiet host, this is overkill — use `common_issues.md`.

## Severity classification (decide first)

| SEV | Meaning | Examples | Response |
|---|---|---|---|
| **SEV1** | Major user-visible outage, money/safety risk | Site down, payments failing, data loss in progress | Page everyone. Incident commander appointed. Status page updated within 15 min. |
| **SEV2** | Significant degradation, no clean workaround | Login flow 5xx for a region, search broken, queue backed up > SLA | Page on-call. IC optional. Status page within 30 min if user-visible. |
| **SEV3** | Limited impact or has workaround | Background job slow, one non-critical service down | On-call investigates during business hours. |
| **SEV4** | Nuisance / observability noise | Flaky test, single host unhealthy in a fleet | Ticket. Fix when convenient. |

Default up when unsure. **Costs of overreacting are small; costs of underreacting are large.** Downgrade later if needed.

## Roles (SEV1/SEV2)

- **Incident Commander (IC)** — runs the call, makes go/no-go decisions, owns the timeline. Does **not** type commands themselves. The IC is process, not labor.
- **Investigator(s)** — actually look at logs, metrics, hosts. Report findings to the IC in plain language. Multiple investigators can work in parallel, each on a hypothesis.
- **Scribe** — writes the timeline as it happens (start with the page time, then every decision and finding). Tomorrow's post-mortem starts here.
- **Comms** — talks to users / Slack / status page / customers. Keeps the IC clear of customer comms.

In a small team, one person plays IC + Scribe and another plays Investigator + Comms. That's fine. The point is *roles are explicit*.

## Five phases

```
1. Detection
   Alert fires / user reports. Note the wall-clock time.
   Confirm the signal — is the dashboard / SLO actually red?

2. Triage
   SEV classification. Page who needs to be paged.
   IC starts the incident channel, posts initial summary:
     "SEV2: <service> 5xx rate at 3% since 14:02 UTC.
      Investigating. Status: investigating."

3. Investigation
   Form hypotheses. Test cheap ones first.
   Every command run goes in the channel (or scribed):
     "[14:07] tail_log.py prod-api-01 ... → spike at 14:01:30"
   When the hypothesis disproves, say so explicitly. Avoid "I think maybe".

4. Resolution
   Apply the fix. For SEV1/2, the IC approves before anything destructive.
   When deploying a fix, monitor for one full duration of "back to normal"
   before declaring resolved.

5. Post-incident
   Status page closed.
   Within 24 h: timeline + initial findings posted.
   Within 1 week: blameless post-mortem published with action items.
```

## What investigators should do

In order:

1. **Reproduce or confirm.** "User reports X" is not the same as "we observe X". Confirm with a metric or a manual probe.
2. **Bisect by signal, not by guess.** What changed in the last hour? Deploy timestamps, config rollouts, autoscaler events, traffic shifts.
3. **Use `compare_across_hosts.py`** when "one host is bad". Use `tail_log.py --hosts` when "the whole fleet is acting up".
4. **Run reads, not writes.** During investigation, don't fix things. If you fix and the symptom disappears, you destroyed evidence.
5. **Report negative findings.** "Disk is fine, conntrack is fine, no recent deploys" is **useful**.

## What the IC should do

1. Decide SEV and stick to it for at least 15 min before reclassifying.
2. Time-box hypotheses. "We'll know in 5 min if it's X." If not, move on.
3. Keep one source of truth (a channel pinned message, a doc, a ticket). Update every 10 min even if the update is "still investigating".
4. **Approve the fix.** Even if it's obviously right. Especially if it's obviously right.
5. Call the end: "Resolved at <time>, monitoring for 15 min."

## Post-mortem (blameless)

Within one week. Required sections:

```
1. Summary (one paragraph the executive can read)
2. Impact (users affected, dollars, time)
3. Timeline (UTC, every decision, no editorializing)
4. Root cause (the *technical* cause, plus the *systemic* cause)
5. What went well
6. What went badly
7. Where we got lucky
8. Action items (owner + due date, tracked in a ticket system)
```

**Blameless** means: humans behaved reasonably given the information they had at the time. The post-mortem looks at why the information was missing, why the tooling let them mistake X for Y, why the system was fragile to the trigger. Names appear in timelines (for accuracy), never as the cause.

## Runbook templates

See `assets/runbooks/incident-runbook-template.md` for a fillable template per service. The discipline is: every alert in your monitoring should link to a runbook with at least:

- **What this alert means** (the signal, not the symptom)
- **First three diagnostics** (commands to run)
- **Likely causes** with links to remediation
- **When to escalate**

Alerts without runbooks become noise. Alerts with bad runbooks become longer noise.
