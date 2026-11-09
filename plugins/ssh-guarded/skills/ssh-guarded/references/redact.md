# Redaction policy

`ssh-guarded` redacts by default. The unredacted form is gated behind an explicit `--show-sensitive` flag on every script that can emit it. Chat transcripts almost never get that flag.

## What gets redacted

### Static config fields
Whenever we report a host's record:

| Field | Redacted as |
|---|---|
| `HostName` | `<redacted-host>` |
| `User` | `<redacted-user>` |
| `Port` (when non-22) | `<redacted-port>` |
| `IdentityFile` | `<redacted-keyfile>` |
| `ProxyJump` | `<redacted-jumphost>` |
| Password in our comment metadata | never emitted |

The alias is **not** redacted. The alias is the handle the user already chose to share; it's how we talk about the host.

### Secret regex catalog

Applied to every stdout/stderr/string we forward. The table below is the
**actual** default catalog in `scripts/lib/redact.py`; the replacement marker
is the `replace_with` column there (for example `[REDACTED-AWS-ACCESS-KEY]`).

| Kind | Pattern | Replaced with |
|---|---|---|
| `private_key_block` | a whole `-----BEGIN … PRIVATE KEY-----` … `-----END … PRIVATE KEY-----` block, body included | `[REDACTED-PRIVATE-KEY-BLOCK]` |
| `private_key_header` | a lone BEGIN line (truncated transcript) | `[REDACTED-PRIVATE-KEY-HEADER]` |
| `aws_access_key` | `AKIA[0-9A-Z]{16}` | `[REDACTED-AWS-ACCESS-KEY]` |
| `aws_secret` | `aws_secret_access_key = <40 base64 chars>` (env or HCL form) | `[REDACTED-AWS-SECRET-KEY]` |
| `github_pat` | `gh[pousr]_[A-Za-z0-9]{36,}` — PAT, OAuth, user, server, refresh | `[REDACTED-GH-TOKEN]` |
| `gitlab_pat` | `glpat-[A-Za-z0-9_-]{20,}` | `[REDACTED-GITLAB-TOKEN]` |
| `slack_token` | `xox[abprs]-[A-Za-z0-9-]{10,}` | `[REDACTED-SLACK-TOKEN]` |
| `jwt` | `eyJ…` three-part token | `[REDACTED-JWT]` |
| `bearer` | `Bearer <20+ token chars>` | `Bearer [REDACTED-BEARER]` |
| `generic_password_env` | `PASSWORD=`/`PASSWD=`/`SECRET=`/`TOKEN=` followed by a value | `<NAME>=[REDACTED]` |
| `mongodb_uri` | `mongodb://…` / `mongodb+srv://…` | `mongodb://[REDACTED]` |
| `postgres_uri` | `postgres://…` / `postgresql://…` | `postgres://[REDACTED]` |

Patterns are applied **in catalog order**, so the whole-block rule runs before
the header-only fallback and a complete key never leaves its body behind.
There is no longest-match arbitration between unrelated patterns; if you add
overlapping rules, put the more specific one first.

### Paths we treat as sensitive

Anything under `~/.ssh/`, `~/.aws/`, `~/.config/gcloud/`, `~/.kube/`, `.env*`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`. We don't redact the path itself (the user named it), but if we'd be quoting *contents*, we refuse instead.

## Where redaction runs

```
ssh-core stdout/stderr
        │
        ▼
ssh-guarded post-filter  ←── secret regex catalog + config field map
        │
        ▼
chat output / artifact files
```

Result files under `reports/` get the **redacted** form. The **unredacted** form is never persisted by `ssh-guarded` itself; the user's terminal scrollback is their own concern.

## Extending the catalog

Users can add to the catalog in `~/.devops/redact.json`:

```json
{
  "version": 1,
  "patterns": [
    {
      "kind": "internal_api_key",
      "regex": "MYCO-[A-Z0-9]{32}",
      "replace_with": "[REDACTED-INTERNAL]"
    }
  ]
}
```

A user catalog **replaces** the built-in defaults (it is not a merge). A
missing file falls back to the defaults; corrupt JSON or a regex that fails to
compile prints a warning on stderr and falls back per-pattern rather than
failing open silently.

## Things redaction is *not*

- Not encryption. The unredacted source still exists in `ssh-core` memory and in the user's terminal. Redaction protects the transcript surface.
- Not a substitute for `sops`/`age`/Vault. If your codebase has plaintext secrets, redaction at output time doesn't help.
- Not foolproof. Regex catalogs miss novel formats. Treat it as defense in depth.

## Auditing your own redaction

```
python scripts/redact_check.py < some_file.log
```

Reports every hit and the kind. Useful before posting a transcript or filing a bug.
