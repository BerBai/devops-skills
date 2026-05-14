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

Applied to every stdout/stderr/string we forward. Hits are replaced with `<redacted-secret:<kind>>`:

| Kind | Pattern (shorthand) |
|---|---|
| `aws_access_key` | `AKIA[0-9A-Z]{16}` |
| `aws_secret` | 40-char base64 after `aws_secret_access_key`-ish context |
| `github_pat` | `gh[pousr]_[A-Za-z0-9]{36,}` |
| `gitlab_pat` | `glpat-[A-Za-z0-9_-]{20}` |
| `slack_token` | `xox[abprs]-[A-Za-z0-9-]+` |
| `jwt` | `eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+` |
| `private_key_block` | `-----BEGIN (RSA|EC|OPENSSH|PGP) PRIVATE KEY-----` |
| `generic_password_env` | `(PASSWORD|PASSWD|SECRET|TOKEN)=[^\s]+` |
| `bearer` | `Bearer [A-Za-z0-9._\-+/=]{20,}` |
| `mongodb_uri` | `mongodb(\+srv)?://[^\s]+` |
| `postgres_uri` | `postgres(ql)?://[^\s]+` |

When a hit overlaps with another, the **longer** match wins.

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
      "replacement": "<redacted-secret:internal_api_key>"
    }
  ]
}
```

Loaded at script start. Invalid regexes are rejected (the script refuses to run rather than fail open).

## Things redaction is *not*

- Not encryption. The unredacted source still exists in `ssh-core` memory and in the user's terminal. Redaction protects the transcript surface.
- Not a substitute for `sops`/`age`/Vault. If your codebase has plaintext secrets, redaction at output time doesn't help.
- Not foolproof. Regex catalogs miss novel formats. Treat it as defense in depth.

## Auditing your own redaction

```
python scripts/redact_check.py < some_file.log
```

Reports every hit and the kind. Useful before posting a transcript or filing a bug.
