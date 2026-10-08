# Security

Only public Binance market data is used; no exchange keys or trading. Bind the
backend to loopback and expose it through an HTTPS reverse proxy. Access is public
by default; optional expiring invitations are described in README.md. Never commit
invitation links, cookies, access registries, data/, backups or environment files.
Keep packages updated and review endpoint/static allowlist changes before deploying.

When reporting vulnerabilities, omit secrets and private server data from public issues.
