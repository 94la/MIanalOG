# Security

The website is intentionally public with no login. Bind the backend to loopback
and expose it through a reverse proxy. Do not store exchange keys in this project;
only public Binance market data is used. Local data/, backups, .env and private
deployment units are excluded from Git. Keep system packages and Python dependencies
updated. Review any changes to endpoints and the static allowlist before deploying.

When reporting vulnerabilities, omit secrets and private server data from public issues.
