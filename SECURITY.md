# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 4.2.x   | Yes       |
| < 4.2   | No        |

## Reporting a vulnerability

If you discover a security issue, **do not** open a public GitHub issue with exploit details or secret values.

Contact the repository owner privately. Include:

- Affected component and version
- Steps to reproduce (without live credentials)
- Impact assessment

## Credential handling

This project uses local-only secrets:

- API keys (`OPENAI_API_KEY`, `YOUTUBE_API_KEY`, etc.) in `.env`
- YouTube session cookies (`YOUTUBE_COOKIES_FILE`)
- Optional Google Drive Desktop paths (no OAuth in active runtime)

**Never** paste tokens, cookies, or private keys into issues, pull requests, or commit messages.

If you accidentally commit a secret:

1. Revoke / rotate the credential immediately
2. Notify the owner
3. Do not rely on history deletion alone

## Public issues

When filing a public issue, redact:

- API keys and tokens
- Cookie file contents
- Personal filesystem paths
- Private folder IDs or account identifiers
