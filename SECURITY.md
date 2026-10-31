# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use GitHub's **private vulnerability reporting** on this repository's Security tab. If that option is unavailable, contact the maintainer through GitHub and include a short reproduction.

## Token safety

This tool reads a GitHub token from `GH_TOKEN` or `GITHUB_TOKEN`. It never writes the token to disk and never logs it.

- Use a dedicated classic token with only the `user:follow` scope.
- Never paste a token into an issue, pull request, log, or screenshot.
- Revoke the token when you no longer need it.
- Treat the audit log as private data: it contains the accounts you followed or unfollowed.
